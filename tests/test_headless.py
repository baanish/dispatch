"""The headless substrate: a worker is a subprocess of a vendor CLI.

No real codex, claude, or grok is ever launched. `fake_cli.py` is copied onto
PATH under each driver's binary name and does what a brief tells a worker to do,
which is write its final answer to the file the prompt names. That is what lets
a real `dispatch run` be driven end to end here: the process, its captured
output, and its exit code are genuine, and only the model behind them is not.
"""

import os
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from herdr_stub import (HerdrStubTestCase, caps, cli, drivers,  # noqa: E402
                        errors, herdr, lanes, processes, records, runner)
from dispatch.substrates import (DETECT_ORDER, detect_substrate,  # noqa: E402
                                 get_substrate, headless)

FAKE_CLIS = ("codex", "claude", "grok")
# What tests/fake_cli.py says on stdout and on stderr.
MESSAGE = "the fake worker is finished"
STDERR_NOTE = "fake-cli: starting"
IS_WINDOWS = os.name == "nt"


@unittest.skipIf(IS_WINDOWS, "the fake vendor CLIs are POSIX executables")
class HeadlessTestCase(HerdrStubTestCase):
    """A run whose worker is a subprocess, with fake vendor CLIs on PATH."""

    def setUp(self):
        super().setUp()
        source = (TESTS_DIR / "fake_cli.py").read_text(encoding="utf-8")
        binaries = self.root / "bin"
        binaries.mkdir()
        for name in FAKE_CLIS:
            target = binaries / name
            target.write_text(source, encoding="utf-8")
            target.chmod(0o755)
        os.environ["PATH"] = os.pathsep.join(
            [str(binaries), os.environ.get("PATH", "")])
        os.environ["DISPATCH_SUBSTRATE"] = "headless"

    def substrate(self):
        return get_substrate("headless")

    def headless_run(self, lane="sol@medium", *extra):
        """One finished foreground run, and its record."""
        code, output = self.capture_stdout("run", lane, str(self.brief), *extra)
        return self.only_record(), code, output

    def pane_log(self, rec):
        return (Path(rec["dir"]) / headless.PANE_LOG).read_text(encoding="utf-8")


class TestHeadlessRun(HeadlessTestCase):
    def test_a_run_delivers_its_brief_in_the_argv_and_journals_the_exit_code(self):
        """The whole contract in one run: the one-shot form carries the prompt,
        the process is the worker, and its status is the run's."""
        rec, code, output = self.headless_run()
        self.assertEqual(rec["substrate"], "headless")
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["rc"], 0)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(rec["argv"][:2], ["codex", "exec"])
        # The brief went in with the process, not typed in afterwards.
        self.assertEqual(rec["argv"][-1], rec["prompt"])
        self.assertIn(str(Path(rec["dir"]) / "brief.md"), rec["prompt"])
        self.assertTrue(rec.get("prompted"), "the run never recorded its brief")
        self.assertIn(MESSAGE, output)
        self.assertIn(MESSAGE, records.read_output(rec))

    def test_a_failing_worker_lands_its_own_exit_code(self):
        """The status is the process's own, not a shell's echo of it."""
        os.environ["FAKE_CLI_RC"] = "3"
        rec, code, _ = self.headless_run()
        self.assertEqual(rec["rc"], 3)
        self.assertEqual(rec["state"], "failed")
        self.assertEqual(code, cli.EXIT_FAILED)

    def test_both_streams_are_captured_to_pane_log(self):
        rec, _, _ = self.headless_run()
        captured = self.pane_log(rec)
        self.assertIn(MESSAGE, captured)
        self.assertIn(STDERR_NOTE, captured)

    def test_a_worker_that_writes_nothing_is_not_journaled_as_done(self):
        """A CLI that printed an answer and ignored the brief's one instruction
        failed it; out.md holds what it said, and the state says so."""
        os.environ["FAKE_CLI_NO_DELIVERABLE"] = "1"
        rec, _, _ = self.headless_run("opus@high")
        self.assertEqual(rec["rc"], 0)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("no out.md", rec["error"])
        # The printed final message is still the best thing there is to show.
        self.assertIn(MESSAGE, records.read_output(rec))

    def test_every_driver_runs_its_own_one_shot_form(self):
        for lane, head in (("sol@medium", ["codex", "exec"]),
                           ("opus@high", ["claude", "-p"]),
                           ("grok@high", ["grok", "--output-format", "plain"])):
            with self.subTest(lane=lane):
                code, _ = self.capture_stdout("run", lane, str(self.brief))
                rec = [r for r in records.all_records() if r["lane"] == lane][0]
                self.assertEqual(rec["argv"][:len(head)], head)
                self.assertEqual(rec["state"], "done", rec.get("error"))
                self.assertEqual(code, cli.EXIT_OK)

    def test_a_background_run_is_journaled_by_its_own_watcher(self):
        """The launcher exits before the worker does, so the exit code has to
        survive the process that started it."""
        os.environ["FAKE_CLI_SECONDS"] = "1"
        code, output = self.capture_stdout("run", "sol@medium", str(self.brief),
                                           "--bg")
        self.assertEqual(code, cli.EXIT_OK)
        rec_id = output.splitlines()[0]
        deadline = time.time() + 60
        rec = records.load_record(rec_id)
        while time.time() < deadline and rec.get("state") not in ("done", "failed"):
            time.sleep(0.1)
            rec = records.load_record(rec_id)
        self.assertEqual(rec["state"], "done", rec.get("error"))
        self.assertEqual(rec["rc"], 0)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_state_and_logs_in_a_run_directory_are_never_written_through_a_link(self):
        from dispatch.substrates import headless
        secret = records.runs_root().parent / "secret"
        secret.write_text("mine", encoding="utf-8")
        substrate = headless.HeadlessSubstrate()
        worker = substrate.open("sol@medium-000000-link")
        home = substrate.home(worker)
        (home / headless.STATE_FILE).unlink(missing_ok=True)
        (home / headless.STATE_FILE).symlink_to(secret)
        substrate.save_state(worker, {"pid": 1})
        self.assertEqual(secret.read_text(encoding="utf-8"), "mine")
        (home / headless.PANE_LOG).unlink(missing_ok=True)
        (home / headless.PANE_LOG).symlink_to(secret)
        with self.assertRaises(OSError):
            records.open_append(home / headless.PANE_LOG)
        (home / "prompt.txt").symlink_to(secret)
        records.replace_text(home / "prompt.txt", "a follow-up")
        self.assertEqual(secret.read_text(encoding="utf-8"), "mine")

    def test_python_planted_in_the_task_directory_is_never_imported(self):
        """The relay and the watcher both start in `--dir`, as the operator and
        outside any sandbox, so its files must not shadow what they import."""
        hostile = self.home / "hostile"
        (hostile / "dispatch").mkdir(parents=True)
        marker = self.home / "imported"
        plant = f"open({str(marker)!r}, 'a').close()\n"
        (hostile / "subprocess.py").write_text(plant, encoding="utf-8")
        (hostile / "dispatch" / "__init__.py").write_text(plant, encoding="utf-8")
        code, output = self.capture_stdout("run", "sol@medium", str(self.brief),
                                           "--dir", str(hostile), "--bg")
        self.assertEqual(code, cli.EXIT_OK)
        rec_id = output.splitlines()[0]
        deadline = time.time() + 60
        rec = records.load_record(rec_id)
        while time.time() < deadline and rec.get("state") not in ("done", "failed"):
            time.sleep(0.1)
            rec = records.load_record(rec_id)
        self.assertEqual(rec["state"], "done", rec.get("error"))
        self.assertFalse(marker.exists())

    def test_a_kill_stops_each_descendant_even_when_the_group_signal_worked(self):
        """A descendant that called setsid is outside the group, so a group
        signal that succeeds says nothing about it."""
        from dispatch.substrates import headless
        substrate = headless.HeadlessSubstrate()
        stopped = []
        with patch.object(headless.HeadlessSubstrate, "read_state",
                          return_value={"pid": 111}), \
                patch.object(headless.HeadlessSubstrate, "read_exit_code",
                             return_value=None), \
                patch.object(headless, "descendant_pids", return_value=[222, 333]), \
                patch.object(headless, "stop_process_group", return_value=True), \
                patch.object(headless, "stop_pid", side_effect=stopped.append):
            substrate.kill_worker_tree(object())
        self.assertEqual(stopped, [222, 333, 111])


class TestHeadlessLiveness(HeadlessTestCase):
    """A headless worker is visible to the caps, or every run is swept away.

    Nothing here has a daemon to ask, so the substrate answers `worker_ids` from
    the runs tree itself. Inheriting the base's "cannot tell" left liveness
    resting on the reservation, and a background run outlives that by design.
    """

    def live_headless_record(self, worker_id="", **fields):
        """A background run record whose worker no substrate can find."""
        rec_id = f"sol@medium-000000-{len(records.all_records())}"
        directory = records.runs_root() / rec_id
        directory.mkdir(parents=True)
        rec = {"id": rec_id, "kind": "run", "dir": str(directory),
               "lane": "sol@medium", "driver": "codex", "substrate": "headless",
               "state": "running", "session": caps.session_key(),
               "worker_id": worker_id or rec_id, "foreground": False,
               "started_at": time.time(),
               # Past the grace: this is where every background run is a minute
               # after it was launched.
               "reserved_at": time.time() - caps.RESERVATION_GRACE_SECONDS - 1,
               "created": rec_id}
        rec.update(fields)
        records.save_record(rec)
        return rec

    def test_a_live_worker_is_listed_and_a_finished_one_is_not(self):
        os.environ["FAKE_CLI_SECONDS"] = "10"
        code, output = self.capture_stdout("run", "sol@medium", str(self.brief),
                                           "--bg")
        self.assertEqual(code, cli.EXIT_OK)
        rec = records.load_record(output.splitlines()[0])
        self.assertIn(rec["worker_id"], self.substrate().worker_ids())
        self.assertEqual(self.run_cli("kill", rec["id"]), cli.EXIT_OK)
        self.assertNotIn(rec["worker_id"], self.substrate().worker_ids())

    def test_a_background_run_outlives_its_reservation(self):
        """Worker, record, and cap slot are all still there after a sweep runs
        against a background run whose reservation has expired. An expired
        reservation is not evidence the run is gone."""
        os.environ["FAKE_CLI_SECONDS"] = "10"
        code, output = self.capture_stdout("run", "sol@medium", str(self.brief),
                                           "--bg")
        self.assertEqual(code, cli.EXIT_OK)
        rec = records.load_record(output.splitlines()[0])
        # Without the watcher, only the substrate can say this run is alive.
        self.stop_watcher(rec.get("watcher_pid"))
        rec["reserved_at"] = time.time() - caps.RESERVATION_GRACE_SECONDS - 1
        records.save_record(rec)
        worker_pid = self.substrate().read_state(self.worker_of(rec))["pid"]

        live = caps.live_records(runner.SubstrateSweep(self.substrate()))

        self.assertIn(rec["id"], [found["id"] for found in live])
        self.assertEqual(records.load_record(rec["id"])["state"], "running")
        self.assertTrue(processes.pid_alive(worker_pid), "the CLI was killed")

    def test_a_run_that_finished_after_its_watcher_died_is_collected(self):
        """The worker wrote its answer and exited 0 with nobody watching. The
        sweep has to read that ending, not call the run orphaned."""
        os.environ["FAKE_CLI_SECONDS"] = "2"
        code, output = self.capture_stdout("run", "sol@medium", str(self.brief),
                                           "--bg")
        self.assertEqual(code, cli.EXIT_OK)
        rec = records.load_record(output.splitlines()[0])
        self.stop_watcher(rec.get("watcher_pid"))
        rec["reserved_at"] = time.time() - caps.RESERVATION_GRACE_SECONDS - 1
        records.save_record(rec)
        worker = self.worker_of(rec)
        deadline = time.time() + 30
        while time.time() < deadline and self.substrate().read_exit_code(worker) is None:
            time.sleep(0.1)
        self.assertEqual(self.substrate().read_exit_code(worker), 0)

        caps.live_records(runner.SubstrateSweep(self.substrate()))

        settled = records.load_record(rec["id"])
        self.assertEqual(settled["state"], "done", settled.get("error"))
        self.assertEqual(settled["rc"], 0)

    def test_a_respawn_takes_no_environment_the_worker_wrote_into_its_state(self):
        """worker.json sits in the run directory. A PYTHONPATH added there would
        have the relay, which runs as the operator, import the worker's code."""
        from dispatch.substrates import headless
        kept = headless.launch_environment({
            "AGENT_DEPTH": "1", "DISPATCH_RUN": "r", "OPENAI_API_KEY": "",
            "PYTHONPATH": "/tmp/planted", "PATH": "", "HOME": "", "LD_PRELOAD": "x",
            "DISPATCH_SESSION": 7}, blanked=("OPENAI_API_KEY",))
        self.assertEqual(kept, {"AGENT_DEPTH": "1", "DISPATCH_RUN": "r",
                                "OPENAI_API_KEY": ""})

    def test_a_headless_worker_receives_everything_run_env_sets(self):
        """The filter names what it lets through, so a variable added to the
        worker environment and not to that list would reach every worker but a
        headless one."""
        from dispatch.substrates import headless
        for name in ("codex", "claude", "grok"):
            driver = drivers.get_driver(name)
            env = runner.run_env("r", driver)
            self.assertIn("CLAUDE_CODE_PROMPT_CACHE_TTL", env)
            self.assertEqual(
                headless.launch_environment(env, driver.metered_key_vars), env, name)
        # At the configured value only: the state file is the worker's to rewrite.
        env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = "planted"
        self.assertNotIn("CLAUDE_CODE_PROMPT_CACHE_TTL",
                         headless.launch_environment(env))

    def test_a_state_file_that_is_not_dispatchs_does_not_break_the_sweep(self):
        """Every home is read on every sweep, under the runs lock. A list where
        the state belongs crashed it for every run, and a FIFO blocked it."""
        from dispatch.substrates import headless
        substrate = headless.HeadlessSubstrate()
        worker = substrate.open("sol@medium-000000-state")
        state = substrate.home(worker) / headless.STATE_FILE
        state.write_text("[]", encoding="utf-8")
        self.assertEqual(substrate.read_state(worker), {})
        if hasattr(os, "mkfifo"):
            state.unlink()
            os.mkfifo(state)
            self.assertEqual(substrate.read_state(worker), {})
        self.assertNotIn(worker.id, substrate.worker_ids())

    def test_a_status_file_written_while_the_relay_runs_ends_nothing(self):
        """The relay writes worker.rc as its last act. One that shows up while
        the relay is still running came from the worker, and believing it ended
        the run and skipped the kill with the CLI still going."""
        from dispatch.substrates import headless
        os.environ["FAKE_CLI_SECONDS"] = "20"
        code, output = self.capture_stdout("run", "sol@medium", str(self.brief),
                                           "--bg")
        self.assertEqual(code, cli.EXIT_OK)
        rec = records.load_record(output.splitlines()[0])
        substrate, worker = self.substrate(), self.worker_of(rec)
        pid = substrate.read_state(worker)["pid"]
        (substrate.home(worker) / headless.RC_FILE).write_text("0", encoding="utf-8")

        self.assertFalse(substrate.process_info(worker).at_prompt)
        self.assertEqual(self.run_cli("kill", rec["id"]), cli.EXIT_OK)
        deadline = time.time() + 15
        while time.time() < deadline and processes.pid_alive(pid) \
                and not processes.pid_is_zombie(pid):
            time.sleep(0.1)
        self.assertTrue(not processes.pid_alive(pid) or processes.pid_is_zombie(pid),
                        "the kill was skipped")

    def test_a_run_a_watcher_is_driving_is_never_swept_away(self):
        """A held watcher lock is liveness in its own right: it is held for
        exactly as long as somebody is driving that worker."""
        rec = self.live_headless_record(worker_id="never-listed")
        handle = records.hold_run_lock(rec, "watcher.lock")
        self.addCleanup(records.release_run_lock, handle)

        live = caps.live_records(runner.SubstrateSweep(self.substrate()))

        self.assertIn(rec["id"], [found["id"] for found in live])
        self.assertEqual(records.load_record(rec["id"])["state"], "running")

    def test_a_run_with_neither_worker_nor_watcher_is_still_abandoned(self):
        rec = self.live_headless_record(worker_id="never-listed")

        caps.live_records(runner.SubstrateSweep(self.substrate()))

        self.assertEqual(records.load_record(rec["id"])["state"], "orphaned")


class TestHeadlessRefusals(HeadlessTestCase):
    def test_steer_refuses_and_names_the_substrate(self):
        rec, _, _ = self.headless_run()
        code, err = self.capture_stderr("steer", rec["id"], "try it the other way")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("headless", err)
        self.assertIn("continue", err)

    def test_inspect_refuses_and_names_the_substrate(self):
        rec, _, _ = self.headless_run()
        code, err = self.capture_stderr("inspect", rec["id"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("headless", err)

    def test_the_capabilities_are_all_false(self):
        found = get_substrate("headless").capabilities
        self.assertEqual(
            (found.can_steer, found.can_inspect, found.can_read_screen,
             found.can_answer_dialogs), (False, False, False, False))

    def test_nothing_can_be_typed_at_a_headless_worker(self):
        rec, _, _ = self.headless_run()
        substrate = self.substrate()
        worker = self.worker_of(rec)
        for call in (lambda: substrate.send_line(worker, "ls"),
                     lambda: substrate.send_keys(worker, ["enter"]),
                     lambda: substrate.send_tui_line(worker, "hello"),
                     lambda: substrate.deliver_prompt(worker, "hello")):
            with self.assertRaises(errors.DispatchError):
                call()


class TestHeadlessContinue(HeadlessTestCase):
    def test_continue_resumes_in_the_headless_form(self):
        """There is no session to reopen, so `continue` is the one-shot form
        again with the CLI's resume flag and the new message."""
        parent, _, _ = self.headless_run("opus@high")
        session = parent["session_id"]
        self.assertTrue(session, "claude runs are handed a session id at launch")
        code, _ = self.capture_stdout("continue", parent["id"], "now do the rest")
        self.assertEqual(code, cli.EXIT_OK)
        child = self.records_of_kind("continue")[0]
        self.assertEqual(child["parent"], parent["id"])
        argv = child["argv"]
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertEqual(argv[argv.index("--resume") + 1], session)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(child["state"], "done", child.get("error"))
        self.assertIn("now do the rest",
                      (Path(child["dir"]) / "brief.md").read_text(encoding="utf-8"))


class TestHeadlessDetection(HerdrStubTestCase):
    """Detection, which needs no vendor CLI and so runs everywhere."""

    def test_headless_is_last_in_the_order(self):
        self.assertEqual(DETECT_ORDER[-1], "headless")

    def test_a_reachable_herdr_still_wins(self):
        self.assertEqual(detect_substrate().name, "herdr")

    def test_detection_falls_through_when_nothing_else_is_there(self):
        """No herdr listening and no tmux to find, so the substrate that needs
        neither daemon nor terminal is the one that answers."""
        os.environ[herdr.SOCKET_ENV] = str(self.root / "nothing-listens-here.sock")
        empty = self.root / "empty-path"
        empty.mkdir(exist_ok=True)
        os.environ["PATH"] = str(empty)
        self.assertEqual(detect_substrate().name, "headless")

    def test_a_pinned_headless_substrate_needs_no_daemon(self):
        os.environ[herdr.SOCKET_ENV] = str(self.root / "nothing-listens-here.sock")
        self.assertTrue(get_substrate("headless").available())

    def test_the_headless_argv_is_what_a_headless_run_records(self):
        """prepare_run asks the substrate which command line it can start, so a
        headless run never carries the interactive one."""
        substrate = get_substrate("headless")
        opts = records.RunOptions(dir=str(self.work))
        for lane_name in ("sol@medium", "opus@high", "grok@high"):
            with self.subTest(lane=lane_name):
                lane = lanes.resolve_lane(lane_name)
                rec = runner.prepare_run(lane, "brief text", opts, substrate)
                self.assertIn(rec["prompt"], rec["argv"])
                self.assertIn(rec["argv"][:2],
                              (["codex", "exec"], ["claude", "-p"],
                               ["grok", "--output-format"]))

    def test_the_codex_resume_form_moves_the_sandbox_to_a_config_override(self):
        """`codex exec resume` takes neither -s nor -C, so a resume that passed
        them would be refused by the binary rather than ignored."""
        lane = lanes.resolve_lane("sol@medium")
        argv = drivers.get_driver("codex").headless_argv(
            lane, records.RunOptions(dir="/w"), "carry on", "/o.md",
            resume_session="00000000-1111-2222-3333-444444444444")
        self.assertEqual(argv[:3], ["codex", "exec", "resume"])
        self.assertNotIn("-s", argv)
        self.assertNotIn("-C", argv)
        self.assertIn("-c", argv)
        self.assertIn("sandbox_mode=read-only", argv)
        self.assertEqual(argv[-2:],
                         ["00000000-1111-2222-3333-444444444444", "carry on"])


if __name__ == "__main__":
    unittest.main()
