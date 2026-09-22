"""The pi driver.

pi has no OS sandbox, so a read lane here is a tool allowlist and nothing more.
That allowlist is stricter than claude's: without `--write` a pi worker holds
`read`, `grep`, `find`, `ls`, and `deliver`, so it can run no command at all and
the only file it can write is the run's own answer, through the tool the shipped
extension registers. `-e` loads that extension on every pi lane, because a write
lane still answers through the same file.

pi takes no working-directory flag, so `--dir` is the directory the substrate
opens the worker in. `--add-dir` is ignored for the same reason it is on grok:
there is no sandbox for a second directory to widen, and every pi worker can
already read what the operator's account can.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

from ..errors import DispatchError
from ..records import SESSION_ID_RE, validate_session_id
from .base import DialogRules, Driver, register_driver

# pi has no read-only mode, so the read lane is an allowlist. `deliver` is the
# tool the packaged extension registers; nothing else here can write.
READ_TOOLS = "read,grep,find,ls,deliver"

EXTENSION_FILE = "dispatch-worker.ts"


def extension_path():
    """The packaged extension, as a path to hand `pi -e`.

    `resources.files` rather than `as_file`: pi reads this file itself, after the
    dispatch process that named it may be gone, so a temporary extraction would
    be gone too.
    """
    return str(resources.files("dispatch").joinpath("pi", EXTENSION_FILE))


def session_root():
    """Where pi keeps its sessions. Both overrides are pi's own."""
    sessions = (os.environ.get("PI_CODING_AGENT_SESSION_DIR") or "").strip()
    if sessions:
        return Path(sessions)
    home = (os.environ.get("PI_CODING_AGENT_DIR") or "").strip()
    return (Path(home) if home else Path.home() / ".pi" / "agent") / "sessions"


def model_args(lane, opts):
    """The model, the thinking level, and the permissions, on every form.

    `--approve` trusts the project-local pi files in the run's directory, which
    is dispatch's own decision for the same reason it answers the other CLIs'
    folder-trust dialogs: the operator chose the directory with `--dir` and the
    worker chose nothing.
    """
    argv = ["--model", lane.model, "--thinking", lane.effort, "--approve"]
    if not opts.write:
        argv += ["--tools", READ_TOOLS]
    return argv + ["-e", extension_path()]


class PiDriver(Driver):
    name = "pi"
    agent_kind = "pi"
    # One enter: pi's completion popup applies the highlighted slash command and
    # submits it in the same keystroke, where codex's leaves the command sitting.
    exit_command = ("/quit", 1)
    turn_signal = "agent-done"
    # pi updates only when `pi update` is run, never on its way into a session.
    update_markers = ()
    # Nothing here is dispatch's to answer: `--approve` settles the only question
    # dispatch has already decided, so a pi standing at a form is standing at one
    # the worker raised, and a human answers that.
    dialog_rules = DialogRules(handback_rules=("live_blocked_form",))
    # pi authenticates per provider through its own auth file and the provider's
    # own key variables, with no subscription billing to be moved off.
    metered_key_vars = ()
    cli_binary = "pi"
    # `pi auth check` answers for one provider or one model and refuses to run
    # without either, so there is nothing to probe before a lane is known.
    login_probe = ()

    def launch_argv(self, lane, opts, session_id=""):
        argv = ["pi", *model_args(lane, opts)]
        if session_id:
            # pi creates the id dispatch generates, so `continue` knows it before
            # the run has produced a single line of output.
            argv += ["--session-id", validate_session_id(session_id)]
        return argv

    def resume_argv(self, lane, opts, session_id):
        if not session_id:
            raise DispatchError(
                "pi cannot resume without a session id; start a fresh run "
                "with the prior result carried in the brief")
        return ["pi", "--session", validate_session_id(session_id),
                *model_args(lane, opts)]

    def headless_argv(self, lane, opts, prompt_text, out_path="", session_id="",
                      resume_session=""):
        """`pi -p`, which prints the final message and exits.

        `--mode text` is stated rather than left to the default, because the
        other two modes print a protocol into the stream the headless substrate
        captures. There is no output-file flag, so `out_path` is accepted and
        unused: the answer file is written by the `deliver` tool, and the
        runner's salvage copies stdout there when a worker wrote nothing.
        """
        argv = ["pi", "-p", "--mode", "text", *model_args(lane, opts)]
        if resume_session:
            argv += ["--session", validate_session_id(resume_session)]
        elif session_id:
            argv += ["--session-id", validate_session_id(session_id)]
        return argv + [prompt_text]

    def validate_options(self, lane, opts):
        if opts.net:
            raise DispatchError(
                "--net is codex-only; a pi lane has no sandbox to open")
        if opts.image:
            raise DispatchError("--image is codex-only (codex `-i`)")

    def transcript_root(self):
        return session_root()

    def resolve_transcript(self, session_id, cwd, hint=""):
        if hint and Path(hint).exists():
            return hint
        if not session_id or not SESSION_ID_RE.match(session_id):
            return hint
        # A session file is named for when it started as well as for its id, and
        # filed under the working directory, so the id is searched for.
        root = self.transcript_root()
        matches = sorted(root.rglob(f"*{session_id}*")) if root.is_dir() else []
        return str(matches[0]) if matches else hint


register_driver(PiDriver())
