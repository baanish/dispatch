"""Machines: placement, launching a run on one, proxying it, and mirroring it.

Every case runs against the fake `ssh` and `scp` on PATH (`fake_ssh.py`). No
machine is reached, no real ssh is spawned, and no substrate is opened: a run
placed on a machine in `dispatch` mode never touches this one's, which is what
lets these cases run with no herdr daemon anywhere.
"""

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ssh import FakeSshTestCase  # noqa: E402

from dispatch import caps, cli, config, lanes, records, remote  # noqa: E402
from dispatch import runner, substrates  # noqa: E402
from dispatch.errors import DispatchError  # noqa: E402


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------


class TestPlacement(FakeSshTestCase):
    def machines(self, **extra):
        table = {self.machine_name: self.machine}
        table.update(extra)
        self.addCleanup(remote.set_machines, remote.set_machines(table))

    def test_the_on_flag_wins_over_the_lane(self):
        other = remote.Machine(name="other", ssh="operator@other")
        self.machines(other=other)
        lane = lanes.resolve_lane("astra@medium")
        self.assertEqual(remote.place_run("other", lane), other)

    def test_a_lane_may_name_the_machine_it_always_runs_on(self):
        previous = lanes.set_lane_table(
            {"pinned": lanes.LaneSpec("codex", "some-model", ("high",),
                                      machine=self.machine_name)})
        self.addCleanup(lanes.set_lane_table, previous)
        lane = lanes.resolve_lane("pinned@high")
        self.assertEqual(remote.place_run("", lane), self.machine)

    def test_nothing_named_means_local(self):
        self.assertIsNone(remote.place_run("", lanes.resolve_lane("astra@medium")))

    def test_on_local_overrides_a_lane_that_names_a_machine(self):
        previous = lanes.set_lane_table(
            {"pinned": lanes.LaneSpec("codex", "some-model", ("high",),
                                      machine=self.machine_name)})
        self.addCleanup(lanes.set_lane_table, previous)
        self.assertIsNone(remote.place_run("local", lanes.resolve_lane("pinned@high")))

    def test_an_unknown_machine_lists_the_configured_ones(self):
        with self.assertRaises(DispatchError) as caught:
            remote.place_run("nowhere", lanes.resolve_lane("astra@medium"))
        self.assertIn("known machines: box", str(caught.exception))

    def test_applying_a_config_is_what_puts_machines_here(self):
        """`[machines.<name>]` is parsed by config and installed here, so that
        one file is the only place a machine is described."""
        path = self.root / "machines.toml"
        path.write_text('[machines.one]\nssh = "operator@one"\n\n'
                        '[machines.two]\nssh = "operator@two"\n'
                        'mode = "shell"\nhome = "/opt/dispatch"\n',
                        encoding="utf-8")
        self.addCleanup(remote.set_machines, remote.machine_table())
        loaded = config.apply_config(
            config.load_config(env={config.CONFIG_ENV: str(path)}))
        table = remote.machine_table()
        self.assertEqual(sorted(table), ["one", "two"])
        self.assertEqual(table, loaded.machines)
        self.assertEqual(table["one"].mode, remote.DISPATCH_MODE)
        self.assertEqual(table["one"].home, remote.DEFAULT_REMOTE_HOME)
        self.assertEqual(table["two"].mode, remote.SHELL_MODE)
        self.assertEqual(table["two"].home, "/opt/dispatch")


# --------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------


class TestRemoteLaunch(FakeSshTestCase):
    def test_the_brief_is_staged_and_the_run_id_recorded(self):
        run_id, remote_dir = self.launched()
        rec = self.run_remote_bg()

        self.assertEqual(rec["machine"], self.machine_name)
        self.assertEqual(rec["remote_id"], run_id)
        self.assertEqual(rec["remote_dir"], remote_dir)
        self.assertEqual(rec["state"], "running")
        self.assertTrue(rec["remote_supervised"])
        staged = (self.remote_root / ".dispatch" / "remote" / rec["id"] / "brief.md")
        self.assertEqual(staged.read_text(encoding="utf-8"), "do the thing\n")
        launch = [c for c in self.commands() if "dispatch run" in c][0]
        self.assertIn(f"dispatch run astra@medium {staged_path(rec)}/brief.md --bg",
                      launch)

    def test_the_machine_is_told_to_run_it_and_not_to_place_it_again(self):
        """It resolves the lane against its own config, where that lane may
        name a machine, and the run would leave the box it was placed on."""
        self.launched()
        self.run_remote_bg()
        launch = [c for c in self.commands() if "dispatch run" in c][0]
        self.assertIn(f"--on {remote.LOCAL}", launch)

    def test_a_home_with_a_space_in_it_is_staged_to_that_exact_path(self):
        """scp transfers over SFTP, which takes the pathname literally, so a
        shell-quoted path would stage the brief under a name holding quotes."""
        self.write_config('\n[machines.roomy]\nssh = "operator@roomy"\n'
                          'home = "~/dispatch home"\n')
        self.launched()
        self.assertEqual(cli.main(["run", "astra@medium", str(self.brief),
                                   "--bg", "--on", "roomy"]), cli.EXIT_OK)
        rec = records.all_records()[-1]
        staged = (self.remote_root / "dispatch home" / "remote" / rec["id"]
                  / "brief.md")
        self.assertEqual(staged.read_text(encoding="utf-8"), "do the thing\n")

    def test_an_uploaded_path_is_an_operand_and_never_one_of_scps_options(self):
        """The brief, the schema, and the image are paths somebody gave
        dispatch, and scp reads one beginning with `-` as its own option:
        `-S program` is the program scp runs on this machine to connect."""
        self.launched()
        self.run_remote_bg()
        transfers = self.calls("scp")
        self.assertTrue(transfers)
        for call in transfers:
            # SFTP is what the raw remote pathname is written for, so it is
            # asked for rather than assumed.
            self.assertEqual(call["argv"][0], "-s")
            self.assertIn("--", call["argv"])
            for operand in call["paths"]:
                if not operand.startswith(f"{self.machine_ssh}:"):
                    self.assertTrue(os.path.isabs(operand), operand)

    def test_a_brief_under_a_directory_with_a_colon_in_its_name_still_goes_up(self):
        """scp reads `a:b/brief.md` as a path on a machine called `a`, and
        fetches it over another connection instead of uploading the file."""
        folder = self.work / "a:b"
        folder.mkdir()
        (folder / "brief.md").write_text("do the thing\n", encoding="utf-8")
        self.launched()
        self.assertEqual(cli.main(["run", "astra@medium", "a:b/brief.md", "--bg",
                                   "--on", self.machine_name]), cli.EXIT_OK)
        rec = records.all_records()[-1]
        staged = self.remote_root / ".dispatch" / "remote" / rec["id"] / "brief.md"
        self.assertEqual(staged.read_text(encoding="utf-8"), "do the thing\n")

    def test_the_launch_is_journaled_where_the_operator_looks(self):
        run_id, _ = self.launched()
        rec = self.run_remote_bg()
        log = (Path(rec["dir"]) / "status.log").read_text(encoding="utf-8")
        self.assertIn("REMOTE ", log)
        self.assertIn(run_id, log)
        self.assertIn(self.machine_name, log)

    def test_dispatch_adds_only_batch_mode_and_the_control_settings(self):
        self.launched()
        self.run_remote_bg()
        argv = self.calls("ssh")[0]["argv"]
        options = [argv[index + 1] for index, item in enumerate(argv) if item == "-o"]
        self.assertIn("BatchMode=yes", options)
        self.assertIn("ControlMaster=auto", options)
        self.assertIn(f"ControlPersist={remote.CONTROL_PERSIST}", options)
        socket = [o for o in options if o.startswith("ControlPath=")][0]
        self.assertTrue(socket.startswith(f"ControlPath={self.home}/ssh/"))
        # No identity, no port, no user, no host key policy: all of that is the
        # operator's ssh config, and overriding it here would silently ignore it.
        self.assertEqual(len(options), 5)

    def test_nothing_sensitive_reaches_a_command_line(self):
        self.brief.write_text("the secret is hunter2\n", encoding="utf-8")
        self.launched()
        rec = self.run_remote_bg()
        for call in self.calls():
            self.assertNotIn("hunter2", json.dumps(call))
        staged = self.remote_root / ".dispatch" / "remote" / rec["id"] / "brief.md"
        self.assertIn("hunter2", staged.read_text(encoding="utf-8"))

    def test_the_ladder_and_the_session_cross_the_connection(self):
        self.launched()
        self.run_remote_bg()
        launch = [c for c in self.commands() if "dispatch run" in c][0]
        # `~` stays bare so the machine's own shell expands it; quoted, the
        # remote dispatch would take it as a directory named with a tilde.
        self.assertIn("DISPATCH_HOME=~/.dispatch ", launch)
        self.assertIn("AGENT_DEPTH=0", launch)
        self.assertIn("DISPATCH_SESSION=test-session", launch)

    def test_remote_paths_are_passed_through_untouched(self):
        """`--dir` names a directory on the machine, so it is never resolved here."""
        self.launched()
        rec = self.run_remote_bg("--dir", "/srv/project", "--write")
        self.assertEqual(rec["cwd"], "/srv/project")
        launch = [c for c in self.commands() if "dispatch run" in c][0]
        self.assertIn("--dir /srv/project", launch)
        self.assertIn("--write", launch)

    def test_a_machine_without_dispatch_says_how_to_fix_it(self):
        self.set_replies([["dispatch run", {
            "rc": 127, "stderr": "bash: dispatch: command not found\n"}]])
        message = self.main_fails(["run", "astra@medium", str(self.brief), "--bg",
                                   "--on", self.machine_name])
        self.assertIn("has no `dispatch` on PATH", message)
        self.assertIn('mode = "shell"', message)
        self.assertIn("dispatch = \"/full/path/to/dispatch\"", message)

    def test_an_unreachable_machine_is_named_with_the_command_that_proves_it(self):
        self.set_replies([["dispatch run", {
            "rc": 255, "stderr": "ssh: connect to host box port 22: No route\n"}]])
        message = self.main_fails(["run", "astra@medium", str(self.brief), "--bg",
                                   "--on", self.machine_name])
        self.assertIn("is not reachable", message)
        self.assertIn(f"ssh {self.machine_ssh} exit 0", message)

    def test_a_reservation_whose_launcher_died_does_not_hold_its_slot_forever(self):
        """No run was ever reported by the machine, so there is nothing to poll
        and the record has to be settled here."""
        rec = remote.prepare_remote_run(
            remote.machine_for(self.machine_name),
            lanes.resolve_lane("astra@medium"), "brief",
            records.RunOptions(dir="/srv/project", bg=True))
        rec["reserved_at"] = 0
        records.save_record(rec)
        live = caps.live_records(runner.SubstrateSweep(None))
        self.assertNotIn(rec["id"], [found["id"] for found in live])
        self.assertEqual(records.load_record(rec["id"])["state"], "orphaned")

    def test_a_launch_that_reports_no_run_id_fails_the_run(self):
        self.set_replies([["dispatch run", {"stdout": "\n"}]])
        message = self.main_fails(["run", "astra@medium", str(self.brief), "--bg",
                                   "--on", self.machine_name])
        self.assertIn("did not report a run id", message)
        rec = records.all_records()[0]
        self.assertEqual(rec["state"], "failed")
        # The reason survives, and it does not claim nothing runs over there.
        self.assertIn("did not report a run id", rec["error"])
        self.assertIn("may be running there", rec["error"])

    def test_a_foreground_run_outlives_a_poll_that_fails(self):
        """One dropped connection used to end the command with a usage error
        and no run id, while the worker carried on over there."""
        run_id, remote_dir = self.launched()
        self.remote_record(run_id, state="done", rc=0,
                           finished="2026-01-01T00:00:00Z")
        self.remote_file(f"{remote_dir}/out.md", "the deliverable\n")
        real, calls = remote.poll_remote_run, []

        def flaky(rec, machine=None):
            calls.append(rec["id"])
            if len(calls) == 1:
                raise DispatchError("machine box is not reachable: timed out")
            return real(rec, machine)

        with patch.object(remote, "poll_remote_run", side_effect=flaky), \
                patch.object(remote, "POLL_SECONDS", 0.01), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            out = self.main_out(["run", "astra@medium", str(self.brief),
                                 "--on", self.machine_name])
        self.assertIn("the deliverable", out)
        self.assertIn(calls[0], err.getvalue())
        self.assertIn("still running", err.getvalue())

    def test_a_foreground_run_waits_for_the_machine_and_prints_what_it_wrote(self):
        run_id, remote_dir = self.launched()
        self.remote_record(run_id, state="done", rc=0,
                           finished="2026-01-01T00:00:00Z")
        self.remote_file(f"{remote_dir}/out.md", "the deliverable\n")
        out = self.main_out(["run", "astra@medium", str(self.brief),
                             "--on", self.machine_name])
        self.assertEqual(out, "the deliverable\n")
        self.assertEqual(records.all_records()[0]["state"], "done")

    def test_a_remote_run_holds_a_slot_like_any_other(self):
        self.launched()
        rec = self.run_remote_bg()
        self.assertEqual([r["id"] for r in caps.live_records()], [rec["id"]])


def staged_path(rec):
    return f"~/.dispatch/remote/{rec['id']}"


# --------------------------------------------------------------------------
# Proxying a live run
# --------------------------------------------------------------------------


class TestRemoteProxy(FakeSshTestCase):
    def setUp(self):
        super().setUp()
        self.remote_id, self.remote_dir = self.launched()
        self.rec = self.run_remote_bg()

    def finish(self, state="done", rc=0):
        self.remote_record(self.remote_id, state=state, rc=rc,
                           finished="2026-01-01T00:00:00Z")

    def test_status_asks_the_machine_and_records_what_it_says(self):
        self.remote_record(self.remote_id, state="running", needs_hand="trust")
        out = self.main_out(["status"])
        self.assertIn(f"on {self.machine_name}", out)
        self.assertIn("NEEDS HAND", out)
        self.assertEqual(records.load_record(self.rec["id"])["needs_hand"], "trust")
        # The reconcile the machine's own status runs is what settles a
        # background run there, so the poll triggers it in the same round trip.
        poll = [c for c in self.commands() if "run.json" in c][-1]
        self.assertIn("dispatch status", poll)

    def test_a_live_remote_run_is_never_orphaned_by_a_local_sweep(self):
        """No worker of this run exists here, and that is not evidence."""
        self.main_out(["status"])
        rec = records.load_record(self.rec["id"])
        self.assertEqual(rec["state"], "running")
        self.assertTrue(caps.run_is_live(rec))

    def test_the_machines_verdict_lands_in_the_local_record(self):
        self.finish(state="done", rc=0)
        self.main_out(["status"])
        rec = records.load_record(self.rec["id"])
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["rc"], 0)
        self.assertEqual(caps.live_records(), [])

    def test_a_record_for_another_run_is_not_taken_as_this_ones(self):
        """Whatever a poll merges is read afterwards as this run's own state,
        so another run's `done` and exit code would end this one."""
        self.remote_record(self.remote_id, id="astra@medium-990000-zzzz",
                           state="done", rc=0, finished="2026-01-01T00:00:00Z")
        self.main_out(["status"])
        rec = records.load_record(self.rec["id"])
        self.assertEqual(rec["state"], "running")
        self.assertIn("astra@medium-990000-zzzz", rec["remote_error"])

    def test_a_record_that_is_not_an_object_is_a_remote_error(self):
        """A machine mid-write answers `null` as readily as a record, and the
        poll that reads it has to fail the way an unreachable machine does."""
        self.remote_file(f"{self.remote_dir}/run.json", "null\n")
        self.main_out(["status"])
        rec = records.load_record(self.rec["id"])
        self.assertEqual(rec["state"], "running")
        self.assertIn("not a run record", rec["remote_error"])

    def test_a_field_the_machine_did_not_write_as_dispatch_does_is_refused(self):
        """`state` decides whether the run is over and `rc` is its exit code."""
        self.remote_record(self.remote_id, state="finished")
        self.main_out(["status"])
        self.assertIn("not a state dispatch has",
                      records.load_record(self.rec["id"])["remote_error"])
        self.remote_record(self.remote_id, state="done", rc="0")
        self.main_out(["status"])
        rec = records.load_record(self.rec["id"])
        self.assertEqual(rec["state"], "running")
        self.assertIn("rc='0'", rec["remote_error"])

    def test_kill_proxies_to_the_machine(self):
        self.set_replies([["dispatch kill",
                           {"stdout": f"{self.remote_id} killed\n"}]])
        self.finish(state="killed")
        out = self.main_out(["kill", self.rec["id"]])
        self.assertIn("killed", out)
        killed = [c for c in self.commands() if "dispatch kill" in c][0]
        self.assertIn(f"dispatch kill {self.remote_id}", killed)
        self.assertEqual(records.load_record(self.rec["id"])["state"], "killed")

    def test_kill_on_an_unreachable_machine_says_the_run_may_still_be_live(self):
        self.set_replies([["dispatch kill", {"rc": 255, "stderr": "no route\n"}]])
        message = self.main_fails(["kill", self.rec["id"]])
        self.assertIn("may still be live on box", message)
        self.assertIn(self.remote_id, message)
        rec = records.load_record(self.rec["id"])
        self.assertEqual(rec["state"], "orphaned")
        self.assertIn("no route", rec["remote_error"])

    def test_an_unreachable_machine_does_not_end_a_run(self):
        """A dropped connection is not a dead worker; it is a dropped connection."""
        self.set_replies([["cat", {"rc": 255, "stderr": "no route\n"}]])
        (self.remote_root / ".dispatch" / "runs" / self.remote_id
         / "run.json").unlink()
        out = self.main_out(["status"])
        self.assertIn("UNREACHABLE", out)
        rec = records.load_record(self.rec["id"])
        self.assertEqual(rec["state"], "running")

    def test_logs_come_from_the_machine_while_the_run_is_live(self):
        self.remote_file(f"{self.remote_dir}/status.log", "START\nHEARTBEAT\n")
        out = self.main_out(["logs", self.rec["id"]])
        self.assertEqual(out, "START\nHEARTBEAT\n")

    def test_steer_delivers_the_message_as_a_file(self):
        self.set_replies([["dispatch steer",
                           {"stdout": f"{self.remote_id} steered\n"}]])
        out = self.main_out(["steer", self.rec["id"], "stop and read the tests"])
        self.assertIn("steered", out)
        steer = [c for c in self.commands() if "dispatch steer" in c][0]
        self.assertNotIn("stop and read the tests", steer)
        self.assertIn("--message-file", steer)
        staged = list((self.remote_root / ".dispatch" / "remote"
                       / self.rec["id"]).glob("steer-*.md"))
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].read_text(encoding="utf-8"),
                         "stop and read the tests")

    def test_two_steers_in_one_second_stage_two_files(self):
        """The machine reads the staged message after the command gets there,
        so one name for both is one steer delivering the other's correction."""
        self.set_replies([["dispatch steer", {"stdout": "steered\n"}]])
        with patch.object(remote.time, "strftime", return_value="120000"):
            self.main_out(["steer", self.rec["id"], "read the tests first"])
            self.main_out(["steer", self.rec["id"], "then run them"])
        staged = (self.remote_root / ".dispatch" / "remote"
                  / self.rec["id"]).glob("steer-*.md")
        self.assertEqual({path.read_text(encoding="utf-8") for path in staged},
                         {"read the tests first", "then run them"})

    def test_a_message_may_also_come_from_a_file(self):
        note = self.work / "note.md"
        note.write_text("read the tests first", encoding="utf-8")
        self.set_replies([["dispatch steer", {"stdout": "steered\n"}]])
        self.main_out(["steer", self.rec["id"], "--message-file", str(note)])
        staged = list((self.remote_root / ".dispatch" / "remote"
                       / self.rec["id"]).glob("steer-*.md"))
        self.assertEqual(staged[0].read_text(encoding="utf-8"),
                         "read the tests first")

    def test_continue_records_the_run_the_machine_started(self):
        self.finish()
        self.main_out(["status"])
        child_id = "astra@medium-130000-cd34"
        self.set_replies([["dispatch continue", {
            "stdout": f"{child_id}\n~/.dispatch/runs/{child_id}\n"}]])
        self.remote_record(child_id, state="running")
        out = self.main_out(["continue", self.rec["id"], "one more thing", "--bg"])
        child = records.load_record(out.splitlines()[0])
        self.assertEqual(child["parent"], self.rec["id"])
        self.assertEqual(child["kind"], "continue")
        self.assertEqual(child["remote_id"], child_id)
        command = [c for c in self.commands() if "dispatch continue" in c][0]
        self.assertIn(f"dispatch continue {self.remote_id}", command)
        self.assertNotIn("one more thing", command)

    def test_steering_a_finished_run_becomes_a_continuation_this_side_records(self):
        """The machine would start a run of its own; this side has to own it."""
        self.finish()
        child_id = "astra@medium-140000-ef56"
        self.set_replies([["dispatch continue", {
            "stdout": f"{child_id}\n~/.dispatch/runs/{child_id}\n"}]])
        self.remote_record(child_id, state="done", rc=0,
                           finished="2026-01-01T00:00:00Z")
        # A run that ends `done` has an answer, and it has to come down.
        self.remote_file(f"~/.dispatch/runs/{child_id}/out.md", "the second answer\n")
        self.main_out(["steer", self.rec["id"], "one more thing"])
        self.assertEqual([c for c in self.commands() if "dispatch steer" in c], [])
        child = [r for r in records.all_records() if r["kind"] == "continue"][0]
        self.assertEqual(child["remote_id"], child_id)

    def test_inspect_points_at_the_machine_that_holds_the_session(self):
        self.finish()
        self.main_out(["status"])
        message = self.main_fails(["inspect", self.rec["id"]])
        self.assertIn(f"ssh -t {self.machine_ssh} dispatch inspect", message)

    def test_watch_attach_points_at_the_machine(self):
        message = self.main_fails(["watch", self.rec["id"], "--attach"])
        self.assertIn(f"ssh -t {self.machine_ssh} dispatch watch", message)


# --------------------------------------------------------------------------
# The terminal mirror
# --------------------------------------------------------------------------


class TestTerminalMirror(FakeSshTestCase):
    def setUp(self):
        super().setUp()
        self.remote_id, self.remote_dir = self.launched()
        self.rec = self.run_remote_bg()
        self.remote_file(f"{self.remote_dir}/out.md", "the deliverable\n")
        self.remote_file(f"{self.remote_dir}/status.log", "START\nCOMPLETE done\n")
        self.remote_file(f"{self.remote_dir}/screen.log", "the last screen\n")
        self.remote_record(self.remote_id, state="done", rc=0,
                           finished="2026-01-01T00:00:00Z", session_id="abc")

    def local(self, name):
        return Path(self.rec["dir"]) / name

    def test_the_artifacts_come_down_at_terminal_state(self):
        self.main_out(["status"])
        self.assertEqual(self.local("out.md").read_text(encoding="utf-8"),
                         "the deliverable\n")
        self.assertIn("COMPLETE", self.local("status.log").read_text(encoding="utf-8"))
        self.assertEqual(self.local("screen.log").read_text(encoding="utf-8"),
                         "the last screen\n")

    def test_a_copy_that_fails_is_tried_again_rather_than_called_done(self):
        """A dropped connection at the last step must not leave an empty mirror
        marked complete, with the answer never fetched."""
        with patch.object(remote, "scp_down", return_value=False):
            self.main_out(["status"])
        rec = records.load_record(self.rec["id"])
        self.assertFalse(rec.get("mirrored"))
        self.assertFalse(self.local("out.md").is_file())
        self.main_out(["status"])
        rec = records.load_record(self.rec["id"])
        self.assertTrue(rec["mirrored"])
        self.assertEqual(self.local("out.md").read_text(encoding="utf-8"),
                         "the deliverable\n")

    def test_a_done_run_whose_answer_never_arrives_is_not_exit_0(self):
        """The machine said `done`. With nothing copied down, 0 would tell the
        caller an answer is in hand."""
        with patch.object(remote, "scp_down", return_value=False), \
                patch.object(remote, "MIRROR_RETRIES", 1), \
                patch.object(cli, "REMOTE_FOLLOW_SECONDS", 0.05), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["wait", self.rec["id"]])
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("could not be copied down", err.getvalue())

    def test_wait_holds_until_the_answer_has_come_down(self):
        """The machine said `done` but the copy failed: a waiter that returned
        now would report success with nothing to show."""
        calls = []
        real = remote.scp_down

        def flaky(*args, **kwargs):
            calls.append(args)
            return False if len(calls) <= 2 else real(*args, **kwargs)

        with patch.object(remote, "scp_down", side_effect=flaky), \
                patch.object(cli, "REMOTE_FOLLOW_SECONDS", 0.05):
            code = cli.main(["wait", self.rec["id"]])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.local("out.md").read_text(encoding="utf-8"),
                         "the deliverable\n")

    def test_the_machines_record_lands_without_replacing_this_ones(self):
        self.main_out(["status"])
        mirrored = json.loads(self.local(remote.REMOTE_RECORD_FILE)
                              .read_text(encoding="utf-8"))
        self.assertEqual(mirrored["id"], self.remote_id)
        rec = records.load_record(self.rec["id"])
        self.assertEqual(rec["id"], self.rec["id"])
        self.assertEqual(rec["dir"], self.rec["dir"])
        self.assertEqual(rec["machine"], self.machine_name)
        self.assertEqual(rec["session_id"], "abc")

    def test_the_mirror_runs_once(self):
        self.main_out(["status"])
        first = len(self.calls("scp"))
        self.main_out(["status"])
        self.main_out(["status"])
        self.assertEqual(len(self.calls("scp")), first)

    def test_a_finished_run_is_read_from_the_mirror_not_the_machine(self):
        self.main_out(["status"])
        before = len(self.calls("ssh"))
        out = self.main_out(["logs", self.rec["id"]])
        self.assertIn("COMPLETE", out)
        self.assertEqual(len(self.calls("ssh")), before)

    def test_out_copies_where_the_caller_asked(self):
        target = self.work / "result.md"
        remote.mirror_remote_run(
            dict(records.load_record(self.rec["id"]),
                 out_copy=records.out_copy_path(str(target)),
                 out_copy_dir=records.out_copy_dir(str(target))))
        self.assertEqual(target.read_text(encoding="utf-8"), "the deliverable\n")

    def test_wait_blocks_until_the_machine_says_it_is_over(self):
        code = cli.main(["wait", self.rec["id"]])
        self.assertEqual(code, cli.EXIT_OK)


# --------------------------------------------------------------------------
# Shell mode
# --------------------------------------------------------------------------


class FakeTmuxSubstrate:
    """Stands in for the tmux substrate, which is what shell mode reuses.

    It answers what a run needs before its pane exists (a name, a TUI, an
    untracked status) and refuses the agent channel tmux does not have, which is
    what puts a brief on the typed fallback.
    """

    name = "tmux"
    has_tui = True

    def __init__(self, command_builder=None):
        self.command_builder = command_builder
        self.typed = []
        self.live = []

    def agent_name(self, run_id):
        return str(run_id)

    def worker_ids(self):
        return list(self.live)

    def status(self, worker):
        return substrates.WorkerStatus()

    def read_screen(self, worker):
        return ""

    def deliver_prompt(self, worker, text):
        raise substrates.SubstrateError("no agent channel in a bare pane")

    def send_tui_line(self, worker, text, enters=1, settle=None, confirm=None,
                      log_path=None):
        self.typed.append(text)


class TestShellMode(FakeSshTestCase):
    def setUp(self):
        super().setUp()
        self.write_config('\n[machines.vm]\nssh = "operator@vm"\n'
                          'mode = "shell"\n')
        self.shell = remote.machine_for("vm")
        self.addCleanup(substrates.SUBSTRATES.__setitem__, "tmux",
                        substrates.SUBSTRATES["tmux"])
        substrates.SUBSTRATES["tmux"] = FakeTmuxSubstrate

    def test_every_tmux_call_is_wrapped_in_ssh(self):
        build = remote.remote_tmux_command(self.shell)
        argv = build(["tmux", "capture-pane", "-p", "-t", "worker-1"])
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-2], self.shell.ssh)
        self.assertEqual(argv[-1], "tmux capture-pane -p -t worker-1")

    def test_a_typed_line_stays_one_argument_on_the_machine(self):
        """ssh hands its arguments to a shell, which would re-split them."""
        build = remote.remote_tmux_command(self.shell)
        argv = build(["tmux", "send-keys", "-t", "w", "read /tmp/prompt.txt", "Enter"])
        self.assertEqual(argv[-1],
                         "tmux send-keys -t w 'read /tmp/prompt.txt' Enter")

    def test_one_control_connection_serves_every_call(self):
        build = remote.remote_tmux_command(self.shell)
        first = build(["tmux", "list-panes"])
        second = build(["tmux", "list-panes"])
        socket = [a for a in first if a.startswith("ControlPath=")]
        self.assertEqual(socket, [a for a in second if a.startswith("ControlPath=")])
        self.assertTrue(socket[0].startswith(f"ControlPath={self.home}/ssh/"))

    def test_the_substrate_is_handed_the_wrapper(self):
        substrate = remote.shell_substrate(self.shell)
        self.assertIsInstance(substrate, FakeTmuxSubstrate)
        self.assertEqual(substrate.command_builder(["tmux", "kill-server"])[0], "ssh")

    def test_shell_mode_needs_a_directory_on_the_machine(self):
        message = self.main_fails(
            ["run", "astra@medium", str(self.brief), "--on", "vm"])
        self.assertIn("no directory in common", message)
        self.assertIn("--dir", message)

    def test_shell_mode_never_asks_the_machine_to_run_dispatch(self):
        self.main_fails(["run", "astra@medium", str(self.brief), "--on", "vm"])
        self.assertEqual([c for c in self.commands() if "dispatch" in c], [])

    def test_an_image_has_no_path_on_the_machine_so_it_is_refused(self):
        image = self.work / "shot.png"
        image.write_bytes(b"png")
        message = self.main_fails(["run", "astra@medium", str(self.brief),
                                   "--on", "vm", "--dir", "/srv/work",
                                   "--image", str(image)])
        self.assertIn("--image", message)
        self.assertIn("shares no filesystem", message)

    # -- staging ---------------------------------------------------------

    def shell_record(self, substrate=None, **fields):
        """The record a shell-mode `dispatch run` prepares, before anything runs."""
        rec = runner.prepare_run(
            lanes.resolve_lane("astra@medium"), "do the thing\n",
            records.RunOptions(dir="/srv/work"),
            substrate or FakeTmuxSubstrate(), machine=self.shell)
        rec.update(fields)
        records.save_record(rec)
        return rec

    def staged(self, rec, name):
        return self.remote_root / ".dispatch" / "remote" / rec["id"] / name

    def test_the_prompt_names_the_machines_paths_and_none_of_this_ones(self):
        """The worker's pane is on a box with a filesystem of its own: a local
        path in the prompt names nothing there, so the run wrote nothing back."""
        rec = self.shell_record()
        staging = f"~/.dispatch/remote/{rec['id']}"
        self.assertEqual(rec["remote_staging"], staging)
        self.assertIn(f"{staging}/brief.md", rec["prompt"])
        self.assertIn(f"{staging}/out.md", rec["prompt"])
        self.assertNotIn(rec["dir"], rec["prompt"])

    def test_the_brief_and_the_prompt_are_put_on_the_machine(self):
        rec = self.shell_record()
        remote.stage_shell_run(rec)
        self.assertEqual(self.staged(rec, "brief.md").read_text(encoding="utf-8"),
                         "do the thing\n")
        self.assertEqual(self.staged(rec, "prompt.txt").read_text(encoding="utf-8"),
                         rec["prompt"])
        self.assertIn("STAGED",
                      (Path(rec["dir"]) / "status.log").read_text(encoding="utf-8"))

    def test_the_typed_fallback_names_the_prompt_on_the_machine(self):
        """A bare pane has no agent channel, so the brief goes in as one line
        naming a file: it has to be a file the pane can open."""
        substrate = FakeTmuxSubstrate()
        rec = self.shell_record(substrate, worker_id="%0", worker_group="s")
        wrapper = runner.RunWrapper(substrate, rec)
        wrapper.attach()
        wrapper.prompt_worker(rec["prompt"])
        self.assertEqual(len(substrate.typed), 1)
        self.assertIn(f"~/.dispatch/remote/{rec['id']}/prompt.txt",
                      substrate.typed[0])
        self.assertNotIn(rec["dir"], substrate.typed[0])
        self.assertEqual(self.staged(rec, "prompt.txt").read_text(encoding="utf-8"),
                         rec["prompt"])

    def test_the_deliverable_is_mirrored_down_only_when_it_changes(self):
        """Everything dispatch asks about the deliverable is asked of the local
        copy, and its mtime is the quiet window a turn ends on."""
        rec = self.shell_record()
        local = Path(rec["dir"]) / "out.md"
        self.remote_file(f"~/.dispatch/remote/{rec['id']}/out.md", "the answer")

        self.assertTrue(remote.fetch_deliverable(rec))
        self.assertEqual(local.read_text(encoding="utf-8"), "the answer")
        stamp = local.stat().st_mtime

        self.assertFalse(remote.fetch_deliverable(rec), "copied an unchanged file")
        self.assertEqual(local.stat().st_mtime, stamp, "the quiet window restarted")

        self.remote_file(f"~/.dispatch/remote/{rec['id']}/out.md", "the answer, plus")
        self.assertTrue(remote.fetch_deliverable(rec))
        self.assertEqual(local.read_text(encoding="utf-8"), "the answer, plus")

    def test_a_deliverable_that_is_not_there_yet_leaves_the_local_copy_alone(self):
        rec = self.shell_record()
        self.assertFalse(remote.fetch_deliverable(rec))
        self.assertFalse((Path(rec["dir"]) / "out.md").is_file())

    def test_a_machine_that_cannot_be_reached_does_not_fail_the_run(self):
        """The mirror runs inside a poll: a copy that cannot be taken is asked
        for again on the next one, and the pane says the machine is gone long
        before a missing copy would."""
        rec = self.shell_record()
        empty = self.root / "no-tools"
        empty.mkdir()
        os.environ["PATH"] = str(empty)
        self.assertFalse(remote.fetch_deliverable(rec))

    # -- reaching a recorded run -----------------------------------------

    def test_a_record_addressed_verb_drives_the_machines_tmux(self):
        """Rebuilt from the substrate name alone, `kill` and `status` drove this
        machine's tmux, where the run's pane id names nothing of its own."""
        rec = self.shell_record()
        substrate = cli.substrate_for(rec)
        self.assertIsInstance(substrate, FakeTmuxSubstrate)
        argv = substrate.command_builder(["tmux", "kill-session", "-t", "s"])
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-2], self.shell.ssh)

    def test_a_local_sweep_leaves_a_run_on_a_machine_alone(self):
        """One sweep sees one substrate on one machine. A pane id from another
        one is a stranger's, and settling it here would journal a live run dead.
        """
        rec = self.shell_record(worker_id="%0", state="running",
                                started_at=0, reserved_at=0)
        sweep = runner.SubstrateSweep(FakeTmuxSubstrate())

        live = caps.live_records(sweep)

        self.assertFalse(sweep.covers(rec))
        self.assertEqual(records.load_record(rec["id"])["state"], "running")
        self.assertNotIn(rec["id"], [found["id"] for found in live])


class TestPowerShellRemote(FakeSshTestCase):
    def setUp(self):
        super().setUp()
        self.write_config('shell = "powershell"\n')
        self.machine = remote.machine_for(self.machine_name)

    def test_argv_and_environment_preserve_spaces_quotes_and_home_expansion(self):
        command = remote.remote_command(
            ["C:/Program Files/dispatch.exe", "run", "~/brief's [1].md", "$literal"],
            {"DISPATCH_HOME": "~/.dispatch", "DISPATCH_SESSION": "seat's $id"},
            self.machine.shell)
        self.assertEqual(command,
                         "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                         "$env:PYTHONIOENCODING = 'utf-8'; "
                         "$ErrorActionPreference = 'Stop'; "
                         "$env:DISPATCH_HOME = ($HOME + '/.dispatch'); "
                         "$env:DISPATCH_SESSION = 'seat''s $id'; "
                         "& 'C:/Program Files/dispatch.exe' 'run' "
                         "($HOME + '/brief''s [1].md') '$literal'")

    def test_launch_records_the_shell_for_later_commands(self):
        run_id, remote_dir = self.launched()
        self.set_replies([["& 'dispatch' 'run'", {
            "stdout": f"{run_id}\n{remote_dir}\n"}]])
        rec = self.run_remote_bg()
        self.assertEqual(rec["state"], "running")
        self.assertEqual(remote.machine_of_record(rec).shell, "powershell")
        self.assertIn("[System.IO.Directory]::CreateDirectory", self.commands()[0])
        launch = self.commands()[-1]
        self.assertIn("$env:AGENT_DEPTH = '0'", launch)
        self.assertIn("$env:DISPATCH_SESSION = 'test-session'", launch)
        self.assertIn("($HOME + '/.dispatch/remote/", launch)

    def test_poll_reads_the_literal_windows_path_after_reconciling(self):
        rec = {"remote_dir": "C:/Users/O'Brien/.dispatch/runs/a[1]",
               "session": "test-session"}
        command = remote.poll_command(rec, self.machine)
        self.assertIn("& 'dispatch' 'status' >$null 2>&1; ", command)
        self.assertTrue(command.endswith(
            "Get-Content -Raw -Encoding UTF8 -ErrorAction Stop -LiteralPath "
            "'C:/Users/O''Brien/.dispatch/runs/a[1]/run.json'"))

    def test_scp_uses_sftp_drive_paths_without_shell_quotes(self):
        path = r"C:\Users\A Person\.dispatch\runs\a\out.md"
        with patch.object(remote, "run_program") as run:
            remote.scp_down(self.machine, path, self.work / "out.md")
        argv = run.call_args.args[1]
        self.assertEqual(argv[-2],
                         f"{self.machine_ssh}:C:/Users/A Person/.dispatch/runs/a/out.md")


if __name__ == "__main__":
    unittest.main()
