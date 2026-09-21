"""The Driver interface: everything that is true of one vendor CLI and no other.

A driver is the adapter for a vendor CLI. It owns the command lines that CLI
accepts, the way it is asked to leave, how its turns and dialogs read from the
outside, where it keeps its transcripts, and which environment variables move it
off subscription billing. It owns no lifecycle: the runner decides when a worker
starts, gets its brief, and ends, and asks the driver only what that CLI needs.

Everything vendor-specific in dispatch is one of these attributes. That is the
test for a new driver: if the runner or a substrate has to branch on which CLI
is running, the branch belongs here instead.

To add a driver, subclass `Driver`, fill the class attributes, implement the
three argv builders and `validate_options`, and register it:

    from dispatch.drivers.base import Driver, register_driver

    class MyDriver(Driver):
        name = "mycli"
        ...

    register_driver(MyDriver())

`launch_argv` is checked against the CLI's own `--help` before it is written
down; a table inside somebody else's release is prior art, not a source of
truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import DispatchError


@dataclass(frozen=True)
class DialogRules:
    """Which of this CLI's dialogs dispatch answers, and which it hands back.

    Two classes, deliberately. `answered` names the detection rules dispatch is
    allowed to answer itself, because it already made that decision: a
    directory-trust question is dispatch's own, since the caller chose the
    directory with `--dir` and the worker chose nothing. `handback` names the
    rules dispatch refuses to answer, because a wrong blind answer at a numbered
    selector picks an option, and a run standing at a dialog can still be
    answered by hand while a run killed for one cannot be un-killed.

    `trust_markers` is the exemption inside `handback`: a CLI whose trust dialog
    matches the same generic rule as every other form is recognised by its own
    words on screen instead, and all of the markers must appear. `screen_marker`
    is the same question asked with no detection available at all, which is what
    the typed fallback leaves behind.
    """

    answered_rules: tuple = ()
    # Typed as a TUI line at an answered rule. A digit is unambiguous where a
    # bare enter depends on which option is highlighted.
    answer_text: str = ""
    handback_rules: tuple = ()
    trust_markers: tuple = ()
    # Keys sent when `trust_markers` all match. Never a digit: text typed at a
    # numbered dialog selects by digit, and the option is already highlighted.
    trust_keys: tuple = ()
    # The accepting option's own text. Which row starts highlighted is the
    # CLI's choice and has changed between releases, so the cursor is walked to
    # this row before `trust_keys` go in. Empty means the keys go in as they are.
    trust_accept: str = ""
    trust_attempts: int = 0
    screen_marker: str = ""


class Driver:
    """One vendor CLI, as everything else in dispatch needs to see it."""

    # The lane table's `driver` field, and the name `get_driver` resolves.
    name = ""

    # What a substrate that spawns known agent kinds calls this CLI.
    agent_kind = ""

    # Prefixed to the argv when the command is typed at a real interactive
    # shell, where a function or alias of the same name would shadow the binary
    # and could drop or widen the permission flags the lane pins. Every driver,
    # because any CLI name can be shadowed. The PowerShell dialect has no
    # equivalent and types the bare name.
    shell_prefix = ("command",)

    # (command, how many enters it takes). Interactive workers do not take
    # themselves down: they finish a turn and sit at the prompt, so the runner
    # ends the session. Some CLIs eat the first enter in a slash-command
    # autocomplete popup, which is what the count is for.
    exit_command = ("", 1)

    # What counts as "the turn is over" for this CLI: `agent-done` where the
    # substrate's own done signal is trustworthy, `deliverable` where it is not
    # and the file the worker was asked to write is the only unambiguous
    # evidence.
    turn_signal = "agent-done"

    # What this CLI leaves on screen when it updates itself and quits without
    # reading its brief. Matched lower-cased, and only for a worker that exited
    # early with nothing to show, which is what keeps an ordinary crash from
    # being restarted instead of reported. Each phrase is the CLI's own.
    update_markers = ()

    dialog_rules = DialogRules()

    # Variables that outrank subscription OAuth in this CLI's credential order,
    # so an ambient one silently moves a subscription lane onto per-token
    # billing. Blanked in the worker's environment when policy says to.
    metered_key_vars = ()

    # True when the CLI names its own session rather than accepting one, which
    # is what makes session capture a search rather than a fact.
    names_own_session = False

    # What must be on PATH for this CLI, when that is not the driver's name.
    cli_binary = ""

    # A probe `dispatch doctor` may run to tell "installed" from "logged in".
    # It must be non-interactive, cost no model call, and exit zero only when
    # the CLI has credentials. Empty where the CLI offers nothing that cheap:
    # doctor then reports "installed" and says that is all it checked.
    login_probe = ()

    # -- command lines ---------------------------------------------------

    def launch_argv(self, lane, opts, session_id=""):
        """The interactive command this lane types into a fresh worker home.

        Interactive, not headless: no prompt is ever an argv element, and flags
        that exist only on a headless subcommand are rejected by the interactive
        binary.
        """
        raise NotImplementedError

    def resume_argv(self, lane, opts, session_id):
        """Reopen a native session in a fresh worker home: `dispatch continue`."""
        raise NotImplementedError

    def headless_argv(self, lane, opts, prompt_text, out_path="", session_id="",
                      resume_session=""):
        """The one-shot form, for the headless substrate.

        The prompt is an argv element here because there is no TUI to type into,
        and it is the same prompt-by-path line the typed fallback uses rather
        than a brief body. `out_path` is where the CLI can be told to write its
        final message; a CLI with no such flag prints it and the substrate
        captures stdout.

        `resume_session` is `dispatch continue` on a substrate with no session
        to reopen: the same one-shot form, carrying the CLI's own resume flag
        and the new message. It replaces `session_id` rather than joining it,
        because naming a new session and reopening an old one are the same flag
        family and no CLI accepts both.
        """
        raise NotImplementedError

    def validate_options(self, lane, opts):
        """Refuse the option combinations this CLI cannot honour.

        Per-driver because the answer differs per CLI: a sandbox flag one CLI
        offers, another has no equivalent for, and refusing generically would
        either forbid work that is possible or promise isolation that is not.
        The runner calls this after the checks that hold for every driver.
        """

    # -- transcripts -----------------------------------------------------

    def transcript_root(self):
        """Where this CLI keeps its own session files, or None."""
        return None

    def capture_session(self, started_at, cwd="", markers=()):
        """Find the session this run produced: (id, path, confirmed).

        Only a CLI that names its own session needs this. `confirmed` is False
        when the match is a guess, which is worth saying out loud before
        anything resumes it.
        """
        return "", "", False

    def resolve_transcript(self, session_id, cwd, hint=""):
        """Where this CLI kept its transcript; the run record joins to it."""
        return hint


_DRIVERS = {}


def register_driver(driver):
    _DRIVERS[driver.name] = driver
    return driver


def get_driver(name):
    driver = _DRIVERS.get(name)
    if driver is None:
        raise DispatchError(
            f"no driver for {name!r}; known drivers: "
            + ", ".join(sorted(_DRIVERS)))
    return driver


def driver_names():
    return sorted(_DRIVERS)


def driver_for_lane(lane):
    return get_driver(lane.driver)


def check_shared_options(opts):
    """The option checks that hold whatever CLI is running.

    Files have to exist and directories have to be directories before a worker
    is paid for. Anything that depends on the CLI is `Driver.validate_options`.
    """
    import json

    if opts.schema:
        path = Path(opts.schema)
        if not path.is_file():
            raise DispatchError(f"schema file not found: {opts.schema}")
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise DispatchError(
                f"schema is not valid JSON: {opts.schema} ({exc})") from exc
    if opts.image and not Path(opts.image).is_file():
        raise DispatchError(f"image not found: {opts.image}")
    for extra in opts.add_dirs:
        if not Path(extra).is_dir():
            raise DispatchError(f"--add-dir is not a directory: {extra}")
    if opts.dir and not Path(opts.dir).is_dir():
        raise DispatchError(f"--dir is not a directory: {opts.dir}")
    if opts.out:
        target = Path(opts.out)
        if target.is_dir():
            raise DispatchError(f"--out is a directory: {opts.out}")
        if not target.parent.is_dir():
            raise DispatchError(f"--out directory does not exist: {target.parent}")


def validate_options(lane, opts):
    """Every option check for one lane: the shared ones, then the driver's."""
    check_shared_options(opts)
    driver_for_lane(lane).validate_options(lane, opts)
