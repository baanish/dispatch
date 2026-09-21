"""The claude driver.

Claude Code has no read-only sandbox flag, so the read lane is a tool allowlist
rather than a sandbox, and `--net` has no meaning here at all: the CLI brings its
own sandbox and dispatch does not get to widen it.

Its first-visit trust dialog matches the same generic "a form is waiting" rule as
every other claude form, which is why the dialog rules below hand that rule back
by default and carve out the trust dialog by the words it draws.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import DispatchError
from ..records import SESSION_ID_RE, validate_session_id
from .base import DialogRules, Driver, register_driver

# Claude has no read-only sandbox flag, so the read lane is an allowlist.
READ_TOOLS = "Read,Grep,Glob"


class ClaudeDriver(Driver):
    name = "claude"
    agent_kind = "claude"
    exit_command = ("/exit", 1)
    turn_signal = "agent-done"
    # The native installer updates in the background and the TUI reports it in
    # place, never exiting for it. These are here so that a claude which ever
    # does exit for an update is restarted rather than journaled as a failed
    # spawn, and this arm is expected to stay unused.
    update_markers = ("update installed", "restart to apply", "restart to update")
    # Every claude form matches one generic detection rule, including ones a
    # blind enter would accept on the operator's behalf, so the whole class is
    # handed back. The exemption is the first-visit trust dialog, recognised by
    # its own two markers: `i trust this folder` is the option text only this
    # dialog offers, and `accessing workspace:` is its header, so a form that
    # merely quotes one phrase does not qualify. That question is dispatch's own
    # to answer, because dispatch chose the directory and the worker chose
    # nothing. The answer is the cursor walked to the accepting row and then an
    # enter, because which row starts highlighted differs between releases: one
    # opens on "Yes, I trust this folder" and another on "No, exit", where a bare
    # enter quits. Never a digit, which selects by number and once picked "No,
    # quit". Two attempts, then it is the operator's: a dialog that will not
    # clear is one dispatch is reading wrong.
    dialog_rules = DialogRules(
        handback_rules=("live_blocked_form",),
        trust_markers=("accessing workspace:", "i trust this folder"),
        trust_keys=("enter",),
        trust_accept="yes, i trust this folder",
        trust_attempts=2)
    metered_key_vars = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                        "CLAUDE_CODE_OAUTH_TOKEN")
    cli_binary = "claude"
    # Deliberately none: every form that reports auth state either opens the TUI
    # or spends a turn, and doctor may do neither.
    login_probe = ()

    def launch_argv(self, lane, opts, session_id=""):
        argv = [
            "claude",
            "--model", lane.model,
            "--effort", lane.effort,
            "--permission-mode", "auto",
        ]
        if not opts.write:
            argv += ["--allowedTools", READ_TOOLS]
        if session_id:
            # Claude takes the id dispatch generates, so `continue` knows it
            # before the run has produced a single line of output.
            argv += ["--session-id", validate_session_id(session_id)]
        argv += ["--add-dir", opts.dir]
        for extra in opts.add_dirs:
            argv += ["--add-dir", extra]
        return argv

    def resume_argv(self, lane, opts, session_id):
        if not session_id:
            raise DispatchError(
                "claude cannot resume without a session id; start a fresh run "
                "with the prior result carried in the brief")
        argv = ["claude", "--resume", validate_session_id(session_id),
                "--model", lane.model, "--effort", lane.effort,
                "--permission-mode", "auto"]
        if not opts.write:
            argv += ["--allowedTools", READ_TOOLS]
        argv += ["--add-dir", opts.dir]
        return argv

    def headless_argv(self, lane, opts, prompt_text, out_path="", session_id="",
                      resume_session=""):
        """`claude -p`, which prints the final message and exits.

        There is no output-file flag, so the headless substrate captures stdout;
        `out_path` is accepted and unused for that reason.
        """
        argv = ["claude", "-p", prompt_text,
                "--model", lane.model,
                "--effort", lane.effort,
                "--permission-mode", "auto"]
        if not opts.write:
            argv += ["--allowedTools", READ_TOOLS]
        if resume_session:
            argv += ["--resume", validate_session_id(resume_session)]
        elif session_id:
            argv += ["--session-id", validate_session_id(session_id)]
        argv += ["--add-dir", opts.dir]
        for extra in opts.add_dirs:
            argv += ["--add-dir", extra]
        return argv

    def validate_options(self, lane, opts):
        if opts.net:
            raise DispatchError(
                "--net is codex-only; claude lanes use Claude Code's own sandbox")
        if opts.image:
            raise DispatchError("--image is codex-only (codex `-i`)")

    def transcript_root(self):
        return Path.home() / ".claude" / "projects"

    def resolve_transcript(self, session_id, cwd, hint=""):
        if hint and Path(hint).exists():
            return hint
        if not session_id or not SESSION_ID_RE.match(session_id):
            return hint
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        candidate = self.transcript_root() / slug / f"{session_id}.jsonl"
        return str(candidate) if candidate.exists() else hint


register_driver(ClaudeDriver())
