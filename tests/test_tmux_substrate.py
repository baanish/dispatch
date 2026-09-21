"""The tmux substrate, against a fake `tmux` on PATH.

`fake_tmux.py` is copied onto PATH as `tmux`, records every invocation, and
models one pane well enough to answer the questions a run asks of it: a shell
that echoes what it is typed, an environment it reports back, an exit code it
gives up when asked, and a screen that scrolls. No real tmux server is ever
started, and no vendor CLI is ever launched.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatch import drivers, errors  # noqa: E402
from dispatch.substrates import get_substrate, tmux  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# The metered keys the fake pane reports as still set, so that a blanking
# failure has something to fail with.
METERED_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")

RUN_ID = "sol@medium-120000-ab12"
SESSION = "dispatch-sol-medium-120000-ab12"


class FakeTmuxTestCase(unittest.TestCase):
    """A fake tmux on PATH, and process facts driven by the fake's own pane."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        binary = self.bin / "tmux"
        shutil.copyfile(TESTS_DIR / "fake_tmux.py", binary)
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        self.state_path = self.root / "tmux-state.json"
        self.write_state({
            "repo_root": str(REPO_ROOT),
            "version": "tmux 3.4",
            "can_start_server": True,
            "metered_vars": list(METERED_VARS),
            "default_rc": 0,
            "replies": [],
            "sticky_env": {},
            "next_pane": 0,
            "panes": {},
        })

        self.env_backup = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["FAKE_TMUX_STATE"] = str(self.state_path)

        # Real terminals need the TUI pacing; the suite proves the rule.
        self.pacing_backup = (tmux.TUI_SETTLE_SECONDS, tmux.TUI_SUBMIT_SECONDS,
                              tmux.TUI_ENTER_BEAT_SECONDS, tmux.PROBE_SECONDS,
                              tmux.CHILD_APPEAR_SECONDS)
        tmux.TUI_SETTLE_SECONDS = 0.05
        tmux.TUI_SUBMIT_SECONDS = 0.1
        tmux.TUI_ENTER_BEAT_SECONDS = 0.01
        tmux.PROBE_SECONDS = 0.5
        tmux.CHILD_APPEAR_SECONDS = 1.0
        self.addCleanup(self._restore_pacing)

        # `ps` cannot be asked about a pane that does not exist, so the fake's
        # own pane answers instead: a shell owning its terminal until something
        # is running in it.
        self.rows_backup = tmux.tty_process_rows
        tmux.tty_process_rows = self.fake_rows
        self.addCleanup(setattr, tmux, "tty_process_rows", self.rows_backup)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self.env_backup)

    def _restore_pacing(self):
        (tmux.TUI_SETTLE_SECONDS, tmux.TUI_SUBMIT_SECONDS,
         tmux.TUI_ENTER_BEAT_SECONDS, tmux.PROBE_SECONDS,
         tmux.CHILD_APPEAR_SECONDS) = self.pacing_backup

    # -- the fake's state ------------------------------------------------

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def update_state(self, **fields):
        state = self.state()
        state.update(fields)
        self.write_state(state)

    def pane(self, pane_id="%0"):
        return self.state()["panes"][pane_id]

    def patch_pane(self, pane_id="%0", **fields):
        state = self.state()
        state["panes"][pane_id].update(fields)
        self.write_state(state)

    def fake_rows(self, tty, command_builder=None):
        for pane in self.state()["panes"].values():
            if pane["pane_tty"] != tty:
                continue
            shell = pane["pane_pid"]
            if pane["running"]:
                # A worker holding the terminal's foreground group.
                return [(shell, shell, 9000), (9000, 9000, 9000)]
            return [(shell, shell, shell)]
        return []

    def invocations(self, command=""):
        log = Path(str(self.state_path) + ".log")
        if not log.exists():
            return []
        calls = [json.loads(line) for line in
                 log.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [call for call in calls if not command or call[0] == command]

    # -- the substrate under test ----------------------------------------

    def substrate(self):
        return tmux.TmuxSubstrate()

    def open_worker(self, substrate=None, label=RUN_ID, env=None):
        substrate = substrate or self.substrate()
        return substrate, substrate.open(label=label, cwd=str(self.root),
                                         env=env or {"AGENT_DEPTH": "1"})


class TestDetection(FakeTmuxTestCase):
    def test_the_substrate_is_unusable_without_tmux_on_path(self):
        os.environ["PATH"] = str(self.root / "empty")
        with self.assertRaises(errors.DispatchError) as caught:
            get_substrate("tmux")
        self.assertIn("no tmux on PATH", str(caught.exception))

    def test_a_tmux_that_cannot_start_a_server_is_unusable(self):
        """On PATH is half the question; a server has to be reachable too."""
        self.update_state(can_start_server=False)
        with self.assertRaises(errors.DispatchError) as caught:
            get_substrate("tmux")
        self.assertIn("start-server", str(caught.exception))

    def test_detection_probes_the_version_and_the_server(self):
        substrate = self.substrate()
        self.assertEqual(substrate.version(), "tmux 3.4")
        self.assertEqual([call[0] for call in self.invocations()],
                         ["-V", "start-server"])

    def test_a_command_builder_rewrites_every_tmux_call(self):
        """Shell mode is this substrate with ssh in front of each call, so the
        builder has to sit between the argv it wants and the one that runs."""
        seen = []

        def build(argv):
            seen.append(list(argv))
            return list(argv)

        substrate = get_substrate("tmux", command_builder=build)
        self.assertEqual(substrate.version(), "tmux 3.4")
        self.assertTrue(seen)
        self.assertTrue(all(call[0].endswith("tmux") for call in seen))
        self.assertIn(["-V"], [call[1:] for call in seen])

    def test_a_builder_means_the_binary_is_resolved_on_the_other_machine(self):
        """This machine's PATH says nothing about the one holding the panes."""
        # A PATH with a python3 for the fake tmux's own shebang, and no tmux.
        os.environ["PATH"] = str(Path(sys.executable).parent)
        substrate = get_substrate(
            "tmux", command_builder=lambda argv: [str(self.bin / "tmux")] + argv[1:])
        self.assertEqual(substrate.binary, "tmux")
        self.assertEqual(substrate.version(), "tmux 3.4")

    def test_the_process_probe_runs_where_the_pane_is(self):
        """A tty belongs to whichever machine holds the pane; asking this one's
        process table about it answers about the wrong machine."""
        rows = self.rows_backup("/dev/ttys004",
                                lambda argv: ["sh", "-c", "echo '11 22 33'"])
        self.assertEqual(rows, [(11, 22, 33)])

    def test_every_capability_this_substrate_claims_is_one_it_has(self):
        found = self.substrate().capabilities
        self.assertTrue(found.can_steer and found.can_inspect)
        self.assertTrue(found.can_read_screen and found.can_answer_dialogs)

    def test_nothing_here_names_a_dialog_or_tracks_an_agent(self):
        """Both readings stay "no opinion", which the runner reads as no veto."""
        substrate, worker = self.open_worker()
        self.assertEqual(substrate.blocked_rule(worker), "")
        self.assertEqual(substrate.screen_state(worker), "")
        self.assertFalse(substrate.status(worker).tracked)

    def test_a_version_that_cannot_be_read_takes_the_older_path(self):
        self.assertFalse(tmux.version_at_least("tmux next", (3, 2)))
        self.assertTrue(tmux.version_at_least("tmux 3.4a", (3, 2)))
        self.assertFalse(tmux.version_at_least("tmux 2.9", (3, 2)))


class TestSessions(FakeTmuxTestCase):
    def test_a_run_gets_a_session_named_after_it_with_its_environment(self):
        substrate, worker = self.open_worker()
        self.assertEqual(worker.group, SESSION)
        self.assertEqual(worker.id, "%0")
        created = self.invocations("new-session")[0]
        self.assertIn("-d", created)
        self.assertEqual(created[created.index("-s") + 1], SESSION)
        self.assertEqual(created[created.index("-e") + 1], "AGENT_DEPTH=1")
        self.assertEqual(created[created.index("-c") + 1], str(self.root))
        self.assertEqual(self.pane()["env"]["AGENT_DEPTH"], "1")
        self.assertEqual(substrate.agent_name(RUN_ID), SESSION)

    def test_a_tmux_too_old_for_the_env_flag_exports_it_before_the_shell(self):
        self.update_state(version="tmux 3.0")
        substrate, worker = self.open_worker()
        created = self.invocations("new-session")[0]
        self.assertNotIn("-e", created)
        self.assertIn("export AGENT_DEPTH=1", created[-1])
        self.assertEqual(self.pane()["env"]["AGENT_DEPTH"], "1")
        self.assertFalse(substrate.env_flag)

    def test_two_labels_that_share_a_tail_do_not_share_a_session(self):
        """`inspect` reopens a run whose own session may still be there."""
        long_id = "sol@medium-" + "x" * 60
        self.assertNotEqual(tmux.session_name(long_id),
                            tmux.session_name("inspect-" + long_id))
        for label in (long_id, "inspect-" + long_id):
            name = tmux.session_name(label)
            self.assertTrue(name.startswith("dispatch-"))
            self.assertLessEqual(len(name), tmux.SESSION_NAME_MAX)

    def test_the_operator_is_told_how_to_reach_the_session_by_hand(self):
        substrate, worker = self.open_worker()
        self.assertEqual(substrate.attach_hint(worker),
                         f"tmux attach -t {SESSION}")

    def test_closing_kills_the_session_and_the_worker_is_gone(self):
        substrate, worker = self.open_worker()
        self.assertTrue(substrate.exists(worker))
        self.assertEqual(substrate.worker_ids(), {"%0"})
        substrate.close(worker)
        killed = self.invocations("kill-session")[0]
        self.assertEqual(killed[killed.index("-t") + 1], SESSION)
        self.assertFalse(substrate.exists(worker))
        self.assertEqual(substrate.worker_ids(), set())

    def test_closing_a_session_that_is_already_gone_is_success(self):
        """A worker that runs `exit` takes its own session with it."""
        substrate, worker = self.open_worker()
        substrate.close(worker)
        substrate.close(worker)

    def test_only_panes_in_dispatch_sessions_count_as_workers(self):
        substrate, worker = self.open_worker()
        state = self.state()
        state["panes"]["%9"] = dict(state["panes"]["%0"], pane_id="%9",
                                    session="someone-elses-work")
        self.write_state(state)
        self.assertEqual(substrate.worker_ids(), {"%0"})


class TestTyping(FakeTmuxTestCase):
    def test_a_brief_is_typed_literally_then_submitted(self):
        """Three beats: type, let it draw, press enter and check that it went."""
        substrate, worker = self.open_worker()
        self.assertTrue(substrate.send_tui_line(worker, "read /w/prompt.txt"))
        typed = [call for call in self.invocations("send-keys") if "-l" in call]
        self.assertEqual(typed[-1][-3:], ["-l", "--", "read /w/prompt.txt"])
        enters = [call for call in self.invocations("send-keys") if "-l" not in call]
        self.assertEqual([call[-1] for call in enters], ["Enter"])
        self.assertIn("read /w/prompt.txt", substrate.read_screen(worker))

    def test_a_line_the_tui_never_took_gets_one_more_enter_and_says_so(self):
        substrate, worker = self.open_worker()
        self.patch_pane(swallow=True)
        log = self.root / "status.log"
        self.assertFalse(substrate.send_tui_line(worker, "read /w/prompt.txt",
                                                 log_path=log))
        enters = [call for call in self.invocations("send-keys") if "-l" not in call]
        self.assertEqual([call[-1] for call in enters], ["Enter", "Enter"])
        self.assertIn("SUBMIT-RETRY", log.read_text(encoding="utf-8"))

    def test_an_exit_command_is_not_confirmed_by_the_screen_moving(self):
        """Its receipt is the shell coming back, which the runner checks itself."""
        substrate, worker = self.open_worker()
        self.patch_pane(swallow=True)
        self.assertTrue(substrate.send_tui_line(worker, "/quit", enters=2,
                                                confirm=0))
        enters = [call for call in self.invocations("send-keys") if "-l" not in call]
        self.assertEqual([call[-1] for call in enters], ["Enter", "Enter"])

    def test_keys_go_in_by_name_and_a_name_tmux_has_no_key_for_refuses(self):
        """tmux types an unrecognised key name as text, so it never reaches it."""
        substrate, worker = self.open_worker()
        substrate.send_keys(worker, ["escape", "enter"])
        self.assertEqual(self.invocations("send-keys")[-1][-2:], ["Escape", "Enter"])
        with self.assertRaises(tmux.TmuxError):
            substrate.send_keys(worker, ["escpe"])

    def test_there_is_no_agent_channel_so_the_brief_goes_in_as_keystrokes(self):
        """The runner's answer to this refusal is the prompt-by-path line."""
        substrate, worker = self.open_worker()
        with self.assertRaises(tmux.SubstrateError):
            substrate.deliver_prompt(worker, "read the brief")


class TestScreen(FakeTmuxTestCase):
    def test_the_screen_is_the_visible_pane_and_history_is_asked_for(self):
        substrate, worker = self.open_worker()
        self.patch_pane(screen="scrolled-away\n" + "filler\n" * 80)
        self.assertNotIn("scrolled-away", substrate.read_screen(worker))
        self.assertIn("scrolled-away", substrate.read_screen(worker, history=True))
        captures = self.invocations("capture-pane")
        self.assertIn("-J", captures[0])
        self.assertEqual(captures[-1][captures[-1].index("-S") + 1],
                         f"-{tmux.HISTORY_LINES}")

    def test_the_screen_since_spawn_starts_at_this_launch(self):
        """A relaunch in the same pane leaves the last one's banner above it."""
        substrate, worker = self.open_worker()
        substrate.verify_environment(worker, [("AGENT_DEPTH", "1")],
                                     "AGENT_DEPTH", 1, run_id=RUN_ID)
        self.patch_pane(screen=self.pane()["screen"] + "session id: abc-123\n")
        found = substrate.screen_since_spawn(worker, RUN_ID)
        self.assertIn("session id: abc-123", found)
        self.assertNotIn("export AGENT_DEPTH", found)

    def test_a_pane_that_is_gone_reads_as_an_empty_screen(self):
        substrate, worker = self.open_worker()
        substrate.close(worker)
        self.assertEqual(substrate.read_screen(worker), "")
        self.assertIsNone(substrate.process_info(worker))


class TestEnvironment(FakeTmuxTestCase):
    def test_the_rung_is_re_asserted_in_the_pane_and_read_back(self):
        substrate, worker = self.open_worker(env={})
        found = substrate.verify_environment(
            worker, [("AGENT_DEPTH", "1"), ("DISPATCH_RUN", RUN_ID)],
            "AGENT_DEPTH", 1, run_id=RUN_ID)
        self.assertEqual(found, "1")
        self.assertEqual(self.pane()["env"]["DISPATCH_RUN"], RUN_ID)

    def test_a_pane_at_the_wrong_rung_never_gets_a_worker(self):
        substrate, worker = self.open_worker(env={})
        with self.assertRaises(tmux.TmuxError) as caught:
            substrate.verify_environment(worker, [("AGENT_DEPTH", "1")],
                                         "AGENT_DEPTH", 2, run_id=RUN_ID)
        self.assertIn("depth ladder", str(caught.exception))

    def test_a_metered_key_the_shell_re_exports_stops_the_run(self):
        self.update_state(sticky_env={"OPENAI_API_KEY": "sk-live"})
        substrate, worker = self.open_worker(env={})
        with self.assertRaises(tmux.TmuxError) as caught:
            substrate.verify_environment(worker, [("AGENT_DEPTH", "1"),
                                                  ("OPENAI_API_KEY", "")],
                                         "AGENT_DEPTH", 1,
                                         blank_keys=("OPENAI_API_KEY",),
                                         run_id=RUN_ID)
        self.assertIn("metered key", str(caught.exception))


class TestWorkerProcesses(FakeTmuxTestCase):
    def test_cpu_is_unknown_for_a_worker_on_another_machine(self):
        """Its pids are that machine's. Read here they are somebody else's
        process, and the check-in would judge the worker on it."""
        from dispatch.substrates import tmux as tmux_module
        remote_substrate = get_substrate(
            "tmux", command_builder=lambda argv: [str(self.bin / "tmux")] + argv[1:])
        with patch.object(tmux_module, "process_cpu_percent",
                          side_effect=AssertionError("measured a remote pid here")):
            self.assertIsNone(remote_substrate.cpu_percent([os.getpid()]))

    def codex_worker(self):
        """A pane whose shell forks a worker when the lane command is typed."""
        self.update_state(replies=[{"match": "codex", "running": True,
                                    "screen": "codex ready\n"}])
        return self.open_worker()

    def test_starting_a_worker_types_the_lane_command_and_waits_for_the_fork(self):
        substrate, worker = self.codex_worker()
        driver = drivers.get_driver("codex")
        spawn = substrate.start_worker(worker, driver, ["codex", "-m", "gpt"])
        self.assertEqual(spawn.method, "send-keys")
        self.assertEqual(spawn.flag, "")
        self.assertIn("codex -m gpt", self.pane()["screen"])
        info = substrate.process_info(worker)
        self.assertFalse(info.at_prompt)
        self.assertEqual(info.child_pids, (9000,))

    def test_a_shell_at_its_prompt_is_never_reported_as_a_worker(self):
        """Prompt hooks fork constantly; the foreground group is the signal."""
        substrate, worker = self.open_worker()
        info = substrate.wait_for_shell(worker)
        self.assertTrue(info.at_prompt)
        self.assertEqual(info.child_pids, ())
        self.assertEqual(substrate.kill_worker_tree(worker), [])

    def test_a_command_that_forks_nothing_is_not_a_worker_to_brief(self):
        substrate, worker = self.open_worker()
        with self.assertRaises(tmux.TmuxError) as caught:
            substrate.start_worker(worker, drivers.get_driver("codex"), ["codex"])
        self.assertIn("no worker to brief", str(caught.exception))

    def test_a_pane_whose_shell_is_gone_is_not_a_pane_at_a_prompt(self):
        substrate, worker = self.open_worker()
        tmux.tty_process_rows = lambda tty, builder=None: []
        self.assertIsNone(substrate.process_info(worker))


class TestExitCode(FakeTmuxTestCase):
    def test_the_exit_code_comes_back_out_of_the_shells_own_echo(self):
        substrate, worker = self.open_worker()
        self.patch_pane(rc=7)
        self.assertEqual(substrate.read_exit_code(worker, RUN_ID), 7)

    def test_an_older_marker_in_the_scrollback_is_not_this_runs_exit_code(self):
        substrate, worker = self.open_worker()
        token = tmux.rc_token(RUN_ID)
        self.patch_pane(rc=7, screen=f"dispatch-rc-{token}=99\n")
        self.assertEqual(substrate.read_exit_code(worker, RUN_ID), 7)

    def test_a_pane_that_never_answers_leaves_the_exit_code_unknown(self):
        substrate, worker = self.open_worker()
        self.patch_pane(swallow=True)
        log = self.root / "status.log"
        self.assertIsNone(substrate.read_exit_code(worker, RUN_ID, log_path=log))
        self.assertIn("RC-PROBE-TIMEOUT", log.read_text(encoding="utf-8"))


class TestKilling(FakeTmuxTestCase):
    def test_a_pane_on_another_machine_is_signalled_there(self):
        """The pids number processes on that machine. Signalling them here would
        hit whatever local process happens to share a number."""
        built = []

        def build(argv):
            # Stands in for `ssh <machine> ...`: the tmux calls run here against
            # the fake, and the signals go nowhere at all.
            built.append(list(argv))
            return ["true"] if argv[0] == "kill" else list(argv)

        substrate, worker = self.open_worker(
            substrate=get_substrate("tmux", command_builder=build))
        self.patch_pane(running=True)
        tmux.KILL_GRACE_SECONDS = 0.01
        self.addCleanup(setattr, tmux, "KILL_GRACE_SECONDS",
                        tmux.processes.KILL_GRACE_SECONDS)
        signalled = []
        stop_group = tmux.processes.stop_process_group
        tmux.processes.stop_process_group = lambda pgid: signalled.append(pgid) or True
        self.addCleanup(setattr, tmux.processes, "stop_process_group", stop_group)

        self.assertEqual(substrate.kill_worker_tree(worker), [9000])
        self.assertEqual(signalled, [])
        kills = [call for call in built if call[0] == "kill"]
        self.assertEqual([call[1] for call in kills], ["-TERM", "-KILL"])
        self.assertEqual({call[2] for call in kills}, {"-9000"})

    def test_the_foreground_group_is_what_gets_signalled(self):
        substrate, worker = self.open_worker()
        self.patch_pane(running=True)
        signalled = []
        stop_group = tmux.processes.stop_process_group
        tmux.processes.stop_process_group = lambda pgid: signalled.append(pgid) or True
        self.addCleanup(setattr, tmux.processes, "stop_process_group", stop_group)
        descendants = tmux.descendant_pids
        tmux.descendant_pids = lambda pids: []
        self.addCleanup(setattr, tmux, "descendant_pids", descendants)
        self.assertEqual(substrate.kill_worker_tree(worker), [9000])
        self.assertEqual(signalled, [9000])


if __name__ == "__main__":
    unittest.main()
