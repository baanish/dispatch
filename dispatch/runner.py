"""The run lifecycle: one worker, from a reserved slot to a journaled exit code.

Everything here is written against a Driver and a Substrate, never against a
vendor CLI or a terminal multiplexer. What that buys is the reason the split
exists: the same lifecycle drives a codex in a herdr pane and a claude in a tmux
one, and a new CLI or a new place to run it is an adapter rather than a branch.

Two things are worth knowing before reading `RunWrapper`:

- Completion is produced, not waited for. Interactive CLIs finish a turn and sit
  at their prompt forever, so the runner decides the turn is over, types the
  driver's exit command, and reads the real exit code back from the substrate.
- A turn ending is not a run ending. A worker that backgrounds a long command
  and ends its turn to wait is still working, and a human's queued message
  starts a turn dispatch never typed. So the run ends on its deliverable: the
  turn is over, it has stayed over across two looks, and the file the brief
  asked for exists.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from . import prompt as prompt_text
from . import remote
from .caps import Sweep, abandon_record, session_key
from .drivers import driver_for_lane
from .errors import DispatchError
from .policy import (DEPTH_ENV, child_depth, current_depth, hand_timeout_seconds,
                     parse_deadline, policy)
from .processes import popen_detached, resolve_python
from .records import (ABORT_MARKER, HEARTBEAT_SECONDS, append_status,
                      deliverable_path, hold_run_lock, new_run_id, out_copy_dir,
                      out_copy_path, read_output, release_run_lock, replace_text,
                      run_dir, save_final_record, save_heartbeat, save_record,
                      utc_now, validate_session_id, write_out_copy,
                      write_status_header)
from .substrates.base import SubstrateError, Worker, WorkerSetupError

# The one substrate error code the runner acts on rather than reports: the
# substrate watched for a state change and saw none, so there is no live TUI.
STALLED_CODE = "agent_prompt_stalled"

# How fast a watcher looks, and how far it backs off. A worker that finishes in
# ten seconds should not wait two more to be noticed, and one that runs for half
# an hour should not be asked about itself seven hundred times.
POLL_SECONDS = 0.25
POLL_MAX_SECONDS = 2.0
POLL_BACKOFF = 1.5

# The launch race: a shell at its prompt and a worker that has exited are the
# same picture, so a run too young to have started anything is not called gone.
START_GRACE_SECONDS = 5.0

# Screen unchanged this long: the worker is a candidate for judgement.
OUTPUT_IDLE_SECONDS = 120.0
# Any process in the worker's tree burning this much CPU is thinking.
CPU_BUSY_PERCENT = 5.0
# A file appearing is not a file finished: it has to stop changing first.
DELIVERABLE_QUIET_SECONDS = 5.0

# A CLI reports idle before it is ready: it can be idle within half a second,
# then blocked by a dialog, then run a startup turn before it will take a brief.
# Readiness is therefore idle that stays idle, not the first idle seen.
READY_IDLE_SECONDS = 2.0
READY_TIMEOUT_SECONDS = 120.0
# A successful write can be swallowed by Codex's startup redraws.
PROMPT_ACCEPT_SECONDS = 10.0
PROMPT_MAX_ATTEMPTS = 3
# What a settled TUI reports. `done` belongs here because a substrate says done
# for any settled prompt box with activity behind it, not only for a finished
# turn. The turn-over question is a different one, asked with DONE_STATES after
# a brief has gone in.
READY_STATES = ("idle", "done")
DONE_STATES = ("done",)
# How long a quiet TUI has to stay quiet before a turn counts as over.
TURN_SETTLE_SECONDS = 5.0
# How many consecutive ready looks make a turn's ending settled rather than the
# one-look done that surfaces while a queued message is handed over.
SETTLED_LOOKS = 2
# How long an answered dialog has to clear before dispatch will answer again.
TRUST_RECHECK_SECONDS = 6.0
# What a dialog is called when it is handed back on a substrate with no rule
# engine to name it: there, a dialog is a shape on a screen and nothing more.
UNNAMED_TRUST_RULE = "trust_dialog"
# `agent start` reporting success is not proof of life: a hung CLI can be
# reported interactive before its TUI ever drew.
ALIVE_PROBE_SECONDS = 5.0

# The exit ladder: how long the CLI has to act on its exit command, and how long
# before dispatch decides the command never landed at all.
EXIT_WAIT_SECONDS = 60.0
EXIT_RETRY_SECONDS = 20.0
EXIT_MAX_ATTEMPTS = 2
# Let an interrupted TUI settle before typing a correction into it.
STEER_INTERRUPT_SECONDS = 1.0

# Concurrent creates race inside a substrate; a home that dies at birth is
# retried with a fresh one.
WORKER_SETUP_ATTEMPTS = 3

# How long after a spawn an exit still counts as an early one. A recorded CLI
# took nine seconds to update and quit; the minutes here are for a cold package
# cache, not for a worker that did any work.
UPDATE_EXIT_SECONDS = 180.0
# Respawns per run. A CLI that announces an update on every launch is broken,
# and restarting it forever would hold a cap slot for a run that is never going
# to read its brief.
UPDATE_RESPAWN_CAP = 2

# Two rounds, then the failure is journaled: a worker that cannot satisfy a
# contract twice will not satisfy it on the third ask, and each round is paid
# inference.
SCHEMA_REPAIR_ROUNDS = 2

# How far behind `time.time()` a just-written file's mtime can read. Zero on
# POSIX, where the two come off the same clock; on Windows the stamp comes from
# the system tick, so a deliverable written right after the prompt can carry an
# mtime just before it.
from .processes import IS_WINDOWS  # noqa: E402  (used by the constant below)
MTIME_LAG_SECONDS = 0.05 if IS_WINDOWS else 0.0

# What a finished CLI prints on its way out. Two shapes in the wild, both
# landing on the same capture group: "Resume this session with: <cli> --resume
# <id>", and "codex resume <id>".
SESSION_RESUME_RE = re.compile(
    r"(?:--resume[=\s]\s*|\bcodex\s+resume\s+)"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


# --------------------------------------------------------------------------
# Liveness: free signals first, a paid judge only behind them
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LivenessSignals:
    """Everything the judge sees. Free signals only; a snapshot costs tokens."""

    worker_id: str
    at_prompt: bool
    child_pids: tuple
    output_idle_seconds: float
    cpu_percent: float          # -1.0 where CPU is unreadable
    elapsed_seconds: float
    seen_alive: bool = False
    screen: str = ""
    # Codex animates its idle composer, so its screen cannot prove progress.
    screen_is_progress: bool = True
    worker_status: str = ""
    transcript_idle_seconds: float | None = None
    session_seen: bool | None = None  # None where transcripts are not local


def free_signal_verdict(signals):
    """`gone` | `working` | `settled` | `starting`, from signals that cost nothing.

    `settled` is the only interesting one: the worker is alive, its screen has
    not moved, and nothing is burning CPU. That is either deep thought or a dead
    TUI, which is exactly the question the snapshot judge exists to answer.
    """
    if signals.at_prompt and not signals.child_pids:
        # A shell at its prompt and a worker that has exited are the same picture
        # to this probe. Calling it gone needs corroboration: either the worker
        # was seen running at some point, or enough time has passed that a cold
        # start cannot still be pending.
        if signals.seen_alive or signals.elapsed_seconds >= START_GRACE_SECONDS:
            return "gone"
        return "starting"
    if signals.screen_is_progress and \
            signals.output_idle_seconds < OUTPUT_IDLE_SECONDS:
        return "working"
    if signals.worker_status == "working":
        return "working"
    if signals.transcript_idle_seconds is not None and \
            signals.transcript_idle_seconds < OUTPUT_IDLE_SECONDS:
        return "working"
    if signals.cpu_percent >= CPU_BUSY_PERCENT:
        return "working"
    return "settled"


def snapshot_judge_seam(signals, rec=None):
    """Seam for a paid judge asked "thinking or dead?" about a settled worker.

    Unimplemented on purpose: it returns None, meaning no opinion. Wiring it to
    a real model run belongs to whoever wants to pay for one, and its verdict
    goes on the record before any kill, because a judge that kills a thinking
    worker silently is worse than no judge.
    """
    return None


LIVENESS_JUDGE = free_signal_verdict


def set_liveness_judge(judge):
    """Injection point: the verdict function is pure, so it is swappable."""
    global LIVENESS_JUDGE
    previous = LIVENESS_JUDGE
    LIVENESS_JUDGE = judge
    return previous


def judge_liveness(signals, rec=None):
    """Free signals first, the paid snapshot judge only behind them."""
    verdict = LIVENESS_JUDGE(signals)
    source = "free-signals"
    if verdict == "settled":
        second = snapshot_judge_seam(signals, rec)
        if second:
            verdict, source = second, "snapshot-judge"
    if rec is not None:
        # The verdict is on record before anything acts on it.
        rec["liveness"] = {"verdict": verdict, "source": source, "at": utc_now(),
                           "output_idle_seconds": round(signals.output_idle_seconds, 1),
                           "cpu_percent": signals.cpu_percent,
                           "children": len(signals.child_pids)}
    return verdict


# A deadline is when a run gets checked on, not when it dies. A wall-clock kill
# is wrong for an AI worker: slow inference, a loaded machine, or a long tool
# loop all make a healthy run look late. So only a worker that is demonstrably
# not working any more is killed, and anything the signals cannot agree on keeps
# its home: one that is still there can be inspected, steered, or killed by
# hand, and a destroyed one cannot be un-destroyed.
CHECKIN_KILL_VERDICTS = ("blocked", "dead", "stuck")


@dataclass(frozen=True)
class CheckinVerdict:
    """What a check-in decided, and the sentence that says why in the log."""

    verdict: str
    reason: str


def checkin_verdict(signals, worker_status, deliverable_seen, prior_reviews,
                    interval_seconds=0.0, blocked_rule="", answered_rules=(),
                    handback_rules=()):
    """`working` | `blocked` | `dead` | `stuck` | `review`, for a run at its deadline.

    Pure, so the ladder that decides whether to end a run can be read and tested
    without a worker. Only the first four kill; `review` says the signals
    disagree, and its answer is always to keep the home and look again.

    `prior_reviews` is how many check-ins in a row have already come back
    ambiguous, which is what makes "blocked or stuck across two consecutive
    check-ins" a kill and one bad look a `review`. A dialog is never a kill,
    whether dispatch answers it itself or hands it to a human, so a run waiting
    on a hand keeps its home however many times it is looked at.

    Every threshold is one the poll already judges liveness by. The exception is
    `interval_seconds`, the run's own deadline: a screen that moved at all since
    the previous check-in is a worker making progress, however far apart the two
    looks were, which is the signal a fixed idle window cannot express.
    """
    at_readable_prompt = signals.at_prompt and (
        signals.seen_alive or signals.elapsed_seconds >= START_GRACE_SECONDS)
    if not signals.child_pids and not at_readable_prompt:
        return CheckinVerdict(
            "dead", "no worker process in its home and no prompt to read a status from")
    if worker_status == "blocked":
        if blocked_rule in answered_rules:
            # This dialog is dispatch's own to answer, so it is never a kill:
            # one still standing at a check-in is a run to look at by hand.
            return CheckinVerdict(
                "review", f"blocked on {blocked_rule}, which dispatch answers itself")
        if blocked_rule in handback_rules:
            # Somebody is going to answer this one, so the sentence below does
            # not apply and neither does the kill: it waits for a human for as
            # long as the hand-wait policy allows.
            return CheckinVerdict(
                "review", f"blocked on {blocked_rule}; waiting on a human's hand, "
                          "not on the worker")
        held = f"blocked on {blocked_rule or 'a dialog dispatch cannot read'}"
        if prior_reviews:
            return CheckinVerdict("blocked",
                                  f"still {held}; nobody is going to answer it")
        return CheckinVerdict("review", f"{held}; one more check-in before that counts")
    if signals.screen_is_progress and \
            signals.output_idle_seconds < OUTPUT_IDLE_SECONDS:
        return CheckinVerdict(
            "working", f"the screen moved {int(signals.output_idle_seconds)}s ago")
    if signals.cpu_percent >= CPU_BUSY_PERCENT:
        return CheckinVerdict(
            "working", f"{signals.cpu_percent:.0f}% CPU in the worker's process tree")
    if worker_status == "working":
        return CheckinVerdict("working", "the substrate reports the worker working")
    if signals.transcript_idle_seconds is not None and \
            signals.transcript_idle_seconds < max(OUTPUT_IDLE_SECONDS, interval_seconds):
        return CheckinVerdict("working", "the transcript changed since the last check-in")
    if not signals.screen_is_progress and signals.session_seen is False and \
            not deliverable_seen:
        return CheckinVerdict("stuck", "no transcript or session after the first "
                              "check-in; no CPU or worker activity")
    if signals.screen_is_progress and signals.child_pids and interval_seconds and \
            signals.output_idle_seconds < interval_seconds:
        return CheckinVerdict(
            "working", "the worker is up and its screen moved since the last check-in")
    if prior_reviews and not deliverable_seen:
        return CheckinVerdict(
            "stuck", f"screen still for {int(signals.output_idle_seconds)}s, no CPU, "
                     f"worker {worker_status or 'untracked'}, nothing written")
    return CheckinVerdict(
        "review", f"worker {worker_status or 'untracked'}, screen still for "
                  f"{int(signals.output_idle_seconds)}s, "
                  f"{'a deliverable on disk' if deliverable_seen else 'nothing written'}, "
                  f"{len(signals.child_pids)} process(es): the signals disagree")


def checkin_count(rec):
    """How many times this run has been checked on since its deadline passed."""
    try:
        return int(rec.get("checkins") or 0)
    except (TypeError, ValueError):
        return 0


def checkin_reviews(rec):
    """How many check-ins in a row have come back ambiguous, from the record.

    Reset by any check-in that finds the worker working, so it counts a run of
    doubt rather than a lifetime of it.
    """
    try:
        return int(rec.get("checkin_reviews") or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Preparing a run
# --------------------------------------------------------------------------


def run_env(run_id, driver):
    """What the worker's environment must carry before the CLI starts.

    The ladder marker has to be the child's rung, not ours; the session key
    keeps every run a worker spawns counted against this session's allowance.

    Metered keys are blanked when policy says to. A worker's shell may inherit a
    daemon's environment rather than this process's, so checking our own
    environment proves nothing about what the worker will see: a key exported
    into the daemon's launch environment would quietly move a subscription lane
    onto per-token billing.
    """
    env = {DEPTH_ENV: str(child_depth()),
           "DISPATCH_SESSION": session_key(),
           "DISPATCH_RUN": str(run_id)}
    for var in blanked_keys(driver):
        env[var] = ""
    return env


def blanked_keys(driver):
    """The driver's metered key variables, when policy says to blank them."""
    return tuple(driver.metered_key_vars) if policy().blank_metered_keys else ()


def warn_metered_key(driver):
    """Say so loudly when the launcher's own environment carries a metered key.

    A warning rather than a refusal: refusing here makes a lane unusable under
    an ambient key dispatch cannot unset, and the worker never sees it either
    way when blanking is on. The warning stays loud because an ambient key is
    exactly how a subscription lane ends up on an invoice.
    """
    if not policy().blank_metered_keys:
        return
    for var in driver.metered_key_vars:
        if os.environ.get(var):
            print(f"dispatch: warning: {var} is set in this environment; the "
                  "worker's environment blanks it (verified before launch), but "
                  "this subscription lane should not be running around an "
                  "ambient metered key. Unset it.", file=sys.stderr)


def worker_path(rec, name):
    """One of a run's files, as the worker sees it.

    Local runs and shell-mode ones differ in exactly this. A run placed on a
    machine in shell mode has its brief, its prompt, and its deliverable in a
    directory over there, so every path dispatch writes into a prompt or types
    at that worker has to be the machine's; this one's would name nothing.
    """
    staging = rec.get("remote_staging")
    if staging:
        return posixpath.join(staging, name)
    return str(Path(rec["dir"]) / name)


def prepare_run(lane, brief_text, opts, substrate, kind="run", parent="",
                resume_session="", machine=None):
    """Create the durable run directory, argv, and prompt, without opening a home.

    Always called with the runs lock held: the record it writes is the slot
    reservation, so it has to exist before anything can be started.

    `machine` is the box a shell-mode run's pane will be on. It is what decides
    whose paths the prompt names, so it is settled here, before the prompt is
    built, rather than stamped on the record afterwards.
    """
    driver = driver_for_lane(lane)
    rec_id = new_run_id(lane.name)
    directory = run_dir(rec_id)
    directory.mkdir(parents=True, exist_ok=False)

    placement = remote.machine_fields(machine, rec_id) if machine is not None else {}
    brief_path = directory / "brief.md"
    brief_path.write_text(brief_text, encoding="utf-8")
    staging = placement.get("remote_staging", "")
    where = (lambda name: posixpath.join(staging, name)) if staging \
        else (lambda name: str(directory / name))
    paths = {"out": where("out.md"), "out_json": where("out.json"),
             "brief": where("brief.md")}

    # A CLI that names its own session is told nothing; the rest are handed one,
    # so `continue` knows it before the run has produced a line of output. A
    # resume is already named by the session it reopens.
    session_id = resume_session or (
        "" if driver.names_own_session else str(uuid.uuid4()))
    prompt = prompt_text.build_prompt(paths["brief"], paths, opts.schema)
    if not substrate.has_tui:
        # Nothing to type into, so the brief is an argv element and the command
        # line is the driver's one-shot form. `continue` is the same form with
        # the CLI's resume flag, since there is no session to reopen and adopt.
        # A schema run's deliverable is out.json, and the CLI's own output file
        # lands there whether or not the sandbox lets the worker write it.
        argv = driver.headless_argv(
            lane, opts, prompt,
            out_path=paths["out_json"] if opts.schema else paths["out"],
            session_id=session_id, resume_session=resume_session)
    elif resume_session:
        argv = driver.resume_argv(lane, opts, resume_session)
    else:
        argv = driver.launch_argv(lane, opts, session_id)

    (directory / "cmd.txt").write_text(
        " ".join(shlex.quote(a) for a in argv) + "\n", encoding="utf-8")
    (directory / "prompt.txt").write_text(prompt, encoding="utf-8")

    rec = {
        "id": rec_id,
        "kind": kind,
        "lane": lane.name,
        "driver": lane.driver,
        "substrate": substrate.name,
        "model": lane.model,
        "effort": lane.effort,
        "tier": lane.tier,
        "dir": str(directory),
        "cwd": opts.dir,
        "write": opts.write,
        "net": opts.net,
        "add_dirs": list(opts.add_dirs),
        "schema": opts.schema,
        "out_copy": out_copy_path(opts.out),
        "out_copy_dir": out_copy_dir(opts.out),
        "image": opts.image,
        "argv": argv,
        "prompt": prompt,
        "background": opts.bg,
        "foreground": not opts.bg,
        "deadline": opts.deadline,
        "deadline_seconds": parse_deadline(opts.deadline) if opts.deadline else None,
        "session_id": session_id,
        "resumed_from_session": resume_session,
        "parent": parent,
        "transcript": "",
        # The launcher's rung. The worker's is one below and is verified in its
        # own environment; a reader comparing this to the ENV line in status.log
        # is comparing two different things.
        "launcher_depth": current_depth(),
        "depth": current_depth(),
        "session": session_key(),  # both caps count from this, not from live homes
        "worker_id": "",
        "worker_group": "",
        "agent": substrate.agent_name(rec_id),
        "owner_pid": os.getpid(),
        "reserved_at": time.time(),
        "state": "reserved",
        "rc": None,
        "created": utc_now(),
        "finished": "",
        **placement,
    }
    save_record(rec)
    return rec


def record_worker(rec):
    """The substrate handle a record points at."""
    return Worker(id=rec.get("worker_id", ""), group=rec.get("worker_group", ""),
                  agent=rec.get("agent", ""))


def run_agent_name(rec, substrate):
    """The name this run registers with its substrate, kept on the record.

    `inspect` and `continue` reopen the same session later and have to name the
    same agent, so the name is derived once and stored.
    """
    name = rec.get("agent")
    if not name:
        name = substrate.agent_name(rec.get("id", ""))
        rec["agent"] = name
    return name


# --------------------------------------------------------------------------
# Schema and output
# --------------------------------------------------------------------------


def schema_error(rec):
    """Why `out.json` fails its contract, or empty when it passes.

    Structural validation only: the file has to exist and parse. A full JSON
    Schema validator is not in the standard library.
    """
    if not rec.get("schema"):
        return ""
    path = Path(rec["dir"]) / "out.json"
    if not path.is_file():
        return f"{path} was never written"
    try:
        json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError as exc:
        return f"not valid JSON ({exc})"
    return ""


def finalize_schema(rec):
    """Record whether the contract was met, once the worker is gone.

    The repair rounds already happened in-session; this is the honest journaling
    of what came out the other side.
    """
    if not rec.get("schema"):
        return rec
    error = schema_error(rec)
    rec["schema_valid"] = not error
    if error and rec.get("state") not in ("aborted", "killed", "timeout"):
        rec["state"] = "failed"
        rec["error"] = (f"schema not satisfied after {SCHEMA_REPAIR_ROUNDS} "
                        f"repair rounds: {error}")
        append_status(Path(rec["dir"]) / "status.log", f"SCHEMA-INVALID {utc_now()}")
    save_final_record(rec)
    return rec


def finalize_output(rec):
    """Leave out.md holding the deliverable, whatever the worker actually wrote.

    A schema run's deliverable is out.json, and out.md mirrors it so every reader
    has one place to look. When the worker wrote nothing at all, the last screen
    is better than silence.
    """
    directory = Path(rec["dir"])
    out_path = directory / "out.md"
    json_path = directory / "out.json"
    if rec.get("schema") and json_path.is_file() and json_path.stat().st_size:
        replace_text(out_path,
                     json_path.read_text(encoding="utf-8", errors="replace"))
        return
    if out_path.is_file() and out_path.stat().st_size:
        return
    screen_log = directory / "screen.log"
    if screen_log.is_file() and screen_log.stat().st_size:
        blob = screen_log.read_text(encoding="utf-8", errors="replace")
        replace_text(out_path, blob[-8000:])
        return
    replace_text(out_path, "")


def refresh_session_id(rec):
    """Make sure a run's native session id is on record, capturing it if needed.

    `continue` and the schema repair rounds both need it, and a CLI that names
    its own session has none until its transcript exists on disk.
    """
    if rec.get("session_id"):
        return rec["session_id"]
    driver = _driver_for_record(rec)
    if driver is None or not driver.names_own_session:
        return ""
    started = rec.get("started_at") or rec.get("reserved_at") or 0
    markers = (str(rec.get("id", "")), str(Path(rec["dir"]) / "prompt.txt"))
    session_id, transcript, confirmed = driver.capture_session(
        float(started or 0), rec.get("cwd", ""), markers)
    if session_id:
        rec["session_id"] = session_id
        rec["session_id_confirmed"] = confirmed
        if not confirmed:
            # Named the right directory but not this run: with several workers in
            # one repo it is the best guess available, and a guess is worth
            # saying out loud before `continue` resumes it.
            append_status(Path(rec["dir"]) / "status.log",
                          f"SESSION-INFERRED {utc_now()} {session_id} "
                          "(matched by directory, not by this run's prompt)")
    if transcript and not rec.get("transcript"):
        rec["transcript"] = transcript
    return rec.get("session_id", "")


def _driver_for_record(rec):
    from .drivers import get_driver
    try:
        return get_driver(rec.get("driver", ""))
    except DispatchError:
        return None


def resolve_transcript(rec, session_id="", hint=""):
    driver = _driver_for_record(rec)
    if driver is None:
        return hint
    return driver.resolve_transcript(session_id or rec.get("session_id", ""),
                                     rec.get("cwd", ""),
                                     hint or rec.get("transcript", ""))


TRUST_CURSOR = "\u276f"


def trust_dialog_keys(screen, rules):
    """The keys that accept the trust dialog as it is drawn now, or none.

    The cursor row and the accepting row are both read off the screen, and the
    cursor is moved from one to the other before the confirming keys. A screen
    that shows no cursor or no accepting row sends nothing: a guess here is an
    enter on "No, exit".
    """
    if not rules.trust_accept:
        return tuple(rules.trust_keys)
    rows = (screen or "").lower().splitlines()
    accept = [i for i, row in enumerate(rows) if rules.trust_accept in row]
    cursor = [i for i, row in enumerate(rows) if TRUST_CURSOR in row]
    if not accept or not cursor:
        return ()
    moves = accept[-1] - cursor[-1]
    return ("down",) * moves + ("up",) * -moves + tuple(rules.trust_keys)


def screen_is_trust_dialog(screen, driver):
    """Is this the driver's own first-visit trust dialog, and nothing else?

    All of the markers or no answer. A substrate may match this dialog under the
    same generic rule as every other form, so the screen is the only thing that
    separates the question dispatch already answered by choosing the directory
    from one only a human can answer.
    """
    markers = driver.dialog_rules.trust_markers
    if not markers:
        return False
    lowered = (screen or "").lower()
    return all(marker in lowered for marker in markers)


class RunWrapper:
    """One dispatch run in one worker home: dispatch owns its lifecycle.

    A substrate classifies workers by reading their screens and usually discards
    exit codes. This wrapper is the answer to both: it watches the worker's real
    child process, reports the terminal state once the worker has gone, and asks
    the substrate for the true exit code. The run record, not the substrate's
    detector, is the source of truth about what happened; the detector is what
    says so while it is happening.
    """

    def __init__(self, substrate, rec, judge_hook=None):
        self.substrate = substrate
        self.rec = rec
        self.driver = _driver_for_record(rec)
        if self.driver is None:
            raise DispatchError(f"{rec.get('id')} names no known driver")
        self.dir = Path(rec["dir"])
        self.status_path = self.dir / "status.log"
        # A transport repair belongs in the log of the run that tripped over it.
        # Substrates are shared between wrappers, so this is last-wrapper-wins;
        # a wrapper claims the log at construction, immediately before it makes
        # any call of its own.
        client = getattr(substrate, "client", None)
        if client is not None:
            client.log_path = self.status_path
        self.agent = run_agent_name(rec, substrate)
        self.judge_hook = judge_hook or judge_liveness
        self.worker = None
        self.seq = 0
        self._exit_sent = False
        self._exit_sent_at = 0.0
        self._exit_attempts = 0
        self._exit_forced = False
        self._repairs = 0
        self._ready_screen = None
        self._ready_since = 0.0
        self._trust_answered_at = 0.0
        # Enters put into a handed-back trust dialog this run, capped.
        self._trust_attempts = 0
        self._prompted_at = 0.0
        self._worked_since_prompt = False
        self._settled_since = None
        # What the substrate said on the last look, so the end-of-run condition
        # can ask whether the turn has settled without paying for a second probe.
        self._last_status = ""
        self._last_state_seq = None
        self._last_agent_seen = True
        # Set when a look finds the turn over but not yet settled, so a
        # reconcile sweep knows to take the second look itself.
        self._second_look_due = False
        self._last_text = ""
        self._last_change = time.time()

    # -- lifecycle -------------------------------------------------------

    def open(self):
        """Create the run's home and wait for a shell that can take a command.

        Concurrent same-second launches race inside a substrate: a home can be
        reported created and be gone before its first keystroke. A birth failure
        is therefore retried with a fresh home, a bounded number of times;
        refusals that must stand are not birth failures and propagate on the
        first home that reports them.
        """
        env = run_env(self.rec["id"], self.driver)
        for attempt in range(1, WORKER_SETUP_ATTEMPTS + 1):
            self.worker = replace(
                self.substrate.open(label=self.rec["id"],
                                    cwd=self.rec.get("cwd") or "", env=env),
                agent=self.agent)
            self.rec["worker_group"] = self.worker.group
            self.rec["worker_id"] = self.worker.id
            self.rec["substrate_version"] = self.substrate.version()
            save_record(self.rec)
            append_status(self.status_path,
                          f"WORKER {utc_now()} {self.substrate.name} "
                          f"{self.worker.id} group {self.worker.group}")
            try:
                self.substrate.wait_for_shell(self.worker)
                self.verify_environment()
                return self.worker
            except (WorkerSetupError, SubstrateError) as exc:
                gone = isinstance(exc, WorkerSetupError) or getattr(exc, "code", "") \
                    in ("pane_not_found", "workspace_not_found")
                if not gone or attempt == WORKER_SETUP_ATTEMPTS:
                    raise
                self.substrate.close(self.worker, release=False)
                append_status(self.status_path,
                              f"RETRY {utc_now()} worker setup #{attempt}: "
                              f"{exc}; recreating it")

    def attach(self, worker=None):
        """Take over a run whose home already exists, from any dispatch process.

        This is what makes a detached run reconcilable: state lives in the record
        and the home, so a later `dispatch status` can pick up exactly where the
        launcher left off.
        """
        if worker is not None:
            self.worker = replace(worker, agent=self.agent)
            self.rec["worker_group"] = self.worker.group
            self.rec["worker_id"] = self.worker.id
            save_record(self.rec)
            return self.worker
        if not self.rec.get("worker_id"):
            raise DispatchError(f"{self.rec['id']} has no worker to attach to")
        self.worker = record_worker(self.rec)
        return self.worker

    def verify_environment(self):
        keys = blanked_keys(self.driver)
        assignments = [(DEPTH_ENV, str(child_depth())),
                       ("DISPATCH_SESSION", session_key()),
                       ("DISPATCH_RUN", str(self.rec["id"]))]
        assignments += [(var, "") for var in keys]
        return self.substrate.verify_environment(
            self.worker, assignments, DEPTH_ENV, child_depth(), keys,
            self.status_path, self.rec["id"])

    def mark_seen_alive(self):
        """Record that this run's worker has actually been observed running.

        On the record rather than on this object, because a reconciling process
        builds a fresh wrapper: without the mark it would have to re-derive
        "has this ever started" from a single look, which is the look that
        cannot tell a cold start from a finished run.
        """
        if not self.rec.get("seen_alive"):
            self.rec["seen_alive"] = True
            save_record(self.rec)
            append_status(self.status_path, f"ALIVE {utc_now()} worker running")
        return True

    def adopt(self, worker):
        """Take over a home whose CLI is already running, and start the clock.

        The session was opened by `dispatch inspect` and has been sitting there
        since; this run owns it from now, including closing it at the end.
        """
        self.attach(worker)
        info = self.substrate.process_info(self.worker)
        self.rec["state"] = "running"
        self.rec["started"] = utc_now()
        self.rec["started_at"] = time.time()
        self.rec["shell_pid"] = info.shell_pid if info else None
        self.rec["seen_alive"] = True   # the CLI already holds the foreground
        save_record(self.rec)
        append_status(self.status_path,
                      f"ADOPTED {utc_now()} {self.worker.id} from inspect")
        write_status_header(self.status_path, self.rec["lane"],
                            self.rec["shell_pid"], self.rec.get("cwd", ""),
                            self.rec.get("argv") or [], self.rec["started"])
        return worker

    def start_worker(self, argv):
        """Spawn through the substrate, and stamp a fallback where a reader sees it."""
        spawn = self.substrate.start_worker(self.worker, self.driver, argv)
        self.rec["spawn_method"] = spawn.method
        if spawn.flag:
            self.rec["spawn_fallback"] = True
            self.rec["spawn_flag"] = spawn.flag
            self.rec["spawn_error"] = spawn.error
            print(f"dispatch: {spawn.flag}", file=sys.stderr)
        self.rec["state"] = "running"
        self.rec["started"] = utc_now()
        # Epoch seconds as well as the stamp: a deadline reconciled by another
        # process later has to measure from the same instant, not from its own.
        self.rec["started_at"] = time.time()
        # The header's `pid` is a real pid, so it holds the worker's shell: the
        # CLI's own pid does not exist yet at header time, and the shell is what
        # a reader can actually signal.
        info = self.substrate.process_info(self.worker)
        self.rec["shell_pid"] = info.shell_pid if info else None
        save_record(self.rec)
        write_status_header(self.status_path, self.rec["lane"],
                            self.rec["shell_pid"], self.rec.get("cwd", ""),
                            argv, self.rec["started"])
        if spawn.flag:
            append_status(self.status_path, f"FALLBACK {utc_now()} {spawn.flag}")
        spawn.alive = self.verify_alive()
        if spawn.alive:
            self.mark_seen_alive()
        return spawn

    def verify_alive(self, seconds=ALIVE_PROBE_SECONDS):
        """Did the CLI actually come up, whatever the substrate says about it?

        A successful spawn is not proof of life: a substrate can report a worker
        interactive while it is hung before its TUI ever drew. Screen movement or
        CPU is the evidence that a process is doing something.
        """
        before = self.substrate.read_screen(self.worker)
        deadline = time.time() + seconds
        while time.time() < deadline:
            time.sleep(0.25)
            info = self.substrate.process_info(self.worker)
            if info is None:
                break
            for pid in info.child_pids:
                cpu = self.substrate.cpu_percent([pid])
                if cpu is None or cpu > 0:
                    return True
            if self.substrate.read_screen(self.worker) != before:
                return True
        append_status(self.status_path,
                      f"NOT-ALIVE {utc_now()} no screen or CPU activity in {seconds}s")
        self.rec["worker_alive"] = False
        save_record(self.rec)
        return False

    def abandon(self, exc):
        """End a run that never reached its watch loop, without leaking its home.

        A home that outlives the process which created it is the whole point of
        the substrate, and exactly why an aborted launch has to clean up after
        itself: nothing else knows it exists yet.
        """
        if self.worker is None:
            return self.rec
        # The screen first, always. A run abandoned before it delivered its brief
        # is exactly the one whose screen nobody will ever see again.
        with contextlib.suppress(Exception):
            replace_text(self.dir / "screen.log",
                         self.substrate.read_screen(self.worker))
        append_status(self.status_path,
                      f"ABANDONED {utc_now()} {type(exc).__name__}: {exc}")
        self.rec["state"] = "failed"
        self.rec["error"] = f"{type(exc).__name__}: {exc}"
        self.rec["rc"] = None
        self.rec["finished"] = utc_now()
        self.rec["closed_by"] = "abandon"
        save_final_record(self.rec)
        append_status(self.status_path, "state: failed")
        with contextlib.suppress(Exception):
            self.close()
        return self.rec

    def close(self, release=True):
        """Homes close when the run ends: the CLI's session store is the afterlife."""
        if self.worker is None:
            return
        self.substrate.close(self.worker, release=release)

    # -- readiness and dialogs -------------------------------------------

    def wait_until_ready(self, seconds=None, wait_for_hand=True):
        """Block until the worker will actually take a brief.

        Three things have to be true at once and stay true: the substrate says
        the TUI is interactive, its status is settled, and its screen is not a
        dialog. The first idle is not enough: a CLI reaches idle in half a
        second, raises a trust dialog three seconds later, and then runs a
        startup turn before it is really listening.

        The screen is asked separately because a substrate's status lags its own
        screen rules by seconds, and trusting the status alone counts those
        seconds as readiness and types the brief into the dialog. Getting this
        wrong is not a delay, it is a wrong answer: a numbered dialog reads
        typed text as an option choice.

        True when the worker is ready, False when it is standing at a dialog only
        a human can clear and `wait_for_hand` says not to wait. A worker held by
        one of those is healthy, so its ready clock does not run and the timeout
        below is never its ending. A resume whose session is already working is
        False for the same reason: it is not late, it is finishing the turn
        `--resume` picked back up.
        """
        seconds = READY_TIMEOUT_SECONDS if seconds is None else seconds
        deadline = time.time() + seconds
        hand_budget = hand_timeout_seconds()
        idle_since = None
        self._trust_answered_at = 0.0
        self._trust_attempts = 0
        while time.time() < deadline:
            found = self.substrate.status(self.worker)
            if not found.tracked:
                # No record at all: a typed fallback started the CLI, so the
                # substrate is not tracking it and there is no status to read.
                # The screen is the only evidence left.
                if self.screen_is_ready_untracked():
                    return True
                time.sleep(min(0.25, READY_IDLE_SECONDS / 2))
                continue
            trust_on_screen = self.screen_has_trust_prompt()
            if found.status == "blocked" or trust_on_screen:
                idle_since = None
                answered = (self.answer_trust_prompt() if trust_on_screen
                            else self.answer_blocking_dialog())
                if not answered:
                    # Not ours to answer, or already answered and not yet
                    # redrawn. Wait it out rather than type at it again.
                    if self.rec.get("needs_hand"):
                        if not wait_for_hand:
                            return False
                        # A run waiting on a human is not late. It waits for the
                        # hand-wait policy's budget, or forever when that is what
                        # the policy says.
                        if hand_budget is None:
                            deadline = time.time() + seconds
                        else:
                            deadline = max(deadline, time.time() + min(seconds,
                                                                       hand_budget))
                    time.sleep(0.5)
                continue
            if found.status == "working" and self.resumed_into_a_live_turn():
                return False
            if found.status in READY_STATES and found.ready \
                    and not self.screen_is_blocked():
                idle_since = time.time() if idle_since is None else idle_since
                if time.time() - idle_since >= READY_IDLE_SECONDS:
                    append_status(self.status_path,
                                  f"READY {utc_now()} {self.agent} idle and settled")
                    return True
            else:
                idle_since = None
            # Never slower than the window being measured.
            time.sleep(min(0.25, READY_IDLE_SECONDS / 2))
        if self.rec.get("needs_hand"):
            raise DispatchError(
                f"{self.rec['id']}: still waiting on a human's hand after "
                f"{int(seconds)}s; answer the dialog in its home, or raise "
                "`hand_timeout`")
        raise SubstrateError(
            f"{self.rec['id']}: the worker never settled into an idle TUI in "
            f"{seconds}s, so its brief was never delivered")

    def screen(self):
        """The worker's current screen, or empty when it cannot be read."""
        return self.substrate.read_screen(self.worker)

    def screen_is_ready_untracked(self):
        """Readiness for a worker the substrate is not tracking.

        Wait for the screen to stop changing, then refuse to treat a dialog as
        readiness. Only the driver's own trust dialog is recognised, by its own
        words; anything else that settles is typed into, because from out here a
        TUI's ordinary input box and a question are the same picture.
        """
        screen = self.screen()
        if screen != self._ready_screen:
            self._ready_screen = screen
            self._ready_since = time.time()
            return False
        if time.time() - self._ready_since < READY_IDLE_SECONDS:
            return False
        if self.screen_has_trust_prompt():
            self.answer_trust_prompt()
            return False
        if screen_is_trust_dialog(screen, self.driver):
            return self.answer_trust_screen()
        append_status(self.status_path,
                      f"READY {utc_now()} screen settled (nothing tracks this worker)")
        return True

    def screen_has_trust_prompt(self):
        marker = self.driver.dialog_rules.screen_marker
        if not marker:
            return False
        screen = self.screen().lower()
        lines = [line.strip().lstrip("›❯> ") for line in screen.splitlines()
                 if line.strip()]
        # Require the choices beside the active footer; old trust text cannot
        # authorize another dialog that also says "Press enter to continue".
        return marker in screen and lines[-3:] == [
            "1. yes, continue", "2. no, quit", "press enter to continue"]

    def answer_trust_prompt(self):
        if self._trust_answered_at and \
                time.time() - self._trust_answered_at < TRUST_RECHECK_SECONDS:
            return False
        if self._trust_attempts >= self.driver.dialog_rules.trust_attempts:
            self.note_needs_hand(UNNAMED_TRUST_RULE)
            return False
        self.substrate.send_tui_line(self.worker,
                                    self.driver.dialog_rules.answer_text,
                                    log_path=self.status_path)
        self._trust_attempts += 1
        self._trust_answered_at = time.time()
        self.rec["trust_approved"] = utc_now()
        save_record(self.rec)
        append_status(self.status_path,
                      f"TRUST-APPROVED {utc_now()} answered the trust dialog "
                      f"on screen with {self.driver.dialog_rules.answer_text!r}")
        self._ready_screen = None
        return False

    def answer_trust_screen(self):
        """Answer the driver's trust dialog where nothing named it as a rule.

        A CLI whose trust dialog is recognised by its own words rather than by a
        detection rule has no path to `handle_handback_form` on a substrate that
        tracks no agent: there is no `blocked` status to route through. Without
        this the dialog is just a screen that has stopped changing, and the next
        thing typed into it is the brief, at a numbered selector.

        Bounded like the tracked path: two answers, and then the dialog is a
        human's, because one that will not clear is one dispatch is reading
        wrong. Always False, so readiness is measured again on the redraw.
        """
        rules = self.driver.dialog_rules
        if self._trust_attempts >= rules.trust_attempts:
            self.note_needs_hand(UNNAMED_TRUST_RULE)
            return False
        keys = trust_dialog_keys(self.screen(), rules)
        if not keys:
            self.note_needs_hand(UNNAMED_TRUST_RULE)
            return False
        self.substrate.send_keys(self.worker, keys)
        self._trust_attempts += 1
        self._trust_answered_at = time.time()
        self.rec["trust_approved"] = utc_now()
        save_record(self.rec)
        append_status(self.status_path,
                      f"TRUST-APPROVED {utc_now()} answered the trust dialog on "
                      f"screen with {'+'.join(keys)}: nothing here "
                      "tracks the agent, so the screen is the whole evidence")
        self._ready_screen = None
        return False

    def screen_is_blocked(self):
        """Is a dialog on this worker's screen, whatever its status still says?

        A substrate's screen rules and its status are two clocks, and the screen
        one is ahead: the screen reports blocked from the moment the dialog is
        drawn, seconds before the status stops saying idle. Readiness is a
        question about the screen, so this is what answers it.
        """
        return self.substrate.screen_state(self.worker) == "blocked"

    def blocked_rule(self):
        return self.substrate.blocked_rule(
            self.worker, self.driver.dialog_rules.blocked_rules())

    def answer_blocking_dialog(self):
        """Answer the one dialog dispatch is allowed to answer, or report it.

        Answered once, then left alone until it demonstrably did not take: a
        substrate keeps reporting blocked for a moment after the answer lands,
        while the TUI redraws, so answering on every poll puts more digits into
        the composer as a message.
        """
        rules = self.driver.dialog_rules
        waited = time.time() - self._trust_answered_at
        if self._trust_answered_at and waited < TRUST_RECHECK_SECONDS:
            return False
        rule = self.blocked_rule()
        if rule not in rules.answered_rules:
            if rule in rules.handback_rules:
                return self.handle_handback_form(rule)
            if rule and self.rec.get("blocked_rule") != rule:
                self.rec["blocked_rule"] = rule
                save_record(self.rec)
                append_status(self.status_path,
                              f"BLOCKED {utc_now()} rule {rule}: dispatch does not "
                              "answer dialogs it cannot read")
            return False
        if self._trust_answered_at and rules.screen_marker \
                and rules.screen_marker not in self.screen().lower():
            # The substrate still says blocked but the dialog is gone from the
            # screen: it is catching up, not waiting for us.
            return False
        self.substrate.send_tui_line(self.worker, rules.answer_text,
                                     log_path=self.status_path)
        self._trust_answered_at = time.time()
        self.rec["trust_approved"] = utc_now()
        save_record(self.rec)
        append_status(self.status_path,
                      f"TRUST-APPROVED {utc_now()} answered {rule} with "
                      f"{rules.answer_text!r}")
        return True

    def handle_handback_form(self, rule):
        """Answer the driver's own trust dialog, or hand the form back.

        The rule name is not the authorisation here, the screen is: everything
        else the handback rule matches keeps the hand-back, including a form that
        has merely got one of the markers on it.

        The answer is confirmed rather than assumed, and the dialog leaving the
        screen is the receipt: a worker still blocked a recheck later with the
        dialog still up gets exactly one more attempt, and after that dispatch
        stops typing.
        """
        rules = self.driver.dialog_rules
        # Only before the brief goes in. Until then the screen is the CLI's own;
        # after it, the markers can be text the worker printed above a form that
        # asks something else, and an enter there approves what nobody read.
        if self.rec.get("prompted") or \
                not screen_is_trust_dialog(self.screen(), self.driver):
            self.note_needs_hand(rule)
            return False
        if self._trust_attempts >= rules.trust_attempts:
            self.note_needs_hand(rule)
            return False
        keys = trust_dialog_keys(self.screen(), rules)
        if not keys:
            self.note_needs_hand(rule)
            return False
        self.substrate.send_keys(self.worker, keys)
        self._trust_attempts += 1
        self._trust_answered_at = time.time()
        self.rec["trust_approved"] = utc_now()
        save_record(self.rec)
        append_status(self.status_path,
                      f"TRUST-APPROVED {utc_now()} answered {rule} with "
                      f"{'+'.join(keys)}: the driver's trust dialog, "
                      "on a directory dispatch chose")
        return True

    def resumed_into_a_live_turn(self):
        """Is this launch a resume that landed on a session already working?

        `dispatch continue` relaunches with the driver's resume flag, and a
        session left mid-turn picks that turn straight back up, so the CLI is
        genuinely working the moment it comes up. Reading that as not-ready would
        time out and close a healthy worker for being busy at launch.

        So resumes wait on working the way they wait on a hand-back, and the wait
        happens in the poll loop: the message goes in when the turn settles.
        Unlike a hand-back the run is waiting on a worker rather than on a human,
        so the check-in ladder runs from launch and an eternally spinning resume
        is ruled by it.
        """
        if not self.rec.get("resumed_from_session"):
            return False
        if not self.rec.get("resume_busy"):
            self.rec["resume_busy"] = utc_now()
            save_record(self.rec)
            append_status(self.status_path,
                          f"RESUME-BUSY {utc_now()} the resumed session is still "
                          "working; the message goes in when its turn ends")
        return True

    def note_needs_hand(self, rule):
        """Say, once and everywhere a human looks, that only a person clears this.

        On the record because the wall, `dispatch status`, and whichever process
        watches this run next all read it from there. Once, because the ready
        loop asks twice a second. To stderr as well, because a foreground run has
        somebody at the keyboard right now and a line in a log file they are not
        tailing is not a hand-off.
        """
        if self.rec.get("needs_hand") == rule:
            return False
        self.rec["needs_hand"] = rule
        self.rec["blocked_rule"] = rule
        save_record(self.rec)
        held = (f"blocked on {rule} in {self.rec.get('worker_id') or '?'}: "
                "dispatch does not answer this dialog. Answer it there and the "
                "run carries on.")
        append_status(self.status_path, f"NEEDS-HAND {utc_now()} {held}")
        print(f"dispatch: {self.rec['id']} {held}", file=sys.stderr)
        return True

    def clear_needs_hand(self):
        """It was answered: stop showing this run as waiting on a hand.

        Mutates without saving, so the caller writes the record once alongside
        whatever else it just learned. True when there was something to clear.
        """
        if not self.rec.get("needs_hand"):
            return False
        self.rec["needs_hand"] = ""
        append_status(self.status_path, f"HAND-CLEARED {utc_now()} the dialog is gone")
        return True

    # -- prompting -------------------------------------------------------

    def report(self, state, message=""):
        """State to the substrate's UI, and only after the worker is gone.

        A report from an outside source replaces the substrate's own detection
        rather than sitting alongside it: reporting once evicts the named agent,
        so every later probe answers "not found". That is why this is called
        once, at the terminal transition, rather than on every heartbeat.

        Never fatal: a report is not the run.
        """
        self.seq += 1
        try:
            self.substrate.report_state(self.worker, state, message=message,
                                        seq=self.seq)
        except SubstrateError as exc:
            append_status(self.status_path, f"REPORT-FAILED {utc_now()} {exc}")

    def note_session(self, session_id, session_path=""):
        """Record the CLI's native session id and hand it to the substrate too.

        The run record joins to the CLI's own transcript, and a finished home's
        session is what makes `dispatch inspect` possible later.
        """
        session_id = validate_session_id(session_id)
        self.rec["session_id"] = session_id
        resolved = session_path or resolve_transcript(self.rec, session_id)
        if resolved:
            self.rec["transcript"] = resolved
        save_record(self.rec)
        try:
            self.substrate.report_session(self.worker, session_id, resolved)
        except SubstrateError as exc:
            append_status(self.status_path,
                          f"SESSION-REPORT-FAILED {utc_now()} {exc}")
        return session_id

    def prompt_file(self, text):
        """Write this prompt where the worker can open it, and name that path.

        A local worker reads the run directory. A shell-mode one reads its
        machine's staging directory, so the file goes up the same connection its
        pane is driven over: a path this machine has and that one does not is
        how a worker ends up with no brief at all.
        """
        (self.dir / "prompt.txt").write_text(text, encoding="utf-8")
        if not self.rec.get("remote_staging"):
            return str(self.dir / "prompt.txt")
        return remote.stage_file(self.rec, remote.machine_of_record(self.rec),
                                 "prompt.txt", text)

    def prompt_worker(self, text):
        """Hand the worker its brief once its TUI is ready to read it."""
        prompt_path = self.prompt_file(text)
        found = self.substrate.status(self.worker)
        if found.status == "blocked":
            # Typing at a dialog answers it: a numbered selector reads a line
            # containing a digit as that option, and the enter after it confirms.
            raise SubstrateError(
                f"{self.rec['id']}: the worker is blocked on a dialog; refusing "
                "to type a brief into it")
        confirm = self.driver.name == "codex"
        before = self.prompt_evidence() if confirm else None
        # An unconfirmed Codex write must not look like an accepted brief to the
        # next watcher. Other drivers retain their delivery contract.
        self.rec["prompted"] = "" if confirm else utc_now()
        # The state this brief goes in on, so the `done` it started from cannot
        # be read as the `done` it ends with.
        self.rec["prompt_state_seq"] = found.seq
        # A turn dispatch started itself, so the look that sees it go working
        # must not be journaled as a human's, and the settledness the previous
        # turn's ending accumulated is void.
        self.rec["turns"] = int(self.rec.get("turns") or 0) + 1
        self.rec["turn_over_at"] = ""
        self.rec["end_ready_looks"] = 0
        self.clear_needs_hand()
        save_record(self.rec)
        self._prompted_at = time.time()
        self._worked_since_prompt = False
        self.forget_deliverable()
        for attempt in range(1, PROMPT_MAX_ATTEMPTS + 1):
            method = self.send_prompt(text, prompt_path, before)
            if not confirm or (self.rec.get("remote_staging") and
                               method not in ("agent.prompt", "delivered")):
                # Remote shell transcripts are unavailable to this watcher.
                self.rec["prompted"] = utc_now()
                save_record(self.rec)
                return method
            until = time.monotonic() + PROMPT_ACCEPT_SECONDS
            while True:
                found = None if method == "delivered" else self.substrate.status(self.worker)
                # Herdr can report Codex's animated idle composer as working.
                # Local acceptance requires new output, including typed retries.
                if method == "delivered" or (self.rec.get("remote_staging") and
                                             found.status == "working") or any(
                        current and current != previous
                        for previous, current in zip(before, self.prompt_evidence())):
                    self.rec["prompted"] = utc_now()
                    save_record(self.rec)
                    append_status(self.status_path,
                                  f"PROMPT-ACCEPTED {utc_now()} attempt {attempt}")
                    return method
                info = self.substrate.process_info(self.worker)
                if info is None or info.at_prompt:
                    # The poll owns early exits, including self-update respawns.
                    return method
                if found.status == "blocked":
                    raise SubstrateError(
                        f"{self.rec['id']}: prompt was never accepted; "
                        "the worker is blocked on a dialog")
                if time.monotonic() >= until:
                    break
                time.sleep(POLL_SECONDS)
            if attempt < PROMPT_MAX_ATTEMPTS:
                append_status(self.status_path,
                              f"PROMPT-RETRY {utc_now()} attempt {attempt + 1}")
        raise SubstrateError(
            f"{self.rec['id']}: prompt was never accepted after "
            f"{PROMPT_MAX_ATTEMPTS} attempts")

    def send_prompt(self, text, prompt_path, before):
        """One delivery attempt, including the substrate's typing fallback."""
        try:
            method = self.substrate.deliver_prompt(self.worker, text)
        except SubstrateError as exc:
            if getattr(exc, "code", "") == STALLED_CODE:
                # Authoritative: the substrate watched for a state change and saw
                # none, so there is no live TUI to type into. Typing anyway would
                # put the brief into a hung program's buffer and then wait out
                # the deadline for a worker that never existed.
                self.rec["worker_alive"] = False
                save_record(self.rec)
                append_status(self.status_path, f"STALLED {utc_now()} {exc}")
                raise SubstrateError(
                    f"{self.rec['id']}: the worker never came alive "
                    f"({exc.code}); nothing was prompted") from exc
            if self.prompt_was_delivered(text, before):
                return "delivered"
            # The brief still has to get in, because a worker with no brief is
            # not a worker, so it goes in as keystrokes. One line naming a file,
            # and never the prompt body: a refused write usually means the
            # substrate never detected the agent, which means the keystrokes
            # land in an interactive shell, where every newline is a command.
            append_status(self.status_path,
                          f"PROMPT {utc_now()} the write channel refused ({exc}); "
                          "typing it")
            self.substrate.send_tui_line(
                self.worker,
                prompt_text.fallback_prompt_line(shlex.quote(prompt_path)),
                log_path=self.status_path)
            return "send-text"
        append_status(self.status_path, f"PROMPT {utc_now()} via {method}")
        return method

    def codex_transcript(self):
        """Find this run's transcript without borrowing a same-directory session."""
        if self.rec.get("remote_staging"):
            return ""
        if self.rec.get("session_id") and self.rec.get("session_id_confirmed") is not False:
            path = resolve_transcript(self.rec)
        else:
            session_id, path, confirmed = self.driver.capture_session(
                self.started_at(), self.rec.get("cwd", ""),
                (self.rec["id"], str(self.dir / "prompt.txt")))
            if not confirmed:
                return ""
            self.rec["session_id"] = session_id
            self.rec["session_id_confirmed"] = True
        if path:
            self.rec["transcript"] = path
        return path

    def prompt_evidence(self):
        """Only new file content proves acceptance of a fast, already-ended turn."""
        evidence = []
        for path in (self.codex_transcript(), deliverable_path(self.rec)):
            try:
                stat = Path(path).stat() if path else None
            except OSError:
                stat = None
            evidence.append((str(path), stat.st_size, stat.st_mtime_ns)
                            if stat and stat.st_size else None)
        return evidence

    def deliver_brief(self, wait_for_hand=True):
        """Get this run's brief in front of the worker that has just launched.

        Nothing to do on a substrate with no TUI: the driver's one-shot form
        carries the prompt as an argv element, so the brief went in with the
        process and the turn it starts is already running.

        False when the worker is not ready and the brief is still owed, which is
        a hand-back dialog or a resume into a live turn; the poll loop hands it
        over when the worker settles.
        """
        if not self.substrate.has_tui:
            self.rec["prompted"] = utc_now()
            self.rec["turns"] = int(self.rec.get("turns") or 0) + 1
            save_record(self.rec)
            self._prompted_at = time.time()
            append_status(self.status_path,
                          f"PROMPT {utc_now()} delivered in the launch argv")
            return True
        if not self.wait_until_ready(wait_for_hand=wait_for_hand):
            return False
        self.prompt_worker(self.rec["prompt"])
        return True

    def prompt_was_delivered(self, text, before=None):
        """Did the brief land, whatever the call said on its way out?

        A refusal is not evidence of non-delivery: a synchronous delivery can
        time out on a turn that received the prompt and is busy working on it,
        and re-sending types the whole thing in again on top. So before
        re-sending anything, look for work or new files. Other drivers can also
        acknowledge delivery by showing the prompt text on screen.
        """
        if (self.driver.name != "codex" or self.rec.get("remote_staging")) and \
                self.substrate.status(self.worker).status == "working":
            append_status(self.status_path,
                          f"PROMPT {utc_now()} the worker is working; not re-sending")
            return True
        if self.driver.name == "codex":
            # Text left in the composer is exactly the swallowed-submit case.
            if before is not None and any(
                    current and current != previous
                    for previous, current in zip(before, self.prompt_evidence())):
                append_status(self.status_path,
                              f"PROMPT {utc_now()} new output; not re-sending")
                return True
            return False
        needle = (text or "").strip().splitlines()[0][:60] if text.strip() else ""
        if needle and needle in self.screen():
            append_status(self.status_path,
                          f"PROMPT {utc_now()} the brief is on screen; not re-sending")
            return True
        return False

    def deliver_pending_prompt(self):
        """Hand over a message the launcher could not, once the worker will take it.

        Two launchers leave one behind: a background one that walked into a
        hand-back dialog, because hanging the caller's shell on a human is not a
        background run, and any resume that landed on a session still finishing
        its own turn. So whichever process is watching when it settles is the one
        that delivers, and the record is what makes it exactly once.

        Two idle looks, not one, for the same reason readiness is settled idle:
        the CLI runs its own startup turn once it is trusted, and a message
        delivered into that one is a message nobody read.
        """
        if self.substrate.status(self.worker).status not in READY_STATES:
            if self.rec.get("hand_idle_looks"):
                self.rec["hand_idle_looks"] = 0
                save_record(self.rec)
            return False
        looks = int(self.rec.get("hand_idle_looks") or 0) + 1
        self.rec["hand_idle_looks"] = looks
        save_record(self.rec)
        if looks < 2:
            return False
        self.prompt_worker(self.rec["prompt"])
        return True

    def reprompt(self, text):
        """Type a follow-up into a worker that is still up, for schema repair.

        False when there is nothing left to talk to, which is the normal case
        once a worker has followed its instruction to exit the session.
        """
        info = self.substrate.process_info(self.worker)
        if info is None:
            append_status(self.status_path, f"REPROMPT {utc_now()} the home is gone")
            return False
        if info.at_prompt:
            append_status(self.status_path,
                          f"REPROMPT {utc_now()} session already exited")
            return False
        self.prompt_worker(text)
        self.rec["state"] = "running"
        self.rec["finished"] = ""
        save_record(self.rec)
        return True

    # -- signals ---------------------------------------------------------

    def signals(self, info, started_at, collect=True):
        """Free signals for the judge, refreshed from one look at the worker.

        `collect=False` skips the screen read: reconciling a background run only
        needs to know whether the worker is done, and pulling a full screen for
        every open home on every dispatch invocation is not free.
        """
        text = self._last_text
        if collect:
            text = self.substrate.read_screen(self.worker)
            if text != self._last_text:
                self._last_text = text
                self._last_change = time.time()
        children = tuple(info.child_pids)
        idle = time.time() - self._last_change
        codex_tui = self.driver.name == "codex" and self.substrate.has_tui
        status = self.substrate.status(self.worker).status if codex_tui else ""
        transcript_idle = None
        session_seen = None
        if codex_tui and not self.rec.get("remote_staging"):
            transcript = self.codex_transcript()
            session_seen = bool(transcript or (self.rec.get("session_id") and
                                self.rec.get("session_id_confirmed") is not False))
            if transcript:
                with contextlib.suppress(OSError):
                    transcript_idle = max(0.0, time.time() - Path(transcript).stat().st_mtime)
        # Codex's animation never goes quiet. Other drivers defer the CPU probe
        # until screen output stops. Measure the whole tree, including tools.
        cpu = None
        if children and (codex_tui or idle >= OUTPUT_IDLE_SECONDS):
            cpu = self.substrate.cpu_percent(children)
        return LivenessSignals(
            worker_id=self.worker.id, at_prompt=info.at_prompt,
            child_pids=children, output_idle_seconds=idle,
            cpu_percent=-1.0 if cpu is None else cpu,
            elapsed_seconds=time.time() - started_at,
            seen_alive=bool(self.rec.get("seen_alive")), screen=text,
            screen_is_progress=not codex_tui, worker_status=status,
            transcript_idle_seconds=transcript_idle, session_seen=session_seen)

    def started_at(self):
        """When the worker began, in epoch seconds, from the record.

        Read from the record rather than from a local clock so a run reconciled
        by a different process an hour later measures its deadline from the same
        instant its launcher did.
        """
        try:
            return float(self.rec.get("started_at") or 0) or time.time()
        except (TypeError, ValueError):
            return time.time()

    def deliverable_landed(self):
        """Is there anything on disk to show for this run yet?

        Existence, not settledness: a check-in asks whether the worker has
        produced anything at all, and a file still being written is the clearest
        evidence there is that it has not stopped working.
        """
        try:
            return deliverable_path(self.rec).stat().st_size > 0
        except OSError:
            return False

    # -- the self-update restart -----------------------------------------

    def update_exit_marker(self):
        """The self-update banner behind this worker's exit, or "".

        A CLI that updates itself at launch prints its updater's banner, exits 0
        within seconds, and never reads the brief it was about to be handed: the
        run lands failed with no deliverable for a CLI that is now perfectly
        healthy. The only honest reading of that screen is to start it again.

        Three things have to be true, and the first two are what keep a genuine
        crash out: nothing was produced, the exit was early, and the screen since
        this spawn carries one of the driver's own update phrases.

        Asked only where the worker's process is already gone and the shell owns
        its home again, which is what "let the update finish first" amounts to.
        """
        if self.deliverable_landed():
            return ""
        markers = self.driver.update_markers
        if not markers:
            return ""
        if self.rec.get("prompted") and \
                time.time() - self.started_at() > UPDATE_EXIT_SECONDS:
            return ""
        screen = self.substrate.screen_since_spawn(
            self.worker, self.rec["id"]).lower()
        return next((marker for marker in markers if marker in screen), "")

    def respawn_after_update(self, marker):
        """Start this run's CLI again, once it has updated itself.

        The same run throughout: id, directory and record are the ones the first
        process had, and the relaunch takes the whole launch sequence a cold
        spawn takes, because a CLI coming up is a cold CLI however many times it
        has been started here. `respawns` goes on the record so a background
        watcher in another process reads the same count the launcher wrote.

        False once the cap is spent, with the loop named on the record.
        """
        append_status(self.status_path,
                      f"UPDATE-DETECTED {utc_now()} {marker!r} on screen and the "
                      "worker exited without delivering anything")
        count = int(self.rec.get("respawns") or 0) + 1
        if count > UPDATE_RESPAWN_CAP:
            self.rec["update_error"] = (
                f"the {self.rec.get('driver') or 'worker'} CLI announced a "
                f"self-update ({marker!r}) and exited without reading its brief "
                f"on {count} launches in a row; dispatch stopped restarting it "
                f"after {UPDATE_RESPAWN_CAP}")
            save_record(self.rec)
            append_status(self.status_path,
                          f"UPDATE-LOOP {utc_now()} {self.rec['update_error']}")
            return False
        self.rec["respawns"] = count
        # The new CLI has been handed nothing, so the brief is owed again.
        self.rec["prompted"] = ""
        self.rec["prompt_state_seq"] = None
        self.rec["hand_idle_looks"] = 0
        self.clear_needs_hand()
        save_record(self.rec)
        append_status(self.status_path,
                      f"RESPAWN {utc_now()} #{count} relaunching the lane command "
                      "in the same home under the same run")
        self.substrate.wait_for_shell(self.worker)
        self.verify_environment()
        self.start_worker(self.rec["argv"])
        # `wait_for_hand=False` because this is inside the poll loop: a respawn
        # that walks into a dialog only a human can clear leaves the brief owed
        # and lets the poll hand it over when the worker settles, instead of
        # blocking the watcher and stopping its heartbeat.
        self.deliver_brief(wait_for_hand=False)
        return True

    # -- polling ---------------------------------------------------------

    def poll(self, deadline_seconds=None, started_at=None, collect=True):
        """One look. None while the worker runs, else (rc, timed_out).

        Completion signal, in order of authority: the worker process is gone and
        the shell owns the foreground again (briefs end "then exit the session",
        so this is the normal ending); or the home itself disappeared, which is
        what a worker that exits its shell leaves behind.
        """
        started_at = self.started_at() if started_at is None else started_at
        # A shell-mode worker writes its deliverable on its own machine, and
        # every question asked about that file below is asked of the local
        # mirror, so the mirror is refreshed first.
        remote.fetch_deliverable(self.rec)
        info = self.substrate.process_info(self.worker)
        if info is None:
            # The shell itself exited: no prompt is left to read a status from.
            append_status(self.status_path, f"WORKER-GONE {utc_now()}")
            return (None, False)
        signals = self.signals(info, started_at, collect=collect)
        if signals.child_pids:
            self.mark_seen_alive()
        self.judge_hook(signals, self.rec)
        # Accepting the prompt before the shell has even forked the worker would
        # read the previous command's status as this run's exit code, so an
        # unstarted run has to age past the launch race first.
        settled = self.rec.get("seen_alive") or \
            signals.elapsed_seconds >= START_GRACE_SECONDS
        if signals.at_prompt and settled:
            # Before the exit code, because a CLI that quit to finish updating
            # itself exits 0 and that reads as a clean run with nothing to show.
            marker = self.update_exit_marker()
            if marker and self.respawn_after_update(marker):
                return None
            return (self.capture_rc(), False)
        if self.rec.get("needs_hand") and not self.rec.get("prompted"):
            # Below the exit code, because "No, exit" is one of the dialog's own
            # options and a worker that took it has ended this run. Above
            # everything else, because a brief that never went in leaves no turn
            # whose end could be detected: a settled screen here is the dialog,
            # not a finished worker. Nor is a run waiting on a hand late, so no
            # check-in either.
            self.deliver_pending_prompt()
            return None
        if self.rec.get("resume_busy") and not self.rec.get("prompted"):
            # A resume that launched into a live turn: the message has not gone
            # in yet, so deliver it when the worker settles. Above the exit
            # ladder because the `done` that ends the resumed turn belongs to
            # that turn and not to this one. Not a `return`, unlike the hand-back
            # above: this run waits on a worker rather than on a human, so it
            # stays subject to the check-ins that rule an eternal spin dead.
            self.deliver_pending_prompt()
        elif signals.child_pids and self._exit_sent:
            # The exit command went in and the worker is still here. A settled
            # screen at this point is not a worker thinking, it is a command that
            # never landed, so this escalates rather than waits. Ahead of the
            # deadline check on purpose, so a run whose work is already done is
            # journaled as done rather than as a timeout, and so only one of the
            # two paths can ever write a terminal state.
            if self.escalate_exit():
                return (None, False)
        elif self.substrate.has_tui and signals.child_pids and not self._exit_sent:
            # The worker is still at its TUI. Interactive CLIs finish a turn and
            # wait forever rather than exiting, so dispatch ends the session as
            # soon as the turn is over. A headless worker has no TUI and no exit
            # command: it ends itself, and the branch above is what notices.
            self.end_session_if_done(signals)
        if deadline_seconds and not self._exit_sent:
            # Not while the exit ladder is running: once the exit command has
            # gone in, that ladder owns the ending and is bounded, so the two can
            # never both write a terminal state.
            # Every check-in that finds the worker alive buys it another
            # interval, so what is due here is the original deadline times the
            # looks this run has already had.
            due = deadline_seconds * (1 + checkin_count(self.rec))
            if signals.elapsed_seconds >= due:
                outcome = self.check_in(signals, deadline_seconds)
                if outcome is not None:
                    return outcome
        return None

    def check_in(self, signals, interval_seconds):
        """Judge a run that reached its deadline: extend it, or end it.

        None to keep polling, or `poll`'s (rc, timed_out) pair once the verdict
        is one that kills. The count, the verdict and the time go on the record
        because the next check-in is quite likely to be made by another process.
        """
        signals = self.checkin_signals(signals)
        found_status = self.substrate.status(self.worker).status
        rule = self.blocked_rule() if found_status == "blocked" else ""
        prior = checkin_reviews(self.rec)
        rules = self.driver.dialog_rules
        found = checkin_verdict(signals, found_status, self.deliverable_landed(),
                                prior, interval_seconds=interval_seconds,
                                blocked_rule=rule,
                                answered_rules=rules.answered_rules,
                                handback_rules=rules.handback_rules)
        count = checkin_count(self.rec) + 1
        self.rec["checkins"] = count
        self.rec["checkin_verdict"] = found.verdict
        self.rec["checkin_reason"] = found.reason
        self.rec["checkin_at"] = utc_now()
        self.rec["checkin_reviews"] = prior + 1 if found.verdict == "review" else 0
        append_status(self.status_path,
                      f"CHECKIN {utc_now()} #{count} {found.verdict}: {found.reason}")
        if found.verdict not in CHECKIN_KILL_VERDICTS:
            save_record(self.rec)
            append_status(self.status_path,
                          f"CHECKIN-EXTENDED {utc_now()} next check-in at "
                          f"{int(interval_seconds * (count + 1))}s")
            return None
        if found.verdict != "stuck":
            # `timeout` is the state of a run that spent its time; these two did
            # not spend it, they stopped being a run, and the record says which.
            self.rec["error"] = f"killed at check-in {count}: {found.reason}"
        save_record(self.rec)
        append_status(self.status_path,
                      f"DEADLINE {utc_now()} {int(interval_seconds)}s exceeded and "
                      f"check-in {count} found the worker {found.verdict}")
        self.substrate.kill_worker_tree(self.worker)
        return (None, True)

    def checkin_signals(self, signals):
        """The same signals, measured honestly enough to end a run on.

        `poll` measures screen stillness against this object and skips the CPU
        read while the screen still looks fresh, which is right for a watcher and
        wrong for a reconciling process that has only ever looked once: to that
        one every worker is a working worker. So the screen fingerprint lives on
        the record, where stillness survives a process boundary, and CPU is read
        outright here, because a kill must not rest on a reading nobody took.
        """
        text = signals.screen or self.substrate.read_screen(self.worker)
        fingerprint = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
        idle = signals.output_idle_seconds
        if fingerprint == self.rec.get("checkin_screen"):
            with contextlib.suppress(TypeError, ValueError):
                since = float(self.rec.get("checkin_screen_since") or 0)
                idle = max(idle, time.time() - since) if since else idle
        else:
            self.rec["checkin_screen"] = fingerprint
            self.rec["checkin_screen_since"] = time.time()
        cpu = signals.cpu_percent
        if cpu < 0 and signals.child_pids:
            reading = self.substrate.cpu_percent(signals.child_pids)
            cpu = -1.0 if reading is None else reading
        return replace(signals, output_idle_seconds=idle, cpu_percent=cpu)

    def watch(self, deadline_seconds=None, poll_seconds=None, close=True):
        """Poll until the worker is done, then journal its exit code."""
        started_at = self.started_at()
        last_beat = 0.0
        interval = POLL_SECONDS if poll_seconds is None else poll_seconds
        while True:
            outcome = self.poll(deadline_seconds, started_at)
            if outcome is not None:
                return self.finish(outcome[0], outcome[1], close=close)
            if time.time() - last_beat >= HEARTBEAT_SECONDS and not self._exit_sent:
                # Nothing to report once the exit is in flight: a heartbeat after
                # the exit was confirmed reads as a worker that is still going.
                last_beat = time.time()
                self.heartbeat()
            time.sleep(interval)
            interval = min(interval * POLL_BACKOFF, POLL_MAX_SECONDS)

    def poll_once(self, deadline_seconds=None):
        """Reconcile a detached run in one pass: this replaces the supervisor.

        Once the exit command has gone in, this keeps polling for the short,
        deterministic stretch it takes a CLI to quit and hand the shell back.
        Otherwise a finished worker would need three separate `dispatch status`
        calls to be noticed, and its cap slot would stay held between them.
        """
        deadline = time.time() + EXIT_WAIT_SECONDS
        second_look = False
        while True:
            outcome = self.poll(deadline_seconds, collect=False)
            if outcome is not None:
                return self.finish(outcome[0], outcome[1])
            if self._second_look_due and not self._exit_sent and not second_look:
                # A turn that looks over on this sweep's only look. The rule
                # wants a second one, and taking it here rather than leaving it
                # to the next sweep is what keeps a `dispatch status`
                # authoritative about a run that has just finished. Exactly one,
                # so a status that flaps cannot hang the caller.
                second_look = True
                time.sleep(POLL_SECONDS)
                continue
            if not self._exit_sent or time.time() >= deadline:
                self.heartbeat()
                return self.rec
            time.sleep(POLL_SECONDS)

    def heartbeat(self):
        append_status(self.status_path, f"HEARTBEAT {utc_now()} working")
        # The record heartbeats too: a run in a pane has no pid to check, so
        # "when was this last seen alive" has to be readable without parsing the
        # log. No state report here: reporting evicts the substrate's named
        # agent, which would blind every probe for the rest of the run.
        save_heartbeat(self.rec)

    # -- ending the turn and the run -------------------------------------

    def turn_is_over(self, signals=None):
        """Has the worker finished its turn? The evidence differs per driver.

        A substrate's `done` is trustworthy for some CLIs and noise for others,
        which is what `Driver.turn_signal` records. A driver judged on its
        deliverable is one whose done fires at the bare idle prompt before any
        task has run, so believing it would end sessions the instant they opened.
        """
        signal = self.driver.turn_signal
        found = self.substrate.status(self.worker)
        # An untracked worker's statuses are unmeasurable rather than bad: that
        # is what a typed fallback leaves behind.
        self._last_agent_seen = found.tracked
        status = found.status
        self._last_status = status
        self._last_state_seq = found.seq
        # Every look, including the blocked ones below: settledness is a run of
        # consecutive ready looks, and a look that is not counted is a run that
        # never breaks.
        self.note_working_look(status)
        self.note_ready_look(status)
        if status == "blocked":
            # Not fatal and not ours to answer: dispatch never types blind enters
            # at a dialog it cannot read.
            rule = self.blocked_rule()
            if rule in self.driver.dialog_rules.handback_rules:
                # The same rule mid-run as at startup: a trust dialog goes to a
                # human, loudly, and the run waits.
                self.note_needs_hand(rule)
                return False
            if self.rec.get("blocked_at") is None:
                self.rec["blocked_at"] = utc_now()
                save_record(self.rec)
                append_status(self.status_path,
                              f"BLOCKED {utc_now()} the worker is blocked; it is "
                              "waiting on something dispatch did not type")
            return False
        if self.clear_needs_hand():
            save_record(self.rec)
        if status == "working":
            self._worked_since_prompt = True
            self._settled_since = None
            if self.working_is_corroborated():
                self.note_turn_started_by_hand()
        if signal != "deliverable":
            if found.tracked:
                return self.agent_turn_is_over(status, found.seq)
            # Nothing tracks this worker, so there is no `done` to wait for and
            # no working look to corroborate one with. The deliverable is the
            # only evidence left, which is what the other signal already runs on.
            return self.untracked_turn_is_over()
        if self.deliverable_is_settled():
            return True
        # No deliverable. A worker judged only on its output would hold its home
        # forever when it writes nothing, so the free signals are the backstop:
        # the substrate says it is not working, its screen has not moved for two
        # minutes, and nothing is burning CPU. All three, deliberately: any one
        # of them alone would end a session that was still thinking.
        if signals is None or status not in ("done", "idle"):
            return False
        if signals.output_idle_seconds < OUTPUT_IDLE_SECONDS:
            return False
        # CPU is a veto, not a vote: a busy tree keeps the session open, and an
        # unreadable reading is no evidence either way. Refusing to end on an
        # unreadable one would hang a foreground run, which has no deadline to
        # rescue it, on any machine where the CPU probe fails.
        if signals.cpu_percent >= CPU_BUSY_PERCENT:
            return False
        return True

    def untracked_turn_is_over(self):
        """Turn detection for an agent-signal driver on a substrate that tracks none.

        `agent_turn_is_over` rests on the substrate having called the worker
        working at least once since it was prompted, and a substrate that tracks
        no agents never calls it anything: on tmux, a claude or grok turn could
        never be seen to end, so the pane and the cap slot were held until
        somebody killed the run by hand.

        So the turn is judged on the file the worker was asked to write, the way
        a deliverable-signal driver's is, plus one condition that signal does not
        need: the file has to have been written during this turn. Without it the
        previous turn's deliverable would end the next one the moment it started.
        """
        return self.deliverable_is_settled() and self.deliverable_after_prompt()

    def agent_turn_is_over(self, status, state_seq=None):
        """Turn detection for a driver whose substrate status is trustworthy.

        `done` is the fast path, for a `done` this turn earned. The slow path is
        for a CLI that leaves a prompt suggestion in its input box after the
        final turn, so the screen rule reports idle and `done` never arrives, and
        the run rides to its deadline with a finished deliverable on disk.

        So a turn is also over when the worker was seen working since it was
        prompted, has gone quiet, stays quiet for a moment, and left the
        deliverable it was asked for behind. All four, because any one of them
        alone is a worker that is merely between thoughts.
        """
        if status in DONE_STATES and self.done_ends_this_turn(state_seq):
            return True
        if not self._worked_since_prompt or status not in ("idle", "done"):
            self._settled_since = None
            return False
        if self._settled_since is None:
            self._settled_since = time.time()
            return False
        if time.time() - self._settled_since < TURN_SETTLE_SECONDS:
            return False
        if not self.deliverable_after_prompt():
            return False
        append_status(self.status_path,
                      f"TURN-SETTLED {utc_now()} idle since the deliverable "
                      "landed; the substrate never said done")
        return True

    def done_ends_this_turn(self, state_seq):
        """Is this `done` the end of the brief's turn, or the one it began at?

        A substrate says done for a settled prompt box with any activity behind
        it, so a CLI that has just had its trust dialog answered is already done
        when the brief goes in, and stays done until the worker moves. Ending the
        session on that one would exit the worker before it had read a word. The
        state-change counter is what tells the two apart.

        Unknown either side means the old answer, yes: an unstamped record or a
        substrate that does not report the counter must not leave a finished
        worker holding its home until the deadline.
        """
        stamped = self.rec.get("prompt_state_seq")
        if stamped is None or state_seq is None:
            return True
        return state_seq != stamped

    def deliverable_after_prompt(self):
        """Did the worker write its deliverable during this turn?

        Corroboration for the slow path above: an idle TUI with nothing new on
        disk is a worker waiting, not a worker finished.
        """
        try:
            return deliverable_path(self.rec).stat().st_mtime \
                >= self._prompted_at - MTIME_LAG_SECONDS
        except OSError:
            return False

    def deliverable_is_settled(self):
        """Has the worker finished writing its deliverable, or only started it?

        A file appearing is not a file finished. A worker that writes an outline
        and keeps researching would be sent its exit command mid-task and have
        the outline journaled as the answer, so the file has to stop changing
        before it counts as the turn signal.

        The observation lives on the record rather than on this object, because
        the object does not outlive one look: a reconcile sweep builds a fresh
        wrapper and polls once, so an in-memory first sight would reset the quiet
        window on every `dispatch status`.
        """
        path = deliverable_path(self.rec)
        try:
            stat = path.stat()
        except OSError:
            stat = None
        if stat is None or not stat.st_size:
            return self.forget_deliverable()
        # Through JSON, so a list: a tuple would come back unequal to itself
        # after one save-and-load round trip.
        signature = [stat.st_size, stat.st_mtime]
        if list(self.rec.get("deliverable_seen") or []) != signature:
            self.rec["deliverable_seen"] = signature
            self.rec["deliverable_since"] = time.time()
            save_record(self.rec)
            return False
        try:
            since = float(self.rec.get("deliverable_since") or 0)
        except (TypeError, ValueError):
            return self.forget_deliverable()
        return (time.time() - since) >= DELIVERABLE_QUIET_SECONDS

    def forget_deliverable(self):
        """Drop a recorded sighting, for a deliverable that is gone or empty."""
        if self.rec.get("deliverable_seen") is not None:
            self.rec["deliverable_seen"] = None
            self.rec["deliverable_since"] = 0.0
            save_record(self.rec)
        return False

    def note_turn_started_by_hand(self):
        """Journal an extra turn a human started, and otherwise leave it alone.

        A message sent to a live worker mid-turn is queued by the CLI and
        delivered the instant that turn ends, which starts a turn dispatch did
        not type and knows nothing about. From out here the only trace is the
        state counter moving and the status going back to working.

        There is nothing to do about it except not mistake it for the end of the
        run, so this only counts the turn and says so in the log. The record's
        prompt stamps are deliberately left alone: they belong to the brief, and
        which turn eventually writes the deliverable is not what ends the run.
        """
        if not self.rec.get("turn_over_at"):
            # The turn dispatch started is still running, so this working is that
            # one. `turn_over_at` is stamped only once a turn has been seen over,
            # and cleared here, which keeps this to one line per turn.
            return False
        self.rec["turn_over_at"] = ""
        turns = int(self.rec.get("turns") or 1) + 1
        self.rec["turns"] = turns
        save_record(self.rec)
        append_status(self.status_path,
                      f"TURN {utc_now()} {turns} started (not by dispatch)")
        return True

    def note_working_look(self, status):
        """Count consecutive working looks, so one flap is not a turn."""
        if status == "working":
            looks = int(self.rec.get("working_looks") or 0)
            if looks < SETTLED_LOOKS:
                looks += 1
                self.rec["working_looks"] = looks
                save_record(self.rec)
            return looks
        if self.rec.get("working_looks"):
            self.rec["working_looks"] = 0
            save_record(self.rec)
        return 0

    def working_is_corroborated(self):
        """True once working has held for SETTLED_LOOKS looks."""
        return int(self.rec.get("working_looks") or 0) >= SETTLED_LOOKS

    def note_ready_look(self, status):
        """Count this look towards settledness, or break the run of them.

        The count lives on the record because a reconcile sweep builds a fresh
        wrapper and looks exactly once, so an in-memory count would never reach
        two for a background run whose watcher died.
        """
        if status not in READY_STATES:
            if not self.working_is_corroborated():
                # One look is not a turn. A CLI at its prompt box can flap to
                # working and back every couple of seconds, and a flap that
                # breaks the run of ready looks is a run that never reaches two.
                return int(self.rec.get("end_ready_looks") or 0)
            if self.rec.get("end_ready_looks"):
                self.rec["end_ready_looks"] = 0
                save_record(self.rec)
            return 0
        looks = int(self.rec.get("end_ready_looks") or 0)
        if looks >= SETTLED_LOOKS:
            # Counted high enough already, which keeps a worker that sits idle
            # for an hour from rewriting its record every poll.
            return looks
        looks += 1
        self.rec["end_ready_looks"] = looks
        save_record(self.rec)
        return looks

    def turn_end_is_settled(self):
        """Two consecutive ready looks.

        One look cannot tell a finished run from a queued turn about to start.
        A CLI hands a human's queued message over the instant a turn ends, and a
        substrate surfaces a one-look done in the gap; a done that is working
        again on the next look was never an ending.
        """
        if not self._last_agent_seen:
            # Nothing to look at twice: an untracked worker's statuses are all
            # empty, and waiting for two ready ones would hold its home until the
            # deadline. An unmeasurable signal must not become a veto.
            return True
        return int(self.rec.get("end_ready_looks") or 0) >= SETTLED_LOOKS

    def note_turn_without_deliverable(self):
        """A settled turn ended with nothing on disk: say so once, nudge once.

        The nudge is for the worker that thinks it is finished and is not. Every
        other reason a turn ends empty is a worker that is still going, and
        typing at it every poll would be a second brief rather than a reminder.

        So exactly one nudge per run, stamped on the record so a reconciling
        process cannot send a second. What ends a worker that has genuinely
        stopped is the check-in ladder.
        """
        turns = int(self.rec.get("turns") or 1)
        if self.rec.get("turn_ended_logged") == turns:
            return False
        self.rec["turn_ended_logged"] = turns
        save_record(self.rec)
        append_status(self.status_path,
                      f"TURN-ENDED {utc_now()} {turns} no deliverable")
        if self.rec.get("nudged"):
            return False
        self.rec["nudged"] = utc_now()
        name = deliverable_path(self.rec).name
        append_status(self.status_path,
                      f"NUDGE {utc_now()} turn {turns} ended without {name}")
        with contextlib.suppress(SubstrateError):
            # The path the worker was given, not the one this side reads: on a
            # shell-mode run those are two different machines' directories.
            self.prompt_worker(prompt_text.nudge_prompt(
                worker_path(self.rec, name), worker_path(self.rec, "brief.md")))
        return True

    def end_session_if_done(self, signals=None):
        """End the run once the turn is over AND the deliverable is on disk.

        A turn ending is not a run ending. A message a human queues is delivered
        the moment turn 1 ends and starts turn 2; a worker that backgrounds a
        long command and ends its turn to wait would have the exit take its
        children with it.

        So the run ends on its deliverable: the turn is over, it has stayed over
        across two looks, and the file the brief asked for exists. A turn that
        ends without it leaves the worker idle with the run still running and the
        check-ins still ticking.

        Validate-and-steer happens here, while the worker is still up: once the
        session is closed the only way to ask for a correction is to pay for a
        whole new one, and the worker that already has the context is the
        cheapest thing in the building to ask.
        """
        if not self.turn_is_over(signals):
            return False
        # Stamped before the settledness gate, because a turn a human queues a
        # message behind ends here too: the stamp is how the next look knows the
        # working it finds is a new turn rather than this one still running.
        if not self.rec.get("turn_over_at"):
            self.rec["turn_over_at"] = utc_now()
            save_record(self.rec)
        if not self.turn_end_is_settled():
            self._second_look_due = True
            return False
        if not self.deliverable_landed():
            self.note_turn_without_deliverable()
            return False
        if self.request_schema_repair():
            return False
        return self.send_exit_command()

    def request_schema_repair(self):
        """Ask a finished worker to fix its own output. True when it was asked."""
        if not self.rec.get("schema") or self._repairs >= SCHEMA_REPAIR_ROUNDS:
            return False
        error = schema_error(self.rec)
        if not error:
            return False
        self._repairs += 1
        append_status(self.status_path,
                      f"SCHEMA-REPAIR {utc_now()} round {self._repairs}: {error}")
        message = prompt_text.repair_prompt(worker_path(self.rec, "out.json"), error)
        with contextlib.suppress(SubstrateError):
            self.prompt_worker(message)
        return True

    def send_exit_command(self):
        """Type the driver's own exit command, the way anything is typed at a TUI.

        The receipt is not a screen delta here but the home itself: the shell
        comes back when the CLI is really gone, and the escalation ladder
        re-sends this if it does not.
        """
        command, enters = self.driver.exit_command
        if not command:
            return False
        self._exit_sent = True
        self._exit_sent_at = time.time()
        self._exit_attempts += 1
        self.rec["exit_command"] = command
        save_record(self.rec)
        append_status(self.status_path,
                      f"EXIT-COMMAND {utc_now()} {command} ({enters} enter"
                      f"{'s' if enters > 1 else ''}, attempt {self._exit_attempts})")
        with contextlib.suppress(SubstrateError):
            self.substrate.send_tui_line(self.worker, command, enters=enters,
                                         confirm=0, log_path=self.status_path)
        if self.confirm_exit():
            append_status(self.status_path,
                          f"EXIT-CONFIRMED {utc_now()} the TUI is gone")
        return True

    def confirm_exit(self, seconds=None):
        """Did the CLI actually leave? The shell coming back is the only proof.

        A moved screen is not it: closing an autocomplete popup moves the screen
        too. Returning False is not a failure here, only "not yet".
        """
        from .substrates.herdr import TUI_SUBMIT_SECONDS
        seconds = TUI_SUBMIT_SECONDS if seconds is None else seconds
        deadline = time.time() + seconds
        while time.time() < deadline:
            info = self.substrate.process_info(self.worker)
            if info is None or info.at_prompt:
                return True
            time.sleep(0.1)
        return False

    def escalate_exit(self):
        """Say it again, then stop asking. True once the home has been forced.

        A CLI that is quitting takes a moment; one that never received the
        command takes forever, and from outside the two look identical. So the
        sequence is repeated once and then the process tree is killed, because
        typing an exit command at something that is ignoring it is not a plan.
        """
        waited = time.time() - self._exit_sent_at
        if waited < EXIT_RETRY_SECONDS:
            return False
        if self._exit_attempts < EXIT_MAX_ATTEMPTS:
            append_status(self.status_path,
                          f"EXIT-RETRY {utc_now()} still up {int(waited)}s after "
                          "its exit command; sending the sequence again")
            self.send_exit_command()
            return False
        self._exit_forced = True
        self.rec["exit_forced"] = True
        self.rec["exit_forced_reason"] = (
            f"{self.rec.get('exit_command')} was sent {self._exit_attempts} times "
            f"and the session was still up {int(waited)}s later")
        save_record(self.rec)
        append_status(self.status_path,
                      f"EXIT-FORCED {utc_now()} {self.rec['exit_forced_reason']}; "
                      "killing the process tree")
        self.substrate.kill_worker_tree(self.worker)
        return True

    def scrape_session_id(self, text):
        """Read the native session id off the CLI's parting screen.

        For a CLI that names its own session, the line it prints on its way out
        is often the only place the id exists at this point. This has to happen
        before the home is closed, because closing takes the screen with it.
        """
        # The last one: the CLI prints its line after it has quit, so anything
        # resume-shaped above it is the worker's text and names whatever session
        # the worker chose.
        found = SESSION_RESUME_RE.findall(text or "")
        if not found:
            return ""
        candidate = found[-1]
        from .records import SESSION_ID_RE
        return candidate if SESSION_ID_RE.match(candidate) else ""

    def capture_rc(self):
        return self.substrate.read_exit_code(self.worker, self.rec["id"],
                                             self.status_path)

    def finish(self, rc, timed_out=False, close=True):
        """Journal the real exit code and, unless asked not to, let the home go."""
        # Last look at what the worker wrote on its own machine: the run is over
        # and its home is about to close, so this is the final chance to have it.
        remote.fetch_deliverable(self.rec)
        screen = self.substrate.read_screen(self.worker)
        with contextlib.suppress(OSError):
            replace_text(self.dir / "screen.log", screen)
        self.rec["rc"] = rc
        append_status(self.status_path, f"EXIT {utc_now()} rc={rc}")
        # Asked before the salvage, because the salvage is what makes a worker
        # that wrote nothing look like one that wrote something.
        deliverable = deliverable_path(self.rec)
        wrote_deliverable = deliverable.is_file() and deliverable.stat().st_size > 0
        # On the record, so a reader can tell the worker's answer from the copy
        # of its screen that the salvage leaves in out.md.
        self.rec["deliverable_written"] = wrote_deliverable
        finalize_output(self.rec)
        # Only when we do not already know it: a CLI handed a session id at spawn
        # makes that value the fact and the screen merely a report of it.
        if not self.rec.get("session_id"):
            scraped = self.scrape_session_id(screen)
            if scraped:
                self.rec["session_id"] = scraped
                append_status(self.status_path,
                              f"SESSION {utc_now()} {scraped} (from screen)")
        session_id = refresh_session_id(self.rec)
        if session_id:
            self.rec["transcript"] = resolve_transcript(self.rec, session_id)
            with contextlib.suppress(SubstrateError, DispatchError):
                self.note_session(session_id, self.rec.get("transcript", ""))
        text = read_output(self.rec)
        if text.strip().startswith(ABORT_MARKER):
            # Terminal and distinct from failure: the worker refused the brief.
            self.rec["state"] = "aborted"
        elif self._exit_forced and wrote_deliverable:
            # The work is finished and on disk; only the CLI's own exit failed.
            # Journaling this as a timeout would say the run did not deliver,
            # which is the opposite of what happened.
            self.rec["state"] = "done"
            append_status(self.status_path,
                          f"FORCED-DONE {utc_now()} deliverable was already "
                          "settled when the session had to be killed")
        elif timed_out:
            self.rec["state"] = "timeout"
        elif rc == 0 and not wrote_deliverable:
            # A clean exit with nothing to show for it. The screen still goes
            # into out.md for the post-mortem, but calling this done puts a lie
            # in the record: a CLI that quit at its opening dialog exits 0 too.
            self.rec["state"] = "failed"
            self.rec["error"] = (f"worker exited rc=0 with no {deliverable.name}; "
                                 "out.md holds the last screen, not an answer")
            append_status(self.status_path, f"NO-DELIVERABLE {utc_now()} at exit")
        elif rc == 0:
            self.rec["state"] = "done"
        else:
            # rc is None when the worker exited its shell outright, taking the
            # home before the status could be read. Journaled as the failure it is.
            self.rec["state"] = "failed"
        if self.rec.get("state") == "failed" and self.rec.get("update_error"):
            # The respawn loop is why this run failed; "exited rc=0 with no
            # deliverable" is only what the last of those launches looked like.
            self.rec["error"] = self.rec["update_error"]
        self.rec["finished"] = utc_now()
        self.rec["closed_by"] = "runner"
        if self.rec.get("out_copy"):
            write_out_copy(self.rec, text)
        # Now, and only now: the worker has gone, so there is no detection left
        # to evict and the wall should show how this ended.
        self.report("idle", f"{self.rec['state']} rc={rc}")
        save_final_record(self.rec)
        append_status(self.status_path, f"state: {self.rec['state']}")
        if close:
            self.close()
        return self.rec


# --------------------------------------------------------------------------
# Running one worker, in the foreground or detached
# --------------------------------------------------------------------------


def execute_run(rec, substrate, deadline_seconds=None, poll_seconds=None,
                attach_worker=None):
    """Run one worker to completion in its own home. The only path that runs one.

    `attach_worker` is a home that already holds this session's CLI, which is
    what `dispatch continue` hands over after `dispatch inspect` opened one: the
    session is already up, so there is nothing to start and only a prompt to
    deliver.
    """
    wrapper = RunWrapper(substrate, rec)
    try:
        if attach_worker is not None:
            wrapper.adopt(attach_worker)
            wrapper.prompt_worker(rec["prompt"])
        else:
            wrapper.open()
            wrapper.start_worker(rec["argv"])
            # A brief left owed here is a resume into a session still finishing
            # the turn it was interrupted mid-way through, which is health
            # rather than lateness. The poll loop hands the message over when
            # that turn settles, and the check-in ladder runs meanwhile.
            wrapper.deliver_brief()
        rec = wrapper.watch(deadline_seconds=deadline_seconds,
                            poll_seconds=poll_seconds)
    except BaseException as exc:
        # Anything at all: a stalled worker, an unreachable substrate, a Ctrl-C.
        # Once a home exists it has to be closed by whoever created it, or it
        # sits there holding a cap slot with the record stuck in running and
        # nothing left to move it along.
        wrapper.abandon(exc)
        raise
    return finalize_schema(rec)


def spawn_detached_watcher(rec):
    """Fork the one process dispatch still forks: itself, watching its own worker.

    It spawns nothing and owns nothing: it attaches to a home that already exists
    and runs the same wrapper a foreground run runs.

    It exists because completion is produced rather than waited for. Workers do
    not exit on their own, so somebody has to notice the turn is over and send
    the exit command; and homes close when the run ends, so somebody has to close
    this one. With nothing watching, a background run would sit at a finished TUI
    forever, holding a cap slot and ignoring its deadline, which is precisely the
    unattended case the deadline is for.
    """
    directory = Path(rec["dir"])
    # `-P`: the watcher's cwd is the task directory, and without it a `dispatch/`
    # package planted there is imported instead of this one, outside any sandbox.
    argv = [resolve_python(), "-P", "-m", "dispatch.cli", "_watch", rec["id"]]
    # An installed dispatch is already importable; one run out of a checkout is
    # only importable because this process put it on the path, and the watcher
    # is a fresh interpreter that inherits none of that.
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(
        [root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    log = open(directory / "watcher.log", "ab")
    try:
        popen = popen_detached(
            argv, cwd=rec.get("cwd") or None, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    except OSError as exc:
        # Operator-facing: the run cannot be left in a home with nothing to close
        # it, and a traceback is not an explanation.
        raise DispatchError(
            f"could not start a watcher for {rec['id']}: {exc}") from exc
    finally:
        log.close()
    return popen.pid


def start_run_background(rec, substrate, attach_worker=None):
    """Start a background run, hand it to a detached watcher, and return.

    The home outlives this process, which is exactly why something has to stay
    behind and close it.
    """
    wrapper = RunWrapper(substrate, rec)
    try:
        if attach_worker is not None:
            wrapper.adopt(attach_worker)
            wrapper.prompt_worker(rec["prompt"])
        else:
            wrapper.open()
            wrapper.start_worker(rec["argv"])
            wrapper.deliver_brief(wait_for_hand=False)
        # A brief left owed means the run is standing at a dialog only a human
        # can answer, or is a resume whose session is still finishing its turn.
        # Both are healthy runs, not failed spawns, so they keep their home and
        # get their watcher as usual; what they do not get is this process
        # waiting, which would hang the shell that asked for a background run.
        rec = wrapper.rec
        rec["watcher_started_at"] = time.time()
        # Inside the guard: a watcher that cannot be spawned leaves a live home
        # with nobody to close it, and the run should fail loudly here rather
        # than wait for a sweep to notice it hours later.
        rec["watcher_pid"] = spawn_detached_watcher(rec)
        save_record(rec)
    except BaseException as exc:
        wrapper.abandon(exc)
        raise
    append_status(Path(rec["dir"]) / "status.log",
                  f"WATCHER {utc_now()} pid {rec['watcher_pid']}")
    return rec


def worker_belongs_to_run(rec, substrate):
    """Same id, but the same worker?

    Ids restart with a substrate's daemon, so an id match across a restart can
    point at a stranger's live worker. The shell pid recorded at launch is the
    identity check: a recycled home has a fresh shell.

    Records from before the pid was recorded answer True, which is the old
    behaviour, not a new risk.
    """
    if not rec.get("worker_id"):
        return False
    expected = rec.get("shell_pid")
    if not expected:
        return True
    try:
        info = substrate.process_info(record_worker(rec))
    except SubstrateError:
        return False
    return info is not None and info.shell_pid == expected


def reconcile_run(rec, substrate):
    """One poll of a detached run: journal its exit code, or enforce its deadline.

    This is what replaces a supervisor process. Every command that reads runs
    calls it, so a finished background worker frees its cap slot and lands its
    exit code at the next dispatch invocation rather than at some daemon's whim.
    """
    if rec.get("state") in policy().terminal_states or not rec.get("worker_id"):
        return rec
    if not worker_belongs_to_run(rec, substrate):
        # The id survived a daemon restart and now names a stranger's worker;
        # adopting it would poll, and eventually exit, someone else's session.
        return abandon_ownerless_run(rec, substrate)
    # Adopting is exclusive. Two `dispatch status` processes sweeping the same
    # unwatched run would otherwise both send its exit command and both journal
    # a completion for it.
    handle = hold_run_lock(rec, "watcher.lock")
    if handle is None:
        return rec
    try:
        wrapper = RunWrapper(substrate, rec)
        wrapper.attach()
        return wrapper.poll_once(deadline_seconds=rec.get("deadline_seconds"))
    finally:
        release_run_lock(handle)


def abandon_ownerless_run(rec, substrate, ids=None):
    """Close out a run that nobody is driving and nothing ever finished.

    Usually this is a record whose home is already gone. But a killed launcher
    leaves its in-flight workers reserved with live homes and nothing left to
    advance them, so this takes the home down rather than leaving it open until
    someone notices: an abandoned worker holds a cap slot and a CLI for as long
    as the machine is up.
    """
    worker_id = rec.get("worker_id")
    took_it_down = bool(worker_id and (ids is None or worker_id in ids))
    if took_it_down:
        took_it_down = worker_belongs_to_run(rec, substrate)
    if took_it_down:
        worker = record_worker(rec)
        with contextlib.suppress(SubstrateError, OSError):
            replace_text(Path(rec["dir"]) / "screen.log",
                         substrate.read_screen(worker))
            substrate.close(worker, release=False)
        append_status(Path(rec["dir"]) / "status.log",
                      f"ABANDONED {utc_now()} no owner and no watcher; "
                      f"closed {worker_id}")
    return abandon_record(
        rec,
        error="abandoned: the process that launched it is gone and no watcher "
              "took it" if took_it_down else "")


def close_finished_worker(rec, substrate, ids=None):
    """Close the home of a run that is over but whose home is still up.

    Homes close when a run ends, and every terminal transition does close its
    own. This is for the ones nothing was left to close: a watcher killed between
    journaling a state and tearing down, or a substrate that was unreachable at
    the moment it mattered. An inspect home is deliberately not touched: that one
    belongs to a finished run on purpose.
    """
    worker_id = rec.get("worker_id")
    if not worker_id or (ids is not None and worker_id not in ids):
        return False
    if not worker_belongs_to_run(rec, substrate):
        return False
    with contextlib.suppress(SubstrateError):
        substrate.close(record_worker(rec), release=False)
    append_status(Path(rec["dir"]) / "status.log",
                  f"WORKER-SWEPT {utc_now()} {worker_id} outlived a "
                  f"{rec.get('state')} run")
    return True


class SubstrateSweep(Sweep):
    """The caps' window onto a real substrate.

    `live_records` needs to know whether a run's worker still exists, and what to
    do about one nobody is driving. Both answers need a substrate, and the caps
    must not depend on one, so they arrive through this.
    """

    def __init__(self, substrate, machine=""):
        self.substrate = substrate
        # Which machine's substrate this is. A shell-mode run is recorded in the
        # tmux substrate exactly like a local one, and the machine is the only
        # thing that tells the two pane numberings apart.
        self.machine = machine or ""

    def worker_ids(self):
        if self.substrate is None:
            return None
        return self.substrate.worker_ids()

    def covers(self, rec):
        return (self.substrate is not None
                and rec.get("substrate") == self.substrate.name
                and (rec.get("machine") or "") == self.machine)

    def reconcile(self, rec):
        if self.substrate is None:
            return rec
        with contextlib.suppress(SubstrateError, OSError):
            return reconcile_run(rec, self.substrate)
        return rec

    def reconcile_remote(self, rec):
        return remote.reconcile_remote_run(rec)

    def close_finished(self, rec, ids):
        if self.substrate is None:
            return False
        with contextlib.suppress(SubstrateError, OSError):
            return close_finished_worker(rec, self.substrate, ids)
        return False

    def abandon(self, rec, ids):
        if self.substrate is None:
            return abandon_record(rec)
        return abandon_ownerless_run(rec, self.substrate, ids)
