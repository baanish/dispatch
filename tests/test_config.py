"""Tests for the config layer, the board, the presets, and their verbs.

Every case runs against a temporary HOME and a temporary working directory, so
nothing here reads or writes the machine's own config, and `doctor` only ever
sees fake CLIs written into a temporary PATH. No vendor CLI and no real ssh is
run.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatch import board, cli, config, doctor, lanes, policy, prompt  # noqa: E402
from dispatch.errors import DispatchError  # noqa: E402

BALANCED_BOARD = """
[lanes.mini]
driver = "codex"
model = "some-mini"
efforts = ["high"]

[lanes.big]
driver = "claude"
model = "some-big"
efforts = ["medium", "high"]

[board]
light = "mini@high"
medium = "big@medium"
high = "big@high"
"""


class ConfigTestCase(unittest.TestCase):
    """A temporary HOME, a temporary cwd, and no inherited config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.work = self.root / "work"
        for path in (self.home, self.work):
            path.mkdir()

        self.env_backup = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ["HOME"] = str(self.home)
        # Windows finds the home through USERPROFILE, not HOME. Checked before
        # any case runs, because `init --force` and the skill install write to
        # wherever `Path.home()` says, and that must never be the real one.
        os.environ["USERPROFILE"] = str(self.home)
        self.assertEqual(Path.home().resolve(), self.home.resolve())
        os.environ.pop(config.CONFIG_ENV, None)
        os.environ.pop("DISPATCH_SUBSTRATE", None)

        self.cwd_backup = os.getcwd()
        self.addCleanup(os.chdir, self.cwd_backup)
        os.chdir(self.work)

        self.addCleanup(lanes.set_lane_table, lanes.lane_table())
        self.addCleanup(lanes.set_default_lane, lanes.default_lane())
        self.addCleanup(policy.set_policy, policy.policy())
        self.addCleanup(prompt.set_preamble, prompt.preamble())
        self.addCleanup(config.apply_config, config.active_config())

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self.env_backup)

    # -- helpers ---------------------------------------------------------

    def write_user_config(self, text):
        path = self.home / ".config" / "dispatch" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def message_for(self, text):
        """The operator-facing message a bad config produces."""
        self.write_user_config(text)
        with self.assertRaises(DispatchError) as caught:
            config.load_config()
        return str(caught.exception)


# --------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------


class TestPrecedence(ConfigTestCase):
    def test_no_config_leaves_the_built_in_lane_table_in_force(self):
        loaded = config.load_config()
        self.assertIsNone(loaded.path)
        self.assertEqual(loaded.lanes, dict(lanes.DEFAULT_LANE_TABLE))
        self.assertEqual(loaded.default_lane, lanes.DEFAULT_LANE)
        self.assertEqual(loaded.board, {})

    def test_the_user_file_replaces_the_lane_table_wholesale(self):
        path = self.write_user_config(BALANCED_BOARD)
        loaded = config.load_config()
        self.assertEqual(loaded.path, path)
        self.assertEqual(sorted(loaded.lanes), ["big", "mini"])
        self.assertEqual(loaded.lanes["mini"].model, "some-mini")

    def test_dispatch_config_outranks_the_home_file(self):
        self.write_user_config(BALANCED_BOARD)
        other = self.root / "elsewhere.toml"
        other.write_text('[lanes.only]\ndriver = "grok"\nmodel = "m"\n'
                         'efforts = ["high"]\n', encoding="utf-8")
        os.environ[config.CONFIG_ENV] = str(other)
        loaded = config.load_config()
        self.assertEqual(loaded.path, other)
        self.assertEqual(sorted(loaded.lanes), ["only"])

    def test_a_named_config_that_is_not_there_is_a_typo_not_a_default(self):
        os.environ[config.CONFIG_ENV] = str(self.root / "missing.toml")
        with self.assertRaises(DispatchError) as caught:
            config.load_config()
        self.assertIn("missing.toml", str(caught.exception))

    def test_a_file_in_the_task_directory_decides_nothing(self):
        self.write_user_config('[general]\ndefault_lane = "mini@high"\n'
                               + BALANCED_BOARD)
        (self.work / "dispatch.toml").write_text(
            '[general]\ndefault_lane = "big@high"\n\n'
            '[board]\nlight = "big@medium"\n', encoding="utf-8")
        loaded = config.load_config()
        self.assertEqual(loaded.default_lane, "mini@high")
        self.assertEqual(loaded.board["light"], "mini@high")
        self.assertEqual(loaded.policy.depth1_lane_keys, ("mini",))

    def test_applying_a_config_installs_it_everywhere_it_is_read(self):
        self.write_user_config('[general]\ndefault_lane = "big@high"\n'
                               'prompt_preamble = "Say less."\n'
                               + BALANCED_BOARD)
        config.load_and_apply()
        self.assertEqual(sorted(lanes.lane_table()), ["big", "mini"])
        self.assertEqual(lanes.default_lane(), "big@high")
        self.assertEqual(prompt.preamble(), "Say less.")
        self.assertEqual(lanes.resolve_lane("big@high").model, "some-big")


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


class TestSections(ConfigTestCase):
    def test_caps_and_policy_land_on_the_policy_record(self):
        self.write_user_config(BALANCED_BOARD + """
[caps]
machine = 4
session = 2

[policy]
default_deadline = "10m"
human_hand_timeout = "forever"
blank_metered_keys = true
depth1_lanes = ["mini"]
""")
        loaded = config.load_config()
        self.assertEqual(loaded.policy.machine_cap, 4)
        self.assertEqual(loaded.policy.session_cap, 2)
        self.assertEqual(loaded.policy.default_deadline, "10m")
        self.assertEqual(loaded.policy.hand_timeout, "forever")
        self.assertTrue(loaded.policy.blank_metered_keys)
        self.assertEqual(loaded.policy.depth1_lane_keys, ("mini",))

    def test_the_light_slot_decides_what_depth_one_may_spawn(self):
        """The ladder follows the board rather than a second list to keep."""
        self.write_user_config(BALANCED_BOARD)
        self.assertEqual(config.load_config().policy.depth1_lane_keys, ("mini",))

    def test_a_board_with_no_light_slot_leaves_depth_one_nothing_to_spawn(self):
        self.write_user_config('[lanes.big]\ndriver = "claude"\n'
                               'model = "m"\nefforts = ["high"]\n')
        self.assertEqual(config.load_config().policy.depth1_lane_keys, ())

    def test_machines_carry_ssh_a_mode_and_a_remote_dispatch(self):
        self.write_user_config("""
[machines.workshop]
ssh = "operator@workshop"

[machines.spare]
ssh = "operator@spare"
mode = "shell"
dispatch = "/opt/bin/dispatch"
home = "/srv/dispatch"
""")
        machines = config.load_config().machines
        self.assertEqual(machines["workshop"].ssh, "operator@workshop")
        self.assertEqual(machines["workshop"].mode, "dispatch")
        self.assertEqual(machines["workshop"].dispatch, "dispatch")
        self.assertEqual(machines["spare"].mode, "shell")
        self.assertEqual(machines["spare"].dispatch, "/opt/bin/dispatch")
        self.assertEqual(machines["spare"].home, "/srv/dispatch")

    def test_a_lane_may_pin_a_machine_it_always_runs_on(self):
        self.write_user_config("""
[machines.workshop]
ssh = "operator@workshop"

[lanes.remote]
driver = "codex"
model = "m"
efforts = ["high"]
machine = "workshop"
""")
        loaded = config.load_config()
        self.assertEqual(loaded.lanes["remote"].machine, "workshop")
        self.assertEqual(
            lanes.resolve_lane("remote@high", loaded.lanes).machine, "workshop")

    def test_tiers_and_their_aliases_survive_the_round_trip(self):
        self.write_user_config("""
[lanes.fastish]
driver = "codex"
model = "m"
efforts = ["high"]
tier = "default"
alt_tiers = ["default", "priority"]
tier_aliases = { fast = "priority" }
""")
        table = config.load_config().lanes
        self.assertEqual(lanes.resolve_lane("fastish@high:fast", table).tier,
                         "priority")


# --------------------------------------------------------------------------
# Validation. Every message names the key and the file.
# --------------------------------------------------------------------------


class TestValidation(ConfigTestCase):
    def test_an_unknown_section_names_itself_and_the_file(self):
        message = self.message_for("[lane]\nmine = 1\n")
        self.assertIn("config.toml", message)
        self.assertIn("[lane]", message)
        self.assertIn("general, caps, policy, worker_env, lanes, machines, board", message)

    def test_an_unknown_lane_key_names_the_lane_and_the_key(self):
        message = self.message_for('[lanes.mini]\ndriver = "codex"\n'
                                   'model = "m"\nefforts = ["high"]\n'
                                   'effort = "high"\n')
        self.assertIn("[lanes.mini] effort", message)
        self.assertIn("driver, model, efforts", message)

    def test_an_unknown_driver_lists_the_drivers_there_are(self):
        message = self.message_for('[lanes.mini]\ndriver = "gemini"\n'
                                   'model = "m"\nefforts = ["high"]\n')
        self.assertIn("[lanes.mini] driver", message)
        self.assertIn("'gemini' is not a driver", message)
        self.assertIn("claude, codex, grok", message)

    def test_a_lane_with_no_efforts_says_which_key_is_missing(self):
        message = self.message_for('[lanes.mini]\ndriver = "codex"\n'
                                   'model = "m"\n')
        self.assertIn("[lanes.mini] efforts: required", message)

    def test_a_wrong_type_names_the_type_it_wanted(self):
        message = self.message_for('[lanes.mini]\ndriver = "codex"\n'
                                   'model = "m"\nefforts = "high"\n')
        self.assertIn("[lanes.mini] efforts: expected a list of strings", message)

    def test_an_effort_outside_the_lane_grammar_is_refused(self):
        """`key@effort[:tier]` is parsed by splitting, so the parts are tokens."""
        message = self.message_for('[lanes.mini]\ndriver = "codex"\n'
                                   'model = "m"\nefforts = ["very high"]\n')
        self.assertIn("not a usable effort", message)
        self.assertIn("key@effort[:tier]", message)

    def test_a_lane_pinned_to_an_unknown_machine_is_refused(self):
        message = self.message_for('[lanes.mini]\ndriver = "codex"\n'
                                   'model = "m"\nefforts = ["high"]\n'
                                   'machine = "workshop"\n')
        self.assertIn("[lanes.mini] machine", message)
        self.assertIn("none configured", message)

    def test_an_unknown_remote_shell_is_refused(self):
        message = self.message_for('[machines.workshop]\nssh = "a@b"\n'
                                   'shell = "cmd"\n')
        self.assertIn("[machines.workshop] shell", message)
        self.assertIn("posix, powershell", message)

    def test_powershell_requires_remote_dispatch(self):
        message = self.message_for('[machines.workshop]\nssh = "a@b"\n'
                                   'mode = "shell"\nshell = "powershell"\n')
        self.assertIn('powershell needs mode = "dispatch"', message)

    def test_a_machine_with_no_ssh_target_is_refused(self):
        message = self.message_for('[machines.workshop]\nmode = "shell"\n')
        self.assertIn("[machines.workshop] ssh: required", message)

    def test_an_option_shaped_ssh_target_is_refused(self):
        """ssh takes the destination positionally, so one starting with `-`
        reaches it as an option and never as a host."""
        message = self.message_for(
            '[machines.workshop]\nssh = "-oProxyCommand=touch /tmp/pwned"\n')
        self.assertIn("[machines.workshop] ssh", message)
        self.assertIn("is not a destination", message)

    def test_an_ssh_target_with_whitespace_in_it_is_refused(self):
        message = self.message_for('[machines.workshop]\nssh = "a@b -v"\n')
        self.assertIn("[machines.workshop] ssh", message)

    def test_local_is_not_a_name_a_machine_may_take(self):
        """`--on local` means here, so a machine answering to it is unreachable."""
        message = self.message_for('[machines.local]\nssh = "operator@box"\n')
        self.assertIn("[machines.local]", message)

    def test_an_unknown_machine_mode_lists_the_modes(self):
        message = self.message_for('[machines.workshop]\nssh = "a@b"\n'
                                   'mode = "carrier-pigeon"\n')
        self.assertIn("[machines.workshop] mode", message)
        self.assertIn("dispatch, shell", message)

    def test_a_board_slot_naming_an_unknown_lane_names_the_slot(self):
        message = self.message_for(BALANCED_BOARD + 'genius = "nope@high"\n')
        self.assertIn("[board] genius", message)
        self.assertIn("valid lanes: mini@high", message)

    def test_a_key_that_is_not_a_slot_lists_the_slots(self):
        message = self.message_for(BALANCED_BOARD + 'cheap = "mini@high"\n')
        self.assertIn("[board] cheap", message)
        self.assertIn("light, medium, high, blindspot, genius", message)

    def test_a_default_lane_the_table_does_not_have_is_refused(self):
        message = self.message_for('[general]\ndefault_lane = "opus@high"\n'
                                   + BALANCED_BOARD)
        self.assertIn("[general] default_lane", message)

    def test_a_bad_deadline_names_the_policy_key(self):
        message = self.message_for('[policy]\ndefault_deadline = "soon"\n')
        self.assertIn("[policy] default_deadline", message)
        self.assertIn("45s, 30m, 2h", message)

    def test_broken_toml_says_so_with_the_file(self):
        message = self.message_for("[board\n")
        self.assertIn("config.toml", message)
        self.assertIn("not valid TOML", message)


# --------------------------------------------------------------------------
# The board
# --------------------------------------------------------------------------


class TestBoard(ConfigTestCase):
    def test_every_slot_is_a_row_filled_or_not(self):
        self.write_user_config(BALANCED_BOARD)
        rendered = board.render_board(config.load_config())
        rows = {line.split()[0]: line for line in rendered.splitlines()
                if line.split() and line.split()[0] in board.SLOTS}
        self.assertEqual(sorted(rows), sorted(board.SLOTS))
        self.assertIn("mini@high", rows["light"])
        self.assertIn("codex", rows["light"])
        self.assertIn("some-mini", rows["light"])
        self.assertIn("unfilled", rows["genius"])

    def test_a_slot_note_replaces_the_standing_one(self):
        self.write_user_config(BALANCED_BOARD + '\n[board.when]\n'
                               'light = "only the boring parts"\n')
        rendered = board.render_board(config.load_config())
        self.assertIn("only the boring parts", rendered)
        self.assertNotIn(board.SLOT_PURPOSE["light"], rendered)
        self.assertIn(board.SLOT_PURPOSE["medium"], rendered)

    def test_a_note_for_something_that_is_not_a_slot_is_refused(self):
        message = self.message_for(BALANCED_BOARD + '\n[board.when]\n'
                                   'cheap = "no such slot"\n')
        self.assertIn("[board.when] cheap", message)

    def test_the_table_names_the_file_the_board_came_from(self):
        path = self.write_user_config(BALANCED_BOARD)
        self.assertIn(str(path), board.render_board(config.load_config()))

    def test_an_empty_board_says_how_to_fill_it(self):
        self.assertIn("dispatch init", board.render_board(config.load_config()))

    def test_a_slot_resolves_to_a_lane_and_an_empty_one_says_so(self):
        self.write_user_config(BALANCED_BOARD)
        loaded = config.load_config()
        self.assertEqual(board.slot_lane(loaded, "light").model, "some-mini")
        with self.assertRaises(DispatchError) as caught:
            board.slot_lane(loaded, "genius")
        self.assertIn("genius", str(caught.exception))
        with self.assertRaises(DispatchError):
            board.slot_lane(loaded, "cheap")


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------


class TestPresets(ConfigTestCase):
    # pi lanes name a `provider/id` that differs per pi install, so this preset
    # ships with its models blank and is filled in before it can load.
    TEMPLATE_PRESET = "pi-only"

    def test_the_shipped_presets_are_the_ones_init_offers(self):
        self.assertEqual(config.preset_names(),
                         ["all-genius", "anthropic-only", "balanced",
                          "openai-only", "pi-only"])
        self.assertIn(config.DEFAULT_PRESET, config.preset_names())

    def test_every_preset_loads_and_fills_the_slots_it_claims(self):
        for name in config.preset_names():
            if name == self.TEMPLATE_PRESET:
                continue
            with self.subTest(preset=name):
                path = self.write_user_config(config.preset_text(name))
                loaded = config.load_config()
                self.assertEqual(loaded.path, path)
                self.assertTrue(loaded.lanes)
                for slot, text in loaded.board.items():
                    lane = lanes.resolve_lane(text, loaded.lanes)
                    self.assertIn(lane.key, loaded.lanes, slot)
                self.assertTrue(board.render_board(loaded))

    def test_every_preset_says_what_it_assumes_is_installed(self):
        for name in config.preset_names():
            with self.subTest(preset=name):
                header = config.preset_text(name).splitlines()
                self.assertTrue(header[0].startswith(f"# {name}:"), header[0])
                self.assertTrue(any("Assumes" in line for line in header[:12]),
                                name)

    def test_all_genius_puts_one_lane_in_every_slot(self):
        self.write_user_config(config.preset_text("all-genius"))
        loaded = config.load_config()
        self.assertEqual(sorted(loaded.board), sorted(board.SLOTS))
        self.assertEqual(len(set(loaded.board.values())), 1)

    def test_the_template_preset_says_which_lane_needs_a_model(self):
        """A blank model is the error a first run should get, and it names the
        lane to fill: a placeholder that validates would read as a working
        config until a real call failed somewhere else."""
        text = config.preset_text(self.TEMPLATE_PRESET)
        self.write_user_config(text)
        with self.assertRaises(DispatchError) as caught:
            config.load_config()
        self.assertIn("[lanes.light] model: required", str(caught.exception))
        self.write_user_config(text.replace('model = ""', 'model = "vendor/some-id"'))
        loaded = config.load_config()
        for slot, lane_text in loaded.board.items():
            self.assertIn(lanes.resolve_lane(lane_text, loaded.lanes).key,
                          loaded.lanes, slot)

    def test_an_unknown_preset_lists_the_ones_there_are(self):
        with self.assertRaises(DispatchError) as caught:
            config.preset_text("wishful")
        self.assertIn("balanced", str(caught.exception))


# --------------------------------------------------------------------------
# the packaged skill
# --------------------------------------------------------------------------


class TestPackagedSkill(ConfigTestCase):
    """What `dispatch skill` and `dispatch init` hand an agent.

    A wheel that shipped without `package-data`, or a placeholder nobody
    replaced, both read as an installed skill and neither says anything.
    """

    def test_the_skill_is_a_document_with_frontmatter(self):
        text = config.skill_text()
        lines = text.splitlines()
        self.assertEqual(lines[0], "---")
        end = lines.index("---", 1)
        frontmatter = "\n".join(lines[1:end])
        self.assertIn("name: dispatch", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertGreater(len(lines), 40, "the skill teaches nothing this short")

    def test_the_snippet_is_a_pastable_block(self):
        text = config.agents_snippet_text()
        self.assertTrue(text.startswith("## "), text[:40])
        self.assertIn("dispatch board", text)
        self.assertLessEqual(len(text.splitlines()), 25,
                             "a snippet longer than this is a skill")


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


class TestInit(ConfigTestCase):
    def agent_home(self, name):
        # `parents=True` because an agent home can be nested: pi's is `.pi/agent`.
        (self.home / name).mkdir(parents=True)

    def test_init_writes_the_config_and_installs_the_skill(self):
        self.agent_home(".claude")
        lines = config.run_init(preset="balanced")
        written = self.home / ".config" / "dispatch" / "config.toml"
        skill_dir = self.home / ".claude" / "skills" / "dispatch"
        installed = skill_dir / "SKILL.md"
        self.assertEqual((skill_dir / "agents-snippet.md").read_text(
            encoding="utf-8"), config.agents_snippet_text())
        self.assertEqual(written.read_text(encoding="utf-8"),
                         config.preset_text("balanced"))
        self.assertEqual(installed.read_text(encoding="utf-8"),
                         config.skill_text())
        self.assertTrue(any("balanced" in line for line in lines))
        self.assertTrue(any(str(installed) in line for line in lines))

    def test_init_skips_an_agent_home_that_is_not_installed(self):
        lines = config.run_init()
        self.assertTrue(any("not installed" in line for line in lines))
        self.assertFalse((self.home / ".claude").exists())

    def test_init_refuses_to_overwrite_without_force(self):
        config.run_init()
        with self.assertRaises(DispatchError) as caught:
            config.run_init()
        self.assertIn("--force", str(caught.exception))

    def test_init_twice_with_force_changes_nothing(self):
        for name in config.SKILL_HOMES:
            self.agent_home(name)
        first = config.run_init(preset="openai-only")
        before = self._tree()
        second = config.run_init(preset="openai-only", force=True)
        self.assertEqual(before, self._tree())
        self.assertEqual(len(first), len(second))
        self.assertTrue(all("unchanged" in line for line in second))

    def test_force_replaces_a_skill_a_previous_install_left(self):
        self.agent_home(".codex")
        installed = self.home / ".codex" / "skills" / "dispatch" / "SKILL.md"
        installed.parent.mkdir(parents=True)
        installed.write_text("stale\n", encoding="utf-8")
        kept = config.install_skill()
        self.assertEqual(installed.read_text(encoding="utf-8"), "stale\n")
        self.assertTrue(any("kept" in line for line in kept))
        config.install_skill(force=True)
        self.assertEqual(installed.read_text(encoding="utf-8"), config.skill_text())

    def test_what_init_wrote_is_a_config_that_loads(self):
        config.run_init(preset="anthropic-only")
        loaded = config.load_config()
        self.assertEqual(loaded.path,
                         self.home / ".config" / "dispatch" / "config.toml")
        self.assertIn("opus", loaded.lanes)

    def test_init_honours_dispatch_config(self):
        target = self.root / "elsewhere" / "config.toml"
        os.environ[config.CONFIG_ENV] = str(target)
        config.run_init()
        self.assertTrue(target.is_file())

    def _tree(self):
        return {str(path.relative_to(self.home)): path.read_text(encoding="utf-8")
                for path in sorted(self.home.rglob("*")) if path.is_file()}


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


class TestDoctor(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # The fakes are the whole PATH: a real codex or ssh reachable from here
        # would be run by the probes.
        os.environ["PATH"] = str(self.bin)

    def fake(self, name, exit_code=0, output=""):
        """A stand-in on PATH. No vendor CLI or ssh is ever really run."""
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{'echo ' + output if output else ':'}\n"
                        f"exit {exit_code}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_a_cli_that_is_not_on_path_is_the_first_thing_reported(self):
        self.write_user_config(BALANCED_BOARD)
        report, ok = doctor.render_report(config.load_config(), substrate="tmux")
        self.assertFalse(ok)
        self.assertIn("codex not on PATH", report)
        self.assertIn("claude not on PATH", report)
        self.assertIn("substrate: tmux", report)

    def test_a_driver_with_a_login_probe_reports_what_the_probe_said(self):
        self.fake("codex")
        self.fake("claude")
        self.write_user_config(BALANCED_BOARD)
        report, ok = doctor.render_report(config.load_config(), substrate="herdr")
        self.assertTrue(ok)
        self.assertIn("logged in", report)

    def test_a_failing_login_probe_is_not_a_missing_cli(self):
        self.fake("codex", exit_code=1, output="not signed in")
        self.fake("claude")
        self.write_user_config(BALANCED_BOARD)
        report, ok = doctor.render_report(config.load_config(), substrate="herdr")
        self.assertFalse(ok)
        self.assertIn("codex login status", report)
        self.assertIn("not signed in", report)

    def test_a_driver_with_no_probe_says_only_that_it_is_installed(self):
        """Reporting less than was checked beats a green line that means less."""
        self.fake("grok")
        self.write_user_config('[lanes.big]\ndriver = "grok"\nmodel = "m"\n'
                               'efforts = ["high"]\n')
        report, ok = doctor.render_report(config.load_config(), substrate="herdr")
        self.assertTrue(ok)
        self.assertIn("no login probe", report)
        self.assertNotIn("logged in", report)

    def test_claude_is_asked_whether_it_is_logged_in(self):
        self.fake("claude", exit_code=1, output="Not logged in")
        self.write_user_config('[lanes.big]\ndriver = "claude"\nmodel = "m"\n'
                               'efforts = ["high"]\n')
        report, ok = doctor.render_report(config.load_config(), substrate="herdr")
        self.assertFalse(ok)
        self.assertIn("claude auth status --text", report)

    def test_login_uses_the_resolved_executable_including_windows_extension(self):
        driver = doctor.get_driver("codex")
        executable = r"C:\Users\operator\bin\codex.CMD"
        with patch.object(doctor.shutil, "which", return_value=executable), \
                patch.object(doctor, "run_probe", return_value=(True, "logged in")) as probe:
            self.assertTrue(doctor.driver_check(driver).ok)
        probe.assert_called_once_with([executable, "login", "status"])

    def test_powershell_machine_uses_a_compatible_probe_and_quoted_executable(self):
        self.write_user_config('[machines.workshop]\nssh = "operator@workshop"\n'
                               'shell = "powershell"\n'
                               'dispatch = "C:/Program Files/dispatch.exe"\n')
        machine = config.load_config().machines["workshop"]
        with patch.object(doctor, "run_probe", return_value=(True, "dispatch 0.1")) as probe:
            self.assertTrue(doctor.machine_check(machine).ok)
        self.assertEqual(probe.call_args_list[0].args[0][-1], "exit 0")
        self.assertEqual(probe.call_args_list[1].args[0][-1],
                         "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                         "$env:PYTHONIOENCODING = 'utf-8'; "
                         "$ErrorActionPreference = 'Stop'; "
                         "& 'C:/Program Files/dispatch.exe' '--version'")

    def test_a_machine_is_ssh_then_the_dispatch_that_answers_there(self):
        self.fake("ssh", output="dispatch 0.1.0")
        self.write_user_config('[machines.workshop]\n'
                               'ssh = "operator@workshop"\n' + BALANCED_BOARD)
        report, ok = doctor.render_report(config.load_config(), substrate="herdr")
        self.assertIn("workshop", report)
        self.assertIn("remote dispatch 0.1.0", report)

    def test_a_machine_ssh_cannot_reach_is_reported_as_such(self):
        self.fake("ssh", exit_code=255, output="Permission denied")
        self.write_user_config('[machines.workshop]\nssh = "operator@workshop"\n')
        report, ok = doctor.render_report(config.load_config(), substrate="herdr")
        self.assertFalse(ok)
        self.assertIn("ssh failed", report)
        self.assertIn("Permission denied", report)

    def test_a_shell_mode_machine_is_not_asked_for_a_remote_dispatch(self):
        self.fake("ssh")
        self.fake("claude")
        self.write_user_config('[lanes.big]\ndriver = "claude"\nmodel = "m"\n'
                               'efforts = ["high"]\n\n'
                               '[machines.spare]\nssh = "operator@spare"\n'
                               'mode = "shell"\n')
        report, ok = doctor.render_report(config.load_config(), substrate="herdr")
        self.assertTrue(ok)
        self.assertIn("mode shell", report)

    def test_no_machines_means_every_run_is_local(self):
        self.fake("codex")
        self.fake("claude")
        self.fake("grok")
        report, ok = doctor.render_report(config.load_config(), substrate="herdr")
        self.assertTrue(ok)
        self.assertIn("every run is local", report)


# --------------------------------------------------------------------------
# The verbs, through the CLI
# --------------------------------------------------------------------------


class TestVerbs(ConfigTestCase):
    def run_cli(self, *argv):
        return _capture(lambda: cli.main(list(argv)))

    def test_board_prints_the_configured_board(self):
        self.write_user_config(BALANCED_BOARD)
        code, out = self.run_cli("board")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("mini@high", out)

    def test_init_force_replaces_a_config_that_will_not_load(self):
        """It is the documented way out, so the broken file cannot stop it."""
        self.write_user_config("[board\n")
        code, _ = self.run_cli("init", "--preset", "openai-only", "--force")
        self.assertEqual(code, cli.EXIT_OK)
        code, out = self.run_cli("board")
        self.assertEqual(code, cli.EXIT_OK, out)

    def test_worker_env_is_laid_over_the_shipped_variables(self):
        """Adding one must not drop the shipped ones, an empty value takes one
        out, and the markers dispatch sets itself are not a config's to name."""
        self.write_user_config('[worker_env]\nTMPDIR = "/scratch/tmp"\n')
        env = dict(config.load_config().policy.worker_env)
        self.assertEqual(env, {"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
                               "TMPDIR": "/scratch/tmp"})
        self.write_user_config('[worker_env]\nCLAUDE_CODE_PROMPT_CACHE_TTL = ""\n')
        self.assertEqual(config.load_config().policy.worker_env, ())
        for body in ('[worker_env]\nAGENT_DEPTH = "0"\n',
                     '[worker_env]\n"BAD NAME" = "x"\n',
                     '[worker_env]\nTMPDIR = 5\n'):
            with self.subTest(body=body):
                self.write_user_config(body)
                with self.assertRaises(DispatchError) as caught:
                    config.load_config()
                self.assertIn("[worker_env]", str(caught.exception))

    def test_config_mistakes_are_named_at_load_not_met_later(self):
        """An empty [lanes] table used to bring the built-in lanes back, an
        unknown substrate surfaced at the first run, and a file that was not
        UTF-8 was a traceback."""
        for body, expected in (
                ("[lanes]\n", "define at least one lane"),
                ('[general]\nsubstrate = "screen"\n', "substrate"),
                (b"[general]\nsubstrate = \"\xff\"\n", "not valid TOML")):
            with self.subTest(expected=expected):
                path = self.home / ".config" / "dispatch" / "config.toml"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body if isinstance(body, bytes) else body.encode())
                with self.assertRaises(DispatchError) as caught:
                    config.load_config()
                self.assertIn(expected, str(caught.exception))
                self.assertIn(str(path), str(caught.exception))

    def test_a_broken_config_stops_the_verb_and_names_the_file(self):
        self.write_user_config("[board\n")
        code, out = self.run_cli("board")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("not valid TOML", out)

    def test_the_default_lane_the_config_names_is_the_one_run_takes(self):
        self.write_user_config('[general]\ndefault_lane = "big@high"\n'
                               + BALANCED_BOARD)
        config.load_and_apply()
        self.assertEqual(cli.split_lane_and_brief(["brief.md"]),
                         ("big@high", "brief.md"))

    def test_skill_and_agents_snippet_print_the_packaged_files(self):
        code, out = self.run_cli("skill")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out, config.skill_text())
        code, out = self.run_cli("agents-snippet")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out, config.agents_snippet_text())

    def test_skill_install_leaves_the_config_it_finds_untouched(self):
        """The reason this verb exists: `init --force` would reset the config."""
        self.write_user_config(BALANCED_BOARD)
        path = self.home / ".config" / "dispatch" / "config.toml"
        before = path.read_bytes()
        (self.home / ".claude").mkdir()
        code, out = self.run_cli("skill", "--install")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(path.read_bytes(), before)
        installed = self.home / ".claude" / "skills" / "dispatch" / "SKILL.md"
        self.assertEqual(installed.read_text(encoding="utf-8"),
                         config.skill_text())
        self.assertIn(str(installed), out)

    def test_init_prints_the_snippet_it_wants_pasted(self):
        code, out = self.run_cli("init", "--preset", "balanced")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn(config.agents_snippet_text().strip(), out)
        self.assertTrue((self.home / ".config" / "dispatch" /
                         "config.toml").is_file())

    def test_doctor_fails_when_a_configured_cli_is_missing(self):
        self.write_user_config('[lanes.big]\ndriver = "claude"\nmodel = "m"\n'
                               'efforts = ["high"]\n')
        os.environ["PATH"] = str(self.root / "empty-bin")  # nothing is on it
        code, out = self.run_cli("doctor")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("claude not on PATH", out)

    def test_a_verb_runs_in_a_fresh_process_of_its_own(self):
        """Every other test calls `run_verb` in this process, on modules the
        suite has already imported. A subprocess is what catches an import-time
        break or a missing packaged resource, which is how the console script
        would fail for a user."""
        done = subprocess.run(
            [sys.executable, "-m", "dispatch.cli", "board"],
            cwd=str(self.work), capture_output=True, text=True,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
        self.assertEqual(done.returncode, cli.EXIT_OK, done.stderr)
        self.assertIn("slot", done.stdout)


def _capture(call):
    """Run a verb, returning its exit code and everything it printed."""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = call()
    return code, buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
