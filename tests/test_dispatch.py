"""End-to-end tests for the verbs, the run lifecycle, and the caps.

Every worker is an interactive CLI in its substrate's own home, so the suite
runs against a stub herdr daemon on a temp endpoint (`herdr_stub.py`) speaking
shapes recorded off a live release. The cap tests are the exception that proves
the rule: a cap that only holds inside one process is not a cap, so those launch
real dispatch subprocesses, which reach the same stub over the same endpoint.

Nothing here ever runs codex, claude, or grok.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from herdr_stub import (CLAUDE_TRUST_SCREEN, CLAUDE_UPDATE_SCREEN,  # noqa: E402
                        CODEX_UPDATE_SCREEN, GROK_UPDATE_SCREEN, METERED_VARS,
                        DISPATCH_ARGV, REPO_ROOT, HerdrStubTestCase, caps, cli,
                        drivers, errors, herdr, lanes, launch_argv, policy,
                        processes, prompt, records, resume_argv, runner)


# --------------------------------------------------------------------------
# Lane table and argv expansion
# --------------------------------------------------------------------------


class TestLanes(HerdrStubTestCase):
    def test_the_default_lane_table_is_the_shipped_set(self):
        """Config replaces this wholesale; a fresh install starts here."""
        self.assertEqual(
            lanes.lane_names(),
            ["luna@high", "luna@high:default", "luna@high:priority",
             "luna@xhigh", "luna@xhigh:default", "luna@xhigh:priority",
             "luna@max", "luna@max:default", "luna@max:priority",
             "sol@medium", "sol@medium:default", "sol@medium:priority",
             "sol@high", "sol@high:default", "sol@high:priority",
             "sol@xhigh", "sol@xhigh:default", "sol@xhigh:priority",
             "sol@max", "sol@max:default", "sol@max:priority",
             "astra@medium", "astra@medium:default", "astra@medium:priority",
             "astra@high", "astra@high:default", "astra@high:priority",
             "astra@xhigh", "astra@xhigh:default", "astra@xhigh:priority",
             "opus@medium", "opus@high", "grok@high"])

    def test_config_replaces_the_lane_table_wholesale(self):
        previous = lanes.set_lane_table(
            {"mine": lanes.LaneSpec("codex", "some-model", ("high",))})
        self.addCleanup(lanes.set_lane_table, previous)
        self.assertEqual(lanes.lane_names(), ["mine@high"])
        self.assertEqual(lanes.resolve_lane("mine@high").model, "some-model")
        with self.assertRaises(errors.DispatchError):
            lanes.resolve_lane("sol@medium")

    def test_the_grok_lane_names_the_model_the_preset_ships(self):
        self.assertEqual(lanes.resolve_lane("grok@high").model, "grok-4.6")

    def test_unknown_lane_is_a_hard_error_listing_valid_lanes(self):
        with self.assertRaises(errors.DispatchError) as caught:
            lanes.resolve_lane("sonnet@high")
        self.assertIn("valid lanes:", str(caught.exception))

    def test_no_fuzzy_matching_on_effort(self):
        for bad in ("sol@med", "luna@medium", "grok@max", "opus@high:priority",
                    "grok@high:fast", "sol"):
            with self.assertRaises(errors.DispatchError, msg=bad):
                lanes.resolve_lane(bad)

    def test_a_tier_alias_resolves_without_being_advertised(self):
        """`fast` is an accepted spelling of `priority`, not a third tier."""
        for name in ("sol@medium:fast", "luna@high:fast"):
            self.assertEqual(lanes.resolve_lane(name).tier, "priority", name)
        for name in ("sol@medium:priority", "luna@high:priority"):
            self.assertEqual(lanes.resolve_lane(name).tier, "priority", name)
        self.assertEqual(lanes.resolve_lane("sol@medium").tier, "default")
        self.assertEqual(lanes.resolve_lane("sol@medium:default").tier, "default")
        # Accepted, not advertised: one canonical name in the lane list.
        self.assertNotIn("sol@medium:fast", lanes.lane_names())

    def test_every_lane_expands_and_never_passes_a_dangerous_flag(self):
        for name in lanes.lane_names():
            for _, argv in cli.lane_expansions(name):
                self.assertNotIn("--ephemeral", argv, name)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
                self.assertNotIn("--dangerously-skip-permissions", argv)
                self.assertNotIn("bypassPermissions", argv)
                self.assertNotIn("--always-approve", argv)

    def test_no_interactive_lane_types_a_headless_subcommand(self):
        """Passing a headless-only flag to the interactive binary is an error,
        not a no-op: they belong to the one-shot subcommand alone."""
        for name in lanes.lane_names():
            for label, argv in cli.lane_expansions(name):
                if label == "headless":
                    continue
                self.assertNotIn("exec", argv, name)
                self.assertNotIn("-p", argv, name)
                self.assertNotIn("--skip-git-repo-check", argv, name)
                self.assertNotIn("-o", argv, name)
                self.assertNotIn("--output-schema", argv, name)
                self.assertNotIn("--output-format", argv, name)

    def test_the_headless_form_is_the_one_shot_subcommand(self):
        """The headless substrate has no TUI to type into, so the prompt is an
        argv element and the one-shot flags are the right ones."""
        opts = records.RunOptions(dir="/w")
        codex = drivers.get_driver("codex").headless_argv(
            lanes.resolve_lane("sol@medium"), opts, "read /p.txt", "/o.md")
        self.assertEqual(codex[:3], ["codex", "exec", "--skip-git-repo-check"])
        self.assertEqual(codex[-1], "read /p.txt")
        self.assertEqual(codex[codex.index("-o") + 1], "/o.md")
        claude = drivers.get_driver("claude").headless_argv(
            lanes.resolve_lane("opus@high"), opts, "read /p.txt")
        self.assertEqual(claude[:3], ["claude", "-p", "read /p.txt"])
        grok = drivers.get_driver("grok").headless_argv(
            lanes.resolve_lane("grok@high"), opts, "read /p.txt")
        self.assertEqual(grok[:3], ["grok", "--output-format", "plain"])
        self.assertEqual(grok[-2:], ["--single", "read /p.txt"])

    def test_headless_codex_write_sets_approvals_by_config_override(self):
        """`codex exec` has no `-a`: it exits 2 on the flag, fresh or resumed."""
        opts = records.RunOptions(dir="/w", write=True)
        lane = lanes.resolve_lane("sol@medium")
        session = "00000000-0000-4000-8000-000000000001"
        for argv in (
                drivers.get_driver("codex").headless_argv(lane, opts, "p"),
                drivers.get_driver("codex").headless_argv(
                    lane, opts, "p", resume_session=session)):
            self.assertNotIn("-a", argv)
            self.assertEqual(argv[argv.index("approval_policy=on-request") - 1], "-c")

    def test_sol_read_only_argv_matches_known_good(self):
        lane = lanes.resolve_lane("sol@medium")
        self.assertEqual(
            launch_argv(lane, records.RunOptions(dir="/w")),
            ["codex", "-m", "gpt-5.6-sol",
             "-c", "model_reasoning_effort=medium",
             "-c", "service_tier=default",
             "-a", "on-request",
             "-c", "approvals_reviewer=guardian_subagent",
             "-s", "read-only", "-C", "/w"])

    def test_sol_write_argv_routes_approvals_to_the_guardian(self):
        lane = lanes.resolve_lane("sol@medium")
        self.assertEqual(
            launch_argv(lane, records.RunOptions(dir="/w", write=True)),
            ["codex", "-m", "gpt-5.6-sol",
             "-c", "model_reasoning_effort=medium",
             "-c", "service_tier=default",
             "-a", "on-request",
             "-c", "approvals_reviewer=guardian_subagent",
             "-c", "sandbox_workspace_write.network_access=false",
             "-s", "workspace-write", "-C", "/w"])

    def test_luna_runs_its_standing_tier_by_default(self):
        luna = launch_argv(lanes.resolve_lane("luna@high"),
                           records.RunOptions(dir="/w"))
        self.assertIn("service_tier=priority", luna)
        self.assertEqual(luna[luna.index("-m") + 1], "gpt-5.6-luna")
        self.assertIn("model_reasoning_effort=high", luna)

    def test_a_tier_suffix_changes_only_the_tier(self):
        default = launch_argv(
            lanes.resolve_lane("luna@high:default"), records.RunOptions(dir="/w"))
        priority = launch_argv(
            lanes.resolve_lane("luna@high"), records.RunOptions(dir="/w"))
        self.assertEqual([a.replace("service_tier=default", "service_tier=priority")
                          for a in default], priority)

    def test_net_only_reaches_codex_inside_the_write_sandbox(self):
        argv = launch_argv(
            lanes.resolve_lane("sol@high"),
            records.RunOptions(dir="/w", write=True, net=True))
        self.assertIn("sandbox_workspace_write.network_access=true", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")

    def test_claude_read_lane_pins_auto_mode_and_a_tool_allowlist(self):
        argv = launch_argv(lanes.resolve_lane("opus@high"),
                                        records.RunOptions(dir="/w"),
                                        session_id="00000000-1111-2222-3333-444444444444")
        self.assertEqual(argv, [
            "claude",
            "--model", "claude-opus-5",
            "--effort", "high",
            "--permission-mode", "auto",
            "--allowedTools", "Read,Grep,Glob",
            "--session-id", "00000000-1111-2222-3333-444444444444",
            "--add-dir", "/w"])

    def test_claude_write_lane_drops_the_allowlist_keeps_auto(self):
        argv = launch_argv(lanes.resolve_lane("opus@medium"),
                                        records.RunOptions(dir="/w", write=True))
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")

    def test_grok_argv_matches_known_good_with_the_turn_cap(self):
        argv = launch_argv(lanes.resolve_lane("grok@high"),
                                        records.RunOptions(dir="/w"),
                                        session_id="00000000-1111-2222-3333-444444444444")
        self.assertEqual(argv, [
            "grok", "-m", "grok-4.6",
            "--reasoning-effort", "high",
            "--permission-mode", "auto",
            "--no-subagents",
            "--max-turns", "12",
            "--session-id", "00000000-1111-2222-3333-444444444444",
            "--cwd", "/w"])

    def test_resume_argv_reopens_the_native_session_per_backend(self):
        """Each form was read off the CLI's own --help, not from herdr's table."""
        opts = records.RunOptions(dir="/w")
        session = "00000000-1111-2222-3333-444444444444"
        codex = resume_argv(lanes.resolve_lane("sol@high"),
                                                opts, session)
        self.assertEqual(codex[:3], ["codex", "resume", session])
        self.assertIn("-C", codex)
        claude = resume_argv(lanes.resolve_lane("opus@high"),
                                                 opts, session)
        self.assertEqual(claude[:3], ["claude", "--resume", session])
        grok = resume_argv(lanes.resolve_lane("grok@high"),
                                               opts, session)
        self.assertEqual(grok[:3], ["grok", "--resume", session])

    def test_only_codex_can_resume_without_a_session_id(self):
        """codex is never told its id, so `--last` is its only handle.

        claude and grok are handed an id at spawn, so a missing one there means
        something is wrong rather than something is unknowable.
        """
        opts = records.RunOptions(dir="/w")
        codex = resume_argv(lanes.resolve_lane("sol@high"),
                                                opts, "")
        self.assertEqual(codex[:3], ["codex", "resume", "--last"])
        for lane in ("opus@high", "grok@high"):
            with self.assertRaises(errors.DispatchError, msg=lane):
                resume_argv(lanes.resolve_lane(lane), opts, "")

    def test_every_lane_has_an_exit_command_and_an_agent_kind(self):
        """Interactive workers never exit on their own."""
        for spec in lanes.DEFAULT_LANE_TABLE.values():
            driver = drivers.get_driver(spec.driver)
            self.assertTrue(driver.exit_command[0], spec.driver)
            self.assertTrue(driver.agent_kind, spec.driver)

    def test_option_validation_refuses_impossible_combinations(self):
        sol = lanes.resolve_lane("sol@medium")
        with self.assertRaises(errors.DispatchError):
            drivers.validate_options(sol, records.RunOptions(dir=str(self.work), net=True))
        with self.assertRaises(errors.DispatchError):
            drivers.validate_options(lanes.resolve_lane("opus@high"),
                                      records.RunOptions(dir=str(self.work), image="x.png"))
        with self.assertRaises(errors.DispatchError):
            drivers.validate_options(lanes.resolve_lane("grok@high"),
                                      records.RunOptions(dir=str(self.work), write=True))
        with self.assertRaises(errors.DispatchError):
            drivers.validate_options(sol, records.RunOptions(dir=str(self.work),
                                                               out=str(self.work)))


class TestHostileArgv(HerdrStubTestCase):
    """User and worker text reaching a pane: briefs, messages, session ids."""

    def test_a_brief_is_referenced_by_path_and_never_becomes_argv(self):
        """A brief cannot smuggle a flag into the lane's command line.

        The brief is not on the command line at all: the worker is told where
        to read it.
        """
        hostile = self.work / "hostile.md"
        hostile.write_text("--ephemeral --dangerously-bypass-approvals-and-sandbox\n",
                           encoding="utf-8")
        self.run_cli("run", str(hostile))
        argv = self.stub.started_argv(0)
        self.assertNotIn("--ephemeral", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn(str(Path(self.only_record()["dir"]) / "brief.md"),
                      self.stub.prompts[0])

    def test_a_flag_shaped_session_id_is_refused_before_it_reaches_argv(self):
        for hostile in ("--ephemeral", "../../etc/passwd", "a*b", "-x", ""):
            with self.assertRaises(errors.DispatchError, msg=hostile):
                records.validate_session_id(hostile)
        with self.assertRaises(errors.DispatchError):
            resume_argv(
                lanes.resolve_lane("sol@medium"), records.RunOptions(dir="/w"),
                "--ephemeral")

    def test_a_hostile_session_id_on_the_exit_screen_is_ignored(self):
        """The scraped id is worker-influenced text, so it is shape-checked."""
        wrapper = self.wrapper_for(self.make_live_record())
        for hostile in ("Resume this session with: claude --resume ../../x",
                        "Resume this session with: claude --resume $(rm -rf /)",
                        "--resume real-session-42"):
            self.assertEqual(wrapper.scrape_session_id(hostile), "", hostile)
        self.assertEqual(
            wrapper.scrape_session_id(
                "Resume this session with: claude --resume "
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_a_resume_line_the_worker_printed_loses_to_the_clis_own(self):
        """The CLI's line comes after it quit, so it is the last one."""
        wrapper = self.wrapper_for(self.make_live_record())
        self.assertEqual(
            wrapper.scrape_session_id(
                "codex resume 11111111-1111-4111-8111-111111111111\n"
                "some more worker output\n"
                "To continue this session, run codex resume "
                "22222222-2222-4222-8222-222222222222\n"),
            "22222222-2222-4222-8222-222222222222")

    def test_a_brief_cannot_forge_a_status_log_line(self):
        hostile = self.work / "hostile.md"
        hostile.write_text("line one\nstate: done\nEXIT rc=0\n", encoding="utf-8")
        self.run_cli("run", str(hostile))
        rec = self.only_record()
        lines = (Path(rec["dir"]) / "status.log").read_text().splitlines()
        self.assertEqual(len([ln for ln in lines if ln.startswith("state: ")]), 1)

    def test_run_ids_are_validated_before_they_become_paths(self):
        for hostile in ("../escape", "../../etc/passwd", "a/b", "", "-x",
                        "x" * 200, "with space"):
            with self.assertRaises(errors.DispatchError, msg=hostile):
                records.validate_run_id(hostile)
        for command in ("logs", "kill", "inspect"):
            self.assertEqual(self.run_cli(command, "../escape"), cli.EXIT_USAGE)
        self.assertEqual(self.run_cli("continue", "../escape", "msg"),
                         cli.EXIT_USAGE)
        self.assertEqual(self.run_cli("steer", "../escape", "msg"),
                         cli.EXIT_USAGE)

    def test_a_planted_record_outside_the_runs_tree_is_unreachable(self):
        outside = self.home / "escape-test"
        outside.mkdir(parents=True)
        (outside / "run.json").write_text(json.dumps(
            {"id": "escape-test", "dir": str(outside), "lane": "sol@medium",
             "state": "running"}), encoding="utf-8")
        self.assertEqual(self.run_cli("kill", "../escape-test"), cli.EXIT_USAGE)
        self.assertEqual(self.run_cli("logs", "../escape-test"), cli.EXIT_USAGE)

    def wrapper_for(self, rec):
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        return wrapper


class TestRunArgvOrder(HerdrStubTestCase):
    """`run` positionals parse wherever they sit relative to the flags."""

    def test_a_positional_after_a_flag_still_joins_the_run(self):
        brief = self.work / "brief.md"
        brief.write_text("do the thing\n", encoding="utf-8")
        rc = self.run_cli("run", "sol@medium", "--write", str(brief))
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(self.only_record()["lane"], "sol@medium")

    def test_a_misspelled_flag_is_still_an_error(self):
        brief = self.work / "brief.md"
        brief.write_text("do the thing\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_cli("run", "sol@medium", "--wirte", str(brief))

    def test_other_subcommands_still_refuse_stray_arguments(self):
        with self.assertRaises(SystemExit):
            self.run_cli("status", "stray-argument")


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


class TestDefaults(HerdrStubTestCase):
    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_the_run_store_is_owner_only_even_if_it_was_created_open(self):
        root = records.runs_root()
        root.chmod(0o755)
        self.assertEqual(records.runs_root().stat().st_mode & 0o777, 0o700)

    def test_bare_run_is_astra_medium_read_only_cwd_foreground(self):
        code = self.run_cli("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["lane"], "astra@medium")
        self.assertFalse(rec["write"])
        self.assertFalse(rec["background"])
        # Foreground runs carry the default check-in deadline too, so a worker
        # that ends its turn without a deliverable cannot hold the terminal
        # forever (d799363).
        self.assertEqual(rec["deadline_seconds"],
                         policy.parse_deadline(policy.policy().default_deadline))
        self.assertEqual(rec["cwd"], str(self.work))
        argv = self.stub.started_argv(0)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertEqual(argv[argv.index("-C") + 1], str(self.work))

    def test_the_pane_opens_in_the_runs_own_directory(self):
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.stub.workspaces[0]["cwd"], str(self.work))
        self.assertTrue(self.stub.workspaces[0]["label"].startswith("dispatch-"))

    def test_lane_positional_is_recognized_by_its_shape(self):
        self.assertEqual(cli.split_lane_and_brief(["luna@max", "b.md"]),
                         ("luna@max", "b.md"))
        self.assertEqual(cli.split_lane_and_brief(["b.md"]),
                         (lanes.DEFAULT_LANE, "b.md"))
        with self.assertRaises(errors.DispatchError):
            cli.split_lane_and_brief([])
        with self.assertRaises(errors.DispatchError):
            cli.split_lane_and_brief(["a.md", "b.md"])

    def test_a_brief_path_containing_an_at_sign_is_not_read_as_a_lane(self):
        odd = self.work / "reports@v2"
        odd.mkdir()
        brief = odd / "brief.md"
        brief.write_text("audit\n", encoding="utf-8")
        self.assertEqual(cli.split_lane_and_brief([str(brief)]),
                         (lanes.DEFAULT_LANE, str(brief)))
        self.assertEqual(self.run_cli("run", str(brief)), cli.EXIT_OK)

    def test_a_lane_shaped_string_that_is_not_a_lane_still_hard_errors(self):
        self.assertEqual(self.run_cli("run", "sonnet@high", str(self.brief)),
                         cli.EXIT_USAGE)
        self.assertEqual(self.stub.starts, [])

    def test_missing_brief_errors_usefully(self):
        code = self.run_cli("run", str(self.work / "nope.md"))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(records.all_records(), [])

    def test_deadline_parsing(self):
        self.assertEqual(policy.parse_deadline("30m"), 1800)
        self.assertEqual(policy.parse_deadline("45s"), 45)
        self.assertEqual(policy.parse_deadline("2h"), 7200)
        self.assertEqual(policy.parse_deadline("90"), 90)
        with self.assertRaises(errors.DispatchError):
            policy.parse_deadline("half an hour")


# --------------------------------------------------------------------------
# Spawning into a pane
# --------------------------------------------------------------------------


class TestPaneSpawn(HerdrStubTestCase):
    def test_a_swallowed_codex_prompt_retries_then_fails_loudly(self):
        self.stub.swallow_prompts = 100
        with patch.dict(runner.__dict__, PROMPT_ACCEPT_SECONDS=0.03):
            code, error = self.capture_stderr("run", str(self.brief), "--bg")
        rec = self.only_record()
        self.assertNotEqual(code, cli.EXIT_OK)
        self.assertEqual(rec["state"], "failed")
        self.assertEqual(len(self.stub.prompts), 3)
        self.assertEqual(rec["turns"], 1)
        self.assertFalse(rec.get("prompted"))
        self.assertFalse(rec.get("watcher_pid"))
        self.assertIn("prompt was never accepted", error)
        self.assertIn("prompt was never accepted",
                      (Path(rec["dir"]) / "status.log").read_text())
        self.assertTrue(self.stub.panes[rec["worker_id"]].closed)

    def test_a_swallowed_codex_prompt_can_start_on_the_second_send(self):
        self.stub.swallow_prompts = 1
        with patch.dict(runner.__dict__, PROMPT_ACCEPT_SECONDS=0.03):
            code = self.run_cli("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(len(self.stub.prompts), 2)
        self.assertEqual(self.only_record()["turns"], 1)

    def test_a_fast_codex_turn_is_not_repeated_after_a_prompt_timeout(self):
        self.stub.errors["agent.prompt"] = {"code": "timeout", "message": "timed out"}
        self.stub.status_when_prompted = "done"
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        self.assertEqual(len(self.stub.prompts), 1)

    def test_old_session_files_do_not_accept_a_swallowed_followup(self):
        rec = self.make_live_record(session_id=self.stub.session_id)
        transcript = Path(rec["dir"]) / "old.jsonl"
        transcript.write_text("old turn\n")
        rec["transcript"] = str(transcript)
        (Path(rec["dir"]) / "out.md").write_text("old answer")
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = rec["agent"]
        pane.running = True
        pane.status = "idle"
        self.stub.swallow_prompts = 100
        wrapper = runner.RunWrapper(self.substrate(), rec)
        wrapper.attach()
        with patch.object(runner, "PROMPT_ACCEPT_SECONDS", 0.03):
            with self.assertRaisesRegex(errors.DispatchError, "prompt was never accepted"):
                wrapper.prompt_worker("follow up")
        self.assertEqual(len(self.stub.prompts), 3)
        self.assertFalse(rec["prompted"])

    def test_codex_working_status_does_not_accept_a_swallowed_prompt(self):
        rec = self.make_live_record()
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = rec["agent"]
        pane.running = True
        pane.status = "working"
        self.stub.swallow_prompts = 100
        wrapper = runner.RunWrapper(self.substrate(), rec)
        wrapper.attach()
        with patch.object(runner, "PROMPT_ACCEPT_SECONDS", 0.03):
            with self.assertRaisesRegex(errors.DispatchError, "prompt was never accepted"):
                wrapper.prompt_worker("follow up")
        self.assertEqual(len(self.stub.prompts), 3)
        self.assertFalse(rec["prompted"])
        self.assertFalse(wrapper.prompt_was_delivered("follow up", [None, None]))

    def test_codex_typing_animation_does_not_accept_a_swallowed_prompt(self):
        rec = self.make_live_record()
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = rec["agent"]
        pane.running = True
        pane.status = "working"
        wrapper = runner.RunWrapper(self.substrate(), rec)
        wrapper.attach()
        with patch.object(runner, "PROMPT_ACCEPT_SECONDS", 0.03), \
                patch.object(wrapper, "send_prompt", return_value="send-text") as send:
            with self.assertRaisesRegex(errors.DispatchError, "prompt was never accepted"):
                wrapper.prompt_worker("follow up")
        self.assertEqual(send.call_count, 3)
        self.assertFalse(rec["prompted"])

    def test_the_lanes_flags_reach_herdr_as_agent_start_arguments(self):
        """`agent.start` runs the kind's own command and appends args.

        Passing only the binary would launch a bare `codex` with no model, no
        effort, and no sandbox: the lane would mean nothing.
        """
        self.run_cli("run", "luna@high", str(self.brief))
        start = self.stub.starts[0]
        self.assertEqual(start["kind"], "codex")
        expected = launch_argv(
            lanes.resolve_lane("luna@high"),
            records.RunOptions(dir=str(self.work)))
        self.assertEqual(list(start["args"]), expected[1:])

    def test_the_brief_is_handed_over_by_path_after_the_tui_is_up(self):
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        prompt = self.stub.prompts[0]
        self.assertIn(str(Path(rec["dir"]) / "brief.md"), prompt)
        self.assertIn(str(Path(rec["dir"]) / "out.md"), prompt)
        self.assertIn("exit the session", prompt)
        self.assertEqual((Path(rec["dir"]) / "prompt.txt").read_text(), prompt)

    def test_a_failed_agent_start_falls_back_to_typing_and_flags_it(self):
        self.stub.errors["agent.start"] = {"code": "unsupported_agent_kind",
                                           "message": "no such kind"}
        _, error = self.capture_stderr("run", str(self.brief))
        rec = self.only_record()
        self.assertTrue(rec["spawn_fallback"])
        self.assertIn("HERDR FALLBACK", error)
        self.assertEqual(rec["state"], "done")

    def test_the_codex_lane_bypasses_the_users_shell_function(self):
        """A user's own shell function of the same name can shadow the real
        binary ever started, so anything dispatch types skips the function."""
        self.stub.errors["agent.start"] = {"code": "unsupported_agent_kind",
                                           "message": "no such kind"}
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("run", "sol@medium", str(self.brief))
        typed = [t for t in self.stub.typed() if "codex" in t][0]
        self.assertTrue(typed.startswith("& 'codex' " if processes.IS_WINDOWS
                                         else "command codex "), typed)

    def test_every_lane_is_typed_past_a_shell_function_of_its_name(self):
        """A `claude` or `grok` function in the pane's shell could drop the
        permission flags the lane pins, exactly as a `codex` one could."""
        if processes.IS_WINDOWS:
            # PowerShell has no `command` builtin to prefix with, so both lanes
            # read the same there and only the quoting is the dialect's.
            self.assertEqual(herdr.PANE_SHELL.command_line(drivers.get_driver("claude").shell_prefix, ["claude", "-x"]),
                             "& 'claude' '-x'")
            self.assertEqual(herdr.PANE_SHELL.command_line(drivers.get_driver("codex").shell_prefix, ["codex", "-x"]),
                             "& 'codex' '-x'")
            return
        self.assertEqual(herdr.PANE_SHELL.command_line(drivers.get_driver("claude").shell_prefix, ["claude", "-x"]),
                         "command claude -x")
        self.assertEqual(herdr.PANE_SHELL.command_line(drivers.get_driver("grok").shell_prefix, ["grok", "-x"]),
                         "command grok -x")
        self.assertEqual(herdr.PANE_SHELL.command_line(drivers.get_driver("codex").shell_prefix, ["codex", "-x"]),
                         "command codex -x")

    def test_a_worker_that_never_comes_alive_fails_fast(self):
        """`agent start` succeeding is not proof of life.

        herdr reported idle/interactive_ready for a codex hung before its TUI
        drew, and prompting it stalls. Waiting out a 30-minute deadline for a
        worker that never existed is the expensive way to learn this.
        """
        self.stub.errors["agent.prompt"] = {"code": herdr.STALLED_CODE,
                                            "message": "no state change in 5s"}
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("never came alive", error)
        self.assertFalse(records.load_record(self.only_record()["id"])
                         .get("worker_alive", True))

    def test_a_stalled_worker_leaves_no_pane_and_no_held_slot(self):
        """A failed launch used to strand its workspace with state=running.

        Nothing else knows the pane exists yet, so nothing else will ever close
        it: the slot is held until somebody kills the run by hand.
        """
        self.stub.errors["agent.prompt"] = {"code": herdr.STALLED_CODE,
                                            "message": "no state change in 5s"}
        code, _ = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_USAGE)
        rec = self.only_record()
        self.assertEqual(rec["state"], "failed")
        self.assertIn(("workspace", rec["worker_group"]), self.stub.closed)
        self.assertEqual(caps.live_records(), [])
        self.assertIn("ABANDONED", (Path(rec["dir"]) / "status.log").read_text())

    def test_a_failure_before_the_watch_loop_closes_the_pane_too(self):
        self.stub.errors["pane.send_text"] = {"code": "invalid_request",
                                              "message": "nope"}
        self.stub.errors["agent.start"] = {"code": "unsupported_agent_kind",
                                           "message": "no such kind"}
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("run", str(self.brief))
        rec = self.only_record()
        self.assertIn(rec["state"], policy.policy().terminal_states)
        self.assertIn(("workspace", rec["worker_group"]), self.stub.closed)

    def test_a_launch_that_fails_before_any_worker_exists_is_still_recorded(self):
        """No home to clean up, but a record left `reserved` holds a slot and
        loses the reason."""
        rec = self.make_live_record()
        rec.update(state="reserved", worker_id="")
        records.save_record(rec)
        wrapper = runner.RunWrapper.__new__(runner.RunWrapper)
        wrapper.rec, wrapper.worker = rec, None
        wrapper.dir = Path(rec["dir"])
        wrapper.status_path = wrapper.dir / "status.log"
        wrapper.abandon(errors.DispatchError("the brief would not copy up"))
        settled = records.load_record(rec["id"])
        self.assertEqual(settled["state"], "failed")
        self.assertIn("would not copy up", settled["error"])

    def test_the_fallback_prompt_never_types_the_prompt_body(self):
        """Those keystrokes may be going into a shell, where a newline is a
        command and a schema body is a command line full of substitutions."""
        schema = self.work / "s.json"
        schema.write_text('{"type":"object","title":"$(touch /tmp/nope)"}',
                          encoding="utf-8")
        self.stub.reply = '{"ok": 1}'          # valid, so no repair round follows
        self.stub.errors["agent.start"] = {"code": "unsupported_agent_kind",
                                           "message": "no"}
        self.stub.errors["agent.prompt"] = {"code": "agent_not_found",
                                            "message": "no agent"}
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("run", str(self.brief), "--schema", str(schema))
        rec = self.only_record()
        typed = self.stub.typed()
        self.assertEqual(len([t for t in typed if "prompt.txt" in t]), 1, typed)
        for line in typed:
            self.assertNotIn("\n", line, "a typed line would run as two commands")
            self.assertNotIn("$(touch", line, "the schema body was typed at a shell")
        # The body is still delivered, just as a file the worker reads.
        self.assertIn("$(touch", (Path(rec["dir"]) / "prompt.txt").read_text())
        self.assertEqual(rec["state"], "done")

    def test_the_ladder_marker_survives_the_users_rc_files(self):
        """A zshrc that exports a key or drops AGENT_DEPTH runs after the
        workspace env is applied, and would otherwise win."""
        self.blank_metered_keys()
        self.stub.rc_clobbers_env = True
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        pane = self.stub.panes[self.only_record()["worker_id"]]
        self.assertEqual(pane.env["AGENT_DEPTH"], "1")
        self.assertEqual(pane.env["OPENAI_API_KEY"], "")
        self.assertIn("AGENT_DEPTH=1 verified",
                      (Path(self.only_record()["dir"]) / "status.log").read_text())

    def test_a_key_restored_after_the_export_is_caught_too(self):
        """A precmd hook that re-exports a key wins over the export itself.

        The read-back asks the shell what it actually has, so it costs the same
        probe line to check the keys as the rung.
        """
        self.blank_metered_keys()
        self.stub.env_reports = "1:set"
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("metered key is still set", error)
        self.assertEqual(self.stub.starts, [])

    def test_key_blanking_is_off_until_the_policy_turns_it_on(self):
        """The environment a worker gets is the operator's, by default."""
        os.environ["OPENAI_API_KEY"] = "sk-metered"
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("OPENAI_API_KEY", self.stub.workspaces[0]["env"])
        self.assertNotIn("warning", error)

    def test_a_lane_with_blank_keys_reads_clean(self):
        self.blank_metered_keys()
        self.stub.rc_clobbers_env = True
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        self.assertIn("keys blank",
                      (Path(self.only_record()["dir"]) / "status.log").read_text())

    def test_a_pane_at_the_wrong_rung_never_starts_its_cli(self):
        self.stub.env_reports = "0:"
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("depth ladder", error)
        self.assertEqual(self.stub.starts, [])
        self.assertIn(("workspace", self.only_record()["worker_group"]),
                      self.stub.closed)

    def test_a_lost_export_line_is_reasserted(self):
        """A daemon busy with a same-second launch burst can drop the export
        while landing the probe, so the read-back comes home with
        AGENT_DEPTH=''. The guard re-asserts instead of refusing a healthy
        pane."""
        self.blank_metered_keys()
        self.stub.rc_clobbers_env = True
        self.stub.eats_exports = 1
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        pane = self.stub.panes[rec["worker_id"]]
        self.assertEqual(pane.env["AGENT_DEPTH"], "1")
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertIn("RETRY", log)
        self.assertIn("AGENT_DEPTH=1 verified", log)

    def test_a_pane_gone_at_birth_gets_a_fresh_workspace(self):
        """Same-second creates race, and a pane can be gone before its first
        send_keys. That is a birth failure, not a refusal: recreate and carry
        on."""
        self.stub.vanish_next_creates = 1
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(len(self.stub.workspaces), 2)
        self.assertEqual(rec["state"], "done")
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertIn("RETRY", log)
        self.assertIn("recreating it", log)

    def test_a_pane_that_keeps_vanishing_fails_after_the_cap(self):
        self.stub.vanish_next_creates = 99
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(self.stub.starts, [])
        self.assertEqual(len(self.stub.workspaces),
                         runner.WORKER_SETUP_ATTEMPTS)

    def forged_record(self, rec, **fields):
        """Rewrite a run's record on disk, the way a stale daemon-cycle
        leftover would read: same file, forged pane identity."""
        record_path = Path(rec["dir"]) / "run.json"
        stored = json.loads(record_path.read_text())
        stored.update(fields)
        record_path.write_text(json.dumps(stored))
        return stored

    def stranger_pane(self):
        """A live pane no record owns, standing in for another run's worker
        holding a recycled pane id after a daemon restart."""
        driver = herdr.HerdrSubstrate()
        pane = driver.open(label="stranger")
        return pane, self.stub.panes[pane.id].shell_pid

    def test_a_recycled_pane_id_is_not_swept(self):
        """Pane ids restart with the daemon; a done run's leftover id can name
        a stranger's live pane. The sweep must not close it."""
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        pane, shell_pid = self.stranger_pane()
        self.forged_record(rec, worker_id=pane.id,
                           worker_group=pane.group,
                           shell_pid=shell_pid + 1)
        self.run_cli("status")
        self.assertNotIn(("pane", pane.id), self.stub.closed)
        self.assertNotIn("PANE-SWEPT",
                         (Path(rec["dir"]) / "status.log").read_text())

    def test_a_finished_runs_own_leftover_pane_is_still_swept(self):
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        pane, shell_pid = self.stranger_pane()
        self.forged_record(rec, worker_id=pane.id,
                           worker_group=pane.group,
                           shell_pid=shell_pid)
        self.run_cli("status")
        self.assertIn(("pane", pane.id), self.stub.closed)
        self.assertIn("WORKER-SWEPT",
                      (Path(rec["dir"]) / "status.log").read_text())

    def test_reconcile_never_adopts_a_strangers_pane(self):
        """A stale running record whose id now names someone else's worker:
        adopting it would poll, and eventually /quit, that worker."""
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        pane, shell_pid = self.stranger_pane()
        typed_before = len(self.stub.typed())
        self.forged_record(rec, state="running", finished="",
                           worker_id=pane.id,
                           worker_group=pane.group,
                           shell_pid=shell_pid + 1,
                           watcher_started_at=0)
        self.run_cli("status")
        stored = json.loads((Path(rec["dir"]) / "run.json").read_text())
        self.assertEqual(stored["state"], "orphaned")
        self.assertNotIn(("pane", pane.id), self.stub.closed)
        self.assertEqual(len(self.stub.typed()), typed_before,
                         "nothing may be typed into a stranger's pane")

    def test_the_metered_key_guard_reaches_into_the_pane(self):
        """The pane's shell inherits the daemon's environment, not ours.

        Checking our own environment proves nothing about what the worker will
        see, so the lane's key vars are blanked in the workspace itself. Opt-in,
        because it is the right default for a subscription-billed lane and the
        wrong one for anybody deliberately running on an API key.
        """
        self.blank_metered_keys()
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.stub.workspaces[0]["env"]["OPENAI_API_KEY"], "")
        self.run_cli("run", "opus@high", str(self.brief))
        env = self.stub.workspaces[1]["env"]
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            self.assertEqual(env[var], "")
        # A subscription's own token, so blanking it would log the lane out.
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)


class TestCompletion(HerdrStubTestCase):
    """How a run ends: the driver closes the session, then reads the rc."""

    def test_the_driver_ends_the_session_once_the_turn_is_done(self):
        """Interactive workers sit at their prompt forever otherwise."""
        self.run_cli("run", "opus@high", str(self.brief))
        rec = self.only_record()
        self.assertEqual(rec["exit_command"], "/exit")
        self.assertIn("/exit", self.stub.typed())
        self.assertIn("EXIT-COMMAND", (Path(rec["dir"]) / "status.log").read_text())

    def test_codex_gets_its_own_exit_command_and_a_second_enter(self):
        """The first enter is eaten by the slash-command popup, so one is not
        enough: the command sits typed and unsent and the pane never returns."""
        self.assertEqual(drivers.get_driver("codex").exit_command, ("/quit", 2))
        self.run_cli("run", "sol@medium", str(self.brief))
        rec = self.only_record()
        self.assertEqual(rec["exit_command"], "/quit")
        self.assertIn("2 enters", (Path(rec["dir"]) / "status.log").read_text())

    def test_codex_completion_never_trusts_the_done_state(self):
        """codex reports `done` at its bare idle prompt, before any task runs."""
        self.assertEqual(drivers.get_driver("codex").turn_signal, "deliverable")
        self.assertEqual(drivers.get_driver("claude").turn_signal, "agent-done")

    def test_a_codex_worker_is_judged_on_its_deliverable(self):
        pane_before = len(self.stub.panes)
        self.run_cli("run", "sol@medium", str(self.brief))
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        self.assertEqual((Path(rec["dir"]) / "out.md").read_text(), "final message")
        self.assertGreater(len(self.stub.panes), pane_before)

    def test_a_deliverable_still_being_written_is_not_the_turn_signal(self):
        """A worker that writes an outline and keeps researching used to be sent
        its exit command mid-task, with the outline journaled as the answer."""
        rec = self.make_live_record(backend="codex", lane="sol@medium")
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        out = Path(rec["dir"]) / "out.md"
        out.write_text("outline so far", encoding="utf-8")
        self.assertFalse(wrapper.deliverable_is_settled(), "counted a first sight")
        out.write_text("outline so far, plus more", encoding="utf-8")
        self.assertFalse(wrapper.deliverable_is_settled(), "counted a changed file")
        time.sleep(runner.DELIVERABLE_QUIET_SECONDS + 0.05)
        self.assertTrue(wrapper.deliverable_is_settled(), "never settled")

    def test_successive_reconcile_sweeps_accumulate_the_quiet_window(self):
        """A sweep builds a fresh wrapper and looks exactly once.

        With the sighting held in memory, every `dispatch status` was a first
        sight, so a finished codex run whose watcher had died never reached its
        exit command and sat on its pane and its cap slot until the deadline.
        """
        rec = self.unwatched_bg_run(lane="sol@medium")
        # A settled codex reports done at its prompt box; the deliverable is
        # what says the turn is over, and the status is what says it stayed.
        self.stub.finish_turn(rec["worker_id"])
        (Path(rec["dir"]) / "out.md").write_text("the answer", encoding="utf-8")
        first = runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        self.assertEqual(first["state"], "running")
        self.assertNotIn("/quit", self.stub.typed())
        self.assertTrue(records.load_record(rec["id"])["deliverable_seen"])
        time.sleep(runner.DELIVERABLE_QUIET_SECONDS + 0.05)
        runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        self.assertIn("/quit", self.stub.typed())
        self.assertEqual(records.load_record(rec["id"])["state"], "done")

    def test_a_deliverable_that_changes_between_sweeps_restarts_the_window(self):
        rec = self.unwatched_bg_run(lane="sol@medium")
        self.stub.finish_turn(rec["worker_id"])
        out = Path(rec["dir"]) / "out.md"
        out.write_text("outline", encoding="utf-8")
        runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        time.sleep(runner.DELIVERABLE_QUIET_SECONDS + 0.05)
        out.write_text("outline plus more", encoding="utf-8")
        runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        self.assertNotIn("/quit", self.stub.typed())
        self.assertEqual(records.load_record(rec["id"])["state"], "running")

    def test_an_empty_deliverable_never_settles(self):
        rec = self.make_live_record(backend="codex", lane="sol@medium")
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        (Path(rec["dir"]) / "out.md").write_text("", encoding="utf-8")
        for _ in range(3):
            self.assertFalse(wrapper.deliverable_is_settled())
            time.sleep(runner.DELIVERABLE_QUIET_SECONDS + 0.02)

    def test_the_exit_command_waits_for_done_and_not_for_idle(self):
        """herdr reports `done` on turn completion; `idle` never arrives."""
        self.assertEqual(runner.DONE_STATES, ("done",))
        self.stub.worker_polls = 3
        self.run_cli("run", "opus@high", str(self.brief))
        typed = self.stub.typed()
        self.assertIn("/exit", typed)
        self.assertLess(typed.index("/exit"),
                        [i for i, t in enumerate(typed)
                         if herdr.PANE_SHELL.parse_line(t)[0] == "rc-probe"][0])

    def test_a_worker_that_does_exit_on_its_own_still_completes(self):
        """The injected instruction stays as a bonus path, never as the plan."""
        self.stub.self_exits = True
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["rc"], 0)

    def test_the_real_exit_code_is_journaled(self):
        self.stub.rc = 3
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_FAILED)
        rec = self.only_record()
        self.assertEqual(rec["rc"], 3)
        self.assertEqual(rec["state"], "failed")

    def test_a_worker_that_takes_its_shell_down_journals_a_null_rc(self):
        """There is no prompt left to read `$?` from; that is the honest answer."""
        rec = self.make_live_record()
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        self.stub.panes[rec["worker_id"]].closed = True
        finished = wrapper.watch(poll_seconds=0.01)
        self.assertIsNone(finished["rc"])
        self.assertEqual(finished["state"], "failed")

    def test_the_native_session_id_is_scraped_off_the_exit_screen(self):
        """Neither `agent get` nor `api snapshot` carries it.

        A codex lane, because that is the case the scrape exists for: codex names
        its own session, where claude and grok are handed one.
        """
        self.stub.session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.run_cli("run", "sol@medium", str(self.brief))
        rec = self.only_record()
        self.assertEqual(rec["session_id"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertIn("(from screen)", (Path(rec["dir"]) / "status.log").read_text())

    def test_the_screen_is_read_before_the_workspace_is_closed(self):
        """herdr releases the agent record on exit, so the screen is the only
        place the native session id still exists at that moment."""
        self.stub.session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        self.assertIn("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                      (Path(rec["dir"]) / "screen.log").read_text())

    def test_a_codex_resume_line_is_scraped_too(self):
        """codex prints `codex resume <id>`, not the `--resume` shape."""
        self.stub.session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.run_cli("run", "sol@medium", str(self.brief))
        self.assertEqual(self.only_record()["session_id"],
                         "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_panes_close_on_done(self):
        """Homes close when a run ends: sixteen orphaned CLIs is a resource hog."""
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        self.assertIn(("pane", rec["worker_id"]), self.stub.closed)
        self.assertIn(("workspace", rec["worker_group"]), self.stub.closed)


class TestForegroundRunsHaveCheckins(HerdrStubTestCase):
    """A foreground run without --deadline still gets the check-in ladder.

    Since a turn end without a deliverable waits instead of failing, a
    foreground run with no deadline would otherwise hold the terminal forever
    on a worker that never writes.
    """

    def test_a_foreground_run_records_the_default_deadline(self):
        brief = self.work / "brief.md"
        brief.write_text("do the thing\n", encoding="utf-8")
        self.run_cli("run", str(brief))
        rec = self.only_record()
        self.assertEqual(rec.get("deadline"), policy.policy().default_deadline)


class TestATurnEndIsNotARunEnd(HerdrStubTestCase):
    """A run ends when a turn ends AND the deliverable is on disk.

    A human steers live workers natively, so turn 1 ending is
    routinely the start of turn 2 rather than the end of the run
   . Every case here drives `reconcile_run`
    a look at a time, because one look is exactly what the rule is about.
    """

    def idle_worker(self, lane="opus@high", wrote=True):
        """A launched run and a wrapper to look at it one poll at a time."""
        self.stub.write_output = wrote
        rec = self.unwatched_bg_run(lane)
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(),
                                           records.load_record(rec["id"]))
        wrapper.attach()
        return wrapper, self.stub.panes[rec["worker_id"]]

    def set_status(self, pane, status):
        pane.turn_polls = 0
        pane.status = status

    def look(self, wrapper, deadline_seconds=None):
        """Exactly one look at the pane, which is what the rule is about."""
        outcome = wrapper.poll(deadline_seconds)
        if outcome is not None:
            wrapper.finish(outcome[0], outcome[1])
        return wrapper.rec

    def log_of(self, rec):
        return (Path(rec["dir"]) / "status.log").read_text()

    def test_a_one_look_done_that_goes_working_again_is_a_queued_turn(self):
        """The gap between turn 1 ending and the human's queued message landing.

        herdr surfaces a `done` in it. Exiting or nudging into that gap is what
        killed a steered run.
        """
        wrapper, pane = self.idle_worker()
        prompted = wrapper.rec["prompted"]
        prompt_seq = wrapper.rec["prompt_state_seq"]
        self.set_status(pane, "done")
        self.look(wrapper)                              # one look: not an ending
        self.set_status(pane, "working")                # a queued message lands
        self.look(wrapper)                              # one flap is not a turn
        after = self.look(wrapper)                      # it holds: a real turn
        self.assertEqual(after["state"], "running")
        self.assertNotIn("/exit", self.stub.typed())
        log = self.log_of(after)
        self.assertNotIn("NUDGE", log)
        self.assertIn("started (not by dispatch)", log)
        self.assertEqual(after["turns"], 2)
        # The stamps that belong to the brief survive a turn dispatch did not
        # start: nothing about the record desyncs.
        self.assertEqual(after["prompted"], prompted)
        self.assertEqual(after["prompt_state_seq"], prompt_seq)

        self.set_status(pane, "done")                   # the queued turn ends
        self.look(wrapper)                              # first ready look
        self.look(wrapper)                              # second: the exit goes in
        settled = self.look(wrapper)                    # the shell comes back
        self.assertEqual(settled["state"], "done")
        self.assertEqual(self.stub.typed().count("/exit"), 1)

    def test_a_first_turn_with_no_deliverable_is_nudged_exactly_once(self):
        wrapper, pane = self.idle_worker(wrote=False)
        self.set_status(pane, "done")
        self.look(wrapper)
        nudged = self.look(wrapper)
        log = self.log_of(nudged)
        self.assertIn("TURN-ENDED", log)
        self.assertEqual(log.count("NUDGE"), 1)
        self.assertTrue(nudged["nudged"])
        self.assertEqual(nudged["state"], "running")
        self.assertNotIn("/exit", self.stub.typed())
        self.assertIn("Your turn ended without writing", self.stub.prompts[-1])
        # A worker whose first prompt was lost at startup has no brief to go on.
        self.assertIn("brief.md", self.stub.prompts[-1])

        # Turn 2 answers the nudge.
        self.set_status(pane, "working")
        self.look(wrapper)
        (Path(nudged["dir"]) / "out.md").write_text("the answer", encoding="utf-8")
        self.set_status(pane, "done")
        self.look(wrapper)
        self.look(wrapper)
        settled = self.look(wrapper)
        self.assertEqual(settled["state"], "done")
        self.assertEqual(self.stub.typed().count("/exit"), 1)
        self.assertEqual(self.log_of(settled).count("NUDGE"), 1)

    def test_a_worker_waiting_on_a_backgrounded_command_finishes_its_work(self):
        """the /exit used to take its children too."""
        wrapper, pane = self.idle_worker(wrote=False)
        self.set_status(pane, "done")
        self.look(wrapper)
        self.look(wrapper)                              # TURN-ENDED and the nudge
        self.set_status(pane, "idle")                   # still waiting on `sleep`
        for _ in range(3):
            still = self.look(wrapper)
            self.assertEqual(still["state"], "running")
        self.assertNotIn("/exit", self.stub.typed())
        self.assertEqual(self.log_of(wrapper.rec).count("NUDGE"), 1)

        (Path(wrapper.rec["dir"]) / "out.md").write_text("finished", encoding="utf-8")
        self.set_status(pane, "working")
        self.look(wrapper)
        self.look(wrapper)                              # working, and it holds
        self.set_status(pane, "done")
        self.look(wrapper)
        self.look(wrapper)
        settled = self.look(wrapper)
        self.assertEqual(settled["state"], "done")
        self.assertEqual((Path(settled["dir"]) / "out.md").read_text(), "finished")

    def test_a_status_that_flaps_to_working_and_back_is_not_a_turn(self):
        """Live regression: a CLI at its prompt box flaps every couple of
        seconds. One run logged hundreds of phantom turns and held its home
        long past its deliverable, because each flap reset the run of ready
        looks the ending needs."""
        wrapper, pane = self.idle_worker()
        for _ in range(6):
            self.set_status(pane, "done")
            self.look(wrapper)
            self.set_status(pane, "working")            # the flap
            last = self.look(wrapper)
            if last["state"] != "running":
                break
        log = self.log_of(wrapper.rec)
        self.assertNotIn("started (not by dispatch)", log)
        self.assertEqual(wrapper.rec.get("turns", 1), 1)
        self.assertEqual(wrapper.rec["state"], "done")
        self.assertEqual(self.stub.typed().count("/exit"), 1)

    def test_a_nudged_worker_that_never_writes_is_killed_by_the_check_ins(self):
        """The ladder is what ends a worker that has genuinely stopped."""
        wrapper, pane = self.idle_worker(wrote=False)
        self.set_status(pane, "done")
        self.look(wrapper)
        self.look(wrapper)
        self.assertIn("NUDGE", self.log_of(wrapper.rec))
        self.set_status(pane, "idle")                   # it ignored the nudge
        wrapper.rec["started_at"] = time.time() - 30
        records.save_record(wrapper.rec)
        for _ in range(3):
            self.look(wrapper, deadline_seconds=1)
            self.freeze_checkin_screen(wrapper.rec)
            wrapper.rec = records.load_record(wrapper.rec["id"])
        killed = records.load_record(wrapper.rec["id"])
        self.assertEqual(killed["checkin_verdict"], "stuck")
        self.assertEqual(killed["state"], "timeout")

    def test_a_self_exiting_worker_with_no_deliverable_still_fails_fast(self):
        """The shell coming back ends a run whatever the turn signal said."""
        self.stub.self_exits = True
        self.stub.write_output = False
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_FAILED)
        rec = self.only_record()
        self.assertEqual(rec["state"], "failed")
        self.assertIn("NO-DELIVERABLE", (Path(rec["dir"]) / "status.log").read_text())
        self.assertFalse(rec.get("nudged"))
        # The salvage fills out.md all the same, so the record is what tells a
        # reader that the text in it is a screen and not an answer.
        self.assertIs(rec["deliverable_written"], False)
        _, output = self.capture_stdout("wait", rec["id"])
        self.assertIn("-- out.md: no answer --", output)


# --------------------------------------------------------------------------
# Background and deadlines
# --------------------------------------------------------------------------


class TestBackground(HerdrStubTestCase):
    def wait_for_state(self, rec_id, seconds=60):
        deadline = time.time() + seconds
        while time.time() < deadline:
            rec = records.load_record(rec_id)
            if rec.get("state") in policy.policy().terminal_states:
                return rec
            time.sleep(0.1)
        self.fail(f"{rec_id} never reached a terminal state: "
                  f"{records.load_record(rec_id).get('state')}")

    def test_a_bg_run_completes_with_no_further_dispatch_invocation(self):
        """The whole point of the watcher.

        Nothing here calls dispatch again: the detached watcher has to notice the
        turn is over, send the exit command itself, read the exit code back off
        the shell, and close the workspace on its own.
        """
        self.run_cli("run", "opus@high", str(self.brief), "--bg")
        rec = records.load_record(self.only_record()["id"])
        self.assertTrue(rec["watcher_pid"])
        finished = self.wait_for_state(rec["id"])
        self.assertEqual(finished["state"], "done")
        self.assertEqual(finished["rc"], 0)
        self.assertEqual(finished["exit_command"], "/exit")
        self.assertIn("/exit", self.stub.typed())
        self.assertIn(("workspace", rec["worker_group"]), self.stub.closed)
        self.assertEqual((Path(rec["dir"]) / "out.md").read_text(), "final message")

    def test_a_watcher_that_cannot_be_spawned_fails_the_run_loudly(self):
        """A live pane with nobody to close it should not wait for a sweep."""
        original = runner.spawn_detached_watcher
        runner.spawn_detached_watcher = lambda rec: (_ for _ in ()).throw(
            errors.DispatchError("could not start a watcher"))
        self.addCleanup(setattr, runner, "spawn_detached_watcher", original)
        code, error = self.capture_stderr("run", str(self.brief), "--bg")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("could not start a watcher", error)
        rec = self.only_record()
        self.assertEqual(rec["state"], "failed")
        self.assertIn(("workspace", rec["worker_group"]), self.stub.closed)

    def test_the_watcher_checks_in_on_a_slow_run_instead_of_killing_it(self):
        """A healthy worker is not killed for being slow."""
        self.stub.worker_polls = 10_000      # a worker still in its turn
        self.run_cli("run", "opus@high", str(self.brief), "--bg", "--deadline", "1s")
        rec = self.only_record()
        log = Path(rec["dir"]) / "status.log"
        deadline = time.time() + 30
        while time.time() < deadline and "CHECKIN" not in log.read_text():
            time.sleep(0.1)
        self.assertIn("working", log.read_text())
        self.assertIn("CHECKIN-EXTENDED", log.read_text())
        checked = records.load_record(rec["id"])
        self.assertGreaterEqual(checked["checkins"], 1)
        self.assertEqual(checked["checkin_verdict"], "working")
        self.assertEqual(checked["state"], "running", "a working worker was killed")

    def test_a_dead_watcher_is_caught_by_the_next_reconcile(self):
        """Belt and suspenders: the sweep adopts a run nobody is driving."""
        rec = self.unwatched_bg_run()
        self.assertTrue(caps.run_is_live(records.load_record(rec["id"])))
        self.stub.finish_turn(rec["worker_id"])
        self.assertEqual(caps.live_records(self.sweep()), [])
        settled = records.load_record(rec["id"])
        self.assertEqual(settled["state"], "done")
        self.assertIn(("workspace", rec["worker_group"]), self.stub.closed)

    def test_a_launcher_adopting_a_finished_orphan_does_not_deadlock(self):
        """The reservation sweeps under the runs lock, and finalizing the run
        it adopts takes that lock again from inside; a second take on a fresh
        descriptor blocked the launcher against itself, and every launcher
        after it, since the record never became terminal."""
        rec = self.unwatched_bg_run()
        self.stub.finish_turn(rec["worker_id"])
        launcher = threading.Thread(
            target=caps.reserve_slot,
            args=(lambda: records.load_record(rec["id"]), self.sweep()),
            daemon=True)
        launcher.start()
        launcher.join(10)
        self.assertFalse(launcher.is_alive(), "reserve_slot blocked on runs.lock")
        self.assertEqual(records.load_record(rec["id"])["state"], "done")

    def test_status_flags_a_bg_run_that_lost_its_watcher(self):
        rec = self.unwatched_bg_run()
        _, output = self.capture_stdout("status")
        self.assertIn("(no watcher)", output)

    def test_a_live_watcher_is_not_reconciled_underneath(self):
        """Two processes driving one pane would race for its exit code."""
        self.stub.worker_polls = 10_000
        self.run_cli("run", "opus@high", str(self.brief), "--bg")
        rec = records.load_record(self.only_record()["id"])
        self.assertTrue(caps.run_is_watched(rec))
        self.assertEqual(len(caps.live_records(self.sweep())), 1)
        self.assertNotIn("/exit", self.stub.typed())
        self.stop_watcher(rec["watcher_pid"])

    def test_a_bg_run_being_started_is_not_adopted_by_a_reconciler(self):
        """`dispatch status` right after `dispatch run --bg` is ordinary usage.

        Opening the pane, starting the CLI, and delivering the brief are three
        blocking socket calls, and no watcher exists yet for any of them. The
        launcher holds owner.lock throughout, and that has to count.
        """
        rec = self.make_live_record(foreground=False, state="running")
        handle = records.hold_run_lock(rec, "owner.lock")
        self.addCleanup(records.release_run_lock, handle)
        self.assertTrue(caps.run_is_watched(rec))
        self.stub.finish_turn(rec["worker_id"])
        self.assertEqual(len(caps.live_records(self.sweep())), 1)
        self.assertNotIn("/exit", self.stub.typed())
        self.assertEqual(records.load_record(rec["id"])["state"], "running")

    def test_two_reconcilers_do_not_both_adopt_one_run(self):
        """Adoption is exclusive, or both sweeps end the same session."""
        rec = self.unwatched_bg_run()
        self.stub.finish_turn(rec["worker_id"])
        blocker = records.hold_run_lock(rec, "watcher.lock")
        self.addCleanup(records.release_run_lock, blocker)
        self.assertIsNotNone(blocker)
        settled = runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        self.assertEqual(settled["state"], "running")
        self.assertNotIn("/exit", self.stub.typed())

    def test_bg_defaults_to_a_thirty_minute_deadline(self):
        code = self.run_cli("run", str(self.brief), "--bg")
        self.assertEqual(code, cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["deadline"], "30m")
        self.assertEqual(rec["deadline_seconds"], 1800)

    def test_a_worker_still_mid_turn_is_left_alone_by_a_reconciler(self):
        """Reconciling is not a nudge: an unfinished worker stays running.

        An `agent-done` lane, so "unfinished" means herdr still reports it
        working; the codex lane's own signal is its deliverable, covered where
        that signal is tested.
        """
        rec = runner.reconcile_run(self.unwatched_bg_run(), self.substrate())
        self.assertEqual(rec["state"], "running")
        self.assertNotIn("/quit", self.stub.typed())

    def test_bg_hands_the_run_to_a_detached_watcher_and_returns(self):
        """The launching process leaves; something else drives the run home."""
        rec = self.unwatched_bg_run()
        self.assertTrue(rec["worker_id"])
        self.assertFalse(rec["foreground"])
        self.assertTrue(rec["watcher_pid"])
        self.assertNotEqual(rec["watcher_pid"], os.getpid())
        self.assertIn("WATCHER", (Path(rec["dir"]) / "status.log").read_text())

    def test_a_launched_bg_run_counts_against_the_cap_immediately(self):
        rec = self.unwatched_bg_run()
        self.assertTrue(caps.run_is_live(rec))
        self.assertEqual(len(caps.live_records()), 1)

    def test_explicit_deadline_wins(self):
        self.run_cli("run", "luna@high", str(self.brief), "--bg", "--deadline", "5m")
        self.assertEqual(self.only_record()["deadline_seconds"], 300)

    def test_reconciling_settles_a_run_whose_watcher_is_gone(self):
        """The sweep is the backstop for a watcher that died mid-run."""
        rec_id = self.unwatched_bg_run()["id"]
        self.assertEqual(records.load_record(rec_id)["state"], "running")
        self.stub.finish_turn(records.load_record(rec_id)["worker_id"])
        runner.reconcile_run(records.load_record(rec_id), self.substrate())
        settled = records.load_record(rec_id)
        self.assertEqual(settled["state"], "done")
        self.assertEqual(settled["rc"], 0)

    def test_a_settled_background_run_frees_its_cap_slot(self):
        rec = self.unwatched_bg_run()
        self.assertEqual(len(caps.live_records()), 1)
        self.stub.finish_turn(rec["worker_id"])
        self.assertEqual(caps.live_records(self.sweep()), [])
        self.assertEqual(records.load_record(self.only_record()["id"])["state"], "done")

    def test_a_reconciler_checks_in_until_the_worker_is_demonstrably_stuck(self):
        """The sweep is the deadline owner for an unwatched run, kill included.

        Two looks, not one: the first ambiguous look only records that the
        signals disagreed, which is what keeps a slow worker's pane alive.
        """
        rec = self.stuck_bg_run()
        runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        looked = records.load_record(rec["id"])
        self.assertEqual(looked["checkin_verdict"], "review")
        self.assertEqual(looked["state"], "running", "killed on one ambiguous look")
        runner.reconcile_run(self.freeze_checkin_screen(rec), self.substrate())
        settled = records.load_record(rec["id"])
        self.assertEqual(settled["state"], "timeout")
        self.assertEqual(settled["checkin_verdict"], "stuck")
        self.assertIn("DEADLINE", (Path(settled["dir"]) / "status.log").read_text())

    def test_status_reconciles_the_runs_it_reports_on(self):
        rec = self.unwatched_bg_run()
        self.stub.finish_turn(rec["worker_id"])
        code, output = self.capture_stdout("status")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("done", output)


# --------------------------------------------------------------------------
# Depth ladder
# --------------------------------------------------------------------------


class TestDeadlineCheckins(HerdrStubTestCase):
    """The deadline is when a run gets checked on, not when it dies.

    The verdict ladder is judged here on synthetic signals, because that is the
    only way to state what each threshold means without waiting out two minutes
    of real screen silence per case.
    """

    def signals(self, **fields):
        """A worker at its deadline with nothing left to show: silent, no CPU."""
        base = {"worker_id": "w1:p1", "at_prompt": False, "child_pids": (4242,),
                "output_idle_seconds": 3600.0, "cpu_percent": -1.0,
                "elapsed_seconds": 3600.0, "seen_alive": True}
        base.update(fields)
        return runner.LivenessSignals(**base)

    def verdict(self, status="idle", deliverable=False, prior_reviews=0,
                rule="", driver="claude", **fields):
        """A verdict for one run at its deadline, judged against a driver's rules.

        Which dialogs are dispatch's to answer and which wait on a human is the
        driver's answer, so the ladder is given both sets rather than a table of
        its own.
        """
        rules = drivers.get_driver(driver).dialog_rules
        return runner.checkin_verdict(
            self.signals(**fields), status, deliverable, prior_reviews,
            interval_seconds=1800.0, blocked_rule=rule,
            answered_rules=rules.answered_rules,
            handback_rules=rules.handback_rules).verdict

    def test_a_screen_that_moved_recently_is_a_working_worker(self):
        self.assertEqual(self.verdict(output_idle_seconds=1.0), "working")

    def test_codex_animation_without_a_session_is_stuck_at_first_checkin(self):
        signals = self.signals(output_idle_seconds=0.0,
                               screen_is_progress=False, session_seen=False)
        found = runner.checkin_verdict(signals, "idle", False, 0,
                                       interval_seconds=1800.0)
        self.assertEqual(found.verdict, "stuck")
        self.assertIn("no transcript or session", found.reason)
        self.assertEqual(runner.judge_liveness(signals), "settled")

    def test_codex_transcript_activity_counts_when_the_screen_cannot(self):
        signals = self.signals(output_idle_seconds=0.0,
                               screen_is_progress=False, session_seen=True,
                               transcript_idle_seconds=0.0)
        self.assertEqual(runner.checkin_verdict(
            signals, "idle", False, 0, interval_seconds=1800.0).verdict, "working")
        self.assertEqual(runner.judge_liveness(signals), "working")

    def test_codex_animation_does_not_count_with_an_old_session_either(self):
        for idle in (0.0, 1799.0):
            signals = self.signals(output_idle_seconds=idle,
                                   screen_is_progress=False, session_seen=True,
                                   transcript_idle_seconds=3600.0)
            self.assertEqual(runner.checkin_verdict(
                signals, "idle", False, 0, interval_seconds=1800.0).verdict, "review")
            self.assertEqual(runner.checkin_verdict(
                signals, "idle", False, 1, interval_seconds=1800.0).verdict, "stuck")

    def test_codex_poll_measures_cpu_despite_animation_and_surfaces_no_session(self):
        rec = self.make_live_record(started_at=time.time() - 3600)
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = rec["agent"]
        pane.running = True
        pane.status = "idle"
        substrate = self.substrate()
        wrapper = runner.RunWrapper(substrate, rec)
        wrapper.attach()
        with patch.object(substrate, "read_screen", return_value="animated idle composer"), \
                patch.object(substrate, "cpu_percent", return_value=None) as cpu:
            self.assertEqual(wrapper.poll(deadline_seconds=1800), (None, True))
        cpu.assert_called()
        self.assertEqual(rec["liveness"]["verdict"], "settled")
        self.assertEqual(rec["checkin_verdict"], "stuck")

    def test_burnt_cpu_is_a_working_worker(self):
        self.assertEqual(
            self.verdict(cpu_percent=runner.CPU_BUSY_PERCENT + 1), "working")

    def test_herdr_calling_the_agent_working_is_a_working_worker(self):
        self.assertEqual(self.verdict(status="working"), "working")

    def test_a_screen_that_moved_since_the_last_checkin_is_a_working_worker(self):
        """Long past the idle window and still making progress: extend it."""
        self.assertEqual(self.verdict(output_idle_seconds=1799.0), "working")

    def test_a_pane_with_no_worker_and_no_prompt_is_dead(self):
        self.assertEqual(self.verdict(child_pids=()), "dead")

    def test_a_dialog_nobody_will_answer_is_a_kill_on_the_second_checkin(self):
        self.assertEqual(self.verdict(status="blocked", rule="permission_prompt"),
                         "review")
        self.assertEqual(self.verdict(status="blocked", rule="permission_prompt",
                                      prior_reviews=1), "blocked")

    def test_the_trust_dialog_is_never_a_kill(self):
        """dispatch answers that one itself, so it is not a worker to shoot."""
        self.assertEqual(
            self.verdict(status="blocked", prior_reviews=3, driver="codex",
                         rule=drivers.get_driver("codex")
                         .dialog_rules.answered_rules[0]), "review")

    def test_a_dialog_waiting_on_a_human_survives_every_check_in(self):
        """Somebody is going to answer this one, however long he takes."""
        for prior in range(5):
            self.assertEqual(
                self.verdict(status="blocked", prior_reviews=prior,
                             rule=drivers.get_driver("claude")
                             .dialog_rules.handback_rules[0]), "review")

    def test_a_silent_worker_with_nothing_written_is_stuck_on_the_second_checkin(self):
        self.assertEqual(self.verdict(), "review")
        self.assertEqual(self.verdict(prior_reviews=1), "stuck")

    def test_a_silent_worker_that_already_delivered_is_never_stuck(self):
        """Signals that disagree keep the pane: the exit ladder owns this one."""
        self.assertEqual(self.verdict(deliverable=True, prior_reviews=5), "review")

    def test_an_ambiguous_check_in_never_kills(self):
        for prior in range(5):
            for status in ("idle", "done", ""):
                self.assertNotIn(
                    self.verdict(status=status, deliverable=True, prior_reviews=prior),
                    runner.CHECKIN_KILL_VERDICTS)

    # -- wired into the poll ---------------------------------------------

    def extended_run(self):
        """A working run whose deadline has passed and been checked on once."""
        rec = self.unwatched_bg_run()
        rec["deadline_seconds"] = 60
        rec["started_at"] = time.time() - 90
        records.save_record(rec)
        runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        return records.load_record(rec["id"])

    def test_a_working_worker_is_bought_another_interval(self):
        checked = self.extended_run()
        self.assertEqual(checked["state"], "running")
        self.assertEqual(checked["checkins"], 1)
        self.assertEqual(checked["checkin_verdict"], "working")
        self.assertTrue(checked["checkin_at"])
        self.assertGreater(cli.watch_deadline_left(checked), 0,
                           "the deadline was not moved")

    def test_the_wall_shows_a_run_that_is_past_its_original_deadline(self):
        rec = self.extended_run()
        _, output = self.capture_stdout("watch")
        self.assertIn("+1", output)
        self.assertIn(rec["id"], output)

    def test_status_says_how_many_times_a_run_was_checked_on(self):
        self.extended_run()
        _, output = self.capture_stdout("status")
        self.assertIn("checked in 1x (working)", output)

    def test_the_logs_say_why_a_run_is_still_alive_without_the_pane(self):
        """What a seat reads instead of attaching: verdict, reason, next look."""
        rec = self.extended_run()
        _, output = self.capture_stdout("logs", rec["id"])
        self.assertIn("CHECKIN", output)
        self.assertIn("working", output)
        self.assertIn("next check-in at 120s", output)

    def test_a_killed_check_in_names_the_verdict_in_the_record(self):
        """`timeout` alone would say a slow run; this one stopped being a run."""
        rec = self.stuck_bg_run()
        self.stub.blocked_rule = "permission_prompt"   # not dispatch's to answer
        self.stub.panes[rec["worker_id"]].blocked = True
        for _ in range(2):
            runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
            self.freeze_checkin_screen(rec)
        settled = records.load_record(rec["id"])
        self.assertEqual(settled["state"], "timeout")
        self.assertEqual(settled["checkin_verdict"], "blocked")
        self.assertIn("blocked", settled["error"])

    @unittest.skipIf(processes.IS_WINDOWS, "the cross-process harness is POSIX-only")
    def test_a_second_process_picks_up_the_checkin_count_from_the_record(self):
        """Nothing watches an adopted run for long, so the ladder lives on disk."""
        rec = self.stuck_bg_run()
        env = dict(os.environ, DISPATCH_HOME=str(self.home), AGENT_DEPTH="0",
                   PYTHONPATH=str(REPO_ROOT))
        env[herdr.SOCKET_ENV] = self.stub.path
        for _ in range(2):
            probe = subprocess.run(DISPATCH_ARGV + ["status"],
                                   cwd=str(self.work), env=env,
                                   stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=180)
            self.assertEqual(probe.returncode, cli.EXIT_OK, probe.stderr)
        settled = records.load_record(rec["id"])
        self.assertEqual(settled["checkins"], 3, "the ladder restarted in a new process")
        self.assertEqual(settled["checkin_verdict"], "stuck")
        self.assertEqual(settled["state"], "timeout")


class TestDepth(HerdrStubTestCase):
    def test_depth_one_refuses_every_non_luna_lane(self):
        os.environ["AGENT_DEPTH"] = "1"
        for lane in ("sol@medium", "opus@high", "grok@high"):
            code = self.run_cli("run", lane, str(self.brief))
            self.assertEqual(code, cli.EXIT_USAGE, lane)
        self.assertEqual(self.stub.starts, [])

    def test_depth_one_allows_luna(self):
        os.environ["AGENT_DEPTH"] = "1"
        self.assertEqual(self.run_cli("run", "luna@high", str(self.brief)),
                         cli.EXIT_OK)
        self.assertEqual(len(self.stub.starts), 1)

    def test_depth_two_spawns_nothing(self):
        os.environ["AGENT_DEPTH"] = "2"
        self.assertEqual(self.run_cli("run", "luna@high", str(self.brief)),
                         cli.EXIT_USAGE)
        self.assertEqual(self.stub.starts, [])

    def test_a_dishonest_depth_marker_fails_closed(self):
        """-1 and garbage used to read as depth 0, which made the whole ladder
        optional for anything that could set an environment variable."""
        for hostile in ("-1", "xyz", "1.5", "0x2", "  -0  ", "2x", "+1", "1e0"):
            os.environ["AGENT_DEPTH"] = hostile
            self.assertEqual(self.run_cli("run", "luna@high", str(self.brief)),
                             cli.EXIT_USAGE, hostile)
        self.assertEqual(self.stub.starts, [])

    def test_which_lanes_depth_one_may_spawn_is_a_policy_value(self):
        """The board fills this from its light slot; until then it is a default."""
        os.environ["AGENT_DEPTH"] = "1"
        self.addCleanup(policy.set_policy,
                        policy.with_policy(depth1_lane_keys=("opus",)))
        self.assertEqual(self.run_cli("run", "luna@high", str(self.brief)),
                         cli.EXIT_USAGE)
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)

    def test_a_huge_depth_is_still_a_refusal_not_an_overflow(self):
        os.environ["AGENT_DEPTH"] = "99999999999999999999"
        self.assertEqual(self.run_cli("run", "luna@high", str(self.brief)),
                         cli.EXIT_USAGE)
        self.assertEqual(self.stub.starts, [])

    def test_absent_or_empty_depth_is_the_top_seat(self):
        for value in (None, "", "  "):
            os.environ.pop("AGENT_DEPTH", None)
            if value is not None:
                os.environ["AGENT_DEPTH"] = value
            self.assertEqual(policy.current_depth(), 0, repr(value))

    def test_the_ladder_marker_is_injected_into_the_workers_pane(self):
        """The pane's shell is where a worker's own dispatch will read it."""
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.stub.workspaces[0]["env"]["AGENT_DEPTH"], "1")
        os.environ["AGENT_DEPTH"] = "1"
        self.run_cli("run", "luna@high", str(self.brief))
        self.assertEqual(self.stub.workspaces[1]["env"]["AGENT_DEPTH"], "2")

    def test_the_session_key_is_injected_too(self):
        os.environ["DISPATCH_SESSION"] = "seat-alpha"
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.stub.workspaces[0]["env"]["DISPATCH_SESSION"], "seat-alpha")

    def test_relocating_the_runs_tree_inside_a_worker_warns(self):
        os.environ["AGENT_DEPTH"] = "1"
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            records.dispatch_home()
        self.assertIn("DISPATCH_HOME", buffer.getvalue())


# --------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------


class TestCaps(HerdrStubTestCase):
    """Two limits: 16 workers machine-wide, 4 per parent session."""

    def hold_session_runs(self, count, session):
        return [self.make_live_record(session=session) for _ in range(count)]

    def test_session_key_prefers_an_explicit_grouping(self):
        os.environ["DISPATCH_SESSION"] = "seat-alpha"
        self.assertEqual(caps.session_key(), "seat-alpha")
        os.environ.pop("DISPATCH_SESSION")
        key = caps.session_key()
        if processes.IS_WINDOWS:
            self.assertEqual(key, f"ppid:{os.getppid()}")
        else:
            self.assertEqual(key, f"sid:{os.getsid(0)}")

    def test_the_session_key_is_stored_on_the_run_record(self):
        os.environ["DISPATCH_SESSION"] = "seat-beta"
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.only_record()["session"], "seat-beta")

    def test_fifth_run_in_one_session_is_refused_by_the_session_cap(self):
        os.environ["DISPATCH_SESSION"] = "seat-alpha"
        self.hold_session_runs(policy.policy().session_cap, "seat-alpha")
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(self.stub.starts, [])
        self.assertIn("session cap reached: 4 workers live for session seat-alpha "
                      "(limit 4 per parent session)", error)

    def test_a_different_session_still_passes_while_one_session_is_full(self):
        self.hold_session_runs(policy.policy().session_cap, "seat-alpha")
        os.environ["DISPATCH_SESSION"] = "seat-beta"
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        self.assertEqual(len(self.stub.starts), 1)

    def test_fourth_run_in_a_session_still_launches(self):
        os.environ["DISPATCH_SESSION"] = "seat-alpha"
        self.hold_session_runs(policy.policy().session_cap - 1, "seat-alpha")
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        self.assertEqual(len(self.stub.starts), 1)

    def test_seventeenth_worker_is_refused_by_the_machine_cap(self):
        # Sixteen live across four other sessions: this session's own allowance is
        # untouched, so only the machine-wide limit can refuse it.
        for session in ("seat-a", "seat-b", "seat-c", "seat-d"):
            self.hold_session_runs(policy.policy().session_cap, session)
        self.assertEqual(len(caps.live_records()), policy.policy().machine_cap)
        os.environ["DISPATCH_SESSION"] = "seat-e"
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(self.stub.starts, [])
        self.assertIn("machine cap reached: 16 workers live "
                      "(hard limit 16 machine-wide)", error)

    def test_the_sixteenth_worker_machine_wide_still_launches(self):
        for session in ("seat-a", "seat-b", "seat-c"):
            self.hold_session_runs(policy.policy().session_cap, session)
        self.hold_session_runs(policy.policy().session_cap - 1, "seat-d")
        self.assertEqual(len(caps.live_records()), policy.policy().machine_cap - 1)
        os.environ["DISPATCH_SESSION"] = "seat-d"
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)

    def test_a_saturated_session_on_a_saturated_machine_names_the_session(self):
        # Both limits are hit; the caller can only act on their own runs.
        for session in ("seat-a", "seat-b", "seat-c"):
            self.hold_session_runs(policy.policy().session_cap, session)
        self.hold_session_runs(policy.policy().session_cap, "seat-mine")
        os.environ["DISPATCH_SESSION"] = "seat-mine"
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("session cap reached", error)
        self.assertNotIn("machine cap reached", error)

    def test_a_closed_pane_frees_the_slot(self):
        """Liveness is the pane now: no lock, no pid, nothing to go stale."""
        live = [self.make_live_record() for _ in range(4)]
        for rec in live:
            self.stub.panes[rec["worker_id"]].closed = True
            rec["reserved_at"] = time.time() - caps.RESERVATION_GRACE_SECONDS - 1
            records.save_record(rec)
        self.assertEqual(caps.live_records(), [])
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)

    def test_caps_stay_conservative_when_herdr_cannot_be_reached(self):
        """A blinking daemon must not read as "every run is dead"."""
        rec = self.make_live_record()
        self.stub.stop()
        self.assertTrue(caps.run_is_live(rec))

    def test_stale_reservations_are_reconciled_to_orphaned(self):
        rec = self.make_live_record()
        self.stub.panes[rec["worker_id"]].closed = True
        rec["reserved_at"] = time.time() - caps.RESERVATION_GRACE_SECONDS - 1
        records.save_record(rec)
        caps.live_records(self.sweep())
        self.assertEqual(records.load_record(rec["id"])["state"], "orphaned")

    def child_env(self, session="seat-alpha"):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["DISPATCH_HOME"] = str(self.home)
        env["AGENT_DEPTH"] = "0"
        env["DISPATCH_SESSION"] = session
        env[herdr.SOCKET_ENV] = self.stub.path
        env["CODEX_HOME"] = str(self.root / "codex")
        for var in METERED_VARS:
            env.pop(var, None)
        return env

    def launch(self, session="seat-alpha"):
        return subprocess.Popen(
            DISPATCH_ARGV + ["run", str(self.brief)],
            cwd=str(self.work), env=self.child_env(session), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_seven_racing_processes_in_one_session_never_exceed_its_four(self):
        self.stub.hold = 0.4
        procs = [self.launch("seat-alpha") for _ in range(7)]
        results = [proc.communicate(timeout=180) for proc in procs]
        codes = [proc.returncode for proc in procs]

        launched = codes.count(cli.EXIT_OK)
        refused = [err for code, (_, err) in zip(codes, results)
                   if code == cli.EXIT_USAGE]
        self.assertEqual(launched + len(refused), 7, results)
        self.assertGreaterEqual(launched, 1)
        self.assertGreaterEqual(len(refused), 1,
                                "seven launched at once and nothing was refused")
        for err in refused:
            self.assertIn("session cap reached", err)
            self.assertIn("seat-alpha", err)
        self.assertLessEqual(self.stub.max_in_flight, policy.policy().session_cap,
                             "more than four workers overlapped in one session")

    def test_a_second_session_runs_while_the_first_is_saturated(self):
        # The per-session limit is not a machine limit: a different parent session
        # keeps working while one seat has used up its own four.
        for _ in range(policy.policy().session_cap):
            self.make_live_record(session="seat-alpha")
        refused = subprocess.run(
            DISPATCH_ARGV + ["run", str(self.brief)],
            cwd=str(self.work), env=self.child_env("seat-alpha"),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
        self.assertEqual(refused.returncode, cli.EXIT_USAGE)
        self.assertIn("session cap reached", refused.stderr)

        other = subprocess.run(
            DISPATCH_ARGV + ["run", str(self.brief)],
            cwd=str(self.work), env=self.child_env("seat-beta"),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
        self.assertEqual(other.returncode, cli.EXIT_OK, other.stderr)

    def test_the_machine_limit_refuses_a_seventeenth_worker_across_sessions(self):
        # Four sessions at their allowance each: sixteen live, machine full. A
        # fresh session has its own four free and is still refused.
        for index in range(policy.policy().machine_cap):
            self.make_live_record(session=f"seat-{index // 4}")
        self.assertEqual(len(caps.live_records()), policy.policy().machine_cap)

        refused = subprocess.run(
            DISPATCH_ARGV + ["run", str(self.brief)],
            cwd=str(self.work), env=self.child_env("seat-fresh"),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
        self.assertEqual(refused.returncode, cli.EXIT_USAGE)
        self.assertIn("machine cap reached: 16 workers live "
                      "(hard limit 16 machine-wide)", refused.stderr)
        self.assertEqual(self.stub.starts, [], "a refused run still spawned a worker")


# --------------------------------------------------------------------------
# status.log
# --------------------------------------------------------------------------


class TestStatusLog(HerdrStubTestCase):
    def test_header_is_the_same_five_lines_the_shell_launchers_wrote(self):
        self.run_cli("run", "luna@high", str(self.brief))
        rec = self.only_record()
        lines = (Path(rec["dir"]) / "status.log").read_text().splitlines()
        # Setting the pane up comes first; the fixed header is still one block.
        self.assertTrue(lines[0].startswith("WORKER "))
        head = lines.index(f"lane: {rec['lane']}")
        self.assertEqual(lines[head + 1], f"pid: {rec['shell_pid']}")
        self.assertEqual(lines[head + 2], f"cwd: {rec['cwd']}")
        self.assertTrue(lines[head + 3].startswith("cmd: codex -m gpt-5.6-luna"))
        self.assertLessEqual(len(lines[head + 3]) - len("cmd: "), 200)
        self.assertRegex(lines[head + 4],
                         r"^started: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertTrue(lines[head + 5].startswith("START "))

    def test_cmd_line_is_truncated_at_two_hundred_characters(self):
        path = self.root / "status.log"
        records.write_status_header(path, "sol@medium", 42, "/w",
                                     ["codex", "x" * 400], "2026-08-17T00:00:00Z")
        line = path.read_text().splitlines()[3]
        self.assertEqual(len(line), len("cmd: ") + 200)

    def test_terminal_state_and_exit_are_recorded(self):
        self.run_cli("run", str(self.brief))
        text = (Path(self.only_record()["dir"]) / "status.log").read_text()
        self.assertIn("EXIT ", text)
        self.assertIn("state: done", text)

    def test_status_command_lists_runs_with_heartbeat_age(self):
        self.run_cli("run", str(self.brief))
        head = records.read_status_head(
            Path(self.only_record()["dir"]) / "status.log", count=6)
        self.assertEqual(head["lane"], lanes.DEFAULT_LANE)
        code, output = self.capture_stdout("status")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("done", output)

    def test_status_flags_a_run_that_fell_back_to_typing(self):
        self.stub.errors["agent.start"] = {"code": "unsupported_agent_kind",
                                           "message": "no"}
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("run", str(self.brief))
        _, output = self.capture_stdout("status")
        self.assertIn("(spawn fallback)", output)


# --------------------------------------------------------------------------
# Session capture, continuation, steering
# --------------------------------------------------------------------------


class TestSessionCapture(HerdrStubTestCase):
    def test_a_codex_prompt_can_be_confirmed_by_its_own_transcript(self):
        sessions = self.rollout_dir()
        rec = self.make_live_record(cwd=str(self.work))
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = rec["agent"]
        pane.running = True
        pane.status = "idle"
        self.stub.swallow_prompts = 100
        wrapper = runner.RunWrapper(self.substrate(), rec)
        wrapper.attach()
        deliver = self.stub.deliver_prompt

        def accept_in_transcript(pane, text):
            deliver(pane, text)
            self.write_rollout(sessions / "rollout-mine.jsonl", self.stub.session_id,
                               str(self.work), prompt=str(Path(rec["dir"]) / "prompt.txt"))

        with patch.object(self.stub, "deliver_prompt", side_effect=accept_in_transcript):
            wrapper.prompt_worker("new task")
        self.assertEqual(len(self.stub.prompts), 1)
        self.assertTrue(rec["prompted"])
        self.assertTrue(rec["session_id_confirmed"])
        self.assertEqual(rec["session_id"], self.stub.session_id)

    def test_another_workers_transcript_does_not_prove_codex_progress(self):
        sessions = self.rollout_dir()
        rec = self.make_live_record(cwd=str(self.work))
        self.write_rollout(sessions / "rollout-other.jsonl", self.stub.session_id,
                           str(self.work), prompt="/other/run/prompt.txt")
        wrapper = runner.RunWrapper(self.substrate(), rec)
        self.assertEqual(wrapper.codex_transcript(), "")
        self.assertFalse(rec.get("session_id"))

    def test_claude_and_grok_sessions_are_pinned_at_spawn(self):
        self.run_cli("run", "opus@high", str(self.brief))
        argv = self.stub.started_argv(0)
        self.assertIn("--session-id", argv)
        self.assertTrue(records.SESSION_ID_RE.match(argv[argv.index("--session-id") + 1]))

    def test_a_codex_rollout_is_found_by_the_time_it_was_written(self):
        """codex cannot be told its id and no longer streams JSON to parse."""
        sessions = self.root / "codex" / "sessions" / time.strftime("%Y/%m/%d",
                                                                    time.gmtime())
        sessions.mkdir(parents=True)
        os.environ["CODEX_HOME"] = str(self.root / "codex")
        old = sessions / "rollout-old.jsonl"
        self.write_rollout(old, "00000000-1111-2222-3333-000000000000", str(self.work))
        os.utime(old, (1, 1))
        started = time.time()
        fresh = sessions / "rollout-fresh.jsonl"
        self.write_rollout(fresh, "00000000-1111-2222-3333-444444444444", str(self.work))
        session_id, path, _ = drivers.get_driver("codex").capture_session(started, str(self.work))
        self.assertEqual(session_id, "00000000-1111-2222-3333-444444444444")
        self.assertEqual(path, str(fresh))

    def test_a_rollout_older_than_the_run_is_not_claimed_as_its_own(self):
        sessions = self.root / "codex" / "sessions" / time.strftime("%Y/%m/%d",
                                                                    time.gmtime())
        sessions.mkdir(parents=True)
        os.environ["CODEX_HOME"] = str(self.root / "codex")
        stale = sessions / "rollout-stale.jsonl"
        self.write_rollout(stale, "00000000-1111-2222-3333-444444444444", str(self.work))
        os.utime(stale, (1, 1))
        self.assertEqual(drivers.get_driver("codex").capture_session(time.time()), ("", "", False))

    def test_a_rollout_from_another_cwd_is_not_claimed(self):
        """Four to sixteen runs are in flight at once; newest-file-wins would
        attach `continue` to whichever of them started last."""
        sessions = self.root / "codex" / "sessions" / time.strftime("%Y/%m/%d",
                                                                   time.gmtime())
        sessions.mkdir(parents=True)
        os.environ["CODEX_HOME"] = str(self.root / "codex")
        started = time.time()
        mine = "00000000-1111-2222-3333-444444444444"
        theirs = "99999999-8888-7777-6666-555555555555"
        self.write_rollout(sessions / "rollout-mine.jsonl", mine, str(self.work))
        # Written later, so it is the newest: ownership has to decide, not mtime.
        self.write_rollout(sessions / "rollout-theirs.jsonl", theirs, "/somewhere/else")
        self.assertEqual(drivers.get_driver("codex").capture_session(started, str(self.work)),
                         (mine, str(sessions / "rollout-mine.jsonl"), False))

    def test_a_rollout_with_no_matching_cwd_is_not_guessed_at(self):
        sessions = self.root / "codex" / "sessions" / time.strftime("%Y/%m/%d",
                                                                   time.gmtime())
        sessions.mkdir(parents=True)
        os.environ["CODEX_HOME"] = str(self.root / "codex")
        self.write_rollout(sessions / "rollout-theirs.jsonl",
                           "99999999-8888-7777-6666-555555555555", "/somewhere/else")
        self.assertEqual(drivers.get_driver("codex").capture_session(time.time(), str(self.work)),
                         ("", "", False))

    def test_two_runs_in_one_directory_are_told_apart_by_their_own_prompt(self):
        """The ordinary case: several workers in one repo, all sharing a cwd.

        Both rollouts pass the directory filter, so newest-wins handed the
        earlier run the later run's session.
        """
        sessions = self.rollout_dir()
        started = time.time()
        mine, theirs = ("00000000-1111-2222-3333-444444444444",
                        "99999999-8888-7777-6666-555555555555")
        rec = self.make_live_record(backend="codex", cwd=str(self.work),
                                    started_at=started)
        self.write_rollout(sessions / "rollout-mine.jsonl", mine, str(self.work),
                           prompt=str(Path(rec["dir"]) / "prompt.txt"))
        # Newer, same directory, but it carries the other run's prompt.
        self.write_rollout(sessions / "rollout-theirs.jsonl", theirs, str(self.work),
                           prompt="/elsewhere/other-run/prompt.txt")
        self.assertEqual(runner.refresh_session_id(rec), mine)
        self.assertTrue(rec["session_id_confirmed"])

    def test_a_session_matched_only_by_directory_is_marked_as_inferred(self):
        """Still the best guess available, and worth saying out loud."""
        sessions = self.rollout_dir()
        started = time.time()
        rec = self.make_live_record(backend="codex", cwd=str(self.work),
                                    started_at=started)
        self.write_rollout(sessions / "rollout-unknown.jsonl",
                           "99999999-8888-7777-6666-555555555555", str(self.work),
                           prompt="/elsewhere/other-run/prompt.txt")
        self.assertEqual(runner.refresh_session_id(rec),
                         "99999999-8888-7777-6666-555555555555")
        self.assertFalse(rec["session_id_confirmed"])
        self.assertIn("SESSION-INFERRED",
                      (Path(rec["dir"]) / "status.log").read_text())

    def rollout_dir(self):
        sessions = self.root / "codex" / "sessions" / time.strftime("%Y/%m/%d",
                                                                   time.gmtime())
        sessions.mkdir(parents=True, exist_ok=True)
        os.environ["CODEX_HOME"] = str(self.root / "codex")
        return sessions

    def test_a_generated_session_id_is_not_overwritten_by_the_screen(self):
        """dispatch hands claude and grok their session id, so that value is the
        fact and the screen is only a report of it."""
        self.stub.session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.run_cli("run", "opus@high", str(self.brief))
        rec = self.only_record()
        argv = self.stub.started_argv(0)
        self.assertEqual(rec["session_id"], argv[argv.index("--session-id") + 1])
        self.assertNotEqual(rec["session_id"], self.stub.session_id)

    def write_rollout(self, path, session_id, cwd, prompt=""):
        """A rollout's opening records: the meta line, then the first user turn."""
        lines = [{"timestamp": "2026-08-17T00:00:00Z", "type": "session_meta",
                  "payload": {"session_id": session_id, "cwd": cwd}}]
        if prompt:
            lines.append({"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text",
                             "text": f"Read and follow the instructions written in {prompt}"}]}})
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n",
                        encoding="utf-8")

    def test_continue_resumes_by_captured_session_id(self):
        self.run_cli("run", "opus@high", str(self.brief))
        parent = self.only_record()["id"]
        self.assertEqual(self.run_cli("continue", parent, "one more thing"),
                         cli.EXIT_OK)
        argv = self.stub.started_argv(1)
        self.assertEqual(argv[:2], ["claude", "--resume"])
        self.assertEqual(argv[2], records.load_record(parent)["session_id"])
        child = [r for r in records.all_records() if r.get("parent") == parent]
        self.assertEqual(len(child), 1)
        self.assertEqual(child[0]["kind"], "continue")
        # The follow-up is durable in the child's own run dir, and the prompt
        # points the worker at it, the same way a first-turn brief works.
        self.assertEqual((Path(child[0]["dir"]) / "brief.md").read_text(),
                         "one more thing")
        self.assertIn(str(Path(child[0]["dir"]) / "brief.md"), self.stub.prompts[-1])

    def test_continue_opens_a_fresh_pane_rather_than_reusing_a_closed_one(self):
        self.run_cli("run", "opus@high", str(self.brief))
        parent = records.load_record(self.only_record()["id"])
        self.run_cli("continue", parent["id"], "again")
        child = [r for r in records.all_records() if r.get("parent") == parent["id"]][0]
        self.assertNotEqual(child["worker_id"], parent["worker_id"])

    def test_continue_refuses_a_run_that_is_still_live(self):
        rec = self.make_live_record(
            session_id="11111111-2222-3333-4444-555555555555", backend="claude",
            lane="opus@high")
        self.assertEqual(self.run_cli("continue", rec["id"], "more"),
                         cli.EXIT_USAGE)
        self.assertEqual(self.stub.starts, [])

    def test_continue_without_a_session_id_refuses_rather_than_guessing(self):
        rec = self.make_live_record(backend="claude", lane="opus@high", state="done")
        code, error = self.capture_stderr("continue", rec["id"], "more")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("session id", error)


class TestSteer(HerdrStubTestCase):
    """Steering types into a live worker; it never destroys the session."""

    def live_run(self, lane="opus@high"):
        self.run_cli("run", lane, str(self.brief), "--bg")
        rec = records.load_record(self.only_record()["id"])
        pane = self.stub.panes[rec["worker_id"]]
        pane.turn_polls = 10_000      # still generating
        pane.status = "working"
        return rec

    def test_steer_interrupts_then_types_the_message(self):
        rec = self.live_run()
        code = self.run_cli("steer", rec["id"], "stop and summarize")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.stub.params_for("pane.send_keys")[-1]["keys"], ["escape"])
        self.assertIn("stop and summarize", self.stub.prompts[-1])

    def test_steer_never_kills_the_worker(self):
        """A worker with no terminal could only be interrupted and continued.

        A pane has a real terminal, so the session survives and no context is
        thrown away.
        """
        rec = self.live_run()
        self.run_cli("steer", rec["id"], "adjust")
        after = records.load_record(rec["id"])
        self.assertNotIn(after["state"], policy.policy().terminal_states)
        self.assertEqual(len(self.stub.starts), 1, "steer respawned the worker")
        self.assertNotIn(("pane", rec["worker_id"]), self.stub.closed)

    def test_the_liveness_verdict_is_recorded_before_anything_is_typed(self):
        rec = self.live_run()
        self.run_cli("steer", rec["id"], "adjust")
        after = records.load_record(rec["id"])
        self.assertIn(after["liveness"]["verdict"], ("working", "settled"))
        self.assertIn("liveness=", (Path(rec["dir"]) / "status.log").read_text())

    def test_steering_a_worker_that_already_exited_becomes_a_continuation(self):
        """Typing into a bare shell prompt would run the message as a command."""
        self.run_cli("run", "opus@high", str(self.brief))
        rec_id = self.only_record()["id"]
        self.assertEqual(self.run_cli("steer", rec_id, "one more thing"),
                         cli.EXIT_OK)
        self.assertIn("run already done",
                      (records.run_dir(rec_id) / "status.log").read_text())
        self.assertEqual(self.stub.started_argv(1)[:2], ["claude", "--resume"])

    def test_a_reconciler_cannot_overwrite_an_explicit_kill(self):
        self.run_cli("run", str(self.brief), "--bg")
        rec = records.load_record(self.only_record()["id"])
        rec["state"] = "killed"
        rec["closed_by"] = "kill"
        records.save_record(rec)
        finishing = dict(rec, state="done", closed_by="herdr-wrapper")
        records.save_final_record(finishing)
        self.assertEqual(records.load_record(rec["id"])["state"], "killed")


# --------------------------------------------------------------------------
# Kill
# --------------------------------------------------------------------------


class TestKill(HerdrStubTestCase):
    def test_kill_stops_the_worker_tree_and_closes_the_pane(self):
        rec = self.make_live_record()
        self.stub.panes[rec["worker_id"]].running = True
        self.stub.panes[rec["worker_id"]].turn_polls = 10_000
        self.assertEqual(self.run_cli("kill", rec["id"]), cli.EXIT_OK)
        self.assertEqual(records.load_record(rec["id"])["state"], "killed")
        self.assertIn(("pane", rec["worker_id"]), self.stub.closed)
        self.assertIn(("workspace", rec["worker_group"]), self.stub.closed)

    def test_kill_keeps_the_screen_before_taking_the_pane_away(self):
        rec = self.make_live_record()
        self.stub.panes[rec["worker_id"]].screen = "half-finished work\n"
        self.run_cli("kill", rec["id"])
        self.assertIn("half-finished work", (Path(rec["dir"]) / "screen.log").read_text())

    def test_kill_never_touches_a_pane_the_run_no_longer_owns(self):
        """After a pane closes its id can be reused; the record is reconciled."""
        rec = self.make_live_record()
        self.stub.panes[rec["worker_id"]].closed = True
        rec["reserved_at"] = time.time() - caps.RESERVATION_GRACE_SECONDS - 1
        records.save_record(rec)
        self.assertEqual(self.run_cli("kill", rec["id"]), cli.EXIT_OK)
        self.assertEqual(records.load_record(rec["id"])["state"], "orphaned")
        self.assertEqual(self.stub.closed, [])

    def test_a_watched_background_run_is_live_after_its_reservation_ages_out(self):
        """The launcher has let go of owner.lock by then, and `steer` and
        `continue` ask without a worker list: only the watcher's lock says the
        run is still going, and without it they resumed a working session."""
        rec = self.make_live_record()
        rec["reserved_at"] = time.time() - 3600
        records.save_record(rec)
        self.assertFalse(caps.run_is_live(rec))
        handle = records.hold_run_lock(rec, "watcher.lock")
        self.addCleanup(records.release_run_lock, handle)
        self.assertTrue(caps.run_is_live(rec))

    def test_the_runs_lock_is_never_entered_without_being_taken(self):
        """A failed acquire used to fall through, so two commands could count
        the same free slot. It waits, and then it refuses."""
        with patch.object(records, "lock_handle", return_value=None), \
                patch.object(records, "RUNS_LOCK_SECONDS", 0.2):
            with self.assertRaises(errors.DispatchError):
                with records.runs_lock():
                    self.fail("entered the critical section without the lock")

    def test_a_retried_finalization_does_not_take_its_own_salvage_for_an_answer(self):
        """The first attempt died after copying the screen into out.md. Asked of
        the file again, "did the worker write it" is yes, and the run was done."""
        rec = self.make_live_record()
        directory = Path(rec["dir"])
        (directory / "screen.log").write_text("error: not logged in\n",
                                              encoding="utf-8")
        self.assertIs(runner.note_deliverable(rec), False)
        runner.finalize_output(rec)                 # the salvage, then a crash
        self.assertTrue((directory / "out.md").stat().st_size)

        retried = records.load_record(rec["id"])
        self.assertIs(runner.note_deliverable(retried), False)

    def test_a_killed_run_says_whether_the_worker_had_answered(self):
        rec = self.make_live_record()
        rec_id = rec["id"]
        self.stub.panes[rec["worker_id"]].running = True
        self.stub.panes[rec["worker_id"]].turn_polls = 10_000
        self.assertEqual(self.run_cli("kill", rec_id), cli.EXIT_OK)
        self.assertIs(records.load_record(rec_id)["deliverable_written"], False)
        _, report = self.capture_stdout("wait", rec_id)
        self.assertIn("-- out.md: no answer --", report)

    def test_the_watcher_takes_up_a_turn_that_steer_started(self):
        """`steer` is another process. The watcher went on holding the previous
        turn as over and its answer as settled, so it exited the worker during
        the correction and handed back the old answer."""
        rec = self.make_live_record()
        watcher = runner.RunWrapper(herdr.HerdrSubstrate(), dict(rec))
        watcher.attach()
        watcher.rec.update(turn_over_at="2026-01-01T00:00:00Z", end_ready_looks=5,
                           deliverable_seen=[1, 2], deliverable_since=1.0)
        self.assertFalse(watcher.absorb_steer())

        steered = records.load_record(rec["id"])
        steered.update(steered=records.utc_now(), steers=1, turn_over_at="",
                       end_ready_looks=0, deliverable_seen=None,
                       deliverable_since=0.0, turns=2)
        records.save_record(steered)

        self.assertTrue(watcher.absorb_steer())
        self.assertEqual((watcher.rec["turn_over_at"], watcher.rec["end_ready_looks"],
                          watcher.rec["deliverable_seen"], watcher.rec["turns"]),
                         ("", 0, None, 2))
        self.assertFalse(watcher.absorb_steer(), "the same steer taken up twice")
        self.assertIn("STEER-SEEN",
                      (Path(rec["dir"]) / "status.log").read_text(encoding="utf-8"))

    def test_a_stale_save_cannot_erase_a_steer_before_the_watcher_sees_it(self):
        """The watcher saves its copy of the record all the time. One of those
        saves landing between the steer and the watcher's next look used to put
        the old turn back, and the steer was never seen at all."""
        rec = self.make_live_record()
        watcher = runner.RunWrapper(herdr.HerdrSubstrate(), dict(rec))
        watcher.attach()
        watcher.rec.update(turn_over_at="2026-01-01T00:00:00Z", end_ready_looks=5)

        steered = records.load_record(rec["id"])
        steered.update(steered=records.utc_now(), steers=1, turn_over_at="",
                       end_ready_looks=0)
        records.save_record(steered)
        records.save_record(watcher.rec)            # the stale save

        self.assertEqual(records.load_record(rec["id"])["steers"], 1)
        self.assertTrue(watcher.absorb_steer())
        self.assertEqual(watcher.rec["turn_over_at"], "")

    def test_the_answer_from_before_a_steer_cannot_end_the_new_turn(self):
        rec = self.make_live_record()
        out = Path(rec["dir"]) / "out.md"
        out.write_text("the first answer", encoding="utf-8")
        found = out.stat()
        rec["stale_deliverable"] = [found.st_size, found.st_mtime]
        records.save_record(rec)
        watcher = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        watcher.attach()
        with patch.object(runner, "DELIVERABLE_QUIET_SECONDS", 0):
            self.assertFalse(watcher.deliverable_is_settled())
            self.assertFalse(watcher.deliverable_is_settled())
            out.write_text("the corrected answer", encoding="utf-8")
            os.utime(out, (found.st_atime + 5, found.st_mtime + 5))
            watcher.deliverable_is_settled()
            self.assertTrue(watcher.deliverable_is_settled())

    def test_steer_never_types_into_a_pane_that_now_belongs_to_another_run(self):
        """Pane ids start again when a substrate restarts, so the id on an old
        record can name somebody else's live worker."""
        rec = self.make_live_record()
        self.stub.panes[rec["worker_id"]].running = True
        self.stub.panes[rec["worker_id"]].turn_polls = 10_000
        rec["shell_pid"] = 999_999            # not the shell in that pane now
        records.save_record(rec)
        prompts = len(self.stub.prompts)
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("steer", rec["id"], "use the other table")
        self.assertEqual(len(self.stub.prompts), prompts, "typed into a stranger")
        self.assertEqual(records.load_record(rec["id"])["state"], "orphaned")

    def test_steer_marks_the_new_turn_before_it_types(self):
        rec = self.make_live_record()
        self.stub.panes[rec["worker_id"]].running = True
        self.stub.panes[rec["worker_id"]].turn_polls = 10_000
        seen = []
        real = runner.RunWrapper.prompt_worker

        def spy(wrapper, text):
            seen.append(records.load_record(rec["id"]).get("steers"))
            return real(wrapper, text)

        with patch.object(runner.RunWrapper, "prompt_worker", spy):
            self.run_cli("steer", rec["id"], "use the other table")
        self.assertEqual(seen, [1])

    def test_a_stale_writer_cannot_change_how_a_run_ended(self):
        """A watcher mid-poll still holds `running` after `kill` wrote `killed`,
        and a late finalizer still holds its own verdict after `done` landed."""
        rec = self.make_live_record()
        stale = dict(rec)
        rec.update(state="killed", finished=records.utc_now(), closed_by="kill")
        records.save_record(rec)

        stale["session_id"] = "noted-late"
        records.save_record(stale)                  # a plain write
        records.save_heartbeat(dict(stale))         # a heartbeat
        stale.update(state="done", rc=0, closed_by="runner")
        records.save_final_record(stale)            # a finalizer

        settled = records.load_record(rec["id"])
        self.assertEqual((settled["state"], settled["closed_by"]), ("killed", "kill"))
        self.assertNotEqual(settled.get("rc"), 0)
        self.assertEqual(settled["session_id"], "noted-late")

    def test_an_orphaned_guess_gives_way_to_the_real_ending(self):
        rec = self.make_live_record()
        late = dict(rec)
        rec.update(state="orphaned", finished=records.utc_now(), closed_by="reconcile")
        records.save_record(rec)
        late.update(state="done", rc=0, closed_by="runner")
        records.save_final_record(late)
        self.assertEqual(records.load_record(rec["id"])["state"], "done")

    def test_kill_writes_its_terminal_state_under_the_runs_lock(self):
        """A watcher finishing at the same moment reads, decides, then writes.

        An unlocked kill can land inside that window and be overwritten by the
        `done` the watcher had already decided on, so the kill has to serialize
        against it rather than merely happen first.
        """
        rec = self.make_live_record()
        self.stub.panes[rec["worker_id"]].running = True
        self.stub.panes[rec["worker_id"]].turn_polls = 10_000
        holding = threading.Event()
        release = threading.Event()

        def hold_the_lock():
            with records.runs_lock():
                holding.set()
                release.wait(20)

        holder = threading.Thread(target=hold_the_lock, daemon=True)
        holder.start()
        self.addCleanup(release.set)
        self.assertTrue(holding.wait(10))

        finished = threading.Event()

        def kill_it():
            self.run_cli("kill", rec["id"])
            finished.set()

        killer = threading.Thread(target=kill_it, daemon=True)
        killer.start()
        self.assertFalse(finished.wait(1.0),
                         "kill wrote a terminal state without taking the runs lock")
        release.set()
        holder.join(10)
        self.assertTrue(finished.wait(20))
        killer.join(10)
        self.assertEqual(records.load_record(rec["id"])["state"], "killed")

    def test_kill_reports_an_already_finished_run(self):
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.run_cli("kill", self.only_record()["id"]), cli.EXIT_OK)
        self.assertEqual(self.only_record()["state"], "done")


# --------------------------------------------------------------------------
# Aborts
# --------------------------------------------------------------------------


class TestAbort(HerdrStubTestCase):
    def test_abort_is_terminal_and_gets_its_own_exit_code(self):
        self.stub.reply = "ABORT: the brief does not say which repo"
        code = self.run_cli("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_ABORTED)
        rec = self.only_record()
        self.assertEqual(rec["state"], "aborted")
        self.assertEqual(rec["rc"], 0)

    def test_standalone_run_never_retries(self):
        self.stub.rc = 1
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_FAILED)
        self.assertEqual(len(self.stub.starts), 1)

    def schema_file(self):
        path = self.work / "s.json"
        path.write_text('{"type":"object"}', encoding="utf-8")
        return path

    def test_a_schema_run_asks_for_json_in_a_file(self):
        """exec's `--output-schema` is gone with exec; the contract is a prompt."""
        self.stub.reply = '{"finding": "ok"}'
        self.run_cli("run", str(self.brief), "--schema", str(self.schema_file()))
        rec = self.only_record()
        self.assertIn(str(Path(rec["dir"]) / "out.json"), self.stub.prompts[0])
        self.assertIn("JSON Schema", self.stub.prompts[0])
        self.assertEqual(json.loads((Path(rec["dir"]) / "out.json").read_text()),
                         {"finding": "ok"})
        self.assertTrue(rec["schema_valid"])

    def test_valid_json_is_mirrored_into_out_md_for_every_reader(self):
        self.stub.reply = '{"finding": "ok"}'
        self.run_cli("run", str(self.brief), "--schema", str(self.schema_file()))
        rec = self.only_record()
        self.assertEqual(json.loads((Path(rec["dir"]) / "out.md").read_text()),
                         {"finding": "ok"})

    def test_bad_json_is_steered_and_repaired(self):
        self.stub.replies = [(0, "not json at all"), (0, '{"finding": "fixed"}')]
        code = self.run_cli("run", str(self.brief), "--schema", str(self.schema_file()))
        self.assertEqual(code, cli.EXIT_OK)
        rec = self.only_record()
        self.assertTrue(rec["schema_valid"])
        repair = [p for p in self.stub.prompts if "does not satisfy the contract" in p]
        self.assertEqual(len(repair), 1)
        self.assertIn("SCHEMA-REPAIR", (Path(rec["dir"]) / "status.log").read_text())

    def test_a_worker_that_cannot_satisfy_the_contract_fails_honestly(self):
        self.stub.reply = "still not json"
        code = self.run_cli("run", str(self.brief), "--schema", str(self.schema_file()))
        self.assertEqual(code, cli.EXIT_FAILED)
        rec = self.only_record()
        self.assertFalse(rec["schema_valid"])
        self.assertEqual(rec["state"], "failed")
        self.assertIn("schema not satisfied", rec["error"])

    def test_a_refusal_in_a_schema_run_is_not_asked_for_again_as_json(self):
        self.stub.reply = "ABORT: this needs production credentials"
        code = self.run_cli("run", str(self.brief), "--schema", str(self.schema_file()))
        self.assertEqual(code, cli.EXIT_ABORTED)
        self.assertEqual(self.only_record()["state"], "aborted")
        self.assertEqual(
            [p for p in self.stub.prompts if "does not satisfy the contract" in p], [])

    def test_the_repair_count_survives_a_new_process_adopting_the_run(self):
        """It lived on the wrapper, so every adoption after a watcher died was
        another first round."""
        self.stub.reply = "never json"
        self.run_cli("run", str(self.brief), "--schema", str(self.schema_file()))
        rec = self.only_record()
        self.assertEqual(rec["schema_repairs"], runner.SCHEMA_REPAIR_ROUNDS)
        adopted = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        self.assertFalse(adopted.request_schema_repair())

    def test_bad_json_is_never_published_as_done_first(self):
        """The verdict used to land after the terminal record, so a waiter could
        read `done` and exit 0 in between, and a reconciled run kept it."""
        published = []
        real = runner.save_final_record

        def spy(rec):
            published.append(rec.get("state"))
            return real(rec)

        self.stub.reply = "still not json"
        with patch.object(runner, "save_final_record", side_effect=spy):
            self.run_cli("run", str(self.brief), "--schema", str(self.schema_file()))
        self.assertEqual(published, ["failed"])

    def test_repair_rounds_are_capped(self):
        self.stub.reply = "never json"
        self.run_cli("run", str(self.brief), "--schema", str(self.schema_file()))
        repairs = [p for p in self.stub.prompts if "does not satisfy the contract" in p]
        self.assertEqual(len(repairs), runner.SCHEMA_REPAIR_ROUNDS)

    def test_a_schema_run_still_closes_its_pane(self):
        self.stub.reply = "never json"
        self.run_cli("run", str(self.brief), "--schema", str(self.schema_file()))
        rec = self.only_record()
        self.assertIn(("workspace", rec["worker_group"]), self.stub.closed)


# --------------------------------------------------------------------------
# Orchestration: journal, resume, parallel, pipeline
# --------------------------------------------------------------------------


class TestOrchestration(HerdrStubTestCase):
    def test_run_dir_is_durable_and_holds_the_full_story(self):
        self.run_cli("run", str(self.brief))
        directory = Path(self.only_record()["dir"])
        self.assertTrue(str(directory).startswith(os.environ["DISPATCH_HOME"]))
        for name in ("brief.md", "cmd.txt", "prompt.txt", "status.log", "out.md",
                     "screen.log", "run.json"):
            self.assertTrue((directory / name).is_file(), name)

    def test_the_pane_transcript_is_kept_as_part_of_the_record(self):
        """A pane is the only witness to what the worker did on screen."""
        self.stub.reply = "answer"
        self.run_cli("run", str(self.brief))
        pane_log = (Path(self.only_record()["dir"]) / "screen.log").read_text()
        self.assertIn("codex", pane_log)

    def test_record_keeps_the_session_id_for_joining_the_harness_transcript(self):
        self.run_cli("run", "opus@high", str(self.brief))
        rec = self.only_record()
        self.assertIn("transcript", rec)
        self.assertTrue(rec["session_id"])

    def test_out_flag_copies_the_final_message(self):
        target = self.work / "answer.md"
        self.stub.reply = "copied answer"
        self.run_cli("run", str(self.brief), "--out", str(target))
        self.assertEqual(target.read_text(), "copied answer")


    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_out_never_writes_through_a_link_the_worker_left(self):
        """The copy is made as the operator, so a leaf swapped for a symlink is
        replaced, and a directory that moved refuses the copy."""
        secret = self.work / "secret.txt"
        secret.write_text("the operator's file", encoding="utf-8")
        target = self.work / "answer.md"
        target.symlink_to(secret)
        rec = {"dir": str(self.work), "out_copy": records.out_copy_path(str(target)),
               "out_copy_dir": records.out_copy_dir(str(target))}
        self.assertTrue(records.write_out_copy(rec, "the answer"))
        self.assertEqual(secret.read_text(encoding="utf-8"), "the operator's file")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "the answer")

        outdir, elsewhere = self.work / "outdir", self.work / "elsewhere"
        outdir.mkdir()
        elsewhere.mkdir()
        rec["out_copy"] = records.out_copy_path(str(outdir / "answer.md"))
        rec["out_copy_dir"] = records.out_copy_dir(str(outdir / "answer.md"))
        outdir.rmdir()
        outdir.symlink_to(elsewhere)
        self.assertFalse(records.write_out_copy(rec, "the answer"))
        self.assertEqual(list(elsewhere.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_out_keeps_the_mode_of_the_file_it_replaces(self):
        target = self.work / "answer.md"
        target.write_text("old", encoding="utf-8")
        target.chmod(0o600)
        rec = {"dir": str(self.work), "out_copy": records.out_copy_path(str(target)),
               "out_copy_dir": records.out_copy_dir(str(target))}
        self.assertTrue(records.write_out_copy(rec, "new"))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_out_flag_refuses_a_directory_or_a_missing_parent(self):
        self.assertEqual(self.run_cli("run", str(self.brief), "--out", str(self.work)),
                         cli.EXIT_USAGE)
        self.assertEqual(self.run_cli("run", str(self.brief),
                                      "--out", str(self.work / "nope" / "x.md")),
                         cli.EXIT_USAGE)

    def test_a_worker_that_wrote_nothing_still_leaves_its_screen(self):
        self.stub.self_exits = True
        self.stub.write_output = False
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        self.assertTrue((Path(rec["dir"]) / "out.md").read_text().strip())

    def test_an_ambient_metered_key_warns_and_the_pane_still_blanks_it(self):
        """The launcher cannot always unset an ambient key, and the pane
        blanking with read-back is the layer that actually protects the worker,
        so the lane launches with a loud warning instead of refusing."""
        self.blank_metered_keys()
        os.environ["OPENAI_API_KEY"] = "sk-metered"
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("OPENAI_API_KEY", error)
        self.assertIn("warning", error)
        self.assertEqual(self.stub.workspaces[0]["env"]["OPENAI_API_KEY"], "")

    def test_logs_prints_the_status_log(self):
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.run_cli("logs", self.only_record()["id"]), cli.EXIT_OK)

    def test_lanes_prints_every_lane(self):
        code, output = self.capture_stdout("lanes")
        self.assertEqual(code, cli.EXIT_OK)
        for name in lanes.lane_names():
            self.assertIn(name, output)


# --------------------------------------------------------------------------
# Observation: watch, inspect, and the pane afterlife
# --------------------------------------------------------------------------


class TestProcessIdentifiers(unittest.TestCase):
    """A pid comes off a record a worker can write, and lands in `os.kill`."""

    # -1 is every process the operator may signal, 0 and anything below -1 is a
    # process group, 1 is init, and the rest coerce to one of those through
    # `int()`.
    NOT_PIDS = (-1, 0, "0", "-1", "4242", 1, True, 1.5, None, [4242])

    def test_a_selector_that_is_not_a_process_is_never_signalled(self):
        for value in self.NOT_PIDS:
            with self.subTest(value=value), \
                    patch.object(processes.os, "kill") as kill, \
                    patch.object(processes.os, "killpg") as killpg, \
                    patch.object(processes.subprocess, "run") as run:
                self.assertFalse(processes.pid_alive(value))
                self.assertFalse(processes.pid_is_zombie(value))
                processes.terminate_pid(value)
                processes.kill_pid(value)
                processes.stop_pid(value)
                self.assertFalse(processes.stop_process_group(value))
                self.assertIsNone(processes.process_cpu_percent(value))
                self.assertIsNone(processes.pids_cpu_percent([value]))
                kill.assert_not_called()
                killpg.assert_not_called()
                run.assert_not_called()

    def test_a_selector_that_is_not_a_process_descends_from_nothing(self):
        """`True` indexes the parent table at 1, whose descendants are signalled."""
        with patch.object(processes, "posix_process_parents",
                          return_value=[(1, 0), (4242, 1)]), \
                patch.object(processes, "windows_process_parents",
                             return_value=[(1, 0), (4242, 1)]):
            for value in self.NOT_PIDS:
                with self.subTest(value=value):
                    self.assertEqual(processes.descendant_pids([value]), [])


class TestRecordLocation(HerdrStubTestCase):
    """A worker can write its own run.json, `id` and `dir` included."""

    def write_record(self, run_id, rec):
        directory = records.runs_root() / run_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "run.json").write_text(json.dumps(rec), encoding="utf-8")
        return directory

    def test_a_record_is_read_back_under_the_id_it_was_found_under(self):
        directory = self.write_record(
            "mine-000000-aaaa", {"id": "theirs-000000-bbbb", "dir": str(self.work)})
        for rec in (records.load_record("mine-000000-aaaa"),
                    records.all_records()[0]):
            self.assertEqual(rec["id"], "mine-000000-aaaa")
            self.assertEqual(rec["dir"], str(directory))

    def test_a_heartbeat_cannot_be_aimed_at_another_directory(self):
        """The heartbeat republishes the copy on disk, whose `dir` moved."""
        directory = records.runs_root() / "mine-000000-aaaa"
        self.write_record("mine-000000-aaaa",
                          {"id": "mine-000000-aaaa", "dir": str(directory),
                           "state": "running"})
        rec = records.load_record("mine-000000-aaaa")
        elsewhere = self.work / "elsewhere"
        elsewhere.mkdir()
        self.write_record("mine-000000-aaaa",
                          {"id": "mine-000000-aaaa", "dir": str(elsewhere),
                           "state": "running"})

        records.save_heartbeat(rec)

        self.assertFalse((elsewhere / "run.json").exists())
        self.assertTrue(json.loads((directory / "run.json")
                                   .read_text(encoding="utf-8"))["heartbeat"])


class TestWindowsExecutableLookup(unittest.TestCase):
    """Windows resolves a bare executable name against the caller's current
    directory before any system or PATH directory, and dispatch's current
    directory is whichever checkout the operator aimed a run at."""

    def test_only_the_absolute_path_entries_are_searched(self):
        with patch.dict(os.environ, {"PATH": os.pathsep.join(
                ["", "tools", os.curdir, "/opt/bin"])}):
            self.assertEqual(processes.path_entries(), [os.getcwd(), "/opt/bin"])

    def test_a_bare_name_never_resolves_into_the_current_directory(self):
        searched = []

        def found_in_the_checkout(name, path=None):
            searched.append(path)
            return os.path.join(os.curdir, name)

        with patch.object(processes, "IS_WINDOWS", True), \
                patch.object(processes.shutil, "which", found_in_the_checkout), \
                patch.dict(os.environ, {"PATH": os.pathsep.join(["rel", "/opt/bin"])}):
            self.assertIsNone(processes.which_absolute("codex.exe"))
        self.assertEqual(searched, ["/opt/bin"])

    def test_the_watcher_interpreter_is_never_the_one_beside_the_brief(self):
        """A python.exe in the checkout would run the detached watcher, which is
        the operator's own process."""
        probed = []

        def probe(argv, **kwargs):
            probed.append(argv[0])
            return subprocess.CompletedProcess(argv, 0, "Python 3.11.0", "")

        with patch.object(processes, "IS_WINDOWS", True), \
                patch.object(processes.shutil, "which",
                             lambda name, path=None: os.path.join(os.curdir, name)), \
                patch.object(processes.subprocess, "run", probe), \
                patch("sys.executable", r"C:\WindowsApps\python.exe"), \
                patch.dict(os.environ, {"PATH": "/opt/bin"}), \
                self.assertRaises(errors.DispatchError):
            processes.resolve_python()
        self.assertEqual(probed, [])

    def test_a_force_kill_runs_the_system_taskkill(self):
        ran = []
        with patch.object(processes, "IS_WINDOWS", True), \
                patch.object(processes.subprocess, "run",
                             lambda argv, **kwargs: ran.append(argv)), \
                patch.dict(os.environ, {"SystemRoot": "/sysroot"}):
            processes.kill_pid(4242)
        self.assertEqual(ran[0][0],
                         os.path.join("/sysroot", "System32", "taskkill.exe"))


class TestRunArtifactOpens(HerdrStubTestCase):
    """One run's leaves must not stall or abort the bookkeeping every run shares."""

    def plant_record(self, run_id, body=None):
        directory = records.runs_root() / run_id
        directory.mkdir(parents=True, exist_ok=True)
        if body is not None:
            (directory / "run.json").write_text(body, encoding="utf-8")
        return directory

    @unittest.skipIf(processes.IS_WINDOWS, "no filesystem FIFOs on Windows")
    def test_a_pipe_in_place_of_a_record_is_refused_not_waited_on(self):
        """The sweep reads every record with the runs lock held, so one read
        that never returns stops every other run's reservation."""
        os.mkfifo(self.plant_record("fifo-000000-aaaa") / "run.json")
        self.plant_record("good-000000-bbbb", json.dumps({"state": "done"}))
        self.assertEqual([rec["id"] for rec in records.all_records()],
                         ["good-000000-bbbb"])

    def test_a_record_too_large_to_be_one_is_skipped(self):
        """Every command reads every record, so no run may size that read."""
        self.plant_record("huge-000000-aaaa",
                          json.dumps({"pad": "x" * records.RECORD_BYTE_LIMIT}))
        self.plant_record("good-000000-bbbb", json.dumps({"state": "done"}))
        self.assertEqual([rec["id"] for rec in records.all_records()],
                         ["good-000000-bbbb"])

    @unittest.skipIf(processes.IS_WINDOWS, "unprivileged symlinks are POSIX-only")
    def test_a_lock_file_that_is_a_link_is_not_opened_through(self):
        """Opening a dangling lock symlink for append creates its target."""
        lock = self.plant_record("link-000000-aaaa") / "owner.lock"
        outside = self.work / "not-a-lock"
        lock.symlink_to(outside)
        self.assertIsNone(records.lock_handle(lock, blocking=False))
        self.assertFalse(outside.exists())

    @unittest.skipIf(processes.IS_WINDOWS, "no FIFOs and no hard links to test")
    def test_an_append_refuses_a_pipe_or_a_second_link_to_another_file(self):
        """A FIFO blocks the append; a hard link makes it extend the other file."""
        directory = self.plant_record("logs-000000-aaaa")
        os.mkfifo(directory / "fifo.log")
        elsewhere = self.work / "operators.txt"
        elsewhere.write_text("theirs\n", encoding="utf-8")
        os.link(elsewhere, directory / "linked.log")
        # A FIFO with nobody reading it is refused by the open itself; the fstat
        # is what catches the rest. Either way the append never blocks.
        refused = (errors.DispatchError, OSError)
        for name in ("fifo.log", "linked.log"):
            with self.subTest(name=name):
                with self.assertRaises(refused):
                    records.append_status(directory / name, "HEARTBEAT")
                with self.assertRaises(refused):
                    records.open_append(directory / name)
        self.assertEqual(elsewhere.read_text(encoding="utf-8"), "theirs\n")

    def test_the_fallback_answer_is_a_bounded_tail_of_the_screen(self):
        """A worker with no deliverable sizes that read, and the screen is its
        own output."""
        directory = self.plant_record("tail-000000-aaaa",
                                      json.dumps({"state": "running"}))
        (directory / "screen.log").write_text("o" * 20000 + "the end\n",
                                              encoding="utf-8")
        rec = records.load_record("tail-000000-aaaa")
        with patch.object(Path, "read_text", side_effect=AssertionError(
                "the screen must not be read whole")):
            runner.finalize_output(rec)
        answer = (directory / "out.md").read_text(encoding="utf-8")
        self.assertTrue(answer.endswith("the end\n"))
        self.assertLessEqual(len(answer.encode("utf-8")), 8000)


class TestLogTail(unittest.TestCase):
    def read_tail(self, data, count, meaningful=False):
        reads = []

        class LogBytes(io.BytesIO):
            def read(self, size=-1):
                if size < 0:
                    raise AssertionError("tail reads must have a byte limit")
                result = super().read(size)
                reads.append(len(result))
                return result

        with patch.object(Path, "is_file", return_value=True), \
                patch.object(Path, "stat") as stat, \
                patch.object(Path, "read_text", side_effect=AssertionError(
                    "tail must not read the whole text file")), \
                patch.object(records, "open", create=True,
                             side_effect=lambda *args: LogBytes(data)):
            stat.return_value.st_size = len(data)
            reader = cli.non_heartbeat_tail if meaningful else cli.tail_lines
            result = reader(Path("status.log"), count)
        return result, reads

    def test_large_log_reads_only_a_bounded_suffix(self):
        data = b"old\n" * 250000 + b"first\nHEARTBEAT pulse\n\nlast\n"
        for meaningful, expected in ((False, ["", "last"]),
                                     (True, ["first", "last"])):
            with self.subTest(meaningful=meaningful):
                result, reads = self.read_tail(data, 2, meaningful)
                self.assertEqual(result, expected)
                self.assertEqual(reads, [4096])

    def test_expands_past_heartbeats_and_a_partial_line(self):
        data = (b"old\n" * 250000 + b"wanted\n" + b"HEARTBEAT pulse\n" * 500
                + b"last\n")
        result, reads = self.read_tail(data, 2, meaningful=True)
        self.assertEqual(result, ["wanted", "last"])
        self.assertEqual(reads, [4096, 8192])

    def test_long_multibyte_line_is_returned_whole(self):
        line = "\u20ac" * 2000
        result, reads = self.read_tail(b"old\n" * 250000
                                       + (line + "\nlast").encode(), 2)
        self.assertEqual(result, [line, "last"])
        self.assertEqual(reads, [4096, 8192])

    def test_fewer_meaningful_lines_expands_to_the_start(self):
        for prefix, expected in ((b"", []), (b"only\n", ["only"])):
            data = prefix + b"HEARTBEAT pulse\n" * 600
            result, reads = self.read_tail(data, 2, meaningful=True)
            self.assertEqual(result, expected)
            self.assertEqual(reads, [4096, 8192, len(data)])

    def test_short_empty_and_invalid_utf8_match_text_tail(self):
        for data in (b"", b"only", b"one\r\ntwo\rthree\n\n",
                     b"bad\xff\nHEARTBEAT pulse\n \nlast",
                     "a\u2028b\nc\n".encode()):
            for meaningful in (False, True):
                for count in (1, 8, 0, -1):
                    with self.subTest(data=data, meaningful=meaningful, count=count):
                        expected = data.decode("utf-8", "replace").splitlines()
                        if meaningful:
                            expected = [line for line in expected if line.strip()
                                        and not line.startswith("HEARTBEAT ")]
                        result, _ = self.read_tail(data, count, meaningful)
                        self.assertEqual(result, expected[-count:])

    def test_missing_file_has_no_tail(self):
        with patch.object(Path, "is_file", return_value=False):
            self.assertEqual(cli.tail_lines("missing", 2), [])
            self.assertEqual(cli.non_heartbeat_tail("missing", 2), [])

    def test_byte_limit_holds_when_file_grows_after_seek(self):
        class GrowingLog(io.BytesIO):
            def read(self, size=-1):
                position = self.tell()
                self.seek(0, os.SEEK_END)
                self.write(b"new\n" * 10000)
                self.seek(position)
                return super().read(size)

        with patch.object(records, "open", create=True,
                          return_value=GrowingLog(b"old\n" * 2000)):
            self.assertEqual(records.read_tail_bytes("status.log", 4096),
                             "old\n" * 1024)


class TestWatch(HerdrStubTestCase):
    def test_the_wall_lists_a_live_run_with_its_pane_and_agent(self):
        rec = self.unwatched_bg_run()
        code, output = self.capture_stdout("watch")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("RUN", output)
        self.assertIn(rec["id"], output)
        self.assertIn(rec["worker_id"], output)
        self.assertIn("working", output)

    def test_the_wall_shows_a_bg_runs_remaining_deadline(self):
        rec = self.unwatched_bg_run()
        rec["deadline_seconds"] = 1800
        rec["started_at"] = time.time() - 600
        records.save_record(rec)
        _, output = self.capture_stdout("watch")
        self.assertRegex(output, r"\b19m\d\ds\b")

    def test_a_finished_stamp_is_read_as_utc(self):
        """mktime would age every finished run by the timezone offset."""
        self.assertEqual(records.stamp_epoch("2026-08-17T00:00:00Z"), 1786924800)
        self.assertIsNone(records.stamp_epoch("not a stamp"))

    def test_the_wall_forgets_a_run_that_finished_long_ago(self):
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        rec["finished"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - cli.WATCH_RECENT_SECONDS - 60))
        rec["started_at"] = time.time() - cli.WATCH_RECENT_SECONDS - 120
        # Straight to the file: `save_record` keeps the ending already on disk,
        # `finished` included, so it cannot backdate one.
        (Path(rec["dir"]) / "run.json").write_text(json.dumps(rec), encoding="utf-8")
        _, output = self.capture_stdout("watch")
        self.assertNotIn(rec["id"], output)
        self.assertIn("no runs in the last", output)

    def test_the_wall_keeps_a_run_that_just_finished(self):
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        rec["finished"] = records.utc_now()
        records.save_record(rec)
        _, output = self.capture_stdout("watch")
        self.assertIn(rec["id"], output)
        self.assertIn("done", output)

    def test_watching_one_run_shows_its_log_and_its_screen(self):
        rec = self.unwatched_bg_run()
        self.stub.panes[rec["worker_id"]].screen += "the worker said this\n"
        code, output = self.capture_stdout("watch", rec["id"])
        self.assertEqual(code, cli.EXIT_FAILED)   # still running, not done
        self.assertIn("WORKER", output)
        self.assertIn(f"-- {rec['worker_id']} --", output)
        self.assertIn("the worker said this", output)

    def test_watching_takes_no_lock_and_never_drives_the_run(self):
        """Another process owns this run; a second authority over one pane is
        the race the whole verb exists to avoid."""
        rec = self.unwatched_bg_run()
        self.stub.finish_turn(rec["worker_id"])
        before = list(self.stub.calls)
        self.capture_stdout("watch", rec["id"])
        self.capture_stdout("watch")
        after = [m for m, _ in self.stub.calls[len(before):]]
        for method in ("pane.send_text", "pane.send_keys", "pane.report_agent",
                       "pane.close", "workspace.close"):
            self.assertNotIn(method, after)
        self.assertEqual(records.load_record(rec["id"])["state"], "running")
        self.assertFalse(records.lock_is_held(Path(rec["dir"]) / "watcher.lock"))

    def test_watching_a_finished_run_exits_with_its_state(self):
        self.stub.rc = 1
        self.run_cli("run", str(self.brief))
        rec_id = self.only_record()["id"]
        code, output = self.capture_stdout("watch", rec_id)
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("failed", output)


class TestWait(HerdrStubTestCase):
    """`dispatch wait`: the polling loop every supervising agent used to type."""

    def setUp(self):
        super().setUp()
        poll = cli.WAIT_POLL_SECONDS
        cli.WAIT_POLL_SECONDS = 0.02
        self.addCleanup(setattr, cli, "WAIT_POLL_SECONDS", poll)

    def logged_run(self, state="running", complete=True, lane="sol@medium"):
        """A run whose status.log reads the way a real wrapper leaves it."""
        rec = self.make_live_record(lane, state=state, finished=records.utc_now())
        log = Path(rec["dir"]) / "status.log"
        records.append_status(log, f"READY {records.utc_now()} agent idle and settled")
        records.append_status(log, f"HEARTBEAT {records.utc_now()} working")
        if complete:
            records.append_status(log, f"EXIT {records.utc_now()} rc=0")
            records.append_status(
                log, f"COMPLETE {records.utc_now()} state={state} "
                     f"out={Path(rec['dir']) / 'out.md'}")
        return records.load_record(rec["id"])

    def wait_cli(self, *argv, **kwargs):
        """`wait` blocks by design; a bug in what ends it must fail, not hang."""
        timeout = kwargs.pop("timeout", 20)
        result = {}

        def call():
            result["answer"] = self.capture_stdout(*argv)

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), f"wait never returned in {timeout}s")
        return result["answer"]

    def test_wait_returns_once_the_log_carries_complete(self):
        rec = self.logged_run("done")
        (Path(rec["dir"]) / "out.md").write_text(
            "".join(f"line {n}\n" for n in range(1, 41)), encoding="utf-8")
        code, output = self.wait_cli("wait", rec["id"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("COMPLETE", output)
        self.assertIn("line 1\n", output)
        self.assertIn("line 40\n", output)    # all of out.md, never a head of it
        self.assertIn("-- out.md (40 lines) --", output)

    def test_an_answer_cannot_drive_the_operators_terminal(self):
        """Escape sequences in out.md clear the screen over the result or load
        the clipboard. Shown as marks on a terminal, untouched when redirected."""
        hostile = "fine\x1b[2J\x1b]52;c;ZXZpbA==\x07\rgone\n"

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        with contextlib.redirect_stdout(Terminal()) as terminal:
            cli.show(hostile)
        shown = terminal.getvalue()
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\x07", shown)
        self.assertNotIn("\r", shown)
        self.assertTrue(shown.startswith("fine") and shown.endswith("gone\n"))
        with contextlib.redirect_stdout(io.StringIO()) as piped:
            cli.show(hostile)
        self.assertEqual(piped.getvalue(), hostile)

    def test_wait_prints_an_answer_that_has_no_trailing_newline(self):
        rec = self.logged_run("done")
        (Path(rec["dir"]) / "out.md").write_text("first\nthe last word",
                                                 encoding="utf-8")
        _, output = self.wait_cli("wait", rec["id"])
        self.assertIn("-- out.md (2 lines) --\nfirst\nthe last word\n", output)

    def test_wait_says_so_when_there_is_no_answer(self):
        """Missing, empty, and the runner's own copy of the last screen are each
        named, because any of them printed bare reads as the deliverable."""
        rec = self.logged_run("failed")
        out = Path(rec["dir"]) / "out.md"
        out.unlink(missing_ok=True)
        code, output = self.wait_cli("wait", rec["id"])
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("-- out.md: missing --", output)

        out.write_text("\n  \n", encoding="utf-8")
        _, output = self.wait_cli("wait", rec["id"])
        self.assertIn("-- out.md: empty --", output)

        out.write_text("error: not logged in\n", encoding="utf-8")
        rec["deliverable_written"] = False
        records.save_record(rec)
        _, output = self.wait_cli("wait", rec["id"])
        self.assertIn("-- out.md: no answer --", output)
        self.assertIn("ended failed without writing one", output)
        self.assertNotIn("-- out.md (", output)

    def test_wait_ends_with_the_tail_of_the_workers_own_cli_output(self):
        """The last 30 lines of what the CLI printed, errors included, from
        pane.log for a headless worker and screen.log for one in a pane."""
        rec = self.logged_run("failed")
        directory = Path(rec["dir"])
        (directory / "screen.log").write_text(
            "".join(f"screen {n}\n" for n in range(1, 51))
            + "error: stream disconnected", encoding="utf-8")
        code, output = self.wait_cli("wait", rec["id"])
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn(f"-- worker CLI output: last 30 lines of "
                      f"{directory / 'screen.log'} --", output)
        self.assertIn("screen 22\n", output)
        self.assertNotIn("screen 21\n", output)
        self.assertTrue(output.rstrip().endswith("error: stream disconnected"))

        (directory / "pane.log").write_text("Traceback\nrc 2\n", encoding="utf-8")
        _, output = self.wait_cli("wait", rec["id"])
        self.assertIn(f"-- worker CLI output: last 2 lines of "
                      f"{directory / 'pane.log'} --\nTraceback\nrc 2\n", output)

    def test_wait_never_passes_the_status_log_off_as_cli_output(self):
        rec = self.logged_run("failed")
        for name in ("pane.log", "screen.log"):
            (Path(rec["dir"]) / name).unlink(missing_ok=True)
        _, output = self.wait_cli("wait", rec["id"])
        self.assertIn("-- worker CLI output: none recorded --", output)
        self.assertIn("dispatch's journal", output)

    def test_a_complete_line_the_worker_wrote_does_not_end_a_wait(self):
        """status.log is a file the worker can write in. The record is what says
        a run is over."""
        rec = self.make_live_record()
        records.append_status(Path(rec["dir"]) / "status.log",
                              "COMPLETE 2026-01-01T00:00:00Z state=done out=x")
        code, output = self.wait_cli("wait", rec["id"], "--give-up", "1s")
        self.assertEqual(code, cli.EXIT_STILL_RUNNING)
        self.assertIn("still running", output)

    def test_wait_prints_the_lines_that_say_something(self):
        rec = self.logged_run("done")
        _, output = self.wait_cli("wait", rec["id"])
        self.assertIn("READY", output)
        self.assertNotIn("HEARTBEAT", output)

    def test_wait_exits_on_the_runs_own_state(self):
        """Same codes the rest of the CLI reports: `aborted` stays distinct from
        failure, so a caller can tell a refused brief from a broken worker."""
        for state, expected in (("done", cli.EXIT_OK),
                                ("failed", cli.EXIT_FAILED),
                                ("timeout", cli.EXIT_FAILED),
                                ("aborted", cli.EXIT_ABORTED)):
            with self.subTest(state=state):
                rec = self.logged_run(state)
                code, output = self.wait_cli("wait", rec["id"])
                self.assertEqual(code, expected)
                self.assertIn(state, output)

    def test_wait_returns_on_a_killed_run_that_never_got_a_complete_line(self):
        """Only a wrapper writes COMPLETE. A kill after the watcher is gone ends
        the run just as thoroughly and never writes one."""
        rec = self.logged_run("killed", complete=False)
        records.append_status(Path(rec["dir"]) / "status.log", "state: killed")
        code, output = self.wait_cli("wait", rec["id"])
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("killed", output)

    def end_the_run(self, rec):
        """End a live run the way its wrapper does: the record settled under the
        runs lock, and only then the COMPLETE line a waiter reads."""
        current = records.load_record(rec["id"])
        current["state"] = "done"
        records.save_final_record(current)
        records.append_status(Path(rec["dir"]) / "status.log",
                              f"COMPLETE {records.utc_now()} state=done")

    def test_wait_blocks_until_the_run_ends(self):
        rec = self.make_live_record()
        result = {}

        def call():
            result["code"] = cli.main(["wait", rec["id"]])

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            thread = threading.Thread(target=call, daemon=True)
            thread.start()
            time.sleep(0.2)
            self.assertTrue(thread.is_alive(), "wait returned on a live run")
            self.end_the_run(rec)
            thread.join(20)
        self.assertFalse(thread.is_alive(), "wait never returned")
        self.assertEqual(result["code"], cli.EXIT_OK)

    def test_several_waiters_on_one_run_are_harmless(self):
        rec = self.make_live_record()
        codes = []
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            threads = [threading.Thread(target=lambda: codes.append(
                cli.main(["wait", rec["id"]])), daemon=True) for _ in range(3)]
            for thread in threads:
                thread.start()
            time.sleep(0.2)
            self.end_the_run(rec)
            for thread in threads:
                thread.join(20)
        self.assertEqual(codes, [cli.EXIT_OK] * 3)
        self.assertFalse(records.lock_is_held(Path(rec["dir"]) / "watcher.lock"))

    def test_giving_up_leaves_the_run_byte_identical(self):
        """--give-up bounds the waiter, never the run: the record it walks away
        from has to be the record it found."""
        rec = self.logged_run("running", complete=False)
        record_path = Path(rec["dir"]) / "run.json"
        log_path = Path(rec["dir"]) / "status.log"
        before = (record_path.read_bytes(), log_path.read_bytes())
        calls = len(self.stub.calls)
        code, output = self.wait_cli("wait", rec["id"], "--give-up", "0")
        self.assertEqual(code, cli.EXIT_STILL_RUNNING)
        self.assertIn("still running", output)
        self.assertIn("running", output)
        self.assertEqual((record_path.read_bytes(), log_path.read_bytes()), before)
        # No socket traffic at all: a waiter opens the substrate the record
        # names, which needs no detection, and asks it nothing.
        self.assertEqual(self.stub.calls[calls:], [])

    def test_giving_up_has_its_own_exit_code(self):
        """A waiter's clock must never be readable as a worker's verdict."""
        self.assertEqual(cli.EXIT_STILL_RUNNING, 4)
        for code in (cli.EXIT_OK, cli.EXIT_FAILED, cli.EXIT_USAGE,
                     cli.EXIT_ABORTED):
            self.assertNotEqual(cli.EXIT_STILL_RUNNING, code)

    def test_a_give_up_that_is_not_a_duration_is_a_usage_error(self):
        rec = self.logged_run("done")
        code, err = self.capture_stderr("wait", rec["id"], "--give-up", "soon")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--give-up", err)

    def test_wait_settles_a_run_whose_watcher_died(self):
        """A dead watcher must not strand a finished worker until --give-up."""
        rec = self.unwatched_bg_run()
        self.stub.finish_turn(rec["worker_id"])
        code, output = self.wait_cli("wait", rec["id"], "--give-up", "10s")
        self.assertEqual(code, cli.EXIT_OK)
        settled = records.load_record(rec["id"])
        self.assertEqual(settled["state"], "done")
        self.assertFalse(records.lock_is_held(Path(rec["dir"]) / "watcher.lock"))

    def test_wait_on_an_unknown_run_is_a_usage_error(self):
        code, err = self.capture_stderr("wait", "no-such-run")
        self.assertEqual(code, cli.EXIT_USAGE)


class TestWatchDeep(HerdrStubTestCase):
    """`watch --deep`: one snapshot, including what the worker itself is doing."""

    TRANSCRIPT = [
        {"type": "assistant", "timestamp": "2026-08-19T18:24:07Z",
         "message": {"content": [{"type": "tool_use", "name": "Grep",
                                  "input": {"pattern": "checkin_verdict"}}]}},
        {"type": "user", "timestamp": "2026-08-19T18:24:08Z",
         "message": {"content": [{"type": "tool_result", "content": "3 matches"}]}},
        {"type": "assistant", "timestamp": "2026-08-19T18:24:11Z",
         "message": {"content": [{"type": "tool_use", "name": "Edit",
                                  "input": {"file_path": "/repo/bin/dispatch"}}]}},
        {"type": "assistant", "timestamp": "2026-08-19T18:24:19Z",
         "message": {"content": [{"type": "text",
                                  "text": "wiring the verb table now"}]}},
    ]

    def claude_run(self, state="running", entries=None):
        rec = self.make_live_record("opus@high", backend="claude", state=state,
                                    session_id="c0ffee00-1111-2222-3333-444444444444")
        transcript = Path(rec["dir"]) / "transcript.jsonl"
        transcript.write_text(
            "".join(json.dumps(e) + "\n"
                    for e in (self.TRANSCRIPT if entries is None else entries)),
            encoding="utf-8")
        rec["transcript"] = str(transcript)
        records.save_record(rec)
        log = Path(rec["dir"]) / "status.log"
        records.append_status(log, f"READY {records.utc_now()} agent idle and settled")
        records.append_status(log, f"HEARTBEAT {records.utc_now()} working")
        return records.load_record(rec["id"])

    def test_deep_reads_a_live_claude_workers_recent_activity(self):
        rec = self.claude_run()
        code, output = self.capture_stdout("watch", rec["id"], "--deep")
        self.assertEqual(code, cli.EXIT_FAILED)   # still running, not done
        self.assertIn(rec["id"], output)
        self.assertIn("check-ins 0", output)
        self.assertIn("READY", output)
        self.assertNotIn("HEARTBEAT", output)
        self.assertIn("tool Edit /repo/bin/dispatch", output)
        self.assertIn("said wiring the verb table now", output)
        self.assertLess(output.index("said wiring"), output.index("tool Grep"),
                        "newest first")

    def test_deep_on_a_finished_run_shows_the_head_of_out_md(self):
        rec = self.claude_run(state="done")
        (Path(rec["dir"]) / "out.md").write_text(
            "".join(f"line {n}\n" for n in range(1, 41)), encoding="utf-8")
        code, output = self.capture_stdout("watch", rec["id"], "--deep")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("line 1\n", output)
        self.assertNotIn("line 11", output)

    def test_deep_on_a_failed_run_exits_with_its_state(self):
        rec = self.claude_run(state="failed")
        code, output = self.capture_stdout("watch", rec["id"], "--deep")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("failed", output)

    def test_deep_never_writes_and_never_touches_the_pane(self):
        rec = self.claude_run()
        record_path = Path(rec["dir"]) / "run.json"
        log_path = Path(rec["dir"]) / "status.log"
        before = (record_path.read_bytes(), log_path.read_bytes())
        seen = len(self.stub.calls)
        self.capture_stdout("watch", rec["id"], "--deep")
        after = [method for method, _ in self.stub.calls[seen:]]
        for method in ("pane.send_text", "pane.send_keys", "pane.report_agent",
                       "pane.close", "workspace.close"):
            self.assertNotIn(method, after)
        self.assertEqual((record_path.read_bytes(), log_path.read_bytes()), before)
        self.assertFalse(records.lock_is_held(Path(rec["dir"]) / "watcher.lock"))

    def test_deep_says_so_when_the_transcript_is_not_readable_from_here(self):
        rec = self.make_live_record("sol@medium")
        _, output = self.capture_stdout("watch", rec["id"], "--deep")
        self.assertIn("no codex transcript on record yet", output)

    def test_deep_names_a_codex_rollout_it_will_not_parse(self):
        rec = self.make_live_record("sol@medium")
        rollout = Path(rec["dir"]) / "rollout.jsonl"
        rollout.write_text("{}\n", encoding="utf-8")
        rec["transcript"] = str(rollout)
        records.save_record(rec)
        _, output = self.capture_stdout("watch", rec["id"], "--deep")
        self.assertIn("not readable from here", output)
        self.assertIn(str(rollout), output)

    def test_deep_needs_a_run_and_refuses_the_verbs_it_is_not(self):
        rec = self.claude_run()
        for argv in (("watch", "--deep"),
                     ("watch", rec["id"], "--deep", "-f"),
                     ("watch", rec["id"], "--deep", "--attach")):
            with self.subTest(argv=argv):
                code, err = self.capture_stderr(*argv)
                self.assertEqual(code, cli.EXIT_USAGE)
                self.assertIn("--deep", err)

    def test_plain_watch_is_untouched_by_the_new_flag(self):
        rec = self.claude_run()
        _, output = self.capture_stdout("watch")
        self.assertIn(rec["id"], output)
        self.assertNotIn("worker activity", output)


class TestInspect(HerdrStubTestCase):
    def finished_run(self, lane="opus@high"):
        self.run_cli("run", lane, str(self.brief))
        return records.load_record(self.only_record()["id"])

    def test_inspect_reopens_the_session_without_typing_anything(self):
        rec = self.finished_run()
        code, output = self.capture_stdout("inspect", rec["id"])
        self.assertEqual(code, cli.EXIT_OK)
        stored = records.load_record(rec["id"])
        self.assertTrue(stored["inspect_worker"])
        self.assertIn(stored["inspect_worker"], output)
        self.assertIn("nothing was typed", output)
        # The lane's resume argv, and no prompt after it.
        argv = self.stub.started_argv(-1)
        self.assertEqual(argv[:2], ["claude", "--resume"])
        self.assertEqual(argv[2], rec["session_id"])
        self.assertNotIn("agent.prompt", self.stub.methods()[-3:])

    def test_inspect_takes_no_worker_slot(self):
        """It counts against caps only if it is actually prompted."""
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"])
        self.assertEqual(caps.live_records(), [])
        self.assertEqual(records.load_record(rec["id"])["state"], "done")

    def test_inspect_is_idempotent_while_its_pane_is_open(self):
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"])
        first = records.load_record(rec["id"])["inspect_worker"]
        starts = len(self.stub.starts)
        self.run_cli("inspect", rec["id"])
        self.assertEqual(records.load_record(rec["id"])["inspect_worker"], first)
        self.assertEqual(len(self.stub.starts), starts, "a second CLI was started")

    def test_inspect_refuses_a_run_with_no_session_to_reopen(self):
        rec = self.make_live_record(state="done", backend="codex", session_id="")
        code, error = self.capture_stderr("inspect", rec["id"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("no captured session id", error)

    def test_inspect_refuses_a_run_that_is_still_going(self):
        rec = self.unwatched_bg_run()
        code, error = self.capture_stderr("inspect", rec["id"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("still running", error)

    def test_close_takes_the_pane_down(self):
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"])
        pane = records.load_record(rec["id"])["inspect_worker"]
        code, output = self.capture_stdout("inspect", rec["id"], "--close")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn(("pane", pane), self.stub.closed)
        self.assertFalse(records.load_record(rec["id"])["inspect_worker"])
        self.assertIn("closed", output)

    def test_continue_reuses_the_inspected_pane_and_charges_the_slot(self):
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"])
        pane = records.load_record(rec["id"])["inspect_worker"]
        starts = len(self.stub.starts)
        self.assertEqual(self.run_cli("continue", rec["id"], "one more thing"),
                         cli.EXIT_OK)
        child = [r for r in records.all_records() if r.get("parent") == rec["id"]][0]
        self.assertEqual(child["worker_id"], pane, "continue opened a second pane")
        # herdr knows that agent by the name inspect registered.
        self.assertEqual(child["agent"],
                         records.load_record(rec["id"])["agent"])
        self.assertEqual(len(self.stub.starts), starts, "a second CLI was started")
        self.assertIn("ADOPTED", (Path(child["dir"]) / "status.log").read_text())
        # The turn is over, so the pane it borrowed is gone with it.
        self.assertIn(("pane", pane), self.stub.closed)
        self.assertFalse(records.load_record(rec["id"])["inspect_worker"])

    def test_a_run_with_no_open_inspect_pane_still_continues_normally(self):
        rec = self.finished_run()
        self.assertEqual(self.run_cli("continue", rec["id"], "again"),
                         cli.EXIT_OK)
        child = [r for r in records.all_records() if r.get("parent") == rec["id"]][0]
        self.assertNotEqual(child["worker_id"], rec["worker_id"])

    def test_status_shows_an_open_inspect_pane(self):
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"])
        _, output = self.capture_stdout("status")
        self.assertIn("inspect", output)


class TestPaneAfterlife(HerdrStubTestCase):
    """Per terminal transition: no state leaves a home open."""

    def assert_closed(self, rec):
        stored = records.load_record(rec["id"])
        self.assertIn(stored["state"], policy.policy().terminal_states)
        self.assertIn(("workspace", stored["worker_group"]), self.stub.closed,
                      f"{stored['state']} left its workspace open")
        return stored

    def test_done_closes_its_workspace(self):
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.assert_closed(self.only_record())["state"], "done")

    def test_failed_closes_its_workspace(self):
        self.stub.rc = 1
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.assert_closed(self.only_record())["state"], "failed")

    def test_aborted_closes_its_workspace(self):
        self.stub.reply = "ABORT: unclear contract"
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.assert_closed(self.only_record())["state"], "aborted")

    def test_killed_closes_its_workspace(self):
        rec = self.unwatched_bg_run()
        self.run_cli("kill", rec["id"])
        self.assertEqual(self.assert_closed(rec)["state"], "killed")

    def test_timeout_closes_its_workspace(self):
        rec = self.stuck_bg_run()
        for _ in range(2):
            runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
            self.freeze_checkin_screen(rec)
        self.assertEqual(self.assert_closed(rec)["state"], "timeout")

    def test_an_abandoned_launch_closes_its_workspace(self):
        self.stub.errors["agent.prompt"] = {"code": herdr.STALLED_CODE,
                                            "message": "no state change"}
        self.capture_stderr("run", str(self.brief))
        self.assertEqual(self.assert_closed(self.only_record())["state"], "failed")

    def test_orphaned_has_no_pane_left_to_close(self):
        """A run is only orphaned once its pane is gone; there is nothing to
        close, and claiming otherwise would hide the case below."""
        rec = self.unwatched_bg_run()
        self.stub.panes[rec["worker_id"]].closed = True
        rec["reserved_at"] = time.time() - caps.RESERVATION_GRACE_SECONDS - 1
        records.save_record(rec)
        caps.live_records(self.sweep())
        self.assertEqual(records.load_record(rec["id"])["state"], "orphaned")

    def test_reconcile_sweeps_a_pane_that_outlived_its_finished_run(self):
        """A watcher killed between journaling a state and tearing down leaves
        the one pane nothing else would ever close."""
        rec = self.unwatched_bg_run()
        rec["state"] = "done"
        rec["finished"] = records.utc_now()
        records.save_record(rec)
        self.assertFalse(self.stub.panes[rec["worker_id"]].closed)
        caps.live_records(self.sweep())
        self.assertTrue(self.stub.panes[rec["worker_id"]].closed)
        self.assertIn("WORKER-SWEPT", (Path(rec["dir"]) / "status.log").read_text())

    def test_the_sweep_leaves_an_inspect_pane_alone(self):
        """That pane belongs to a finished run on purpose."""
        self.run_cli("run", "opus@high", str(self.brief))
        rec = records.load_record(self.only_record()["id"])
        self.run_cli("inspect", rec["id"])
        pane = records.load_record(rec["id"])["inspect_worker"]
        caps.live_records(self.sweep())
        self.assertFalse(self.stub.panes[pane].closed)


class TestWorkerStartup(HerdrStubTestCase):
    """Naming the worker: what herdr will accept, and what a run must keep."""

    def test_the_agent_name_is_legal_for_herdr(self):
        """A run id is not a legal agent name: the `@` in a lane fails the
        whole start, and so does a leading digit or a name this long."""
        self.assertEqual(herdr.agent_name("sol@medium-101500-a1b2"),
                         "sol-medium-101500-a1b2")
        for run_id in ("sol@medium-101500-a1b2", "opus@high:fast-120000-ab12",
                       "@@@", "x" * 60, "9-starts-with-a-digit"):
            name = herdr.agent_name(run_id)
            self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,31}$", run_id)

    def test_the_name_keeps_what_makes_a_run_unique(self):
        """Truncation drops the lane, never the timestamp and suffix."""
        first = herdr.agent_name("sol@medium:fast-" + "z" * 20 + "-120000-aaaa")
        second = herdr.agent_name("sol@medium:fast-" + "z" * 20 + "-120000-bbbb")
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("120000-aaaa"))

    def test_agent_start_is_accepted_and_the_name_is_recorded(self):
        """herdr rejects an illegal name outright, which is a fallback for
        every run rather than the exception it is meant to be."""
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        self.assertNotIn("spawn_fallback", rec)
        self.assertEqual(self.stub.starts[0]["name"], rec["agent"])
        self.assertRegex(rec["agent"], r"^[a-z][a-z0-9_-]{0,31}$")

    def test_inspect_names_the_same_agent_as_the_run(self):
        self.run_cli("run", "opus@high", str(self.brief))
        rec = records.load_record(self.only_record()["id"])
        self.run_cli("inspect", rec["id"])
        self.assertEqual(self.stub.starts[-1]["name"], rec["agent"])

    def no_alive_probe(self):
        """Stop `verify_alive` polling long enough to absorb the fork delay.

        It polls for five seconds looking for CPU or a moving screen, which
        incidentally waits out a slow fork. That is not the guarantee under test
        here: nothing should be prompted until a worker actually exists.
        """
        original = runner.ALIVE_PROBE_SECONDS
        runner.ALIVE_PROBE_SECONDS = 0.0
        self.addCleanup(setattr, runner, "ALIVE_PROBE_SECONDS", original)

    def test_the_brief_is_not_delivered_before_the_worker_forks(self):
        """A cold CLI takes seconds to fork. Typing the brief into that window
        puts it at a shell prompt or into a TUI that is still drawing."""
        self.no_alive_probe()
        self.stub.child_delay_polls = 4
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        self.assertEqual(self.stub.prompts_while_starting, 0)
        self.assertEqual((Path(self.only_record()["dir"]) / "out.md").read_text(),
                         "final message")

    def test_the_fallback_waits_for_the_fork_too(self):
        """Nothing confirms a typed command, so that path waits by itself."""
        self.no_alive_probe()
        self.stub.errors["agent.start"] = {"code": "unsupported_agent_kind",
                                           "message": "no"}
        self.stub.child_delay_polls = 4
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        self.assertEqual(self.stub.prompts_while_starting, 0)

    def test_a_worker_that_never_appears_fails_the_run_loudly(self):
        self.stub.errors["agent.start"] = {"code": "unsupported_agent_kind",
                                           "message": "no"}
        self.stub.agent_comes_alive = False
        self.stub.child_delay_polls = 10_000
        code, error = self.capture_stderr("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("nothing started", error)
        rec = self.only_record()
        self.assertEqual(rec["state"], "failed")
        self.assertIn(("workspace", rec["worker_group"]), self.stub.closed)

    def test_a_cold_start_is_never_journaled_as_a_finished_run(self):
        """The exact first-real-run failure: no children yet read as exited, so
        the shell's own exit code was journaled as the worker's."""
        rec = self.make_live_record(backend="codex", lane="sol@medium",
                                    started_at=time.time())
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        self.assertIsNone(wrapper.poll(), "a run that never forked was finished")
        self.assertEqual(rec["liveness"]["verdict"], "starting")
        self.assertNotIn("dispatch-rc", "".join(self.stub.typed()))

    def test_gone_needs_the_worker_to_have_been_seen_alive(self):
        base = {"worker_id": "w1:p1", "at_prompt": True, "child_pids": (),
                "output_idle_seconds": 0.0, "cpu_percent": -1.0,
                "elapsed_seconds": 1.0}
        self.assertEqual(
            runner.free_signal_verdict(runner.LivenessSignals(**base)), "starting")
        self.assertEqual(
            runner.free_signal_verdict(
                runner.LivenessSignals(**dict(base, seen_alive=True))), "gone")
        # Or long enough that a cold start cannot still be pending.
        self.assertEqual(
            runner.free_signal_verdict(
                runner.LivenessSignals(**dict(
                    base, elapsed_seconds=runner.START_GRACE_SECONDS + 1))),
            "gone")

    def test_seeing_the_worker_alive_is_recorded_for_other_processes(self):
        """A reconciling process builds a fresh wrapper and looks once: the one
        look that cannot tell a cold start from a finished run."""
        self.stub.child_delay_polls = 2
        self.run_cli("run", str(self.brief), "--bg")
        rec = records.load_record(self.only_record()["id"])
        self.assertTrue(rec["seen_alive"])
        self.assertIn("ALIVE", (Path(rec["dir"]) / "status.log").read_text())
        for pid in [r.get("watcher_pid") for r in records.all_records()]:
            self.stop_watcher(pid)

    def test_the_exit_sequence_and_the_rc_probe_are_both_timestamped(self):
        """A post-mortem has to be able to order what dispatch typed."""
        self.run_cli("run", str(self.brief))
        log = (Path(self.only_record()["dir"]) / "status.log").read_text()
        for marker in ("PROMPT ", "EXIT-COMMAND ", "RC-PROBE ", "EXIT "):
            self.assertIn(marker, log)
        stamped = [line for line in log.splitlines()
                   if line.startswith(("PROMPT ", "EXIT-COMMAND ", "RC-PROBE "))]
        for line in stamped:
            self.assertRegex(line, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class TestAgentAddressing(HerdrStubTestCase):
    """Every agent call addresses the worker by name, link by link."""

    def test_agent_calls_target_the_name_not_the_pane(self):
        """herdr answers agent_not_found for a pane id, so addressing a pane
        refuses every prompt and drops the run to the typed fallback."""
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        self.assertNotIn("spawn_fallback", rec)
        targets = [p["target"] for p in self.stub.params_for("agent.prompt")]
        self.assertTrue(targets)
        for target in targets:
            self.assertEqual(target, rec["agent"])
            self.assertNotIn(":", target, "a pane id was used as an agent target")
        self.assertIn("via agent.prompt",
                      (Path(rec["dir"]) / "status.log").read_text())

    def test_the_status_and_explain_calls_use_the_name_too(self):
        self.run_cli("run", str(self.brief))
        name = self.only_record()["agent"]
        for method in ("agent.get", "agent.explain"):
            for params in self.stub.params_for(method):
                self.assertEqual(params["target"], name)

    def test_the_wall_asks_herdr_about_the_agent_by_name(self):
        rec = self.unwatched_bg_run()
        self.capture_stdout("watch")
        gets = [p["target"] for p in self.stub.params_for("agent.get")]
        self.assertIn(rec["agent"], gets)


class TestTrustDialog(HerdrStubTestCase):
    def test_tracked_codex_answers_trust_even_when_herdr_reports_idle(self):
        screen_of = runner.RunWrapper.screen
        trust = "Do you trust the contents of this directory?\n1. Yes, continue\n2. No, quit\nPress enter to continue"

        def screen(wrapper):
            if "1" not in self.stub.typed():
                return trust
            return screen_of(wrapper)

        with patch.object(runner.RunWrapper, "screen", screen):
            self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        self.assertIn("1", self.stub.typed())
        self.assertTrue(self.only_record().get("trust_approved"))
        self.assertEqual(len(self.stub.prompts), 1)

    def test_misclassified_codex_trust_stops_after_two_answers(self):
        rec = self.make_live_record()
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = rec["agent"]
        pane.running = True
        pane.status = "idle"
        wrapper = runner.RunWrapper(self.substrate(), rec)
        wrapper.attach()
        with patch.object(wrapper, "screen", return_value=
                          "Do you trust the contents of this directory?\n1. Yes, continue\n2. No, quit\nPress enter to continue"), \
                patch.object(runner, "TRUST_RECHECK_SECONDS", 0.01):
            self.assertFalse(wrapper.wait_until_ready(seconds=2, wait_for_hand=False))
        self.assertEqual(self.stub.typed(), ["1", "1"])
        self.assertEqual(rec["needs_hand"], runner.UNNAMED_TRUST_RULE)
        self.assertEqual(self.stub.prompts, [])

    def test_old_trust_text_does_not_answer_the_current_composer(self):
        rec = self.make_live_record()
        wrapper = runner.RunWrapper(self.substrate(), rec)
        wrapper.attach()
        old = ("Do you trust the contents of this directory?\n"
               "1. Yes, continue\n2. No, quit\nPress enter to continue\n")
        update = "Update available!\n1. Update now\n2. Skip\nPress enter to continue"
        for screen in ("Do you trust this command?", old + "› Ask anything", old + update):
            with self.subTest(screen=screen), patch.object(wrapper, "screen", return_value=screen):
                self.assertFalse(wrapper.screen_has_trust_prompt())
        self.assertEqual(self.stub.typed(), [])

    def test_the_trust_dialog_is_answered_and_the_run_proceeds(self):
        """codex opens a numbered dialog the config override does not prevent."""
        self.stub.trust_dialog = True
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(self.stub.trust_answers, [drivers.get_driver("codex").dialog_rules.answer_text])
        self.assertTrue(rec["trust_approved"])
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertIn("TRUST-APPROVED", log)
        self.assertIn("READY", log)
        self.assertEqual(rec["state"], "done")

    def test_nothing_is_typed_at_the_dialog_but_the_answer(self):
        """A line with a digit in it selects an option; ours named a run dir."""
        self.stub.trust_dialog = True
        self.run_cli("run", str(self.brief))
        self.assertEqual(self.stub.prompts_while_blocked, 0)
        self.assertEqual(self.stub.trust_answers, [drivers.get_driver("codex").dialog_rules.answer_text])

    def test_the_brief_waits_for_the_startup_turn_to_finish(self):
        """codex is idle, then blocked, then idle, then runs a startup turn.

        The first idle is not readiness; delivering there lands the brief in a
        TUI that is still setting itself up.
        """
        self.stub.trust_dialog = True
        self.run_cli("run", str(self.brief))
        pane = self.stub.panes[self.only_record()["worker_id"]]
        self.assertEqual(pane.startup_queue, [], "the startup turn never ran")
        self.assertEqual(self.stub.prompts_while_starting, 0)

    def test_the_fallback_answers_the_dialog_off_the_screen(self):
        """No agent record on that path, so herdr has no status to report."""
        self.stub.errors["agent.start"] = {"code": "unsupported_agent_kind",
                                           "message": "no"}
        original = herdr.HerdrSubstrate.read_screen
        stub = self.stub

        def screen(driver_self, pane_id, *args, **kwargs):
            text = original(driver_self, pane_id, *args, **kwargs)
            if drivers.get_driver("codex").dialog_rules.answer_text not in [t.strip() for t in stub.typed()]:
                # On screen until something answers it, as a real dialog is.
                return text + "\nDo you trust the contents of this directory?\n1. Yes, continue\n2. No, quit\nPress enter to continue\n"
            return text

        herdr.HerdrSubstrate.read_screen = screen
        self.addCleanup(setattr, herdr.HerdrSubstrate, "read_screen", original)
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("run", str(self.brief))
        rec = self.only_record()
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertIn("TRUST-APPROVED", log)
        self.assertIn("nothing tracks this worker", log)
        self.assertIn(drivers.get_driver("codex").dialog_rules.answer_text,
                      [t.strip() for t in self.stub.typed()])

    def test_any_other_blocked_rule_is_reported_and_left_alone(self):
        """dispatch answers the one dialog it can read, and no others."""
        rec = self.make_live_record(backend="codex", lane="sol@medium")
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = runner.run_agent_name(rec, self.substrate())
        pane.blocked = True
        original = herdr.HerdrSubstrate.blocked_rule
        herdr.HerdrSubstrate.blocked_rule = (
            lambda self, worker: "some_other_rule")
        self.addCleanup(setattr, herdr.HerdrSubstrate, "blocked_rule", original)
        self.assertFalse(wrapper.answer_blocking_dialog())
        self.assertEqual(self.stub.trust_answers, [])
        self.assertEqual(records.load_record(rec["id"])["blocked_rule"],
                         "some_other_rule")
        self.assertIn("does not answer dialogs it cannot read",
                      (Path(rec["dir"]) / "status.log").read_text())

    def test_a_brief_is_refused_while_the_agent_is_blocked(self):
        rec = self.make_live_record(backend="codex", lane="sol@medium")
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = runner.run_agent_name(rec, self.substrate())
        pane.blocked = True
        with self.assertRaises(herdr.SubstrateError) as caught:
            wrapper.prompt_worker("do the thing")
        self.assertIn("blocked on a dialog", str(caught.exception))
        self.assertEqual(self.stub.prompts_while_blocked, 0)

    def test_no_lane_still_passes_the_inert_trust_override(self):
        """It parses and does nothing; the dialog is answered where it appears."""
        for name in lanes.lane_names():
            for _, argv in cli.lane_expansions(name):
                self.assertFalse([a for a in argv if "trust_level" in a], name)


class TestHandBackDialog(HerdrStubTestCase):
    """Driver forms: a human answers them, except the one dispatch chose itself.

    herdr names the rule `live_blocked_form` for all of them (verified live,
    0.8.0), so the screen is what separates the trust dialog dispatch answers
    from every other form. The hand-back tests use the stub's `hand_dialog`,
    which nothing dispatch types can clear: only `answer_by_hand`, which is the
    point. The trust tests use `claude_trust_dialog`, which only a bare enter
    clears.
    """

    def blocked_bg_run(self, hand_dialog=True):
        """A `--bg` run standing at the dialog, with its watcher stopped.

        The dialog stands until the test answers it, and the watcher is stopped
        so the test is also the process that reconciles the run: what it does
        between those two is the whole hand-off. `hand_dialog=False` is for the
        tests that stand the run at the trust dialog instead.
        """
        self.stub.hand_dialog = hand_dialog
        self.stub.worker_polls = 10_000
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                self.run_cli("run", "opus@high", str(self.brief), "--bg"),
                cli.EXIT_OK)
        rec = records.load_record(self.only_record()["id"])
        self.stop_watcher(rec.get("watcher_pid"))
        deadline = time.time() + 30
        while time.time() < deadline and caps.run_is_watched(rec):
            time.sleep(0.05)
        return rec

    def test_the_rule_name_and_where_herdr_puts_it(self):
        """Both halves of the live probe, pinned: herdr 0.8.0 with claude in a
        fresh untrusted directory answers `matched_rule: {"id":
        "live_blocked_form"}` and has no `rule` key at all. Reading the name off
        the serialized body instead finds rules that were merely evaluated,
        which is how a dialog dispatch must not touch would come back as one it
        answers with a keystroke.
        """
        self.assertIn("live_blocked_form", drivers.get_driver("claude").dialog_rules.handback_rules)
        payload = {"state": "blocked",
                   "matched_rule": {"id": "live_blocked_form", "priority": 980,
                                    "state": "blocked"},
                   "evaluated_rules": [{"id": drivers.get_driver("codex").dialog_rules.answered_rules[0],
                                        "matched": False}]}
        original = herdr.HerdrSubstrate.explain_agent
        herdr.HerdrSubstrate.explain_agent = lambda self, agent: payload
        self.addCleanup(setattr, herdr.HerdrSubstrate, "explain_agent", original)
        self.assertEqual(herdr.HerdrSubstrate().blocked_rule("any"),
                         "live_blocked_form")

    def test_a_rule_that_was_only_evaluated_answers_nothing(self):
        """An explanation with no rule that fired still lists the ones that did
        not. Taking a name from there answers a dialog nobody matched, with the
        keystroke the rule that did not fire would have taken."""
        rec = self.make_live_record(backend="codex", lane="sol@medium")
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = runner.run_agent_name(rec, self.substrate())
        pane.blocked = True
        payload = {"state": "blocked",
                   "evaluated_rules": [
                       {"id": drivers.get_driver("codex").dialog_rules.answered_rules[0],
                        "matched": False}]}
        original = herdr.HerdrSubstrate.explain_agent
        herdr.HerdrSubstrate.explain_agent = lambda self, worker: payload
        self.addCleanup(setattr, herdr.HerdrSubstrate, "explain_agent", original)
        self.assertEqual(wrapper.blocked_rule(), "")
        self.assertFalse(wrapper.answer_blocking_dialog())
        self.assertEqual(self.stub.trust_answers, [])

    def test_a_foreground_run_waits_for_the_hand_and_then_finishes(self):
        """Somebody is at the keyboard, so it waits: no timeout, no answer."""
        self.stub.hand_dialog = True
        self.stub.hand_polls = 2       # answered on the second look
        code, errors = self.capture_stderr("run", "opus@high", str(self.brief))
        self.assertEqual(code, cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        self.assertEqual(self.stub.trust_answers, [], "dispatch answered it")
        self.assertEqual(self.stub.prompts_while_blocked, 0)
        self.assertIn(drivers.get_driver("claude").dialog_rules.handback_rules[0], errors)
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertEqual(log.count("NEEDS-HAND"), 1, log)
        self.assertIn("HAND-CLEARED", log)
        self.assertFalse(rec["needs_hand"], "still asking for a hand when done")

    def test_a_background_launch_returns_while_the_dialog_stands(self):
        """It must not hold the caller's shell open waiting on a human."""
        rec = self.blocked_bg_run()
        self.assertEqual(rec["needs_hand"], drivers.get_driver("claude").dialog_rules.handback_rules[0])
        self.assertFalse(rec.get("prompted"), "a blocked TUI was briefed")
        self.assertEqual(self.stub.prompts, [])
        self.assertEqual(self.stub.trust_answers, [])
        self.assertIn("NEEDS-HAND", (Path(rec["dir"]) / "status.log").read_text())

    def test_the_watcher_delivers_the_brief_once_he_answers(self):
        rec = self.blocked_bg_run()
        self.stub.answer_by_hand(self.stub.panes[rec["worker_id"]])
        self.stub.worker_polls = 2
        for _ in range(12):
            rec = runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
            if rec.get("state") in policy.policy().terminal_states:
                break
        self.assertEqual(rec["state"], "done")
        self.assertEqual(len(self.stub.prompts), 1, self.stub.prompts)
        self.assertTrue(rec["prompted"])
        self.assertFalse(rec["needs_hand"])

    def test_a_run_he_answers_with_no_exit_ends_instead_of_waiting(self):
        """Quitting is one of the dialog's own two options. The hand arrived; it
        just ended the run, and a pane held open for it would never come back."""
        rec = self.blocked_bg_run()
        pane = self.stub.panes[rec["worker_id"]]
        self.stub.exit_session(pane)
        pane.blocked = False        # a CLI that quit has no dialog left up
        settled = runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        self.assertIn(settled["state"], policy.policy().terminal_states)
        self.assertEqual(self.stub.prompts, [])

    def test_the_watcher_never_re_sends_a_brief_the_launcher_already_sent(self):
        """The dialog can come up after the brief went in; the record knows."""
        rec = self.unwatched_bg_run()
        self.stub.blocked_rule = drivers.get_driver("claude").dialog_rules.handback_rules[0]
        self.stub.panes[rec["worker_id"]].blocked = True
        for _ in range(3):
            runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        settled = records.load_record(rec["id"])
        self.assertEqual(settled["needs_hand"], drivers.get_driver("claude").dialog_rules.handback_rules[0])
        self.assertEqual(len(self.stub.prompts), 1, self.stub.prompts)

    def test_a_run_waiting_on_a_hand_is_never_killed_at_a_check_in(self):
        """The same pane the check-in ladder would shoot for a dialog nobody
        will answer; this one is a dialog somebody will."""
        rec = self.stuck_bg_run()
        self.stub.blocked_rule = drivers.get_driver("claude").dialog_rules.handback_rules[0]
        self.stub.panes[rec["worker_id"]].blocked = True
        for _ in range(4):
            runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
            self.freeze_checkin_screen(rec)
        settled = records.load_record(rec["id"])
        self.assertEqual(settled["state"], "running")
        self.assertNotIn(settled.get("checkin_verdict"),
                         runner.CHECKIN_KILL_VERDICTS)

    def test_status_and_the_wall_say_the_run_is_waiting_on_a_hand(self):
        rec = self.blocked_bg_run()
        _, status = self.capture_stdout("status")
        self.assertIn(f"NEEDS HAND: {drivers.get_driver('claude').dialog_rules.handback_rules[0]}", status)
        self.assertIn(rec["worker_id"], status)
        _, wall = self.capture_stdout("watch")
        self.assertIn(f"NEEDS HAND {drivers.get_driver('claude').dialog_rules.handback_rules[0]}", wall)

    # -- the one exemption: the trust dialog dispatch answers itself ------

    def test_only_the_trust_screen_authorises_an_answer(self):
        """The rule name cannot: the handback rule matches every form of this
        driver's, so a form that merely quotes one of the two markers is not the
        trust dialog, and a blind enter at it would accept something nobody read.
        """
        claude = drivers.get_driver("claude")
        self.assertTrue(runner.screen_is_trust_dialog(CLAUDE_TRUST_SCREEN, claude))
        self.assertFalse(runner.screen_is_trust_dialog(
            " Accessing workspace: /tmp/x\n Do you want to make this edit?\n"
            " 1. Yes\n Enter to confirm\n", claude))
        self.assertFalse(runner.screen_is_trust_dialog(
            " Bash(rm -rf ~/)\n 1. Yes, I trust this folder is fine\n"
            " Enter to confirm\n", claude))

    def test_the_trust_answer_walks_to_yes_wherever_the_cursor_starts(self):
        """One release opens on "Yes", another on "No, exit", where a bare enter
        quits the CLI. A screen with no cursor on it gets no keys at all."""
        rules = drivers.get_driver("claude").dialog_rules
        self.assertEqual(runner.trust_dialog_keys(CLAUDE_TRUST_SCREEN, rules),
                         ("enter",))
        no_first = (" Accessing workspace:\n /w\n \u276f No, exit\n"
                    "   Yes, I trust this folder\n Enter to confirm\n")
        self.assertEqual(runner.trust_dialog_keys(no_first, rules),
                         ("down", "enter"))
        self.assertEqual(runner.trust_dialog_keys(
            " Accessing workspace:\n Yes, I trust this folder\n", rules), ())

    def test_a_trust_screen_after_the_brief_went_in_is_handed_back(self):
        """Once the worker has the brief, the markers can be its own output
        sitting above a form that asks something else."""
        wrapper = runner.RunWrapper.__new__(runner.RunWrapper)
        wrapper.driver = drivers.get_driver("claude")
        wrapper.rec = {"id": "r", "prompted": "2026-01-01T00:00:00Z"}
        wrapper.screen = lambda: CLAUDE_TRUST_SCREEN
        wrapper._trust_attempts = 0
        handed, keys = [], []
        wrapper.note_needs_hand = handed.append
        wrapper.substrate = type("S", (), {"send_keys": lambda *a: keys.append(a)})()
        self.assertFalse(wrapper.handle_handback_form("live_blocked_form"))
        self.assertEqual((handed, keys), (["live_blocked_form"], []))

    def test_the_trust_dialog_is_answered_with_an_enter_and_the_run_finishes(self):
        """dispatch picked the directory with `--dir`, so the answer is yes and
        it is dispatch's to give: no hand, no wait, and the brief goes in."""
        self.stub.claude_trust_dialog = True
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        self.assertEqual(self.stub.trust_answers, ["enter"])
        self.assertEqual(self.stub.prompts_while_blocked, 0)
        self.assertTrue(rec["trust_approved"])
        self.assertFalse(rec.get("needs_hand"))
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertEqual(log.count("TRUST-APPROVED"), 1, log)
        self.assertNotIn("NEEDS-HAND", log)

    def test_the_trust_answer_goes_in_once_not_on_every_poll(self):
        """herdr keeps reporting blocked for a beat after the dialog clears.

        Answering again on that beat types an enter at whatever the CLI drew
        next, which is the mistake the codex path already paid for.
        """
        self.stub.claude_trust_dialog = True
        self.stub.trust_lag_polls = 3
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        self.assertEqual(self.stub.trust_answers, ["enter"],
                         self.stub.trust_answers)
        self.assertEqual(len(self.stub.prompts), 1, self.stub.prompts)

    def test_readiness_settles_on_the_done_the_answered_dialog_leaves_behind(self):
        """Answering the dialog was never the hard part: settling after it was.

        herdr reports a claude that has just been trusted as `done`, not
        `idle`, and it stays `done` until something moves. Readiness that holds
        out for `idle` waits out its whole 120s window and the run is abandoned
        with the brief undelivered, which is what every fresh directory did.
        """
        self.stub.claude_trust_dialog = True
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertNotIn("ABANDONED", log)
        stages = [line.split()[0] for line in log.splitlines()
                  if line.split()[:1] and line.split()[0] in
                  ("TRUST-APPROVED", "READY", "PROMPT")]
        self.assertEqual(stages, ["TRUST-APPROVED", "READY", "PROMPT"], log)
        self.assertEqual(len(self.stub.prompts), 1, self.stub.prompts)

    def test_the_brief_is_not_typed_at_a_dialog_the_status_has_not_caught_up_to(self):
        """herdr's agent status lags its own screen rules by seconds.

        Probed live: the trust dialog is drawn and matched by `agent explain`
        at 1.2s while `agent get` still answers `idle`, and only turns
        `blocked` at 4.9s. Readiness that believes the status alone counts
        those seconds as settled and hands the brief to a numbered dialog,
        where the enter after it picks an option.
        """
        self.stub.claude_trust_dialog = True
        self.stub.trust_lead_polls = 40
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        self.assertEqual(self.stub.prompts_while_blocked, 0, self.stub.prompts)
        self.assertEqual(self.stub.trust_answers, ["enter"])
        log = (Path(self.only_record()["dir"]) / "status.log").read_text()
        self.assertLess(log.index("TRUST-APPROVED"), log.index("READY"), log)

    def test_the_done_a_brief_is_handed_to_does_not_end_its_turn(self):
        """The `done` readiness settled on is still there when the brief lands.

        herdr holds it for about a second after the prompt goes in, so a turn
        judged on the word alone is over before the worker has read anything,
        and the /exit that follows takes its work with it.
        """
        rec = self.make_live_record(backend="claude", lane="opus@high")
        wrapper = self.wrapper_for(rec)
        rec["prompt_state_seq"] = 7
        self.assertFalse(wrapper.agent_turn_is_over("done", 7))
        self.assertTrue(wrapper.agent_turn_is_over("done", 8))
        # No counter to compare (an older record, or a herdr that reports none):
        # the old answer, so a finished worker cannot be left holding its pane.
        rec["prompt_state_seq"] = None
        self.assertTrue(wrapper.agent_turn_is_over("done", 7))

    def test_a_trusted_claude_is_not_exited_before_it_reads_its_brief(self):
        self.stub.claude_trust_dialog = True
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        self.assertEqual(self.stub.exits_mid_turn, 0)
        self.assertEqual(self.stub.prompts,
                         [(Path(self.only_record()["dir"]) / "prompt.txt")
                          .read_text(encoding="utf-8")])

    def wrapper_for(self, rec):
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        return wrapper

    def test_a_trust_dialog_that_will_not_clear_goes_back_to_his_hand(self):
        """Two enters and it is still up: dispatch is reading the screen wrong,
        and the answer to that is to stop typing, not to keep going."""
        original = runner.TRUST_RECHECK_SECONDS
        runner.TRUST_RECHECK_SECONDS = 0.05
        self.addCleanup(setattr, runner, "TRUST_RECHECK_SECONDS", original)
        self.stub.claude_trust_dialog = True
        self.stub.trust_dialog_sticks = True
        rec = self.blocked_bg_run(hand_dialog=False)
        self.assertEqual(rec["needs_hand"], drivers.get_driver("claude").dialog_rules.handback_rules[0])
        self.assertEqual(self.stub.trust_answers,
                         ["enter"] * drivers.get_driver("claude").dialog_rules.trust_attempts,
                         self.stub.trust_answers)
        self.assertEqual(self.stub.prompts, [], "a blocked TUI was briefed")
        self.assertIn("NEEDS-HAND", (Path(rec["dir"]) / "status.log").read_text())


class TestResumeIntoALiveTurn(HerdrStubTestCase):
    """A resume that lands on a session still working waits for it.

    `dispatch continue` relaunches with `--resume`, and a session left mid-turn
    picks that turn straight back up, so the CLI is genuinely working at launch.
    Readiness read that as not-ready, timed out, and `abandon` closed the pane on
    a healthy worker. Resumes wait on working the way
    they wait on a hand-back.
    """

    def parent_run(self, lane="opus@high"):
        """A finished run whose session a continue can be pointed at."""
        self.assertEqual(self.run_cli("run", lane, str(self.brief)), cli.EXIT_OK)
        return self.only_record()["id"]

    def child_of(self, parent):
        children = [r for r in records.all_records() if r.get("parent") == parent]
        self.assertEqual(len(children), 1, children)
        return records.load_record(children[0]["id"])

    def deliveries_to(self, child):
        """Prompts naming this turn's own brief: what was delivered, and how often."""
        needle = str(Path(child["dir"]) / "brief.md")
        return [text for text in self.stub.prompts if needle in text]

    def busy_bg_continue(self, parent, message="again"):
        """A `--bg` continue into a session whose turn never ends, unwatched.

        The watcher is stopped so the test is the process that reconciles it,
        which is what makes the delivery observable one look at a time.
        """
        self.stub.resume_turn_polls = 10_000     # the resumed turn runs on
        self.assertEqual(self.run_cli("continue", parent, message, "--bg"),
                         cli.EXIT_OK)
        child = self.child_of(parent)
        self.stop_watcher(child.get("watcher_pid"))
        deadline = time.time() + 30
        while time.time() < deadline and caps.run_is_watched(child):
            time.sleep(0.05)
        self.assertFalse(caps.run_is_watched(child), "the watcher would not die")
        return child

    def test_a_continue_into_a_mid_turn_session_waits_instead_of_being_abandoned(self):
        """The same rule from the other side: busy at launch is not a failed
        spawn, so the continue waits the turn out instead of giving up."""
        parent = self.parent_run()
        self.stub.resume_turn_polls = 6     # still finishing when the pane opens
        self.assertEqual(self.run_cli("continue", parent, "one more thing"),
                         cli.EXIT_OK)
        child = self.child_of(parent)
        self.assertEqual(child["state"], "done")
        self.assertTrue(child["resume_busy"], "the launcher never saw it working")
        log = (Path(child["dir"]) / "status.log").read_text()
        self.assertIn("RESUME-BUSY", log)
        self.assertNotIn("ABANDONED", log)
        self.assertNotEqual(child.get("closed_by"), "abandon")

    def test_the_message_goes_in_once_after_the_resumed_turn_ends(self):
        parent = self.parent_run()
        self.stub.resume_turn_polls = 6
        self.assertEqual(self.run_cli("continue", parent, "one more thing"),
                         cli.EXIT_OK)
        child = self.child_of(parent)
        self.assertTrue(child["prompted"])
        self.assertEqual(len(self.deliveries_to(child)), 1, self.stub.prompts)
        self.assertEqual(self.stub.exits_mid_turn, 0,
                         "the session was exited before the message went in")

    def test_a_background_continue_returns_while_the_session_is_still_busy(self):
        """It must not hold the caller's shell open for somebody else's turn."""
        parent = self.parent_run()
        child = self.busy_bg_continue(parent)
        self.assertTrue(child["resume_busy"])
        self.assertFalse(child.get("prompted"), "a working session was briefed")
        self.assertEqual(self.deliveries_to(child), [])

    def test_the_watcher_delivers_once_the_resumed_turn_ends(self):
        parent = self.parent_run()
        child = self.busy_bg_continue(parent)
        self.stub.panes[child["worker_id"]].startup_queue = []   # the turn ends
        for _ in range(12):
            child = runner.reconcile_run(records.load_record(child["id"]), self.substrate())
            if child.get("state") in policy.policy().terminal_states:
                break
        self.assertEqual(child["state"], "done")
        self.assertTrue(child["prompted"])
        self.assertEqual(len(self.deliveries_to(child)), 1, self.stub.prompts)

    def test_a_resume_that_never_finishes_its_turn_is_ruled_by_check_ins(self):
        """Not an eternal silent wait: a resume is checked on from launch, so a
        session that stops being a working session is ruled stuck and killed."""
        parent = self.parent_run()
        child = self.busy_bg_continue(parent)
        pane = self.stub.panes[child["worker_id"]]
        pane.startup_queue = []
        pane.status = "unknown"        # herdr stopped calling it working
        child["deadline_seconds"] = 1
        child["started_at"] = time.time() - 30
        records.save_record(child)
        runner.reconcile_run(records.load_record(child["id"]), self.substrate())
        self.freeze_checkin_screen(child)
        for _ in range(4):
            child = runner.reconcile_run(records.load_record(child["id"]), self.substrate())
            if child.get("state") in policy.policy().terminal_states:
                break
        self.assertEqual(child["state"], "timeout")
        self.assertEqual(child["checkin_verdict"], "stuck")
        self.assertFalse(child.get("prompted"), "a hung session was briefed")

    def test_a_fresh_run_that_launches_working_is_left_alone(self):
        """Only the resume path changed: nothing stamps `resume_busy` on a run
        that has no session to pick back up."""
        self.stub.resume_turn_polls = 4
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        self.assertFalse(rec.get("resume_busy"))
        self.assertTrue(rec["prompted"])


class TestDeliverableHonesty(HerdrStubTestCase):
    def test_a_clean_exit_with_no_deliverable_is_a_failure(self):
        """A CLI that quits at its opening dialog exits 0 too."""
        self.stub.self_exits = True
        self.stub.write_output = False
        code = self.run_cli("run", str(self.brief))
        self.assertEqual(code, cli.EXIT_FAILED)
        rec = self.only_record()
        self.assertEqual(rec["rc"], 0)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("no out.md", rec["error"])
        self.assertIn("NO-DELIVERABLE", (Path(rec["dir"]) / "status.log").read_text())

    def test_the_screen_is_still_kept_for_the_post_mortem(self):
        self.stub.self_exits = True
        self.stub.write_output = False
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        self.assertTrue((Path(rec["dir"]) / "out.md").read_text().strip())
        self.assertTrue((Path(rec["dir"]) / "screen.log").read_text().strip())

    def test_a_schema_run_is_judged_on_its_json(self):
        schema = self.work / "s.json"
        schema.write_text('{"type":"object"}', encoding="utf-8")
        self.stub.reply = '{"ok": 1}'
        code = self.run_cli("run", str(self.brief), "--schema", str(schema))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.only_record()["state"], "done")

    def test_a_worker_that_wrote_its_answer_is_still_done(self):
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        self.assertEqual(self.only_record()["state"], "done")

    def test_no_state_is_reported_while_the_worker_is_live(self):
        original = records.HEARTBEAT_SECONDS
        records.HEARTBEAT_SECONDS = 0          # beat on every poll
        self.addCleanup(setattr, records, "HEARTBEAT_SECONDS", original)
        self.stub.worker_polls = 4
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        self.assertIn("HEARTBEAT", (Path(rec["dir"]) / "status.log").read_text())
        self.assertEqual(len(self.stub.reports), 1,
                         "state was reported before the worker exited")

    def test_herdr_still_knows_the_agent_all_through_the_turn(self):
        """The eviction is why every run fell to typing: one report at the start
        and agent.prompt, agent.get, and agent.explain all stop answering."""
        self.stub.worker_polls = 4
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        prompts = [p["target"] for p in self.stub.params_for("agent.prompt")]
        self.assertEqual(prompts, [rec["agent"]])
        self.assertIn("via agent.prompt",
                      (Path(rec["dir"]) / "status.log").read_text())
        self.assertEqual(self.stub.evicted, [rec["agent"]],
                         "the agent was evicted somewhere other than the ending")

    def test_the_ending_is_reported_once_the_worker_has_gone(self):
        self.run_cli("run", str(self.brief))
        report = self.stub.reports[-1]
        self.assertEqual(report["source"], "dispatch")
        self.assertEqual(report["agent"], self.only_record()["agent"])
        self.assertIn("done", report["message"])

    def test_a_failed_run_says_so_on_the_wall(self):
        self.stub.rc = 1
        self.run_cli("run", str(self.brief))
        self.assertIn("failed", self.stub.reports[-1]["message"])

    def test_the_session_id_report_does_not_evict(self):
        """It is the one agent write that leaves detection intact, which is why
        session capture can still happen through herdr."""
        self.run_cli("run", "opus@high", str(self.brief))
        rec = self.only_record()
        session = self.stub.params_for("pane.report_agent_session")
        self.assertTrue(session)
        self.assertEqual(session[0]["agent"], rec["agent"])


class TestTuiSubmission(HerdrStubTestCase):
    def fallback(self):
        self.stub.errors["agent.start"] = {"code": "unsupported_agent_kind",
                                           "message": "no"}
        self.stub.errors["agent.prompt"] = {"code": "agent_not_found",
                                            "message": "no agent"}

    def test_the_text_is_on_screen_before_enter_is_pressed(self):
        """Back-to-back text and enter left the brief sitting in the input box
        for eight minutes, looking exactly like a worker that was thinking."""
        self.fallback()
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("run", str(self.brief))
        # The TUI line specifically: the env exports before it are shell typing.
        calls = [(m, p) for m, p in self.stub.calls
                 if m in ("pane.send_text", "pane.read", "pane.send_keys")]
        typed = [i for i, (m, p) in enumerate(calls)
                 if m == "pane.send_text" and "prompt.txt" in p.get("text", "")][0]
        after = [m for m, _ in calls[typed + 1:]]
        self.assertIn("pane.read", after[:after.index("pane.send_keys")],
                      "enter followed the text with no look at the screen")

    def test_an_eaten_enter_is_retried_and_logged(self):
        self.fallback()
        self.stub.tui_swallows_first_enter = True
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        self.assertIn("SUBMIT-RETRY", (Path(rec["dir"]) / "status.log").read_text())
        self.assertEqual((Path(rec["dir"]) / "out.md").read_text(), "final message")

    def test_a_submitted_line_is_not_pressed_twice(self):
        self.fallback()
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("run", str(self.brief))
        self.assertNotIn("SUBMIT-RETRY",
                         (Path(self.only_record()["dir"]) / "status.log").read_text())

    def test_the_exit_command_waits_for_its_text_to_render(self):
        """`/quit` is typed at a TUI too. Back-to-back it sat in codex's input
        box while dispatch heartbeated `working` at it for five minutes."""
        self.run_cli("run", str(self.brief))
        calls = [(m, p) for m, p in self.stub.calls
                 if m in ("pane.send_text", "pane.read", "pane.send_keys")]
        typed = [i for i, (m, p) in enumerate(calls)
                 if m == "pane.send_text" and p.get("text") == "/quit"][0]
        after = [m for m, _ in calls[typed + 1:]]
        self.assertIn("pane.read", after[:after.index("pane.send_keys")],
                      "the exit command's enter followed its text blind")

    def quick_escalation(self):
        original = runner.EXIT_RETRY_SECONDS
        runner.EXIT_RETRY_SECONDS = 0.05
        self.addCleanup(setattr, runner, "EXIT_RETRY_SECONDS", original)

    def test_an_exit_command_that_never_landed_is_sent_again(self):
        """A CLI that is quitting and one that never heard look the same."""
        self.quick_escalation()
        self.stub.swallow_enters = 2          # the first attempt goes nowhere
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertIn("EXIT-RETRY", log)
        self.assertIn("attempt 2", log)
        self.assertEqual(rec["state"], "done")

    def test_a_session_that_will_not_leave_is_killed(self):
        """Eight minutes of heartbeating at an unsubmitted `/quit` is not a plan."""
        self.quick_escalation()
        self.stub.swallow_enters = 100        # nothing will ever submit
        code = self.run_cli("run", str(self.brief))
        rec = self.only_record()
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertIn("EXIT-RETRY", log)
        self.assertIn("EXIT-FORCED", log)
        self.assertTrue(rec["exit_forced"])
        self.assertIn("still up", rec["exit_forced_reason"])
        # The work was done and on disk, so the journal says done, not timeout.
        self.assertEqual(rec["state"], "done")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIsNone(rec["rc"])
        self.assertIn("FORCED-DONE", log)

    def test_a_forced_exit_with_no_deliverable_is_not_called_done(self):
        """The deliverable is what authorises the exit now, so the only way to
        reach a forced exit without one is for the file to go away underneath
        it. `done` must never rest on a file that is not there."""
        self.quick_escalation()
        self.stub.swallow_enters = 100
        rec = self.make_live_record(lane="opus@high", backend="claude")
        out = Path(rec["dir"]) / "out.md"
        out.write_text("the answer", encoding="utf-8")
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = runner.run_agent_name(rec, self.substrate())
        pane.running = True
        pane.status = "done"

        def vanish(signals, record):
            if record.get("exit_command"):
                with contextlib.suppress(OSError):
                    out.unlink()

        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec,
                                           judge_hook=vanish)
        wrapper.attach()
        finished = wrapper.watch(poll_seconds=0.01)
        self.assertTrue(finished["exit_forced"])
        self.assertEqual(finished["state"], "failed")

    def test_the_escalation_beats_the_deadline_to_the_record(self):
        """Both paths write a terminal state; only one of them may."""
        self.quick_escalation()
        self.stub.swallow_enters = 100
        rec = self.make_live_record(backend="codex", lane="sol@medium",
                                    started_at=time.time() - 3600)
        (Path(rec["dir"]) / "out.md").write_text("the answer", encoding="utf-8")
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        pane = self.stub.panes[rec["worker_id"]]
        pane.agent_name = runner.run_agent_name(rec, self.substrate())
        pane.running = True
        pane.turn_polls = 10_000
        pane.status = "done"
        # The deliverable is already settled, so the second poll sends the exit
        # command and the ladder owns the ending from there.
        out = Path(rec["dir"]) / "out.md"
        rec["deliverable_seen"] = [out.stat().st_size, out.stat().st_mtime]
        rec["deliverable_since"] = time.time() - 60
        finished = wrapper.watch(deadline_seconds=1, poll_seconds=0.01)
        self.assertEqual(finished["state"], "done", "the deadline won the race")
        self.assertTrue(finished["exit_forced"])
        # The settledness gate costs this run one look, so a deadline this far
        # past does get to check in once. It may extend the run; it may never
        # end it, because the ladder above already owns the ending.
        self.assertNotIn(finished.get("checkin_verdict"),
                         runner.CHECKIN_KILL_VERDICTS)

    def test_resending_the_exit_command_is_bounded(self):
        self.assertEqual(runner.EXIT_MAX_ATTEMPTS, 2)

    def test_shell_typing_still_goes_in_back_to_back(self):
        """The rc probe and the env exports talk to a shell, not a TUI."""
        self.run_cli("run", str(self.brief))
        self.assertTrue([t for t in self.stub.typed()
                         if herdr.PANE_SHELL.parse_line(t)[0] == "rc-probe"])
        self.assertNotIn("SUBMIT-RETRY",
                         (Path(self.only_record()["dir"]) / "status.log").read_text())


class TestAcceptanceRound1(HerdrStubTestCase):
    """The three blockers real workers found on the first end-to-end pass."""

    # -- A: the trust dialog, answered once -----------------------------

    def test_the_trust_dialog_is_answered_once_not_on_every_poll(self):
        """herdr keeps saying blocked for a beat while the TUI redraws.

        Answering on every poll put two more `1`s into codex's composer as a
        message; it replied "What does 1 refer to?", went to done, and never
        came back to idle, so the readiness gate timed out with the brief
        undelivered. Every tester's first run in a fresh directory died here.
        """
        self.stub.trust_dialog = True
        self.stub.trust_lag_polls = 3
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        self.assertEqual(self.stub.trust_answers,
                         [drivers.get_driver("codex").dialog_rules.answer_text], self.stub.trust_answers)
        self.assertEqual(self.stub.composer_messages, [],
                         "an extra answer landed in the composer as a message")
        log = (Path(self.only_record()["dir"]) / "status.log").read_text()
        self.assertEqual(log.count("TRUST-APPROVED"), 1)

    def test_a_settled_tui_is_ready_whether_it_says_idle_or_done(self):
        """Every lane says `done` at a prompt box with no turn behind it.

        codex says it at its bare prompt; claude says it from the moment its
        trust dialog is answered, and never says `idle` again.
        """
        self.assertEqual(runner.READY_STATES, ("idle", "done"))

    def test_a_codex_pane_that_only_says_done_still_gets_its_brief(self):
        self.stub.trust_dialog = True
        self.stub.ready_status = "done"
        self.assertEqual(self.run_cli("run", str(self.brief)), cli.EXIT_OK)
        rec = self.only_record()
        self.assertIn("READY", (Path(rec["dir"]) / "status.log").read_text())
        self.assertTrue(self.stub.prompts)

    def test_an_abandoned_run_keeps_its_screen(self):
        """The one run whose pane nobody will ever see again."""
        self.stub.errors["agent.prompt"] = {"code": herdr.STALLED_CODE,
                                            "message": "no state change"}
        self.capture_stderr("run", str(self.brief))
        rec = self.only_record()
        self.assertEqual(rec["state"], "failed")
        self.assertTrue((Path(rec["dir"]) / "screen.log").read_text().strip(),
                        "an abandoned run kept no screen to diagnose from")

    # -- B: prompting is not a turn ------------------------------------

    def test_the_prompt_is_delivered_without_waiting_for_the_turn(self):
        """With wait, the call returns only when the turn ends: a --bg launch
        blocked two minutes and a 75s deadline was spent before it started."""
        self.run_cli("run", str(self.brief))
        params = self.stub.params_for("agent.prompt")
        self.assertTrue(params)
        for call in params:
            self.assertNotIn("wait", call, "agent.prompt blocked on the turn")

    def test_a_refused_prompt_is_not_re_sent_when_it_clearly_landed(self):
        """The timeout came back on turns that had the brief and were busy."""
        self.stub.errors["agent.prompt"] = {"code": "timeout",
                                            "message": "timed out"}
        self.stub.status_when_prompted = "working"
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("run", str(self.brief))
        rec = self.only_record()
        log = (Path(rec["dir"]) / "status.log").read_text()
        self.assertIn("not re-sending", log)
        self.assertNotIn("typing it", log)
        self.assertEqual([t for t in self.stub.typed() if "prompt.txt" in t], [])

    def test_the_prompt_is_logged_when_it_is_sent(self):
        """The line used to land forty seconds late, on return."""
        self.stub.hold = 0.3
        self.run_cli("run", str(self.brief))
        log = (Path(self.only_record()["dir"]) / "status.log").read_text()
        prompt_line = [ln for ln in log.splitlines() if ln.startswith("PROMPT ")][0]
        heartbeat = [ln for ln in log.splitlines() if ln.startswith("HEARTBEAT ")]
        self.assertTrue(heartbeat)
        self.assertLessEqual(prompt_line.split()[1], heartbeat[0].split()[1])

    # -- C: a turn can end without herdr ever saying done ----------------

    def test_a_claude_turn_ends_on_a_settled_idle_with_its_deliverable(self):
        """Claude Code leaves a prompt suggestion in the box, so herdr's
        live_prompt_box rule says idle and `done` never comes."""
        original = runner.TURN_SETTLE_SECONDS
        runner.TURN_SETTLE_SECONDS = 0.05
        self.addCleanup(setattr, runner, "TURN_SETTLE_SECONDS", original)
        self.stub.never_reports_done = True
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        self.assertIn("TURN-SETTLED", (Path(rec["dir"]) / "status.log").read_text())
        self.assertIn("/exit", self.stub.typed())

    def test_an_idle_worker_that_wrote_nothing_is_not_finished(self):
        """Idle with nothing new on disk is a worker waiting, not a worker done."""
        rec = self.make_live_record(backend="claude", lane="opus@high")
        wrapper = self.wrapper_for(rec)
        wrapper._worked_since_prompt = True
        wrapper._prompted_at = time.time()
        wrapper._settled_since = time.time() - 3600
        self.assertFalse(wrapper.agent_turn_is_over("idle"))
        (Path(rec["dir"]) / "out.md").write_text("the answer", encoding="utf-8")
        self.assertTrue(wrapper.agent_turn_is_over("idle"))

    def test_an_idle_worker_that_never_worked_is_not_finished(self):
        rec = self.make_live_record(backend="claude", lane="opus@high")
        wrapper = self.wrapper_for(rec)
        (Path(rec["dir"]) / "out.md").write_text("stale", encoding="utf-8")
        wrapper._worked_since_prompt = False
        self.assertFalse(wrapper.agent_turn_is_over("idle"))

    def test_done_is_still_the_fast_path(self):
        rec = self.make_live_record(backend="claude", lane="opus@high")
        wrapper = self.wrapper_for(rec)
        self.assertTrue(wrapper.agent_turn_is_over("done"))

    def wrapper_for(self, rec):
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        return wrapper


class TestUntrackedSubstrate(HerdrStubTestCase):
    """A pane substrate with no detection engine, the way tmux has none.

    An `agent-done` driver had no evidence at all there: `status` is untracked,
    so the seen-working gate never opened, the turn was never seen to end, and
    the pane and the cap slot were held until the operator killed the run by
    hand. The check-in ladder could not rescue it either, because a run with a
    deliverable on disk comes back `review` at every check-in for ever.
    """

    class Untracked(herdr.HerdrSubstrate):
        """Panes, screens, and processes; no agent anybody is tracking."""

        def status(self, worker):
            return herdr.WorkerStatus()

    def untracked_status(self):
        """Make every herdr substrate in this process track nothing, as tmux does."""
        self.addCleanup(setattr, herdr.HerdrSubstrate, "status",
                        herdr.HerdrSubstrate.status)
        herdr.HerdrSubstrate.status = lambda self, worker: herdr.WorkerStatus()

    def wrapper_for(self, lane="opus@high"):
        rec = self.make_live_record(lane=lane)
        wrapper = runner.RunWrapper(
            self.Untracked(herdr.HerdrClient(self.stub.path)), rec)
        wrapper.attach()
        wrapper._prompted_at = time.time()
        return wrapper

    def settle_deliverable(self, wrapper, text="the answer"):
        """Write the deliverable and let it stop changing, as a worker does."""
        (Path(wrapper.rec["dir"]) / "out.md").write_text(text, encoding="utf-8")
        wrapper.turn_is_over()                       # the first sight of it
        time.sleep(runner.DELIVERABLE_QUIET_SECONDS + 0.05)

    def test_an_agent_signal_driver_ends_its_turn_on_its_deliverable(self):
        for lane in ("opus@high", "grok@high"):
            with self.subTest(lane=lane):
                wrapper = self.wrapper_for(lane)
                self.assertEqual(wrapper.driver.turn_signal, "agent-done")
                self.assertFalse(wrapper.turn_is_over(), "nothing written yet")
                self.settle_deliverable(wrapper)
                self.assertTrue(wrapper.turn_is_over())

    def test_a_deliverable_still_being_written_is_not_the_turn_signal(self):
        wrapper = self.wrapper_for()
        out = Path(wrapper.rec["dir"]) / "out.md"
        out.write_text("outline so far", encoding="utf-8")
        self.assertFalse(wrapper.turn_is_over())
        out.write_text("outline so far, plus more", encoding="utf-8")
        self.assertFalse(wrapper.turn_is_over())

    def test_the_previous_turns_deliverable_does_not_end_this_one(self):
        """A steer or a nudge starts a turn with the last one's answer already
        on disk; ending on that would exit the worker the moment it began."""
        wrapper = self.wrapper_for()
        out = Path(wrapper.rec["dir"]) / "out.md"
        out.write_text("the answer to the turn before", encoding="utf-8")
        stale = time.time() - 600
        os.utime(out, (stale, stale))
        wrapper.turn_is_over()
        time.sleep(runner.DELIVERABLE_QUIET_SECONDS + 0.05)
        self.assertFalse(wrapper.turn_is_over())

    def test_a_claude_run_completes_where_nothing_tracks_the_agent(self):
        """End to end: the exit command goes in and the run is journaled done."""
        self.untracked_status()
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done", rec.get("error"))
        self.assertIn("/exit", self.stub.typed())

    def test_the_trust_dialog_is_answered_with_a_key_and_never_typed_into(self):
        """A driver whose trust dialog is recognised by its own words had no
        path to an answer here: the tracked one runs off a `blocked` status
        nothing reports. The dialog was just a screen that had stopped changing,
        and the next thing to go into it was the brief, at a numbered selector.
        """
        self.untracked_status()
        screen_of = herdr.HerdrSubstrate.read_screen
        prompt_of = herdr.HerdrSubstrate.deliver_prompt
        line_of = herdr.HerdrSubstrate.send_tui_line
        state = {"at_the_dialog": 0}

        def standing():
            """The dialog leaves the screen when dispatch answers it, not before."""
            return not any(r.get("trust_approved") for r in records.all_records())

        def screen(substrate, worker, *args, **kwargs):
            text = screen_of(substrate, worker, *args, **kwargs)
            return text + CLAUDE_TRUST_SCREEN if standing() else text

        def deliver_prompt(substrate, worker, text):
            state["at_the_dialog"] += bool(standing())
            return prompt_of(substrate, worker, text)

        def send_tui_line(substrate, worker, text, *args, **kwargs):
            state["at_the_dialog"] += bool(standing())
            return line_of(substrate, worker, text, *args, **kwargs)

        for name, patched in (("read_screen", screen),
                              ("deliver_prompt", deliver_prompt),
                              ("send_tui_line", send_tui_line)):
            self.addCleanup(setattr, herdr.HerdrSubstrate, name,
                            getattr(herdr.HerdrSubstrate, name))
            setattr(herdr.HerdrSubstrate, name, patched)

        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(state["at_the_dialog"], 0, "something was typed at it")
        self.assertTrue(rec.get("trust_approved"))
        self.assertIn("TRUST-APPROVED", (Path(rec["dir"]) / "status.log").read_text())
        self.assertEqual(rec["state"], "done", rec.get("error"))


class TestAcceptanceShouldFix(HerdrStubTestCase):
    """The rest of what the first end-to-end pass found."""

    def test_a_killed_worker_takes_its_detached_children_with_it(self):
        """codex's background terminal reparents to init with its own group and
        survived both a kill and a deadline."""
        stopped = []
        original_group = processes.stop_process_group
        original_pid = processes.stop_pid
        original_kin = herdr.descendant_pids
        processes.stop_process_group = lambda pgid: True
        processes.stop_pid = stopped.append
        herdr.descendant_pids = lambda pids: [999001, 999002]
        for name, value in (("stop_process_group", original_group),
                            ("stop_pid", original_pid)):
            self.addCleanup(setattr, processes, name, value)
        self.addCleanup(setattr, herdr, "descendant_pids", original_kin)
        rec = self.make_live_record()
        pane = self.stub.panes[rec["worker_id"]]
        pane.running = True
        pane.turn_polls = 10_000
        signalled = herdr.HerdrSubstrate().kill_worker_tree(self.worker_of(rec))
        self.assertEqual(stopped, [999001, 999002])
        self.assertIn(999001, signalled)

    def test_descendants_are_found_by_walking_ppid_links(self):
        parent = os.getpid()
        self.assertEqual(processes.descendant_pids([]), [])
        self.assertNotIn(parent, processes.descendant_pids([parent]))

    def test_a_reserved_worker_with_no_owner_is_abandoned(self):
        """A killed exec leaves its in-flight workers reserved with live panes."""
        rec = self.make_live_record(state="reserved", foreground=True)
        rec["reserved_at"] = time.time() - caps.RESERVATION_GRACE_SECONDS - 1
        rec.pop("started_at", None)          # its worker never began
        records.save_record(rec)
        pane = rec["worker_id"]
        self.assertEqual(caps.live_records(self.sweep()), [])
        settled = records.load_record(rec["id"])
        self.assertIn(settled["state"], policy.policy().terminal_states)
        self.assertIn("abandoned", settled["error"])
        self.assertTrue(self.stub.panes[pane].closed, "its workspace stayed open")

    def test_a_steer_resets_the_turn_so_its_answer_is_waited_for(self):
        """The previous turn's out.md is on disk; a watcher that sees it settled
        sends the exit command into the middle of the correction."""
        self.run_cli("run", "opus@high", str(self.brief), "--bg")
        rec = records.load_record(self.only_record()["id"])
        self.stop_watcher(rec.get("watcher_pid"))
        (Path(rec["dir"]) / "out.md").write_text("first answer", encoding="utf-8")
        rec["deliverable_seen"] = [12, time.time()]
        rec["deliverable_since"] = time.time() - 3600
        records.save_record(rec)
        pane = self.stub.panes[rec["worker_id"]]
        pane.running = True
        pane.turn_polls = 10_000
        pane.status = "working"
        self.assertEqual(self.run_cli("steer", rec["id"], "use Delta"),
                         cli.EXIT_OK)
        after = records.load_record(rec["id"])
        self.assertIsNone(after["deliverable_seen"],
                          "the old deliverable still counted as this turn's")

    def test_steering_a_run_whose_tui_is_gone_routes_instead_of_contradicting(self):
        """It used to be refused by continue for being live and by steer for
        being finished, which the operator cannot act on."""
        self.run_cli("run", "opus@high", str(self.brief), "--bg")
        rec = records.load_record(self.only_record()["id"])
        self.stop_watcher(rec.get("watcher_pid"))
        self.stub.finish_turn(rec["worker_id"])
        code, error = self.capture_stderr("steer", rec["id"], "one more thing")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("is a continuation", error)

    def test_continue_takes_a_deadline(self):
        self.run_cli("run", "opus@high", str(self.brief))
        rec_id = self.only_record()["id"]
        self.assertEqual(self.run_cli("continue", rec_id, "again", "--deadline", "5m"),
                         cli.EXIT_OK)
        child = [r for r in records.all_records() if r.get("parent") == rec_id][0]
        self.assertEqual(child["deadline_seconds"], 300)

    def test_a_foreground_continue_gets_the_default_deadline_too(self):
        """With none there is no check-in ladder at all, and a resumed worker
        that goes idle without answering is never judged."""
        self.run_cli("run", "opus@high", str(self.brief))
        rec_id = self.only_record()["id"]
        self.assertEqual(self.run_cli("continue", rec_id, "again"), cli.EXIT_OK)
        child = [r for r in records.all_records() if r.get("parent") == rec_id][0]
        self.assertEqual(child["deadline_seconds"],
                         policy.parse_deadline(policy.policy().default_deadline))

    def test_no_heartbeat_after_the_exit_is_in_flight(self):
        original = records.HEARTBEAT_SECONDS
        records.HEARTBEAT_SECONDS = 0
        self.addCleanup(setattr, records, "HEARTBEAT_SECONDS", original)
        self.run_cli("run", str(self.brief))
        lines = (Path(self.only_record()["dir"]) / "status.log").read_text().splitlines()
        exit_at = [i for i, ln in enumerate(lines) if ln.startswith("EXIT-COMMAND")][0]
        self.assertFalse([ln for ln in lines[exit_at:] if ln.startswith("HEARTBEAT")],
                         "a heartbeat after the exit reads as a worker still going")

    def test_the_record_names_whose_depth_it_records(self):
        self.run_cli("run", str(self.brief))
        rec = self.only_record()
        self.assertEqual(rec["launcher_depth"], 0)
        self.assertEqual(self.stub.workspaces[0]["env"]["AGENT_DEPTH"], "1")


class TestInspectAttach(HerdrStubTestCase):
    """One step when there is a terminal; two when there is not."""

    def finished_run(self, lane="opus@high"):
        self.run_cli("run", lane, str(self.brief))
        return records.load_record(self.only_record()["id"])

    def with_a_terminal(self, on_attach=None):
        """Pretend a human is at the keyboard, and record the attach.

        The attach itself is stubbed on the substrate: handing a terminal to a
        real multiplexer is the one thing this suite must never do.
        """
        attached = []
        original_tty = cli.stdio_is_tty
        original_attach = herdr.HerdrSubstrate.attach
        cli.stdio_is_tty = lambda: True

        def attach(substrate, worker):
            attached.append(True)
            if on_attach is not None:
                on_attach()
            return 0

        herdr.HerdrSubstrate.attach = attach
        self.addCleanup(setattr, cli, "stdio_is_tty", original_tty)
        self.addCleanup(setattr, herdr.HerdrSubstrate, "attach", original_attach)
        return attached

    def test_without_a_terminal_it_stays_the_two_step_form(self):
        """Piped or scripted: there is nobody to hand the terminal to."""
        rec = self.finished_run()
        code, output = self.capture_stdout("inspect", rec["id"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("attach with your substrate", output)
        self.assertTrue(records.load_record(rec["id"])["inspect_worker"])

    def test_with_a_terminal_it_attaches_and_then_closes(self):
        attached = self.with_a_terminal()
        rec = self.finished_run()
        code, output = self.capture_stdout("inspect", rec["id"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(attached, [True], "it never attached")
        self.assertIn("attaching now", output)
        self.assertIn("closed", output)
        self.assertFalse(records.load_record(rec["id"])["inspect_worker"])
        self.assertIn("INSPECT-DETACH", (Path(rec["dir"]) / "status.log").read_text())

    def test_no_attach_keeps_the_old_behaviour_explicitly(self):
        attached = self.with_a_terminal()
        rec = self.finished_run()
        _, output = self.capture_stdout("inspect", rec["id"], "--no-attach")
        self.assertEqual(attached, [])
        self.assertIn("attach with your substrate", output)
        self.assertTrue(records.load_record(rec["id"])["inspect_worker"])

    def test_keep_leaves_the_pane_open_after_detaching(self):
        self.with_a_terminal()
        rec = self.finished_run()
        _, output = self.capture_stdout("inspect", rec["id"], "--keep")
        self.assertIn("left open", output)
        self.assertTrue(records.load_record(rec["id"])["inspect_worker"])
        self.assertIn("kept: --keep", (Path(rec["dir"]) / "status.log").read_text())

    def test_a_session_that_was_used_while_attached_is_kept(self):
        """A human typing into it is exactly what must not be closed."""
        rec = self.finished_run()
        def type_into_it():
            pane_id = records.load_record(rec["id"])["inspect_worker"]
            self.stub.panes[pane_id].screen += "\nsomething typed by hand\n"

        self.with_a_terminal(type_into_it)
        _, output = self.capture_stdout("inspect", rec["id"])
        self.assertIn("left open", output)
        self.assertIn("its screen changed", output)
        self.assertTrue(records.load_record(rec["id"])["inspect_worker"])
        self.assertIn("kept:", (Path(rec["dir"]) / "status.log").read_text())

    def test_a_working_session_is_kept(self):
        rec = self.finished_run()
        def start_working():
            pane_id = records.load_record(rec["id"])["inspect_worker"]
            self.stub.panes[pane_id].status = "working"

        self.with_a_terminal(start_working)
        _, output = self.capture_stdout("inspect", rec["id"])
        self.assertIn("the session is working", output)
        self.assertTrue(records.load_record(rec["id"])["inspect_worker"])

    def test_a_pane_a_continue_adopted_is_never_closed(self):
        """Its wrapper owns it now, including closing it."""
        self.with_a_terminal()
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"], "--keep")
        pane = records.load_record(rec["id"])["inspect_worker"]
        self.make_live_record(worker_id=pane, state="running")
        _, output = self.capture_stdout("inspect", rec["id"])
        self.assertIn("owns it now", output)
        self.assertTrue(records.load_record(rec["id"])["inspect_worker"])

    def test_a_second_continue_cannot_adopt_a_pane_a_turn_is_already_in(self):
        """Both read the same pointer on the parent. The second one used to type
        its prompt into the first one's turn, and either could close the pane."""
        self.with_a_terminal()
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"], "--keep")
        pane = records.load_record(rec["id"])["inspect_worker"]
        first = self.make_live_record(state="running")
        first["adopts_worker"] = pane
        records.save_record(first)
        prompts = len(self.stub.prompts)

        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.run_cli("continue", rec["id"], "me too")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn(first["id"], err.getvalue())
        self.assertEqual(len(self.stub.prompts), prompts, "typed into a live turn")
        loser = [r for r in records.all_records() if r.get("parent") == rec["id"]][0]
        self.assertEqual(loser["state"], "failed")
        self.assertNotIn(("pane", pane), self.stub.closed)

        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.run_cli("inspect", rec["id"], "--close")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertNotIn(("pane", pane), self.stub.closed)

    def test_a_kept_pane_is_still_reusable_by_continue(self):
        self.with_a_terminal()
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"], "--keep")
        pane = records.load_record(rec["id"])["inspect_worker"]
        self.assertEqual(self.run_cli("continue", rec["id"], "one more thing"),
                         cli.EXIT_OK)
        child = [r for r in records.all_records() if r.get("parent") == rec["id"]][0]
        self.assertEqual(child["worker_id"], pane)

    def test_a_kept_pane_stays_on_the_wall_however_old_the_run_is(self):
        """The pane is the thing that must not be forgotten."""
        self.with_a_terminal()
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"], "--keep")
        aged = records.load_record(rec["id"])
        aged["finished"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - cli.WATCH_RECENT_SECONDS - 600))
        records.save_record(aged)
        _, wall = self.capture_stdout("watch")
        self.assertIn(rec["id"], wall)
        self.assertIn("inspect", wall)
        _, status = self.capture_stdout("status")
        self.assertIn("inspect", status)

    def test_close_still_closes(self):
        rec = self.finished_run()
        self.run_cli("inspect", rec["id"], "--no-attach")
        _, output = self.capture_stdout("inspect", rec["id"], "--close")
        self.assertIn("closed", output)
        self.assertFalse(records.load_record(rec["id"])["inspect_worker"])


class TestWatchAttach(HerdrStubTestCase):
    """`watch <id> --attach` is presence in a live run's own pane."""

    def finished_run(self):
        self.run_cli("run", "opus@high", str(self.brief))
        return records.load_record(self.only_record()["id"])

    def capture_attach_argv(self):
        """Record what the attach would exec, without taking this terminal."""
        argv = []
        original = subprocess.run
        self.addCleanup(setattr, subprocess, "run", original)

        def fake_run(command, **kwargs):
            argv.append(list(command))
            return subprocess.CompletedProcess(list(command), 0)

        subprocess.run = fake_run
        return argv

    def test_attach_focuses_the_run_s_workspace_and_execs_herdr(self):
        rec = self.make_live_record()
        argv = self.capture_attach_argv()
        code, output = self.capture_stdout("watch", rec["id"], "--attach")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(argv, [list(herdr.ATTACH_ARGV)])
        self.assertEqual(self.stub.focused, [rec["worker_group"]],
                         "the attach would land on whatever was focused last")
        self.assertIn(rec["worker_id"], output)

    def test_an_attach_leaves_the_run_exactly_as_it_was(self):
        """Read-only presence: no record write, no pane closed, nothing typed."""
        rec = self.make_live_record()
        record_path = Path(rec["dir"]) / "run.json"
        before = record_path.read_bytes()
        self.capture_attach_argv()
        self.assertEqual(self.run_cli("watch", rec["id"], "--attach"),
                         cli.EXIT_OK)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(self.stub.closed, [])
        pane = self.stub.panes[rec["worker_id"]]
        self.assertFalse(pane.closed)
        self.assertEqual(pane.pending_text, "")

    def test_attach_on_a_finished_run_points_at_inspect(self):
        rec = self.finished_run()
        argv = self.capture_attach_argv()
        code, error = self.capture_stderr("watch", rec["id"], "--attach")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn(f"dispatch inspect {rec['id']}", error)
        self.assertEqual(argv, [], "it attached to a run that had already ended")
        self.assertFalse(records.load_record(rec["id"]).get("inspect_worker"),
                         "it reopened the session instead of pointing at inspect")

    def test_attach_without_an_id_is_a_usage_error(self):
        code, error = self.capture_stderr("watch", "--attach")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("dispatch watch <id> --attach", error)

    def test_attach_to_a_run_whose_pane_is_gone_says_so(self):
        rec = self.make_live_record()
        argv = self.capture_attach_argv()
        self.stub.panes[rec["worker_id"]].closed = True
        code, error = self.capture_stderr("watch", rec["id"], "--attach")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("which is gone", error)
        self.assertEqual(argv, [])

    def test_plain_watch_is_untouched(self):
        rec = self.make_live_record()
        argv = self.capture_attach_argv()
        code, wall = self.capture_stdout("watch")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn(rec["id"], wall)
        self.assertEqual(argv, [])


# --------------------------------------------------------------------------
# A CLI that updates itself at launch
# --------------------------------------------------------------------------


class TestSelfUpdateRespawn(HerdrStubTestCase):
    """The first spawn after a release updates the CLI and quits; restart it.

    The rule: if the CLI has an update, let it finish, and then
    restart the CLI on the latest and greatest. Same with claude and grok."
    """

    def status_log(self, rec):
        return (Path(rec["dir"]) / "status.log").read_text(encoding="utf-8")

    def test_a_banner_phrase_in_a_working_workers_output_is_not_an_update(self):
        """"Installed successfully" from a package manager, on the screen of a
        worker that then exits with no answer, used to relaunch the run and
        deliver the whole brief a second time."""
        rec = self.make_live_record()
        wrapper = runner.RunWrapper(herdr.HerdrSubstrate(), rec)
        wrapper.attach()
        wrapper.substrate = type("S", (), {
            "screen_since_spawn": lambda *a: "Update ran successfully! restart"})()
        self.assertTrue(wrapper.update_exit_marker())
        wrapper._worked_since_prompt = True
        self.assertEqual(wrapper.update_exit_marker(), "")

    def test_a_codex_update_exit_is_respawned_and_the_run_completes(self):
        self.stub.update_exits = 1
        self.stub.update_banner = CODEX_UPDATE_SCREEN
        self.assertEqual(self.run_cli("run", "sol@medium", str(self.brief)),
                         cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["respawns"], 1)
        self.assertEqual((Path(rec["dir"]) / "out.md").read_text(), "final message")
        log = self.status_log(rec)
        self.assertIn("UPDATE-DETECTED", log)
        self.assertIn("updating codex via", log)
        self.assertIn("RESPAWN", log)

    def test_the_respawned_cli_is_briefed_exactly_once(self):
        """Two launches, one brief each: never two into the same CLI."""
        self.stub.update_exits = 1
        self.run_cli("run", "sol@medium", str(self.brief))
        self.assertEqual(len(self.stub.starts), 2)
        self.assertEqual(len(self.stub.prompts), 2)
        # The first went to a codex that had already quit, which is what the
        # live occurrence recorded: herdr accepts a prompt for a gone agent.
        self.assertEqual(self.only_record()["turns"], 2)

    def test_the_respawn_keeps_the_same_run_record_and_pane(self):
        self.stub.update_exits = 1
        self.run_cli("run", "sol@medium", str(self.brief))
        rec = self.only_record()
        self.assertEqual(len(records.all_records()), 1)
        panes = {start["pane_id"] for start in self.stub.starts}
        self.assertEqual(panes, {rec["worker_id"]})
        # The relaunch is a cold launch: the ladder is re-verified before the
        # CLI is started again, exactly as it was the first time.
        self.assertEqual(self.status_log(rec).count("ENV "), 2)

    def test_a_claude_update_exit_is_respawned(self):
        self.stub.update_exits = 1
        self.stub.update_banner = CLAUDE_UPDATE_SCREEN
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["respawns"], 1)
        self.assertIn("update installed", self.status_log(rec))

    def test_a_grok_update_exit_is_respawned(self):
        self.stub.update_exits = 1
        self.stub.update_banner = GROK_UPDATE_SCREEN
        self.assertEqual(self.run_cli("run", "grok@high", str(self.brief)),
                         cli.EXIT_OK)
        rec = self.only_record()
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["respawns"], 1)
        self.assertIn("updating grok", self.status_log(rec))

    def test_an_early_exit_with_no_banner_still_fails_fast(self):
        """Genuine crashes are not softened: no marker, no restart."""
        self.stub.update_exits = 1
        self.stub.update_banner = ""
        self.assertEqual(self.run_cli("run", "sol@medium", str(self.brief)),
                         cli.EXIT_FAILED)
        rec = self.only_record()
        self.assertEqual(rec["state"], "failed")
        self.assertIn("no out.md", rec["error"])
        self.assertNotIn("respawns", rec)
        self.assertEqual(len(self.stub.starts), 1)
        self.assertNotIn("UPDATE-DETECTED", self.status_log(rec))

    def test_three_update_exits_in_a_row_fail_naming_the_loop(self):
        self.stub.update_exits = 3
        self.assertEqual(self.run_cli("run", "sol@medium", str(self.brief)),
                         cli.EXIT_FAILED)
        rec = self.only_record()
        self.assertEqual(rec["state"], "failed")
        self.assertEqual(rec["respawns"], runner.UPDATE_RESPAWN_CAP)
        self.assertEqual(len(self.stub.starts),
                         runner.UPDATE_RESPAWN_CAP + 1)
        self.assertIn("self-update", rec["error"])
        self.assertIn("stopped restarting it", rec["error"])
        self.assertIn("UPDATE-LOOP", self.status_log(rec))

    def test_a_banner_from_the_launch_before_is_not_a_second_update(self):
        """The screen is scrollback, so the scan is anchored to this spawn."""
        self.stub.update_exits = 1
        self.stub.update_banner = CLAUDE_UPDATE_SCREEN
        self.stub.self_exits = True
        self.stub.write_output = False
        self.assertEqual(self.run_cli("run", "opus@high", str(self.brief)),
                         cli.EXIT_FAILED)
        rec = self.only_record()
        self.assertEqual(rec["respawns"], 1)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("no out.md", rec["error"])

    def test_the_respawn_count_survives_on_the_record_for_a_bg_watcher(self):
        """The watcher is another process: the count has to be on the record."""
        self.stub.update_exits = 1
        self.run_cli("run", "sol@medium", str(self.brief), "--bg")
        rec_id = self.only_record()["id"]
        deadline = time.time() + 60
        while time.time() < deadline:
            rec = records.load_record(rec_id)
            if rec.get("state") in policy.policy().terminal_states:
                break
            time.sleep(0.1)
        rec = records.load_record(rec_id)
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["respawns"], 1)
        self.assertIn("RESPAWN", self.status_log(rec))


if __name__ == "__main__":
    unittest.main()
