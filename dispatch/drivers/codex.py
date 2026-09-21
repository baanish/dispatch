"""The codex driver.

Two things are codex's alone and shape most of this file. It names its own
session rather than accepting one, so a run's session id is discovered from the
rollout it writes instead of being a fact from the start. And a substrate's
"agent done" signal is noise here: codex reports done at the bare idle prompt
before any task has run, and a fast turn may never report working at all, so its
turn is judged on the deliverable it was asked for.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..errors import DispatchError
from ..records import UUID_RE, SESSION_ID_RE, validate_session_id
from .base import DialogRules, Driver, register_driver

# How far into a rollout to look for the prompt dispatch typed. It is the first
# user turn; everything after it can be megabytes of tool output.
ROLLOUT_SCAN_LINES = 40


def session_root():
    """Where codex keeps its rollouts. `CODEX_HOME` is codex's own override."""
    home = (os.environ.get("CODEX_HOME") or "").strip()
    return (Path(home) if home else Path.home() / ".codex") / "sessions"


def read_session_meta(path):
    """(session_id, cwd) from a rollout's `session_meta` first line."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline(65536)
    except OSError:
        return "", ""
    try:
        meta = json.loads(first)
    except ValueError:
        return "", ""
    # A rollout is a file on disk, and a line of it that is not the record
    # shape would otherwise end the scan of every other candidate.
    if not isinstance(meta, dict) or not isinstance(meta.get("payload"), dict):
        return "", ""
    payload = meta["payload"]
    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
        return "", ""
    cwd = payload.get("cwd")
    return session_id, cwd if isinstance(cwd, str) else ""


def rollout_mentions(path, markers, max_lines=ROLLOUT_SCAN_LINES):
    """Does this rollout's opening turn carry one of the run's own markers?"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for _ in range(max_lines):
                line = handle.readline()
                if not line:
                    break
                if any(marker and marker in line for marker in markers):
                    return True
    except OSError:
        pass
    return False


# Stated both ways on every write run: left unsaid, the operator's own codex
# config decides, and a run without `--net` can have network after all.
NET = {True: "true", False: "false"}


def model_args(lane):
    """Model, effort, and the service tier when the lane names one.

    `tier` is optional in a lane, and an empty `service_tier=` is not "no tier"
    to codex: it is a value codex refuses, in an error that never mentions
    dispatch.
    """
    argv = ["-m", lane.model, "-c", f"model_reasoning_effort={lane.effort}"]
    if lane.tier:
        argv += ["-c", f"service_tier={lane.tier}"]
    return argv


class CodexDriver(Driver):
    name = "codex"
    agent_kind = "codex"
    # Two enters: the first is swallowed by the slash-command autocomplete
    # popup, so one enter leaves the command sitting there unsent.
    exit_command = ("/quit", 2)
    turn_signal = "deliverable"
    update_markers = ("updating codex via", "update ran successfully",
                      "please restart codex")
    # codex opens with a directory-trust dialog that no config override
    # pre-empts. Detection names the rule precisely, so answering it is specific
    # rather than a blind enter, and `1` is "Yes, continue".
    dialog_rules = DialogRules(
        answered_rules=("trust_directory",),
        answer_text="1",
        trust_attempts=2,
        screen_marker="do you trust")
    metered_key_vars = ("OPENAI_API_KEY",)
    names_own_session = True
    cli_binary = "codex"
    # Prints the signed-in account and exits non-zero when there is none.
    login_probe = ("codex", "login", "status")

    def launch_argv(self, lane, opts, session_id=""):
        argv = ["codex", *model_args(lane)]
        # No trust-level override. It parses, and it does not work: codex still
        # opens its directory-trust dialog with the key set. The dialog is
        # answered where it appears instead, and an inert flag on every command
        # line is worse than no flag, because the next reader believes it.
        # Nobody is watching a worker's screen, so escalations route to a
        # reviewing agent rather than waiting for a keyboard. Read-only runs
        # too: the answer file lives outside the sandbox, and on a stock codex
        # config writing it opens an approval form nobody will ever answer.
        argv += ["-a", "on-request",
                 "-c", "approvals_reviewer=guardian_subagent"]
        if opts.write:
            argv += ["-c", f"sandbox_workspace_write.network_access={NET[opts.net]}"]
        argv += ["-s", "workspace-write" if opts.write else "read-only",
                 "-C", opts.dir]
        # codex 0.154 exits on `--add-dir` under read-only ("effective
        # permissions do not allow additional writable roots"); read-only
        # already reads every path, so the flag only means anything with --write.
        if opts.write:
            for extra in opts.add_dirs:
                argv += ["--add-dir", extra]
        if opts.image:
            argv += ["-i", opts.image]
        return argv

    def resume_argv(self, lane, opts, session_id):
        # codex cannot be told its session id up front, so a run whose id was
        # never captured resumes by `--last`, which is cwd-filtered by default.
        session_id = validate_session_id(session_id) if session_id else ""
        argv = ["codex", "resume"] + ([session_id] if session_id else ["--last"])
        argv += model_args(lane)
        argv += ["-a", "on-request",
                 "-c", "approvals_reviewer=guardian_subagent"]
        if opts.write:
            argv += ["-c", f"sandbox_workspace_write.network_access={NET[opts.net]}"]
        argv += ["-s", "workspace-write" if opts.write else "read-only",
                 "-C", opts.dir]
        return argv

    def headless_argv(self, lane, opts, prompt_text, out_path="", session_id="",
                      resume_session=""):
        """`codex exec`, whose flags are its own and not the interactive binary's.

        `--skip-git-repo-check`, `-o`, and `--output-schema` exist here and
        nowhere else; passing any of them to the interactive form is an error
        rather than a no-op.
        """
        if resume_session:
            return self.headless_resume_argv(lane, opts, prompt_text, out_path,
                                             resume_session)
        argv = ["codex", "exec", "--skip-git-repo-check", *model_args(lane)]
        if opts.write:
            # `-a` belongs to the interactive binary; `codex exec` exits 2 on it.
            argv += ["-c", "approval_policy=on-request",
                     "-c", "approvals_reviewer=guardian_subagent"]
            argv += ["-c", f"sandbox_workspace_write.network_access={NET[opts.net]}"]
        argv += ["-s", "workspace-write" if opts.write else "read-only",
                 "-C", opts.dir]
        # codex 0.154 exits on `--add-dir` under read-only ("effective
        # permissions do not allow additional writable roots"); read-only
        # already reads every path, so the flag only means anything with --write.
        if opts.write:
            for extra in opts.add_dirs:
                argv += ["--add-dir", extra]
        if opts.image:
            argv += ["-i", opts.image]
        if opts.schema:
            argv += ["--output-schema", opts.schema]
        if out_path:
            argv += ["-o", str(out_path)]
        return argv + [prompt_text]

    def headless_resume_argv(self, lane, opts, prompt_text, out_path, session_id):
        """`codex exec resume`, which takes neither `-s`, `-C` nor `--add-dir`.

        So the sandbox moves to a `-c` override and the working root is the
        process's own working directory, which the substrate sets. The session
        id and the message are positional, behind `--`, because a message whose
        first character is `-` is text rather than flags.
        """
        sandbox = "workspace-write" if opts.write else "read-only"
        argv = ["codex", "exec", "resume", "--skip-git-repo-check",
                *model_args(lane), "-c", f"sandbox_mode={sandbox}"]
        if opts.write:
            # `-a` belongs to the interactive binary; `codex exec` exits 2 on it.
            argv += ["-c", "approval_policy=on-request",
                     "-c", "approvals_reviewer=guardian_subagent"]
            argv += ["-c", f"sandbox_workspace_write.network_access={NET[opts.net]}"]
        if opts.schema:
            argv += ["--output-schema", opts.schema]
        if out_path:
            argv += ["-o", str(out_path)]
        return argv + ["--", validate_session_id(session_id), prompt_text]

    def validate_options(self, lane, opts):
        if opts.net and not opts.write:
            raise DispatchError(
                "--net grants network inside the write sandbox; add --write")

    def transcript_root(self):
        return session_root()

    def capture_session(self, started_at, cwd="", markers=()):
        """Find the rollout codex wrote for this run: (id, path, confirmed).

        Two filters, because one is not enough. The first line is a
        `session_meta` record carrying the working directory, which rules out a
        rollout from another project or a codex the operator ran by hand. That
        still leaves several workers in one repo sharing a cwd, where "newest
        wins" hands an earlier run the later run's session, so the opening turn
        is checked too: it carries the prompt dispatch typed, which names this
        run's own directory and prompt file. A rollout that names this run is a
        confirmed match; falling back to newest-by-cwd is a guess, and says so.
        """
        root = session_root()
        if not root.is_dir():
            return "", "", False
        # Rollouts are filed under YYYY/MM/DD. Walking the whole tree can mean
        # thousands of files and many gigabytes, so only the day the run started
        # and the one after it (for a run that crossed midnight) are searched.
        days = {time.strftime("%Y/%m/%d", time.gmtime(started_at)),
                time.strftime("%Y/%m/%d", time.gmtime(started_at + 86400)),
                time.strftime("%Y/%m/%d", time.localtime(started_at))}
        candidates = []
        for stamp in days:
            folder = root / stamp
            if folder.is_dir():
                candidates.extend(folder.glob("*.jsonl"))
        target = os.path.realpath(cwd) if cwd else ""
        named, unnamed = [], []
        for path in candidates:
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            # A second of slack: the rollout is created moments after the worker
            # is, and filesystem and wall-clock stamps need not agree exactly.
            if stamp + 1 < started_at:
                continue
            session_id, rollout_cwd = read_session_meta(path)
            if not session_id:
                continue
            if target and os.path.realpath(rollout_cwd or "") != target:
                continue
            found = (stamp, session_id, str(path))
            if markers and rollout_mentions(path, markers):
                named.append(found)
            else:
                unnamed.append(found)
        if named:
            newest = max(named)
            return newest[1], newest[2], True
        if unnamed:
            newest = max(unnamed)
            return newest[1], newest[2], False
        return "", "", False

    def resolve_transcript(self, session_id, cwd, hint=""):
        if hint and Path(hint).exists():
            return hint
        if not session_id or not SESSION_ID_RE.match(session_id):
            return hint
        root = session_root()
        matches = sorted(root.rglob(f"*{session_id}*")) if root.is_dir() else []
        return str(matches[0]) if matches else hint


register_driver(CodexDriver())
