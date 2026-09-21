"""Tests for the herdr substrate: the socket layer itself.

Every case here runs against the shared stub daemon (`herdr_stub.py`) on a temp
unix socket, which answers with the response shapes in `herdr-protocol-19.json`.
No real daemon is contacted and no agent CLI is ever launched, so a failure here
is dispatch's, never the environment's.

The stub is what makes the failure paths testable: a spawn test can force
`agent.start` to fail and watch the fallback type a plain shell command instead,
which a real daemon would not oblige.
"""

import contextlib
import io
import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from herdr_stub import (ERRORS, RECORDINGS, RESULTS, HerdrStubTestCase,  # noqa: E402
                        StubHerdr, caps, cli, drivers, errors, herdr, lanes,
                        launch_argv, policy, processes, records, resume_argv,
                        runner)


class HerdrTestCase(HerdrStubTestCase):
    """The driver-level view of the shared stub."""

    def setUp(self):
        super().setUp()
        os.environ["DISPATCH_SESSION"] = "test-session"
        # One worker home to drive. Tests that create their own get the next ids.
        self.pane_id = self.open_pane()
        self.workspace_id = self.pane_id.split(":")[0]
        self.worker = herdr.Worker(id=self.pane_id, group=self.workspace_id,
                                   agent="test-agent")

    def open_pane(self):
        """A pane to drive, created the way a run's pane is."""
        result = self.stub.create_workspace({"label": "dispatch-test"})
        return result["root_pane"]["pane_id"]

    def make_record(self, **fields):
        rec_id = f"sol@medium-{uuid.uuid4().hex[:6]}"
        directory = records.runs_root() / rec_id
        directory.mkdir(parents=True)
        rec = {"id": rec_id, "kind": "run", "lane": "sol@medium", "driver": "codex",
               "substrate": "herdr", "agent": herdr.agent_name(rec_id),
               "dir": str(directory), "cwd": str(self.root), "state": "reserved",
               "session": "test-session", "rc": None, "created": records.utc_now()}
        rec.update(fields)
        records.save_record(rec)
        return rec

    def wrapper(self, rec=None):
        return runner.RunWrapper(self.substrate(), rec or self.make_record())

    def params_for(self, method):
        return self.stub.params_for(method)

    def methods(self):
        return self.stub.methods()


class TestClient(HerdrTestCase):
    def test_the_pinned_protocol_passes_and_is_cached(self):
        client = herdr.HerdrClient(self.stub.path)
        self.assertEqual(client.check_protocol(), herdr.PROTOCOL)
        client.check_protocol()
        self.assertEqual(self.methods().count("ping"), 1, "handshake re-pinged")

    def test_protocol_drift_refuses_and_cites_the_breakage_protocol(self):
        drifted = herdr.PROTOCOL + 1
        self.stub.protocol = drifted
        with self.assertRaises(herdr.HerdrError) as caught:
            herdr.HerdrClient(self.stub.path).check_protocol()
        message = str(caught.exception)
        self.assertIn(
            f"herdr protocol drift: expected {herdr.PROTOCOL}, "
            f"got {drifted}", message)
        self.assertIn("update dispatch's pinned protocol", message)

    def test_drift_stops_the_substrate_before_it_creates_anything(self):
        self.stub.protocol = herdr.PROTOCOL + 1
        with self.assertRaises(herdr.HerdrError):
            self.substrate().open("drift", cwd=str(self.root))
        self.assertNotIn("workspace.create", self.methods())

    def test_any_first_call_pins_the_protocol_not_just_workspace_creation(self):
        """A verb steers panes it did not create; those calls pin too."""
        self.stub.protocol = herdr.PROTOCOL + 1
        with self.assertRaises(herdr.HerdrError) as caught:
            self.substrate().send_line(self.worker, "echo hi")
        self.assertIn("protocol drift", str(caught.exception))
        self.assertNotIn("pane.send_text", self.methods())

    def test_missing_daemon_names_the_socket_and_the_fix(self):
        with self.assertRaises(herdr.HerdrError) as caught:
            herdr.HerdrClient(str(self.root / "absent.sock")).ping()
        self.assertIn("no herdr daemon", str(caught.exception))
        # The fix is not the same command on both: brew runs the daemon on
        # macOS, and on Windows there is no service manager to name.
        self.assertIn("run `herdr`" if processes.IS_WINDOWS
                      else "brew services start herdr", str(caught.exception))

    def test_error_body_becomes_a_typed_error(self):
        self.stub.errors["pane.send_text"] = ERRORS["pane_not_found"]
        with self.assertRaises(herdr.HerdrCallError) as caught:
            self.substrate().send_line(self.worker, "echo hi")
        self.assertEqual(caught.exception.code, "pane_not_found")
        self.assertIn("pane w999:p999 not found", str(caught.exception))

    def test_each_call_opens_its_own_connection(self):
        """herdr hangs up after every response; a pooled client would break."""
        client = herdr.HerdrClient(self.stub.path)
        for _ in range(3):
            client.ping()
        self.assertEqual(self.stub.connections, 3)

    def test_request_ids_name_dispatch_and_the_method(self):
        client = herdr.HerdrClient(self.stub.path)
        self.assertEqual(client._next_id("pane.split"), "dispatch:pane:split:1")
        self.assertEqual(client._next_id("pane.split"), "dispatch:pane:split:2")


class DeniedTransport:
    """An endpoint that is up and will not open for this process.

    What a daemon owned by another security context looks like from inside a
    sandboxed session: the endpoint is bound, opening it is refused, and no
    amount of retrying or spawning changes that.
    """

    def __init__(self, address):
        self.address = address
        self.attempts = 0

    def request(self, payload, timeout=None):
        self.attempts += 1
        raise herdr.HerdrAccessDenied(herdr.access_denied_message(
            self.address, OSError(13, "Permission denied")))


class TestTransportSelfHeal(HerdrTestCase):
    """Start a daemon that is down, diagnose one that is not ours."""

    def setUp(self):
        super().setUp()
        self.spawns = []
        self.log = self.root / "status.log"
        self.real_spawn = herdr.spawn_daemon
        self.addCleanup(setattr, herdr, "spawn_daemon", self.real_spawn)
        self.addCleanup(setattr, herdr, "_SPAWNED", False)

    def fake_spawn(self, on_spawn=None):
        """Stand in for the spawn, keeping its once-per-invocation contract."""
        def spawn():
            self.spawns.append(True)
            if len(self.spawns) > 1:
                return False
            if on_spawn is not None:
                on_spawn()
            return True
        herdr.spawn_daemon = spawn

    def client(self):
        client = herdr.HerdrClient(self.stub.path)
        client.log_path = self.log
        return client

    def log_text(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_a_down_daemon_is_started_once_and_the_call_retried(self):
        self.stub.server_down = True
        self.fake_spawn(lambda: setattr(self.stub, "server_down", False))
        self.assertEqual(self.client().ping()["protocol"], herdr.PROTOCOL)
        self.assertEqual(len(self.spawns), 1, "one spawn per invocation")

    def test_the_repair_reaches_calls_that_are_not_the_handshake(self):
        """Every herdr call funnels through `call`, so every one is repairable."""
        self.stub.server_down = True
        self.fake_spawn(lambda: setattr(self.stub, "server_down", False))
        worker = self.substrate().open("repair", cwd=str(self.root))
        self.assertTrue(worker.id)
        self.assertEqual(len(self.spawns), 1)

    def test_a_repaired_daemon_is_pinned_again_before_the_retry(self):
        """The daemon that answers a repair is a different process, and may be a
        different version, so the retry must not go out on the old pin."""
        client = self.client()
        client.check_protocol()
        self.stub.server_down = True

        def come_back_drifted():
            self.stub.server_down = False
            self.stub.protocol = herdr.PROTOCOL + 1

        self.fake_spawn(come_back_drifted)
        with self.assertRaises(herdr.HerdrError) as caught:
            client.call("pane.send_text", {})
        self.assertIn("protocol drift", str(caught.exception))
        # One attempt reached the daemon on its way down; a second would be the
        # one the drifted daemon answered.
        self.assertEqual(self.methods().count("pane.send_text"), 1)

    def test_the_repair_and_the_retry_are_in_the_runs_log(self):
        self.stub.server_down = True
        self.fake_spawn(lambda: setattr(self.stub, "server_down", False))
        self.client().ping()
        text = self.log_text()
        self.assertIn("REPAIR ", text)
        self.assertIn("herdr server", text)
        self.assertIn("RETRY ", text)

    def test_a_daemon_that_does_not_come_up_says_what_was_tried(self):
        self.stub.server_down = True
        self.fake_spawn()
        wait_backup = herdr.SPAWN_WAIT_SECONDS
        poll_backup = herdr.SPAWN_POLL_SECONDS
        herdr.SPAWN_WAIT_SECONDS = 0.1
        herdr.SPAWN_POLL_SECONDS = 0.01
        try:
            with self.assertRaises(herdr.HerdrError) as caught:
                self.client().ping()
        finally:
            herdr.SPAWN_WAIT_SECONDS = wait_backup
            herdr.SPAWN_POLL_SECONDS = poll_backup
        message = str(caught.exception)
        self.assertIn("server_not_running", message)
        self.assertIn("herdr server", message)
        self.assertIn(self.stub.path, message)
        self.assertEqual(len(self.spawns), 1, "a failed repair must not loop")

    def test_a_refused_endpoint_carries_the_bounce_command_and_no_spawn(self):
        self.fake_spawn()
        transport = DeniedTransport(self.stub.path)
        client = herdr.HerdrClient(self.stub.path, transport=transport)
        client.log_path = self.log
        with self.assertRaises(herdr.HerdrAccessDenied) as caught:
            client.ping()
        message = str(caught.exception)
        self.assertIn("another security context", message)
        self.assertIn("Stop-Process -Id " if processes.IS_WINDOWS
                      else "launchctl kickstart -k" if processes.IS_MACOS
                      else "kill ", message)
        self.assertEqual(transport.attempts, 1, "a refusal must not be retried")
        self.assertEqual(self.spawns, [], "a bound endpoint cannot be respawned")
        self.assertIn("REPAIR-REFUSED ", self.log_text())

    def test_the_bounce_command_is_the_one_for_this_platform(self):
        line = herdr.bounce_command(4321)
        if processes.IS_WINDOWS:
            self.assertEqual(line, "Stop-Process -Id 4321 -Force; "
                                   "Start-Process herdr -ArgumentList 'server'")
        elif processes.IS_MACOS:
            self.assertEqual(line, f"launchctl kickstart -k "
                                   f"gui/{os.getuid()}/{herdr.MACOS_SERVICE}")
        else:
            self.assertEqual(line, "kill 4321 && herdr server")

    @unittest.skipIf(processes.IS_WINDOWS, "unix socket permissions")
    @unittest.skipIf(hasattr(os, "getuid") and os.getuid() == 0, "root opens anything")
    def test_a_socket_this_process_cannot_open_reads_as_refused(self):
        """The classification itself, against a real EACCES from connect(2)."""
        walled = self.root / "walled"
        walled.mkdir()
        self.addCleanup(walled.chmod, 0o700)
        walled.chmod(0o000)
        self.fake_spawn()
        with self.assertRaises(herdr.HerdrAccessDenied):
            herdr.HerdrClient(str(walled / "herdr.sock")).ping()
        self.assertEqual(self.spawns, [])

    def test_the_spawn_is_detached_and_happens_at_most_once(self):
        calls = []
        real_popen = subprocess.Popen
        subprocess.Popen = lambda argv, **kw: calls.append((argv, kw))
        os.environ.pop(herdr.SPAWN_ENV, None)
        herdr._SPAWNED = False
        try:
            self.assertTrue(herdr.spawn_daemon())
            self.assertFalse(herdr.spawn_daemon())
        finally:
            subprocess.Popen = real_popen
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv, list(herdr.SPAWN_ARGV))
        if processes.IS_WINDOWS:
            self.assertEqual(kwargs["creationflags"], processes.DETACHED_FLAGS)
        else:
            self.assertTrue(kwargs["start_new_session"])

    def test_a_job_that_forbids_breakaway_still_gets_a_detached_child(self):
        """Windows OpenSSH's job allows breakaway; a sandbox's may not."""
        calls = []

        def popen(argv, **kw):
            calls.append(kw["creationflags"])
            if kw["creationflags"] & processes.CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError(5, "Access is denied")
            return "child"

        real_popen, real_windows = subprocess.Popen, processes.IS_WINDOWS
        subprocess.Popen, processes.IS_WINDOWS = popen, True
        try:
            self.assertEqual(processes.popen_detached(["herdr", "server"]), "child")
        finally:
            subprocess.Popen, processes.IS_WINDOWS = real_popen, real_windows
        self.assertEqual(calls, [processes.DETACHED_FLAGS,
                                 processes.DETACHED_FLAGS & ~processes.CREATE_BREAKAWAY_FROM_JOB])

    def test_the_spawn_can_be_switched_off(self):
        """The escape hatch a sandboxed caller (and this suite) runs with."""
        os.environ[herdr.SPAWN_ENV] = "0"
        herdr._SPAWNED = False
        real_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **kw: self.fail("spawned anyway")
        try:
            self.assertFalse(herdr.spawn_daemon())
        finally:
            subprocess.Popen = real_popen

    def test_a_run_wrapper_logs_transport_repairs_into_its_own_run(self):
        wrapper = self.wrapper()
        self.assertEqual(wrapper.substrate.client.log_path, wrapper.status_path)


# --------------------------------------------------------------------------
# Pane lifecycle
# --------------------------------------------------------------------------


class TestPaneLifecycle(HerdrTestCase):
    def test_opening_a_home_carries_cwd_and_the_ladder_env(self):
        os.environ["AGENT_DEPTH"] = "0"
        env = runner.run_env("sol@medium-abc", drivers.get_driver("codex"))
        pane = self.substrate().open("test-env", cwd="/tmp", env=env)
        params = self.params_for("workspace.create")[0]
        self.assertEqual(params["cwd"], "/tmp")
        self.assertEqual(params["label"], "dispatch-test-env")
        self.assertFalse(params["focus"])
        self.assertEqual(params["env"]["AGENT_DEPTH"], "1")
        self.assertEqual(params["env"]["DISPATCH_SESSION"], "test-session")
        self.assertEqual(params["env"]["DISPATCH_RUN"], "sol@medium-abc")
        self.assertIn(pane.id, self.stub.panes)
        self.assertTrue(pane.id.startswith(pane.group + ":"))
        self.assertNotEqual(pane.id, self.pane_id)

    def test_run_env_marks_the_child_depth_not_ours(self):
        os.environ["AGENT_DEPTH"] = "1"
        env = runner.run_env("r", drivers.get_driver("codex"))
        self.assertEqual(env["AGENT_DEPTH"], "2")

    def test_send_line_types_then_presses_enter(self):
        self.substrate().send_line(self.worker, "echo hi")
        self.assertEqual([m for m in self.methods() if m != "ping"],
                         ["pane.send_text", "pane.send_keys"])
        self.assertEqual(self.params_for("pane.send_text")[0]["text"], "echo hi")
        self.assertEqual(self.params_for("pane.send_keys")[0]["keys"], ["enter"])

    def test_read_asks_for_unwrapped_text(self):
        """`recent` hard-wraps at pane width, which corrupts anything parsed."""
        self.stub.panes[self.pane_id].screen = "hello\n"
        self.assertEqual(self.substrate().read_screen(self.worker), "hello\n")
        params = self.params_for("pane.read")[0]
        self.assertEqual(params["source"], "recent_unwrapped")
        self.assertEqual(params["format"], "text")
        self.assertNotIn("lines", params)

    def test_process_info_reads_a_gone_pane_as_gone(self):
        self.stub.panes[self.pane_id].closed = True
        self.assertIsNone(self.substrate().process_info(self.worker))
        self.assertFalse(self.substrate().exists(self.worker))

    def test_closing_an_already_gone_pane_is_not_an_error(self):
        """A worker that runs `exit` takes its pane with it before teardown does."""
        self.stub.errors["pane.close"] = ERRORS["pane_not_found"]
        self.stub.errors["workspace.close"] = {"code": "workspace_not_found",
                                               "message": "workspace not found"}
        self.substrate().close(self.worker, release=False)

    def test_close_failures_other_than_absence_still_raise(self):
        self.stub.errors["pane.close"] = ERRORS["invalid_request"]
        with self.assertRaises(herdr.HerdrCallError):
            self.substrate().close(self.worker, release=False)


class TestPromptDetection(unittest.TestCase):
    """The completion signal, against process_info shapes recorded live."""

    def info(self, key):
        return RESULTS[key]["process_info"]

    def test_settled_shell_reads_as_the_prompt(self):
        self.assertTrue(herdr.pane_at_prompt(self.info("pane.process_info.settled")))

    def test_running_child_does_not(self):
        info = self.info("pane.process_info.child_running")
        self.assertFalse(herdr.pane_at_prompt(info))
        self.assertEqual(sorted(herdr.pane_worker_pids(info)), [4011, 4012])

    def test_prompt_returns_when_the_child_exits(self):
        self.assertTrue(
            herdr.pane_at_prompt(self.info("pane.process_info.prompt_returned")))

    def test_the_shells_own_subprocesses_are_not_a_worker(self):
        """Prompt hooks and rc files fork constantly, all inside the shell's
        own group. Read as a running worker they leave the pane looking busy
        forever, and they put the prompt's own children in kill's way."""
        info = self.info("pane.process_info.rc_files_running")
        self.assertEqual(info["foreground_process_group_id"], info["shell_pid"])
        self.assertGreater(len(info["foreground_processes"]), 1)
        self.assertTrue(herdr.pane_at_prompt(info))
        self.assertEqual(herdr.pane_worker_pids(info), [])

    def test_missing_info_is_not_a_prompt(self):
        self.assertFalse(herdr.pane_at_prompt(None))
        self.assertFalse(herdr.pane_at_prompt({}))


# --------------------------------------------------------------------------
# Agent authority and the spawn path
# --------------------------------------------------------------------------


class TestAgentAuthority(HerdrTestCase):
    def test_report_agent_claims_authority_as_dispatch(self):
        self.substrate().report_state(
            herdr.Worker(id=self.pane_id, agent="sol@medium"), "working",
            message="m", seq=3)
        params = self.params_for("pane.report_agent")[0]
        self.assertEqual(params["source"], "dispatch")
        self.assertEqual(params["state"], "working")
        self.assertEqual(params["agent"], "sol@medium")
        self.assertEqual(params["seq"], 3)

    def test_unknown_state_is_refused_before_it_reaches_the_socket(self):
        with self.assertRaises(herdr.HerdrError):
            self.substrate().report_state(
                herdr.Worker(id=self.pane_id, agent="sol"), "running")
        self.assertNotIn("pane.report_agent", self.methods())

    def test_report_agent_session_carries_id_and_path(self):
        self.substrate().report_session(
            herdr.Worker(id=self.pane_id, agent="sol"), "sess-1", "/tmp/t.jsonl")
        params = self.params_for("pane.report_agent_session")[0]
        self.assertEqual(params["agent_session_id"], "sess-1")
        self.assertEqual(params["agent_session_path"], "/tmp/t.jsonl")
        self.assertEqual(params["source"], "dispatch")


class TestSpawnPath(HerdrTestCase):
    def worker_with_agent(self, name="run-1"):
        return herdr.Worker(id=self.pane_id, group=self.workspace_id, agent=name)

    def test_the_substrates_own_spawn_is_tried_first_with_the_drivers_kind(self):
        spawn = self.substrate().start_worker(
            self.worker_with_agent(), drivers.get_driver("codex"),
            ["codex", "-m", "gpt-5.6-sol"])
        self.assertEqual(spawn.method, "agent.start")
        self.assertEqual(spawn.flag, "")
        params = self.params_for("agent.start")[0]
        self.assertEqual(params["kind"], "codex")
        self.assertEqual(params["pane_id"], self.pane_id)
        self.assertEqual(params["args"], ["-m", "gpt-5.6-sol"])
        self.assertNotIn("pane.send_text", self.methods())

    def test_every_lane_driver_names_an_agent_kind(self):
        for spec in lanes.DEFAULT_LANE_TABLE.values():
            self.assertTrue(drivers.get_driver(spec.driver).agent_kind, spec.driver)
        self.assertEqual(drivers.get_driver("grok").agent_kind, "grok")
        with self.assertRaises(errors.DispatchError):
            drivers.get_driver("gemini")

    def test_agent_start_failure_falls_back_to_raw_primitives_and_flags_it(self):
        self.stub.errors["agent.start"] = ERRORS["unsupported_agent_kind"]
        spawn = self.substrate().start_worker(
            self.worker_with_agent(), drivers.get_driver("codex"),
            ["codex", "-m", "gpt-5.6-sol"])
        self.assertEqual(spawn.method, "send-text")
        # `command` bypasses the user's zsh `codex` function;
        # PowerShell has no such builtin, so there the line is the call operator.
        self.assertEqual(self.params_for("pane.send_text")[0]["text"],
                         "& 'codex' '-m' 'gpt-5.6-sol'" if processes.IS_WINDOWS
                         else "command codex -m gpt-5.6-sol")
        self.assertEqual(self.params_for("pane.send_keys")[0]["keys"], ["enter"])
        self.assertIn("HERDR FALLBACK", spawn.flag)
        self.assertIn("unsupported_agent_kind", spawn.error)
        self.assertIn("Investigate before the next batch", spawn.flag)

    def test_a_spawn_whose_answer_was_lost_is_not_typed_into(self):
        """An `agent start` that fails after the pane has a process running may
        only have lost its answer, and the lane argv typed at a CLI that is
        already up is submitted to it as a prompt."""
        self.stub.errors["agent.start"] = ERRORS["unsupported_agent_kind"]
        self.stub.pane(self.pane_id).running = True
        spawn = self.substrate().start_worker(
            self.worker_with_agent(), drivers.get_driver("codex"),
            ["codex", "-m", "gpt-5.6-sol"])
        self.assertEqual(spawn.method, "agent.start")
        self.assertIn("HERDR SPAWN UNCONFIRMED", spawn.flag)
        self.assertIn("unsupported_agent_kind", spawn.error)
        self.assertNotIn("pane.send_text", self.methods())

    def test_fallback_argv_is_quoted_for_the_shell(self):
        self.stub.errors["agent.start"] = ERRORS["unsupported_agent_kind"]
        self.substrate().start_worker(
            self.worker_with_agent("r"), drivers.get_driver("claude"),
            ["claude", "-p", "do the thing; rm -rf /"])
        self.assertEqual(self.params_for("pane.send_text")[0]["text"],
                         "& 'claude' '-p' 'do the thing; rm -rf /'"
                         if processes.IS_WINDOWS
                         else "claude -p 'do the thing; rm -rf /'")

    def test_fallback_is_stamped_into_the_run_record(self):
        self.stub.errors["agent.start"] = ERRORS["unsupported_agent_kind"]
        rec = self.make_record()
        wrapper = self.wrapper(rec)
        wrapper.open()
        with contextlib.redirect_stderr(io.StringIO()):
            wrapper.start_worker(["codex"])
        stored = records.load_record(rec["id"])
        self.assertTrue(stored["spawn_fallback"])
        self.assertEqual(stored["spawn_method"], "send-text")
        self.assertIn("HERDR FALLBACK", stored["spawn_flag"])
        self.assertIn("FALLBACK", (Path(rec["dir"]) / "status.log").read_text())

    def test_successful_start_leaves_no_fallback_marks(self):
        rec = self.make_record()
        wrapper = self.wrapper(rec)
        wrapper.open()
        wrapper.start_worker(["codex"])
        stored = records.load_record(rec["id"])
        self.assertEqual(stored["spawn_method"], "agent.start")
        self.assertNotIn("spawn_fallback", stored)


# --------------------------------------------------------------------------
# The wrapper: completion, rc, deadline
# --------------------------------------------------------------------------


class TestWrapper(HerdrTestCase):
    def run_worker(self, rec=None, deadline=None):
        wrapper = self.wrapper(rec)
        wrapper.open()
        wrapper.start_worker(["codex"])
        wrapper.prompt_worker(f"write to {wrapper.dir / 'out.md'} then exit")
        return wrapper, wrapper.watch(deadline_seconds=deadline, poll_seconds=0.01)

    def test_open_records_the_pane_and_the_pinned_protocol(self):
        rec = self.make_record()
        wrapper = self.wrapper(rec)
        wrapper.open()
        stored = records.load_record(rec["id"])
        self.assertEqual(stored["worker_id"], wrapper.worker.id)
        self.assertEqual(stored["worker_group"], wrapper.worker.group)
        self.assertEqual(stored["substrate_version"], herdr.PROTOCOL)
        self.assertIn("herdr", (Path(rec["dir"]) / "status.log").read_text())

    def test_worker_exit_code_is_journaled_from_the_shell(self):
        """herdr keeps no exit codes, so the rc comes back off the screen."""
        self.stub.rc = 0
        wrapper, rec = self.run_worker()
        self.assertEqual(rec["rc"], 0)
        self.assertEqual(rec["state"], "done")
        self.assertIn("EXIT", (Path(rec["dir"]) / "status.log").read_text())
        self.assertEqual(records.load_record(rec["id"])["rc"], 0)

    def test_nonzero_exit_code_fails_the_run(self):
        self.stub.rc = 7
        _, rec = self.run_worker()
        self.assertEqual(rec["rc"], 7)
        self.assertEqual(rec["state"], "failed")

    def test_completion_waits_for_the_child_before_trusting_the_prompt(self):
        """The launch race: a pane polled before the shell forks is still at the
        prompt, and its `$?` belongs to the previous command, not this run."""
        self.stub.rc = 3
        _, rec = self.run_worker()
        probes = [p["text"] for p in self.params_for("pane.send_text")
                  if herdr.PANE_SHELL.parse_line(p["text"])[0] == "rc-probe"]
        self.assertEqual(len(probes), 1)
        self.assertEqual(rec["rc"], 3)

    def test_a_run_whose_worker_was_never_seen_waits_out_the_grace(self):
        """A cold start and a finished run look identical to one probe.

        With the worker never observed running, completing has to wait for the
        grace; a worker that truly never appears is refused at spawn instead
        (see the startup tests), not journaled as a finished run.
        """
        original = runner.START_GRACE_SECONDS
        runner.START_GRACE_SECONDS = 0.3
        self.addCleanup(setattr, runner, "START_GRACE_SECONDS", original)
        rec = self.make_record(started_at=time.time())
        wrapper = self.wrapper(rec)
        wrapper.attach(self.worker)
        self.assertIsNone(wrapper.poll(), "finished a run that never started")
        self.assertEqual(rec["liveness"]["verdict"], "starting")
        time.sleep(runner.START_GRACE_SECONDS + 0.05)
        self.assertIsNotNone(wrapper.poll(), "never gave up on it either")

    def test_pane_disappearing_ends_the_run_without_an_rc(self):
        """A worker that exits its shell takes the pane; there is no prompt left."""
        wrapper = self.wrapper()
        wrapper.open()
        wrapper.start_worker(["codex"])
        self.stub.panes[wrapper.worker.id].closed = True
        rec = wrapper.watch(poll_seconds=0.01)
        self.assertIsNone(rec["rc"])
        self.assertEqual(rec["state"], "failed")
        self.assertIn("WORKER-GONE", (Path(rec["dir"]) / "status.log").read_text())

    def test_a_checkin_that_finds_a_stuck_worker_kills_the_pane_tree(self):
        wrapper = self.wrapper()
        wrapper.open()
        wrapper.start_worker(["codex"])
        pane = self.stub.panes[wrapper.worker.id]   # a worker that never returns
        pane.running = True
        pane.turn_polls = 10_000
        # Untracked rather than idle: an agent herdr still calls idle is one the
        # turn detector would end the session on, and this is the deadline's own
        # path, not the exit ladder's.
        pane.status = "unknown"
        killed = []
        wrapper.substrate.kill_worker_tree = lambda worker: killed.append(worker.id)
        rec = wrapper.watch(deadline_seconds=0.05, poll_seconds=0.01)
        self.assertEqual(rec["state"], "timeout")
        self.assertEqual(rec["checkin_verdict"], "stuck")
        self.assertEqual(killed, [wrapper.worker.id])
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertIn("DEADLINE", log)
        self.assertIn("CHECKIN", log)

    def test_panes_close_on_done(self):
        """Homes close when a run ends: sixteen orphaned CLIs is a resource hog."""
        wrapper, _ = self.run_worker()
        self.assertIn(("pane", wrapper.worker.id), self.stub.closed)
        self.assertIn(("workspace", wrapper.worker.group), self.stub.closed)

    def test_dispatch_reports_the_ending_and_only_the_ending(self):
        """Reporting state evicts herdr's named agent, so it happens once, after
        the worker has gone: herdr's own detection is the status during the turn."""
        self.run_worker()
        self.assertEqual(len(self.stub.reports), 1, self.stub.reports)
        report = self.stub.reports[0]
        self.assertEqual(report["source"], "dispatch")
        self.assertEqual(report["state"], "idle")
        self.assertIn("rc=", report["message"])

    def test_heartbeats_land_in_the_status_log(self):
        original = records.HEARTBEAT_SECONDS
        records.HEARTBEAT_SECONDS = 0
        self.addCleanup(setattr, records, "HEARTBEAT_SECONDS", original)
        self.stub.worker_polls = 3
        _, rec = self.run_worker()
        self.assertIn("HEARTBEAT", (Path(rec["dir"]) / "status.log").read_text())
        self.assertTrue(records.load_record(rec["id"])["heartbeat"])

    def test_a_failed_report_does_not_fail_the_run(self):
        """herdr's UI is not the run: losing the pane's label is not losing work."""
        self.stub.errors["pane.report_agent"] = ERRORS["pane_not_found"]
        _, rec = self.run_worker()
        self.assertEqual(rec["state"], "done")
        self.assertIn("REPORT-FAILED", (Path(rec["dir"]) / "status.log").read_text())

    def test_pane_screen_is_kept_as_the_runs_artifact(self):
        _, rec = self.run_worker()
        self.assertIn("codex", (Path(rec["dir"]) / "screen.log").read_text())

    def test_session_id_is_recorded_and_reported(self):
        rec = self.make_record()
        wrapper = self.wrapper(rec)
        wrapper.open()
        wrapper.note_session("11111111-2222-3333-4444-555555555555")
        stored = records.load_record(rec["id"])
        self.assertEqual(stored["session_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(self.params_for("pane.report_agent_session")[0]["agent_session_id"],
                         "11111111-2222-3333-4444-555555555555")

    def test_a_hostile_session_id_is_refused(self):
        wrapper = self.wrapper()
        wrapper.open()
        with self.assertRaises(errors.DispatchError):
            wrapper.note_session("../../etc/passwd")


class TestRcParsing(unittest.TestCase):
    def test_the_recorded_screen_yields_the_real_exit_code(self):
        """Recorded live: the typed line and its output are both on screen."""
        screen = RECORDINGS["screens"]["rc_probe"]
        self.assertEqual(herdr.parse_rc_echo(screen, "abc123"), 7)

    def test_a_marker_already_on_screen_is_not_read_as_the_exit_code(self):
        """The pane is scrollback, not a fresh buffer.

        A `dispatch-rc-<token>=0` printed during the turn is still there when the
        probe runs. Taking the first match meant that line won and the real exit
        code was never waited for; the scan starts after the command we typed.
        """
        probe = "echo dispatch-rc-tok=$?"
        screen = f"dispatch-rc-tok=0\n% {probe}\ndispatch-rc-tok=7\n"
        self.assertEqual(herdr.parse_rc_echo(screen, "tok", after=probe), 7)

    def test_no_answer_until_the_probe_itself_is_on_screen(self):
        self.assertIsNone(herdr.parse_rc_echo(
            "dispatch-rc-tok=0\n", "tok", after="echo dispatch-rc-tok=$?"))

    def test_the_last_answer_after_the_probe_wins(self):
        probe = "echo dispatch-rc-tok=$?"
        screen = f"% {probe}\ndispatch-rc-tok=1\ndispatch-rc-tok=4\n"
        self.assertEqual(herdr.parse_rc_echo(screen, "tok", after=probe), 4)

    def test_the_env_probe_reads_back_the_same_way(self):
        probe = "echo dispatch-env-tok=$AGENT_DEPTH"
        screen = f"dispatch-env-tok=0\n% {probe}\ndispatch-env-tok=2\n"
        self.assertEqual(herdr.parse_env_echo(screen, "tok", after=probe), "2")

    def test_the_echoed_command_is_never_mistaken_for_the_answer(self):
        self.assertIsNone(herdr.parse_rc_echo("% echo dispatch-rc-tok=$?\n", "tok"))

    def test_another_runs_marker_is_ignored(self):
        screen = "dispatch-rc-other=1\ndispatch-rc-mine=0\n"
        self.assertEqual(herdr.parse_rc_echo(screen, "mine"), 0)

    def test_the_last_answer_wins(self):
        self.assertEqual(herdr.parse_rc_echo("dispatch-rc-t=1\ndispatch-rc-t=4\n", "t"), 4)

    def test_tokens_are_filesystem_and_shell_safe(self):
        self.assertEqual(herdr.rc_token("sol@medium-120000-ab12"),
                         "medium120000ab12")
        self.assertEqual(herdr.rc_token("@@@"), "run")


# --------------------------------------------------------------------------
# The liveness judge seam
# --------------------------------------------------------------------------


class TestLiveness(HerdrTestCase):
    def signals(self, **fields):
        base = {"worker_id": self.pane_id, "at_prompt": False, "child_pids": (999,),
                "output_idle_seconds": 0.0, "cpu_percent": 0.0,
                "elapsed_seconds": 1.0}
        base.update(fields)
        return runner.LivenessSignals(**base)

    def test_a_finished_pane_reads_as_gone(self):
        """Finished, so the worker was seen running at some point: without that
        corroboration an empty pane is a cold start, not an ending."""
        self.assertEqual(
            runner.free_signal_verdict(self.signals(at_prompt=True, child_pids=(),
                                                      seen_alive=True)),
            "gone")

    def test_moving_output_is_free_evidence_of_work(self):
        self.assertEqual(runner.free_signal_verdict(self.signals()), "working")

    def test_a_silent_but_busy_worker_is_working(self):
        self.assertEqual(
            runner.free_signal_verdict(self.signals(output_idle_seconds=9999,
                                                      cpu_percent=80.0)),
            "working")

    def test_silent_and_idle_is_the_question_the_judge_exists_for(self):
        self.assertEqual(
            runner.free_signal_verdict(self.signals(output_idle_seconds=9999,
                                                      cpu_percent=0.0)),
            "settled")

    def test_unreadable_cpu_does_not_read_as_busy(self):
        self.assertEqual(
            runner.free_signal_verdict(self.signals(output_idle_seconds=9999,
                                                      cpu_percent=-1.0)),
            "settled")

    def test_the_snapshot_judge_seam_has_no_opinion_in_phase_1(self):
        self.assertIsNone(runner.snapshot_judge_seam(self.signals()))

    def test_a_verdict_is_on_the_record_before_anything_acts_on_it(self):
        rec = self.make_record()
        verdict = runner.judge_liveness(self.signals(output_idle_seconds=9999), rec)
        self.assertEqual(verdict, "settled")
        self.assertEqual(rec["liveness"]["verdict"], "settled")
        self.assertEqual(rec["liveness"]["source"], "free-signals")

    def test_a_wired_judge_overrides_only_the_settled_case(self):
        original = runner.snapshot_judge_seam
        runner.snapshot_judge_seam = lambda signals, rec=None: "working"
        self.addCleanup(setattr, runner, "snapshot_judge_seam", original)
        rec = self.make_record()
        self.assertEqual(runner.judge_liveness(self.signals(), rec), "working")
        self.assertEqual(rec["liveness"]["source"], "free-signals")
        self.assertEqual(
            runner.judge_liveness(self.signals(output_idle_seconds=9999), rec),
            "working")
        self.assertEqual(rec["liveness"]["source"], "snapshot-judge")

    def test_the_judge_itself_is_swappable_for_phase_2(self):
        previous = runner.set_liveness_judge(lambda signals: "gone")
        self.addCleanup(runner.set_liveness_judge, previous)
        self.assertEqual(runner.judge_liveness(self.signals()), "gone")

    def test_the_watch_loop_judges_every_poll(self):
        seen = []
        wrapper = self.wrapper()
        wrapper.judge_hook = lambda signals, rec: seen.append(signals.at_prompt)
        wrapper.open()
        wrapper.start_worker(["codex"])
        wrapper.watch(poll_seconds=0.01)
        self.assertTrue(seen)
        self.assertIn(False, seen, "the running child was never judged")


class TestKillPaneTree(HerdrTestCase):
    def test_the_foreground_group_is_killed_and_the_shell_left_alone(self):
        killed = []
        original = processes.stop_process_group
        processes.stop_process_group = lambda pgid: killed.append(pgid) or True
        self.addCleanup(setattr, processes, "stop_process_group", original)
        pane = self.stub.panes[self.pane_id]
        pane.running = True
        pane.turn_polls = 5
        children = self.substrate().kill_worker_tree(self.worker)
        self.assertEqual(killed, [pane.shell_pid + 1])   # the foreground group
        self.assertNotIn(pane.shell_pid, children)

    def test_a_pane_at_its_prompt_has_nothing_to_kill(self):
        self.assertEqual(self.substrate().kill_worker_tree(self.worker), [])

    def test_a_gone_pane_is_not_signalled(self):
        self.stub.panes[self.pane_id].closed = True
        self.assertEqual(self.substrate().kill_worker_tree(self.worker), [])

    def test_pids_are_signalled_one_by_one_where_groups_are_unavailable(self):
        stopped = []
        original_group = processes.stop_process_group
        original_pid = processes.stop_pid
        processes.stop_process_group = lambda pgid: False
        processes.stop_pid = stopped.append
        self.addCleanup(setattr, processes, "stop_process_group", original_group)
        self.addCleanup(setattr, processes, "stop_pid", original_pid)
        pane = self.stub.panes[self.pane_id]
        pane.running = True
        pane.turn_polls = 5
        self.substrate().kill_worker_tree(self.worker)
        self.assertEqual(stopped, [pane.shell_pid + 1])


if __name__ == "__main__":
    unittest.main()
