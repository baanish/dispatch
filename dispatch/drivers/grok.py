"""The grok driver.

grok is the bounded opinion lane: `--max-turns` caps it, subagents are off, and
it takes no write sandbox. Writing work routes to a driver that has one.

Its `--session-id` names a NEW conversation and refuses an existing one, so
resuming is `--resume` and nothing else.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import DispatchError
from ..records import SESSION_ID_RE, validate_session_id
from .base import Driver, register_driver

# The turn cap on an opinion run: an outside view is bounded work by definition.
MAX_TURNS = "12"


class GrokDriver(Driver):
    name = "grok"
    agent_kind = "grok"
    exit_command = ("/exit", 1)
    turn_signal = "agent-done"
    # No recorded occurrence of grok exiting for an update, so these come from
    # its own updater's strings rather than from a pane that ate a run.
    # Phrases that name grok, never a bare "installed successfully!": a package
    # manager prints that too, and a match relaunches the run.
    update_markers = ("updating grok", "please restart grok")
    metered_key_vars = ("XAI_API_KEY", "GROK_API_KEY")
    cli_binary = "grok"
    # Deliberately none, for the same reason as claude: the CLI exposes no
    # non-interactive auth check that does not start a session.
    login_probe = ()

    def launch_argv(self, lane, opts, session_id=""):
        argv = [
            "grok",
            "-m", lane.model,
            "--reasoning-effort", lane.effort,
            "--permission-mode", "auto",
            "--no-subagents",
            "--max-turns", MAX_TURNS,
        ]
        if session_id:
            argv += ["--session-id", validate_session_id(session_id)]
        argv += ["--cwd", opts.dir]
        return argv

    def resume_argv(self, lane, opts, session_id):
        if not session_id:
            raise DispatchError(
                "grok cannot resume without a session id; start a fresh run "
                "with the prior result carried in the brief")
        return ["grok", "--resume", validate_session_id(session_id),
                "-m", lane.model,
                "--reasoning-effort", lane.effort,
                "--permission-mode", "auto", "--no-subagents",
                "--max-turns", MAX_TURNS, "--cwd", opts.dir]

    def headless_argv(self, lane, opts, prompt_text, out_path="", session_id="",
                      resume_session=""):
        """The non-interactive form: `--single` prints one answer and exits.

        A bare prompt argument opens the TUI on it instead, so the one-shot
        entry point is the flag. There is no output-file flag, so the headless
        substrate captures stdout.
        """
        argv = ["grok", "--output-format", "plain",
                "-m", lane.model,
                "--reasoning-effort", lane.effort,
                "--permission-mode", "auto",
                "--no-subagents",
                "--max-turns", MAX_TURNS]
        if resume_session:
            # `--resume` carries its value with `=`: the value is optional, so a
            # detached one would be read as the prompt.
            argv += [f"--resume={validate_session_id(resume_session)}"]
        elif session_id:
            argv += ["--session-id", validate_session_id(session_id)]
        return argv + ["--cwd", opts.dir, "--single", prompt_text]

    def validate_options(self, lane, opts):
        if opts.write:
            raise DispatchError(
                "grok is the opinion lane and takes no write sandbox; route "
                "writing work to a codex or claude lane")
        if opts.net:
            raise DispatchError("--net is codex-only")
        if opts.image:
            raise DispatchError("--image is codex-only (codex `-i`)")

    def transcript_root(self):
        return Path.home() / ".grok" / "sessions"

    def resolve_transcript(self, session_id, cwd, hint=""):
        if hint and Path(hint).exists():
            return hint
        if not session_id or not SESSION_ID_RE.match(session_id):
            return hint
        root = self.transcript_root()
        matches = sorted(root.rglob(f"*{session_id}*")) if root.is_dir() else []
        return str(matches[0]) if matches else hint


register_driver(GrokDriver())
