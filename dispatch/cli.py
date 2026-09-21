"""The verbs.

Everything here is thin on purpose: parse, resolve a lane, pick a substrate,
call the runner, print. The one thing the CLI owns that nothing else does is the
exit code, because a caller reading it has to be able to tell "the worker
refused the brief" from "it broke" from "I stopped waiting".
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import sys
import time
from dataclasses import replace
from pathlib import Path

from . import __version__
from . import remote
from .board import render_board
from .caps import live_records, reserve_slot, run_is_live, run_is_watched
from .config import (DEFAULT_PRESET, active_config, agents_snippet_text,
                     install_skill, load_and_apply, preset_names, run_init,
                     skill_text)
from .doctor import render_report
from .drivers import driver_for_lane, validate_options
from .errors import DispatchError
from .lanes import (default_lane, lane_names, looks_like_lane, resolve_lane,
                    valid_lanes_hint)
from .policy import current_depth, enforce_depth, parse_deadline, policy
from .processes import stop_pid
from .prompt import steer_prompt
from .records import (RunOptions, all_records, append_status,
                      deliverable_path,
                      hold_run_lock, last_heartbeat_age, load_record, read_output,
                      read_status_head, read_tail_bytes, read_text, replace_text,
                      release_run_lock, runs_lock, runs_root, sanitize_log_line,
                      save_record,
                      stamp_epoch, utc_now, validate_run_id)
from .runner import (STEER_INTERRUPT_SECONDS, RunWrapper, SubstrateSweep,
                     checkin_count, execute_run, finalize_output,
                     judge_liveness, note_deliverable, prepare_run, reconcile_run,
                     record_worker,
                     refresh_session_id, run_agent_name, start_run_background,
                     warn_metered_key)
from .substrates import detect_substrate, get_substrate
from .substrates.base import SubstrateError, Worker

# Exit codes. `aborted` is terminal and distinct from failure, so it gets its
# own code: a caller can tell "the worker refused the brief" from "it broke".
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_ABORTED = 3
# `wait` gave up on the run, which is not the run giving up: the worker is still
# going and still owns its check-in ladder. Reusing a run-outcome code here
# would let a waiter's clock read as a worker's verdict.
EXIT_STILL_RUNNING = 4

# `dispatch watch`: how far back a finished run stays on the wall, how often a
# follow redraws, and how much of the log and screen a single-run follow shows.
WATCH_RECENT_SECONDS = 900
WATCH_INTERVAL_SECONDS = 2.0
WATCH_LOG_LINES = 20
WATCH_SCREEN_LINES = 40

# `dispatch watch --deep`: one snapshot of a run, deep enough to answer "what is
# it doing right now" without attaching. The transcript tail is bounded because
# a working session's log runs to megabytes.
DEEP_LOG_LINES = 6
DEEP_OUT_LINES = 10
DEEP_ACTIVITY_LINES = 5
DEEP_ACTIVITY_WIDTH = 110
DEEP_TRANSCRIPT_BYTES = 262144

# `dispatch wait`: the cadence a waiter reads status.log at, and how much it
# prints once the run ends. A waiter is a spectator and must never cost more
# than the run it is watching.
WAIT_POLL_SECONDS = 3.0
WAIT_LOG_LINES = 6
WAIT_CLI_LINES = 30

# How often a verb that is following a run on another machine goes back over the
# connection. Slower than the local cadence: every look is an ssh round trip.
REMOTE_FOLLOW_SECONDS = 5.0


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def open_substrate(name=""):
    """The substrate this invocation will drive."""
    pinned = (name or (os.environ.get("DISPATCH_SUBSTRATE") or "").strip()
              or active_config().substrate)
    return get_substrate(pinned) if pinned else detect_substrate()


def substrate_for(rec):
    """The substrate a recorded run lives in, whatever this invocation prefers.

    A shell-mode run's home is a pane on another machine, reached through the
    same tmux substrate with an ssh in front of every call. Rebuilding it from
    the recorded name alone would drop that ssh and drive this machine's tmux,
    where the run's pane id names nothing, or worse, somebody else's pane.
    """
    machine = remote.machine_of_record(rec)
    if machine is not None and machine.mode == remote.SHELL_MODE:
        return remote.shell_substrate(machine)
    return open_substrate(rec.get("substrate", ""))


def optional_substrate():
    """A substrate if one can be reached, else None. For read-only verbs."""
    try:
        return open_substrate()
    except (DispatchError, NotImplementedError):
        return None


def record_substrate_or_none(rec):
    """The substrate of one recorded run, or None when it cannot be reached."""
    try:
        return substrate_for(rec)
    except (DispatchError, NotImplementedError):
        return None


def record_worker_ids(rec):
    """Live worker ids in the id space of this record's own substrate.

    Asked per record rather than once per command: a run's `worker_id` means
    something only inside the substrate and the machine that issued it, and
    checking it against another one's list is how a live run reads as orphaned.
    """
    substrate = record_substrate_or_none(rec)
    if substrate is None:
        return None
    with contextlib.suppress(SubstrateError, OSError):
        return substrate.worker_ids()
    return None


def holds_the_home(rec, substrate):
    """Is this the substrate a run's home is actually in?

    The wall opens one substrate for every row it draws, so a run placed on a
    machine, or recorded in another substrate, is one this one can say nothing
    about: its worker id belongs to another numbering.
    """
    return (substrate is not None and not rec.get("machine")
            and rec.get("substrate", "") == substrate.name)


def sweep():
    return SubstrateSweep(optional_substrate())


def state_exit_code(state):
    if state == "done":
        return EXIT_OK
    if state == "aborted":
        return EXIT_ABORTED
    return EXIT_FAILED


def run_exit_code(rec):
    """A finished run's exit code, which is not 0 while its answer is elsewhere.

    A remote run can end `done` on its machine with the copy down still failing.
    Exit 0 there tells a caller the answer is in hand when there is none to read.
    """
    if rec.get("state") == "done" and rec.get("mirror_pending"):
        print(f"dispatch: {rec['id']} finished on {rec.get('machine') or 'its machine'} "
              "but its files could not be copied down; "
              f"`dispatch wait {rec['id']}` tries again", file=sys.stderr)
        return EXIT_FAILED
    return state_exit_code(rec.get("state", ""))


def split_lane_and_brief(positional):
    """`dispatch run [lane] <brief>`; the lane is recognized by its shape."""
    items = list(positional or ())
    if not items:
        raise DispatchError("usage: dispatch run [lane] <brief> [options]")
    lane_text = default_lane()
    if len(items) > 1 or looks_like_lane(items[0]):
        if len(items) > 1 and not looks_like_lane(items[0]):
            raise DispatchError(f"not a lane: {items[0]!r}. {valid_lanes_hint()}")
        lane_text = items.pop(0)
    if len(items) != 1:
        raise DispatchError("usage: dispatch run [lane] <brief> [options]")
    return lane_text, items[0]


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def stage_or_fail(rec, substrate):
    """Send a shell-mode run's files up, failing the run if they will not go.

    Staging happens before any worker exists, so nothing else would record the
    failure: the run would sit `reserved` with the reason lost.
    """
    try:
        remote.stage_shell_run(rec)
    except BaseException as exc:
        RunWrapper(substrate, rec).abandon(exc)
        raise


def cmd_run(args):
    lane_text, brief = split_lane_and_brief(args.positional)
    lane = resolve_lane(lane_text)
    enforce_depth(lane, "run")
    machine = remote.place_run(args.on, lane)
    # `--dir` and `--add-dir` name paths on whichever machine runs the worker, so
    # a remote run's are passed through untouched: resolving them here would
    # anchor them to a filesystem the worker never sees.
    sandbox = args.dir or ""
    if machine is None:
        sandbox = os.path.abspath(sandbox) if sandbox else os.getcwd()
    opts = RunOptions(
        dir=sandbox,
        write=args.write, net=args.net, add_dirs=tuple(args.add_dir or ()),
        schema=args.schema or "", out=args.out or "", bg=args.bg,
        deadline=args.deadline or policy().default_deadline,
        image=args.image or "")
    validate_options(lane, opts if machine is None
                     else replace(opts, dir="", add_dirs=()))
    if not Path(brief).is_file():
        raise DispatchError(
            f"brief file not found: {brief} (briefs are files; there are no "
            "inline prompts)")
    brief_text = read_text(brief)
    warn_metered_key(driver_for_lane(lane))

    if machine is None:
        substrate = open_substrate()
    elif machine.mode == remote.DISPATCH_MODE:
        # The machine's own dispatch runs it: nothing here opens a substrate,
        # and the record this side keeps is a pointer to the run over there.
        rec = remote.run_remote(machine, lane, brief_text, opts, brief)
        if opts.bg:
            print(rec["id"])
            print(rec["dir"])
            return EXIT_OK
        show(read_output(rec))
        return run_exit_code(rec)
    else:
        remote.check_shell_options(machine, opts)
        substrate = remote.shell_substrate(machine)
    rec, handle = reserve_slot(
        lambda: prepare_run(lane, brief_text, opts, substrate, machine=machine),
        sweep=SubstrateSweep(substrate, machine=machine.name if machine else ""))
    try:
        if machine is not None:
            # The pane is on a box with a filesystem of its own, so the brief and
            # the prompt go up before anything is started: the run's own record
            # already names them by their paths there.
            stage_or_fail(rec, substrate)
        if opts.bg:
            # A detached watcher drives the run from here on. If it exits
            # before recording the ending, the next command that reads runs
            # reconciles what it left behind.
            rec = start_run_background(rec, substrate)
            print(rec["id"])
            print(rec["dir"])
            return EXIT_OK
        rec = execute_run(rec, substrate,
                          parse_deadline(opts.deadline) if opts.deadline else None)
    finally:
        release_run_lock(handle)
    show(read_output(rec))
    return state_exit_code(rec["state"])


# --------------------------------------------------------------------------
# continue and steer
# --------------------------------------------------------------------------


def message_text(args):
    """The message for `steer` or `continue`: the argument, or a file's contents.

    A message is a prompt, so it can arrive the way a brief does. That is what a
    run on another machine needs, because an argv element there is readable in
    that machine's process table by every account on it.
    """
    path = getattr(args, "message_file", "") or ""
    if path and args.message:
        raise DispatchError("pass a message or --message-file, not both")
    if path:
        if not Path(path).is_file():
            raise DispatchError(f"message file not found: {path}")
        return read_text(path)
    if not args.message:
        raise DispatchError(
            f"usage: dispatch {args.command} <id> <message> | "
            "--message-file <path>")
    return args.message


def cmd_continue(args):
    rec = load_record(validate_run_id(args.id))
    lane = resolve_lane(rec["lane"])
    enforce_depth(lane, "run")
    message = message_text(args)
    if remote.is_remote(rec):
        return continue_on_machine(rec, lane, message, args)
    session_id = refresh_session_id(rec)
    if not session_id:
        raise DispatchError(
            f"{args.id} has no captured session id; start a fresh run with the "
            "prior result carried in the brief")
    if run_is_live(rec):
        raise DispatchError(
            f"{args.id} is still live; `continue` is for finished runs, "
            "`steer` corrects a live one")
    # The default deadline in the foreground too, as `run` has it: without one
    # there is no check-in ladder, and a resumed worker that goes idle without
    # answering holds its slot for good.
    deadline = getattr(args, "deadline", "") or policy().default_deadline
    bg = bool(getattr(args, "bg", False))
    opts = RunOptions(dir=rec.get("cwd") or os.getcwd(), write=bool(rec.get("write")),
                      net=bool(rec.get("net")), schema=rec.get("schema") or "",
                      bg=bg, deadline=deadline)
    warn_metered_key(driver_for_lane(lane))
    substrate = substrate_for(rec)
    # A shell-mode parent's session is on its machine, so the new turn is placed
    # there too: same pane substrate, same staging directory, its own run.
    machine = remote.machine_of_record(rec)
    # An inspected run already has its session open; prompting that is cheaper
    # than paying for a second CLI to resume the same session. The slot is taken
    # here, where the tokens are, rather than when the home was opened.
    inspected = live_inspect_worker(rec, substrate)
    child, handle = reserve_slot(
        lambda: prepare_run(lane, message, opts, substrate, kind="continue",
                            parent=rec["id"], resume_session=session_id,
                            machine=machine),
        sweep=SubstrateSweep(substrate, machine=machine.name if machine else ""))
    if machine is not None:
        try:
            stage_or_fail(child, substrate)
        except BaseException:
            release_run_lock(handle)
            raise
    if inspected is not None:
        # The substrate knows that home's agent by the name inspect registered,
        # which is the parent's. The child adopts the session, so it adopts the
        # name: asking about a name that was never registered gets "not found"
        # for every probe, and the turn would never be seen to end.
        child["agent"] = run_agent_name(rec, substrate)
        # The claim goes on the record before the turn starts. Two `continue`s
        # read the same parent pointer, and without it both adopted the one
        # home: two prompts typed into it, and one run closing it under the
        # other. The older claim wins and the newer run ends here.
        # Checked and claimed in one step under the runs lock, so whichever
        # `continue` gets there first holds the home and the other sees its claim.
        with runs_lock():
            holders = inspect_home_claimants(inspected.id, child["id"])
            closed = load_record(rec["id"]).get("inspect_worker") != inspected.id
            if not holders and not closed:
                child["adopts_worker"] = inspected.id
                save_record(child)
        if holders or closed:
            error = DispatchError(
                f"{args.id}'s inspect home was closed while this was starting; "
                f"run `dispatch continue {args.id}` again" if not holders else
                f"{holders[0]['id']} is already running a turn in {args.id}'s "
                f"inspect home; `dispatch steer {holders[0]['id']}` corrects it, "
                "or wait for it")
            RunWrapper(substrate, child).abandon(error)
            release_run_lock(handle)
            raise error
    try:
        if bg:
            child = start_run_background(child, substrate, attach_worker=inspected)
        else:
            child = execute_run(
                child, substrate, attach_worker=inspected,
                deadline_seconds=parse_deadline(deadline) if deadline else None)
    finally:
        release_run_lock(handle)
    if inspected is not None:
        # The child owns that home now and closes it when its turn ends, so the
        # parent's pointer to it is stale either way.
        rec["inspect_worker"] = ""
        rec["inspect_group"] = ""
        save_record(rec)
    if bg:
        print(child["id"])
        print(child["dir"])
        return EXIT_OK
    show(read_output(child))
    return state_exit_code(child["state"])


def continue_on_machine(rec, lane, message, args):
    """A new turn on the machine the run is on, recorded here as its own run."""
    deadline = getattr(args, "deadline", "") or ""
    bg = bool(getattr(args, "bg", False))
    if bg and not deadline:
        deadline = policy().default_deadline
    opts = RunOptions(dir=rec.get("cwd") or "", write=bool(rec.get("write")),
                      net=bool(rec.get("net")), schema=rec.get("schema") or "",
                      bg=bg, deadline=deadline)
    child = remote.continue_remote(rec, lane, message, opts)
    if bg:
        print(child["id"])
        print(child["dir"])
        return EXIT_OK
    show(read_output(child))
    return run_exit_code(child)


def cmd_steer(args):
    """Type a correction into a live worker, liveness-gated.

    Steering is what the word means here: interrupt what the worker is saying,
    then say the thing. The session is never destroyed and no context is lost.
    """
    rec = load_record(validate_run_id(args.id))
    lane = resolve_lane(rec["lane"])
    enforce_depth(lane, "run")
    message = message_text(args)
    if remote.is_remote(rec):
        # Ask the machine before typing. Steering a finished run is a
        # continuation, and a continuation there starts a run of its own, which
        # this side has to record as one rather than discover later.
        rec = remote.reconcile_remote_run(rec)
        if rec.get("state") in policy().terminal_states:
            print(f"dispatch: {args.id} is {rec.get('state')}; steering a "
                  "finished run is a continuation, so this starts a new turn on "
                  "its session", file=sys.stderr)
            return continue_on_machine(rec, lane, message, args)
        # Everything else is the machine's judgement to make: its own dispatch
        # runs the same liveness gate a local steer runs.
        print(remote.steer_remote(rec, message,
                                  getattr(args, "deadline", "") or ""))
        return EXIT_OK
    substrate = substrate_for(rec)
    if not substrate.capabilities.can_steer:
        raise DispatchError(
            f"{args.id} lives in the {substrate.name} substrate, which cannot be "
            "typed into; `dispatch continue` starts a new turn on its session")
    status_path = Path(rec["dir"]) / "status.log"

    # Reconcile first, then route. A run whose TUI is gone but whose record still
    # says running was refused by `continue` for being live and by `steer` for
    # being finished, which is a contradiction the operator cannot act on.
    if rec.get("worker_id") and not run_is_watched(rec):
        with contextlib.suppress(SubstrateError, OSError):
            rec = reconcile_run(rec, substrate)
    if not rec.get("worker_id") or not run_is_live(rec):
        # It finished between the operator's decision and this call. Steering a
        # terminal run is a continuation, which is what happens next anyway.
        append_status(status_path, f"STEER {utc_now()} run already {rec.get('state')}")
        print(f"dispatch: {args.id} is {rec.get('state')}; steering a finished run "
              "is a continuation, so this starts a new turn on its session",
              file=sys.stderr)
        return cmd_continue(args)

    wrapper = RunWrapper(substrate, rec)
    wrapper.attach()
    info = substrate.process_info(wrapper.worker)
    if info is None:
        append_status(status_path, f"STEER {utc_now()} the home is gone")
        return cmd_continue(args)

    # The verdict is on the record before anything is typed. Steering a worker
    # that has already exited its session would type the message into a bare
    # shell prompt, which runs it as a command.
    verdict = judge_liveness(wrapper.signals(info, wrapper.started_at()), rec)
    save_record(rec)
    append_status(status_path, f"STEER {utc_now()} liveness={verdict}")
    if verdict == "gone":
        return cmd_continue(args)

    if info.child_pids:
        # Actively generating: interrupt first, or the message lands in the
        # middle of a stream and the TUI eats half of it.
        substrate.send_keys(wrapper.worker, ["escape"])
        time.sleep(STEER_INTERRUPT_SECONDS)
    # A steer starts a turn, and turn detection has to know that: the previous
    # turn's deliverable is already on disk, and a watcher that sees it settled
    # would send the exit command into the middle of the correction.
    # Stamped before anything is typed: the stamp is how the process watching
    # this run learns a new turn has begun, and one that landed only after the
    # prompt left the whole delivery for that watcher to exit the worker in.
    rec["steered"] = utc_now()
    # Counted as well: two steers inside one second carry the same stamp.
    rec["steers"] = int(rec.get("steers") or 0) + 1
    # The answer already on disk belongs to the turn being corrected. Noted by
    # size and time, so it cannot end the new turn unless the worker rewrites it.
    with contextlib.suppress(OSError):
        old = deliverable_path(rec).stat()
        rec["stale_deliverable"] = [old.st_size, old.st_mtime]
    rec["deliverable_seen"] = None
    rec["deliverable_since"] = 0.0
    save_record(rec)
    wrapper.prompt_worker(steer_prompt(message))
    append_status(status_path, f"STEER {utc_now()} delivered")
    print(f"{args.id} steered")
    return EXIT_OK


# --------------------------------------------------------------------------
# status, logs, kill
# --------------------------------------------------------------------------


def cmd_status(args):
    records = all_records()
    if not records:
        print("no runs")
        return EXIT_OK
    # Reading status is also what settles a detached run whose watcher is gone:
    # no daemon supervises runs, so the operator looking is the trigger.
    with contextlib.suppress(SubstrateError, OSError):
        live_records(sweep())
    records = all_records()
    rows = []
    for rec in sorted(records, key=lambda r: r.get("created", "")):
        status_path = Path(rec["dir"]) / "status.log"
        head = read_status_head(status_path)
        state = rec.get("state", "?")
        note = ""
        if state not in policy().terminal_states:
            if not run_is_live(rec, record_worker_ids(rec)):
                state = "orphaned"
            elif rec.get("worker_id"):
                note = f"  worker {rec['worker_id']}"
                if not run_is_watched(rec):
                    # Nothing is left to send its exit command or enforce its
                    # deadline; the next reconcile adopts it.
                    note += "  (no watcher)"
        if rec.get("needs_hand") and state not in policy().terminal_states:
            note += (f"  NEEDS HAND: {rec['needs_hand']} in "
                     f"{rec.get('worker_id') or '?'}")
        if checkin_count(rec):
            # What kept this run alive past its deadline: how many times it was
            # looked at and what the last look saw.
            note += (f"  checked in {checkin_count(rec)}x "
                     f"({rec.get('checkin_verdict') or '?'})")
        if rec.get("machine"):
            note += f"  on {rec['machine']}"
            if rec.get("remote_error"):
                # The run is not over; this machine just cannot see it. Saying so
                # is the difference between a stalled worker and a dropped VPN.
                note += f"  UNREACHABLE: {rec['remote_error']}"
        if rec.get("spawn_fallback"):
            note += "  (spawn fallback)"
        if rec.get("inspect_worker"):
            note += f"  inspect {rec['inspect_worker']}"
        age = last_heartbeat_age(status_path)
        rows.append((rec["id"], head.get("lane", rec.get("lane", "?")), state,
                     "-" if age is None else f"{age}s", note))
    width = max(len(row[0]) for row in rows)
    for row in rows:
        # Through `show`: the lane column is read from a status.log a worker can
        # write in, and so is part of the note.
        show(f"{row[0]:<{width}}  {row[1]:<16} {row[2]:<9} hb {row[3]}{row[4]}\n")
    return EXIT_OK


def cmd_logs(args):
    rec = load_record(validate_run_id(args.id))
    if remote.is_remote(rec):
        return remote_logs(rec, args)
    path = Path(rec["dir"]) / "status.log"
    if not path.is_file():
        raise DispatchError(f"{args.id} has no status.log yet")
    if not args.follow:
        show(path.read_text(encoding="utf-8", errors="replace"))
        return EXIT_OK
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            line = fh.readline()
            if line:
                show(line)
                sys.stdout.flush()
                continue
            current = load_record(args.id)
            if current.get("state") in policy().terminal_states:
                show(fh.read())
                return EXIT_OK
            time.sleep(1)


def remote_logs(rec, args):
    """A remote run's log: the machine's while it is live, the mirror after.

    Following reprints the whole log each time rather than streaming: the far
    side appends to a file this side does not have open, and a poll that asked
    for an offset would still be a poll.
    """
    printed = ""
    while True:
        text = remote.remote_log(rec)
        if text.startswith(printed):
            show(text[len(printed):])
        else:
            show(text)
        sys.stdout.flush()
        printed = text
        if not args.follow or rec.get("state") in policy().terminal_states:
            return EXIT_OK
        time.sleep(REMOTE_FOLLOW_SECONDS)
        rec = remote.reconcile_remote_run(load_record(rec["id"]))


def cmd_kill(args):
    """Stop a run's worker and close its home."""
    rec = load_record(validate_run_id(args.id))
    if remote.is_remote(rec):
        return kill_on_machine(rec)
    if not run_is_live(rec, record_worker_ids(rec)):
        state = rec.get("state", "gone")
        if state not in policy().terminal_states:
            state = "orphaned"
            rec["state"] = state
            rec["finished"] = utc_now()
            rec["closed_by"] = "kill"
            save_record(rec)
            append_status(Path(rec["dir"]) / "status.log", f"state: {state}")
        print(f"{args.id} is not live ({state}); nothing to kill")
        return EXIT_OK
    # Stop the watcher before the worker. It is the deadline owner and the
    # finalizer; leaving it alive while its worker dies lets it write `done` over
    # an explicit kill, and race the teardown for the last screen.
    watcher = rec.get("watcher_pid")
    if watcher and int(watcher) != os.getpid():
        stop_pid(watcher)
    substrate = substrate_for(rec)
    # Whatever the worker had written by now, before its home goes: a shell-mode
    # run's deliverable is on its machine and this is the last look at it.
    remote.fetch_deliverable(rec)
    if rec.get("worker_id"):
        worker = record_worker(rec)
        # The worker's whole process group, then its home: an agent CLI spawns
        # tool subprocesses, and killing only what the substrate lists first
        # orphans them.
        substrate.kill_worker_tree(worker)
        with contextlib.suppress(SubstrateError, OSError):
            replace_text(Path(rec["dir"]) / "screen.log",
                         substrate.read_screen(worker))
        substrate.close(worker, release=False)
    rec = load_record(rec["id"])
    note_deliverable(rec)
    rec["state"] = "killed"
    rec["finished"] = utc_now()
    rec["closed_by"] = "kill"
    finalize_output(rec)
    # Under the runs lock, so a watcher finishing at this exact moment cannot
    # read running, decide done, and write it over the kill afterwards.
    from .records import save_final_record
    save_final_record(rec)
    append_status(Path(rec["dir"]) / "status.log", f"KILL {utc_now()}")
    append_status(Path(rec["dir"]) / "status.log", "state: killed")
    print(f"{args.id} killed")
    return EXIT_OK


# --------------------------------------------------------------------------
# Observation: watch and wait
# --------------------------------------------------------------------------
#
# Everything here is read-only about a run it does not own. A wall that took
# locks, reported worker state, or sent an exit command would be a second
# lifecycle authority for runs another process is already driving, so watching
# reads records, status logs, and screens, and writes nothing. The one exception
# is a run nobody is watching, which `wait` reconciles under the watcher lock.


def kill_on_machine(rec):
    """Kill a run on the machine it is on, and mirror what it produced."""
    try:
        report = remote.kill_remote_run(rec)
    except DispatchError as exc:
        # The record here is a pointer, so closing it cannot stop a worker. The
        # operator asked for this run to be over, and the honest answer is that
        # it is over as far as this machine is concerned and may not be over
        # there.
        remote.abandon_unreachable(rec, str(exc))
        raise DispatchError(
            f"{rec['id']}: {exc}\nthe run is recorded as orphaned here; it may "
            f"still be live on {rec['machine']} as {rec['remote_id']}")
    rec = remote.reconcile_remote_run(load_record(rec["id"]))
    print(report or f"{rec['id']} killed")
    return EXIT_OK


def tail_lines(path, count, *, skip_heartbeats=False):
    path = Path(path)
    if not path.is_file():
        return []
    size = path.stat().st_size
    limit = 4096
    while True:
        lines = read_tail_bytes(path, limit).splitlines()
        if limit < size:
            # The first line may start mid-character or mid-heartbeat.
            lines = lines[1:]
        if skip_heartbeats:
            lines = [line for line in lines
                     if line.strip() and not line.startswith("HEARTBEAT ")]
        if limit >= size or (count > 0 and len(lines) >= count):
            return lines[-count:]
        limit = min(size, limit * 2)


def non_heartbeat_tail(path, count):
    """The last few log lines that say something. Heartbeats are the noise here.

    A run that has been up for an hour has hundreds of heartbeat lines and maybe
    six that matter; a plain tail shows nothing but the pulse.
    """
    return tail_lines(path, count, skip_heartbeats=True)


def head_lines(text, count):
    return text.splitlines()[:count]


TERMINAL_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def show(text):
    """Print text a worker wrote, without letting it drive the terminal.

    An answer, a status line, or a screen can carry escape sequences, and printed
    raw they clear the screen over a result, rewrite what was shown, or on some
    terminals load the clipboard. On a terminal every control character but
    newline and tab is shown as a mark instead. Redirected output is the file as
    it is, byte for byte.
    """
    if sys.stdout.isatty():
        text = TERMINAL_CONTROLS.sub(
            lambda found: "\u241b" if found.group() == "\x1b" else "\ufffd", text)
    sys.stdout.write(text)


def emit_frame(lines, redraw):
    """Print a frame, clearing first when this is a follow on a real terminal."""
    if redraw and sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J")
    show("\n".join(lines) + "\n")
    sys.stdout.flush()


def watch_deadline_left(rec):
    """Seconds a run has before its next check-in, or None.

    Extended by one interval per check-in already made, so the wall counts down
    to the look that is actually coming rather than sitting at zero for a run
    that was looked at and let through.
    """
    seconds = rec.get("deadline_seconds")
    started = rec.get("started_at")
    if not seconds or not started:
        return None
    try:
        due = float(seconds) * (1 + checkin_count(rec))
        return max(0, int(due - (time.time() - float(started))))
    except (TypeError, ValueError):
        return None


def watch_idle_seconds(rec, status_path):
    """How long the screen has been still, as last measured.

    The wrapper measures this every poll and writes it to the record with the
    liveness verdict, so the wall reports the run's own observation rather than
    scraping a screen it does not own.
    """
    liveness = rec.get("liveness") or {}
    idle = liveness.get("output_idle_seconds")
    if isinstance(idle, (int, float)):
        return int(idle)
    return last_heartbeat_age(status_path)


def watch_rows(substrate=None):
    """One row per run worth looking at: live ones, plus the recently finished."""
    rows = []
    now = time.time()
    for rec in sorted(all_records(), key=lambda r: r.get("created", "")):
        state = rec.get("state", "?")
        terminal = state in policy().terminal_states
        if terminal and not rec.get("inspect_worker"):
            # A run with a home still open stays on the wall however long ago it
            # finished: that home is the thing that must not be forgotten.
            finished = stamp_epoch(rec.get("finished")) or rec.get("started_at")
            try:
                if now - float(finished or 0) > WATCH_RECENT_SECONDS:
                    continue
            except (TypeError, ValueError):
                continue
        worker_id = rec.get("worker_id") or ""
        reported = rec.get("machine") or "-"
        if worker_id and not terminal and holds_the_home(rec, substrate):
            # One call per live worker, and only for live ones: the wall must
            # never poll harder than the wrapper driving the run.
            with contextlib.suppress(SubstrateError):
                reported = substrate.status(record_worker(rec)).status or "-"
        idle = watch_idle_seconds(rec, Path(rec["dir"]) / "status.log")
        left = watch_deadline_left(rec) if not terminal else None
        # A run past its original deadline is not overdue, it has been checked on
        # and extended. The count is on the wall so a runaway is obvious at a
        # glance rather than discovered on the bill.
        checked = checkin_count(rec)
        rows.append({
            "id": rec["id"], "lane": rec.get("lane", "?"), "state": state,
            "worker": worker_id or "-", "reported": reported,
            "idle": "-" if idle is None else f"{idle}s",
            "deadline": ("-" if left is None else f"{left // 60}m{left % 60:02d}s")
                        + (f"+{checked}" if checked else ""),
            "inspect": rec.get("inspect_worker") or "",
            "hand": "" if terminal else (rec.get("needs_hand") or ""),
        })
    return rows


def render_wall(rows):
    if not rows:
        return [f"no runs in the last {WATCH_RECENT_SECONDS // 60} minutes"]
    width = max(len(row["id"]) for row in rows)
    lines = [f"{'RUN':<{width}}  {'LANE':<16} {'STATE':<9} {'WORKER':<8} "
             f"{'REPORTED':<9} {'IDLE':>6} {'DEADLINE':>9}"]
    for row in rows:
        line = (f"{row['id']:<{width}}  {row['lane']:<16} {row['state']:<9} "
                f"{row['worker']:<8} {row['reported']:<9} {row['idle']:>6} "
                f"{row['deadline']:>9}")
        if row["hand"]:
            line += f"  NEEDS HAND {row['hand']}"
        if row["inspect"]:
            line += f"  inspect {row['inspect']}"
        lines.append(line)
    return lines


def cmd_watch(args):
    if args.id:
        rec = load_record(validate_run_id(args.id))
        if remote.is_remote(rec):
            return watch_on_machine(rec, args)
    if args.deep:
        return watch_deep_run(args)
    if args.attach:
        return attach_to_live_run(args)
    if args.id:
        return watch_single_run(args)
    substrate = optional_substrate()
    while True:
        emit_frame(render_wall(watch_rows(substrate)), redraw=args.follow)
        if not args.follow:
            return EXIT_OK
        time.sleep(WATCH_INTERVAL_SECONDS)


def watch_on_machine(rec, args):
    """Watch a run on another machine: its log, and nothing this side invents.

    There is no screen here to read and no transcript here to summarize, so the
    log is the whole view. Attaching means attaching on that machine, which is
    an ssh the operator runs rather than one dispatch runs for them.
    """
    if args.attach:
        raise DispatchError(
            f"{rec['id']} runs on {rec['machine']}; attach to it there with "
            f"`ssh -t {rec['machine_ssh']} dispatch watch {rec['remote_id']} "
            "--attach`")
    return remote_logs(rec, args)


def watch_single_run(args):
    """Follow one run: its log, and the screen of the home it lives in."""
    substrate = record_substrate_or_none(load_record(validate_run_id(args.id)))
    while True:
        rec = load_record(args.id)
        lines = [f"{rec['id']}  {rec.get('lane', '?')}  {rec.get('state', '?')}"
                 + (f"  worker {rec['worker_id']}" if rec.get("worker_id") else "")]
        lines += tail_lines(Path(rec["dir"]) / "status.log", WATCH_LOG_LINES)
        worker_id = rec.get("worker_id") or rec.get("inspect_worker")
        if worker_id and substrate is not None \
                and substrate.capabilities.can_read_screen:
            with contextlib.suppress(SubstrateError):
                screen = substrate.read_screen(
                    Worker(id=worker_id, group=rec.get("worker_group", "")))
                if screen.strip():
                    lines.append("")
                    lines.append(f"-- {worker_id} --")
                    lines += screen.splitlines()[-WATCH_SCREEN_LINES:]
        emit_frame(lines, redraw=args.follow)
        if not args.follow or rec.get("state") in policy().terminal_states:
            return state_exit_code(rec.get("state", ""))
        time.sleep(WATCH_INTERVAL_SECONDS)


def attach_to_live_run(args):
    """Hand the terminal to a live run's own home.

    `watch <id>` is a rendered view of a run; this is the run itself. Presence
    only: the run keeps its watcher, its deadline, and its record, so nothing
    here takes a lock, types, or closes anything on the way out.

    A finished run is `dispatch inspect`, which pays for a fresh CLI to reopen
    the session; keeping the two apart is the point, because reopening a run that
    is still going would be a second worker on one session.
    """
    if not args.id:
        raise DispatchError(
            "`--attach` needs the run to attach to: `dispatch watch <id> --attach`")
    rec = load_record(validate_run_id(args.id))
    if rec.get("state") in policy().terminal_states:
        raise DispatchError(
            f"{rec['id']} is {rec.get('state')}; `dispatch inspect {rec['id']}` "
            "reopens a finished run's session and attaches to that")
    worker_id = rec.get("worker_id")
    if not worker_id:
        raise DispatchError(
            f"{rec['id']} is {rec.get('state')} and has no worker yet; "
            f"`dispatch watch {rec['id']} -f` follows it until it has one")
    substrate = substrate_for(rec)
    worker = record_worker(rec)
    if not substrate.exists(worker):
        raise DispatchError(
            f"{rec['id']} is recorded in {worker_id}, which is gone; "
            "`dispatch status` settles a run whose worker died under it")
    # Focus first, then attach: an attach lands on whichever home is focused.
    with contextlib.suppress(SubstrateError):
        substrate.focus(worker)
    print(f"{rec['id']} is live in {worker_id}; attaching now "
          "(detach to come back here, the run carries on either way)")
    sys.stdout.flush()
    substrate.attach(worker)
    print(f"{rec['id']} left running in {worker_id}; "
          f"`dispatch watch {rec['id']} -f` follows it from here")
    return EXIT_OK


def run_transcript(rec):
    """The CLI's own transcript for a run, or "" if there is none on disk."""
    from .runner import resolve_transcript
    hint = rec.get("transcript") or ""
    if hint and Path(hint).is_file():
        return hint
    resolved = resolve_transcript(rec, rec.get("session_id", ""), hint)
    return resolved if resolved and Path(resolved).is_file() else ""


def summarize_claude_entry(entry):
    """One line per assistant turn part: a tool call, or what the worker said.

    Tool calls are what a supervisor actually wants (they say which file is being
    read and which command is running), so they are named with their first
    identifying argument rather than their whole input.
    """
    if entry.get("type") != "assistant":
        return []
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    stamp = str(entry.get("timestamp") or "")[11:19]
    out = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_use":
            args = part.get("input") if isinstance(part.get("input"), dict) else {}
            detail = ""
            for key in ("command", "file_path", "path", "pattern", "query", "prompt"):
                if args.get(key):
                    detail = f" {args[key]}"
                    break
            out.append(f"{stamp} tool {part.get('name') or '?'}{detail}".rstrip())
        elif part.get("type") == "text" and str(part.get("text") or "").strip():
            said = " ".join(str(part["text"]).split())
            out.append(f"{stamp} said {said}")
    return out


def transcript_activity(path, limit):
    """The worker's most recent assistant activity, newest first.

    Bounded tail read, so the first line of the window is usually a fragment of
    one; it fails to parse and is skipped like any other unreadable line.
    """
    lines = []
    for raw in reversed(read_tail_bytes(path, DEEP_TRANSCRIPT_BYTES).splitlines()):
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        for line in reversed(summarize_claude_entry(entry)):
            lines.append(sanitize_log_line(line)[:DEEP_ACTIVITY_WIDTH])
            if len(lines) >= limit:
                return lines
    return lines


def deep_activity_lines(rec, limit):
    """What the worker has been doing lately, from its own transcript.

    This is the part the plain wall cannot do: a screen shows the last frame, the
    transcript shows the last few turns. One driver keeps a readable per-session
    jsonl; the others are a different shape per release, so this says so rather
    than guessing at them.
    """
    driver = rec.get("driver") or "?"
    path = run_transcript(rec)
    if not path:
        return [f"no {driver} transcript on record yet"]
    if driver != "claude":
        return [f"{driver} transcripts are not readable from here; the session "
                f"file is {path}"]
    lines = transcript_activity(path, limit)
    return lines or [f"nothing read back from {path} yet"]


def watch_deep_run(args):
    """One deep snapshot of a run: no blocking, no redraw, no traffic to its home.

    It is a snapshot, which is why `-f` and `--attach` are refused instead of
    quietly ignored: following a snapshot and attaching to one are different
    verbs, and a caller that asked for both asked for something that does not
    exist.
    """
    if args.follow:
        raise DispatchError(
            "`--deep` is a snapshot, `-f` is a redraw; run one or the other")
    if args.attach:
        raise DispatchError(
            "`--deep` reads a run, `--attach` hands the terminal to it; "
            "run one or the other")
    if not args.id:
        raise DispatchError(
            "`--deep` needs the run to look at: `dispatch watch <id> --deep`")
    rec = load_record(validate_run_id(args.id))
    checked = checkin_count(rec)
    header = f"{rec['id']}  {rec.get('lane', '?')}  {rec.get('state', '?')}"
    if rec.get("worker_id"):
        header += f"  worker {rec['worker_id']}"
    header += f"  check-ins {checked}"
    if checked:
        header += f" ({rec.get('checkin_verdict') or '?'})"
    lines = [header]
    log_tail = non_heartbeat_tail(Path(rec["dir"]) / "status.log", DEEP_LOG_LINES)
    if log_tail:
        lines.append("")
        lines.append("-- status.log --")
        lines += log_tail
    lines.append("")
    lines.append("-- worker activity, newest first --")
    lines += deep_activity_lines(rec, DEEP_ACTIVITY_LINES)
    out_head = head_lines(read_output(rec), DEEP_OUT_LINES)
    if out_head:
        lines.append("")
        lines.append("-- out.md --")
        lines += out_head
    emit_frame(lines, redraw=False)
    return state_exit_code(rec.get("state", ""))


def parse_give_up(text):
    """`--give-up` bounds the waiter, so its errors must not read as a deadline."""
    try:
        return parse_deadline(text)
    except DispatchError:
        raise DispatchError(f"bad --give-up: {text!r} (use 45s, 30m, 2h)")


def run_has_ended(rec, status_path):
    """A terminal record, and nothing else.

    The COMPLETE line in status.log is a journal entry for a reader, not the
    ending: a worker can write in that log, and a line it wrote there used to
    stop a waiter while the run was still going.
    """
    return rec.get("state") in policy().terminal_states


def wait_report(rec, status_path, count):
    lines = [f"{rec['id']}  {rec.get('lane', '?')}  {rec.get('state', '?')}"]
    tail = non_heartbeat_tail(status_path, count)
    if tail:
        lines.append("")
        lines += tail
    return lines


def wait_answer_section(rec):
    """All of out.md, or a plain statement of why there is no answer to print.

    Never a head or a tail: the waiter asked for the answer, and a cut one reads
    as a complete one. A run whose worker wrote nothing still has an out.md,
    because the runner copies the last screen into it for the post-mortem, and
    that copy is named for what it is instead of being printed as an answer.
    """
    path = Path(rec["dir"]) / "out.md"
    state = rec.get("state", "?")
    if rec.get("deliverable_written") is False:
        return ["-- out.md: no answer --",
                f"the worker ended {state} without writing one; {path} holds "
                "dispatch's copy of its last output, not a deliverable"]
    if not path.is_file():
        return ["-- out.md: missing --",
                f"the run ended {state} and {path} was never written"]
    answer = read_output(rec).splitlines()
    if not any(line.strip() for line in answer):
        return ["-- out.md: empty --",
                f"the run ended {state} and {path} has nothing in it"]
    return [f"-- out.md ({len(answer)} lines) --"] + answer


def wait_cli_section(rec):
    """The end of what the worker's CLI itself printed, labeled with its source.

    `pane.log` is a headless worker's stdout and stderr. `screen.log` is the last
    screen of a worker that ran in a pane, and what a remote run mirrors down.
    status.log is dispatch's journal of the run and says nothing the CLI said,
    so it is never offered as this section.
    """
    directory = Path(rec["dir"])
    for name in ("pane.log", "screen.log"):
        tail = tail_lines(directory / name, WAIT_CLI_LINES)
        if any(line.strip() for line in tail):
            return [f"-- worker CLI output: last {len(tail)} lines of "
                    f"{directory / name} --"] + tail
    other = rec.get("transcript") or ""
    return ["-- worker CLI output: none recorded --",
            f"no pane.log or screen.log with anything in it under {directory}; "
            + (f"the CLI's own transcript is {other}" if other else
               "the status lines above are dispatch's journal, the only "
               "diagnostic this run has")]


def cmd_wait(args):
    """Block until a run ends, then print what ended it.

    It polls the run's own status.log and, while somebody is watching the run,
    takes no lock and reports nothing, so any number of waiters on one run stay
    harmless and none of them is an authority over it.

    The one exception is a run nobody is watching: a watcher killed mid-run
    leaves a finished worker at its prompt with nothing to journal the exit, and
    a waiter that only read would sit on it until `--give-up` while the
    deliverable lay finished on disk. So an unwatched run gets the same one-look
    reconcile `dispatch status` gives it, on every poll; the watcher lock keeps
    several waiters from racing for it.

    `--give-up` bounds the waiter and never the run. On expiry the run is
    untouched, keeps its check-in ladder, and this exits EXIT_STILL_RUNNING: wall
    clock ends no run anywhere in dispatch, the check-in verdicts do.
    """
    rec = load_record(validate_run_id(args.id))
    give_up = parse_give_up(args.give_up) if args.give_up else None
    status_path = Path(rec["dir"]) / "status.log"
    substrate = None if remote.is_remote(rec) else record_substrate_or_none(rec)
    started = time.time()
    copies_left = remote.MIRROR_RETRIES
    while True:
        rec = load_record(rec["id"])
        if remote.is_remote(rec) and (
                rec.get("state") not in policy().terminal_states
                or rec.get("mirror_pending")):
            rec = remote.reconcile_remote_run(rec)
        # A remote run whose files have not come down yet has not ended for a
        # waiter: reporting it now is `done` with no answer to print. It gets a
        # few more polls, not forever, since a missing file never arrives.
        if run_has_ended(rec, status_path) and rec.get("mirror_pending") \
                and copies_left > 0:
            copies_left -= 1
        elif run_has_ended(rec, status_path):
            # The wrapper saves the state and then writes COMPLETE, so a record
            # read before that line landed is a record read one write too early.
            # Reading it again is what keeps a finished run from being reported
            # with the state it held while it was still working.
            if rec.get("state") not in policy().terminal_states:
                rec = load_record(rec["id"])
            break
        left = None if give_up is None else give_up - (time.time() - started)
        if left is not None and left <= 0:
            emit_frame(wait_report(rec, status_path, WAIT_LOG_LINES)
                       + [f"still running after {args.give_up}; the run is "
                          f"untouched, `dispatch watch {rec['id']} --deep` looks "
                          "closer"],
                       redraw=False)
            return EXIT_STILL_RUNNING
        if rec.get("worker_id") and substrate is not None and not run_is_watched(rec):
            with contextlib.suppress(SubstrateError, OSError):
                rec = reconcile_run(rec, substrate)
            if rec.get("state") in policy().terminal_states \
                    and run_has_ended(rec, status_path):
                break
        pace = REMOTE_FOLLOW_SECONDS if remote.is_remote(rec) else WAIT_POLL_SECONDS
        time.sleep(pace if left is None else min(pace, left))
    lines = wait_report(rec, status_path, WAIT_LOG_LINES)
    lines += [""] + wait_answer_section(rec) + [""] + wait_cli_section(rec)
    emit_frame(lines, redraw=False)
    return run_exit_code(rec)


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


def live_inspect_worker(rec, substrate):
    """The run's inspect home if it is still open, else None (and forgotten)."""
    worker_id = rec.get("inspect_worker")
    if not worker_id:
        return None
    worker = Worker(id=worker_id, group=rec.get("inspect_group", ""),
                    agent=rec.get("agent", ""))
    if substrate.process_info(worker) is None:
        rec["inspect_worker"] = ""
        rec["inspect_group"] = ""
        save_record(rec)
        return None
    return worker


def cmd_inspect(args):
    """Reopen a finished run's native session in a fresh home.

    Injects nothing. The CLI's own session store is what makes a closed home
    recoverable, so this pays for a CLI but not for a turn, and it takes no cap
    slot: prompting it is `dispatch continue`, and that is where the slot is
    charged, because that is where the tokens are.
    """
    rec = load_record(validate_run_id(args.id))
    if remote.is_remote(rec):
        # The session is in that machine's CLI session store, and a home opened
        # here would have nothing to resume.
        raise DispatchError(
            f"{args.id} ran on {rec['machine']}; its session lives there. "
            f"`ssh -t {rec['machine_ssh']} dispatch inspect {rec['remote_id']}` "
            "opens it, and `dispatch continue` from here starts a new turn on it")
    substrate = substrate_for(rec)
    if not substrate.capabilities.can_inspect:
        raise DispatchError(
            f"{args.id} lives in the {substrate.name} substrate, which has no "
            "session to reopen; its transcript is at "
            f"{rec.get('transcript') or Path(rec['dir']) / 'screen.log'}")
    if args.close:
        return close_inspect_worker(rec, substrate)
    if rec.get("state") not in policy().terminal_states:
        raise DispatchError(
            f"{args.id} is still {rec.get('state')}; `dispatch watch {args.id}` "
            "follows a live run and `dispatch steer` corrects one")
    existing = live_inspect_worker(rec, substrate)
    if existing is not None:
        return finish_inspect(rec, existing, substrate, args)
    session_id = refresh_session_id(rec)
    if not session_id:
        raise DispatchError(
            f"{args.id} has no captured session id, so there is no session to "
            "reopen; its transcript is at "
            f"{rec.get('transcript') or Path(rec['dir']) / 'screen.log'}")
    lane = resolve_lane(rec["lane"])
    driver = driver_for_lane(lane)
    opts = RunOptions(dir=rec.get("cwd") or os.getcwd(), write=bool(rec.get("write")),
                      net=bool(rec.get("net")))
    argv = driver.resume_argv(lane, opts, session_id)
    from .runner import run_env
    from dataclasses import replace as _replace
    worker = _replace(
        substrate.open(label=f"inspect-{rec['id']}", cwd=rec.get("cwd") or "",
                       env=run_env(rec["id"], driver), focus=True),
        agent=run_agent_name(rec, substrate))
    log_path = Path(rec["dir"]) / "status.log"
    try:
        substrate.wait_for_shell(worker)
        wrapper = RunWrapper(substrate, rec)
        wrapper.worker = worker
        wrapper.verify_environment()
        spawn = substrate.start_worker(worker, driver, argv)
    except BaseException:
        substrate.close(worker, release=False)
        raise
    rec["inspect_worker"] = worker.id
    rec["inspect_group"] = worker.group
    rec["spawn_method"] = spawn.method
    if spawn.flag:
        rec["spawn_fallback"] = True
        rec["spawn_flag"] = spawn.flag
    save_record(rec)
    append_status(log_path, f"INSPECT {utc_now()} {worker.id}")
    return finish_inspect(rec, worker, substrate, args)


def finish_inspect(rec, worker, substrate, args):
    """One step when there is a terminal, two when there is not."""
    if getattr(args, "no_attach", False) or not stdio_is_tty():
        # Piped or scripted: there is nobody to hand the terminal to, so this
        # stays the two-step form and says how to finish it by hand.
        print_inspect_worker(rec, worker, substrate)
        return EXIT_OK
    return attach_to_inspect_worker(rec, worker, substrate,
                                    keep=getattr(args, "keep", False))


def stdio_is_tty():
    """Is there a human at this terminal to hand the home to?"""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def inspect_worker_adopted(rec):
    """Is some other run driving this run's inspect home right now?

    `dispatch continue` adopts it rather than paying for a second CLI, and its
    wrapper owns it from that point, including closing it. Detaching from an
    attach must not close a home out from under a live turn.
    """
    worker_id = rec.get("inspect_worker")
    if not worker_id:
        return False
    return bool(inspect_home_claimants(worker_id, rec.get("id")))


def inspect_home_claimants(worker_id, but_not=""):
    """Live runs driving, or about to drive, this inspect home, oldest first.

    `adopts_worker` is the claim a `continue` writes the moment it has its own
    record, before the turn starts and `worker_id` says the same thing.
    """
    found = [other for other in all_records()
             if other.get("id") != but_not
             and worker_id in (other.get("worker_id"), other.get("adopts_worker"))
             and other.get("state") not in policy().terminal_states]
    return sorted(found, key=lambda r: (r.get("reserved_at") or 0, r.get("id", "")))


def inspect_fingerprint(rec, worker, substrate):
    """What the home looked like when inspect handed it over.

    Enough to answer one question on the way back: did anything happen here while
    the operator was attached? A hash rather than the screen itself, because the
    record is not the place to keep a copy of a terminal.
    """
    screen = ""
    with contextlib.suppress(SubstrateError):
        screen = substrate.read_screen(worker)
    return {"reported": substrate.status(worker).status,
            "screen": hashlib.sha256(screen.encode("utf-8", "replace")).hexdigest(),
            "at": utc_now()}


def inspect_detach_decision(rec, substrate):
    """(close it?, why). Anything that looks like work in progress stays open.

    An extra home costs a cap slot; a killed manual turn costs work. So this
    closes only a home that is exactly as inspect left it, and treats every other
    answer, including "cannot tell", as a reason to keep it.
    """
    if inspect_worker_adopted(rec):
        return False, "a `dispatch continue` is running in it and owns it now"
    worker = Worker(id=rec.get("inspect_worker", ""),
                    group=rec.get("inspect_group", ""),
                    agent=rec.get("agent", ""))
    reported = substrate.status(worker).status
    if reported in ("working", "blocked"):
        return False, f"the session is {reported}"
    before = rec.get("inspect_state") or {}
    if not before:
        return False, "there is no record of how it looked when it was opened"
    now = inspect_fingerprint(rec, worker, substrate)
    if before.get("screen") != now["screen"]:
        return False, "its screen changed while you were attached, so it was used"
    if before.get("reported") != now["reported"]:
        return False, (f"the worker went from {before.get('reported') or 'unknown'}"
                       f" to {now['reported'] or 'unknown'}")
    return True, "it is untouched since it was opened"


def attach_to_inspect_worker(rec, worker, substrate, keep=False):
    """Attach, then decide what the home deserves once the operator detaches.

    Closing is the tidy default (an inspected TUI left open is an orphan, and the
    session store makes reopening cheap), but only for a home nothing happened
    in. A turn in flight, or a session the operator typed into themselves, stays
    exactly where it is.
    """
    log_path = Path(rec["dir"]) / "status.log"
    rec["inspect_state"] = inspect_fingerprint(rec, worker, substrate)
    save_record(rec)
    with contextlib.suppress(SubstrateError):
        substrate.focus(worker)
    print(f"{rec['id']} reopened in {worker.id}; attaching now "
          "(detach to come back here)")
    sys.stdout.flush()
    substrate.attach(worker)
    # Re-read: a `dispatch continue` may have adopted this home, and the operator
    # may have used the session, while they were attached to it.
    rec = load_record(rec["id"])
    if keep:
        append_status(log_path, f"INSPECT-DETACH {utc_now()} kept: --keep")
        print(f"{rec['id']} left open in {rec.get('inspect_worker')}; "
              f"close it with `dispatch inspect {rec['id']} --close`")
        return EXIT_OK
    if live_inspect_worker(rec, substrate) is None:
        append_status(log_path, f"INSPECT-DETACH {utc_now()} the home is already gone")
        print(f"{rec['id']} home is gone; reopen it with "
              f"`dispatch inspect {rec['id']}`")
        return EXIT_OK
    close_it, reason = inspect_detach_decision(rec, substrate)
    append_status(log_path,
                  f"INSPECT-DETACH {utc_now()} "
                  f"{'closed' if close_it else 'kept'}: {reason}")
    if close_it:
        return close_inspect_worker(rec, substrate)
    print(f"{rec['id']} left open in {rec.get('inspect_worker')}: {reason}")
    print(f"close it with `dispatch inspect {rec['id']} --close`, or prompt it "
          f"with `dispatch continue {rec['id']} <message>`")
    return EXIT_OK


def print_inspect_worker(rec, worker, substrate):
    hint = substrate.attach_hint(worker)
    print(f"{rec['id']} reopened in {worker.id} (group {worker.group}, focused)")
    print(f"attach with `{hint}`; nothing was typed into the session" if hint else
          "attach with your substrate's own attach; nothing was typed into the "
          "session")
    print(f"prompt it with `dispatch continue {rec['id']} <message>`, which "
          "reuses this home and takes a worker slot")
    print(f"close it with `dispatch inspect {rec['id']} --close`")


def close_inspect_worker(rec, substrate):
    worker_id = rec.get("inspect_worker")
    if not worker_id:
        print(f"{rec['id']} has no inspect home open")
        return EXIT_OK
    # Under the same lock a `continue` claims the home under, and the pointer is
    # cleared before the lock is let go, so a claim cannot land on a home that is
    # being closed.
    group = rec.get("inspect_group", "")
    with runs_lock():
        claimants = inspect_home_claimants(worker_id, rec["id"])
        if claimants:
            raise DispatchError(
                f"{claimants[0]['id']} is running a turn in that home and closes "
                f"it when it ends; `dispatch kill {claimants[0]['id']}` stops it now")
        rec["inspect_worker"] = ""
        rec["inspect_group"] = ""
        save_record(rec)
    substrate.close(Worker(id=worker_id, group=group), release=False)
    append_status(Path(rec["dir"]) / "status.log", f"INSPECT-CLOSED {utc_now()}")
    print(f"{rec['id']} inspect home {worker_id} closed; reopen it any time with "
          f"`dispatch inspect {rec['id']}`")
    return EXIT_OK


# --------------------------------------------------------------------------
# lanes
# --------------------------------------------------------------------------


def lane_expansions(lane_text):
    """(label, argv) pairs a human can diff against what the lane actually types."""
    lane = resolve_lane(lane_text)
    driver = driver_for_lane(lane)
    session = "00000000-1111-2222-3333-444444444444"
    rows = []
    read_opts = RunOptions(dir="<dir>")
    rows.append(("read-only", driver.launch_argv(lane, read_opts, session)))
    write_opts = RunOptions(dir="<dir>", write=True)
    with contextlib.suppress(DispatchError):
        driver.validate_options(lane, write_opts)
        rows.append(("--write", driver.launch_argv(lane, write_opts, session)))
    rows.append(("continue", driver.resume_argv(lane, read_opts, session)))
    rows.append(("headless", driver.headless_argv(lane, read_opts,
                                                  "<prompt>", "<out>", session)))
    return rows


def cmd_lanes(args):
    substrate = optional_substrate()
    print(f"dispatch {__version__}  runs: {runs_root()}  depth: {current_depth()}  "
          f"substrate: {substrate.name if substrate else 'none reachable'}")
    print("A worker is an interactive CLI in its substrate's own home; the brief "
          "goes in after the TUI is up, never as an argv element.")
    for name in lane_names():
        lane = resolve_lane(name)
        driver = driver_for_lane(lane)
        print()
        print(f"{name}  [{driver.name}]")
        for label, argv in lane_expansions(name):
            print(f"  {label:<10} {' '.join(shlex.quote(a) for a in argv)}")
    return EXIT_OK


# --------------------------------------------------------------------------
# board, doctor, init, and the packaged text
# --------------------------------------------------------------------------


def cmd_board(args):
    print(render_board(active_config()), end="")
    return EXIT_OK


def cmd_doctor(args):
    substrate = optional_substrate()
    report, ok = render_report(active_config(),
                               substrate=substrate.name if substrate else "",
                               version=__version__)
    print(report, end="")
    return EXIT_OK if ok else EXIT_FAILED


def cmd_init(args):
    for line in run_init(preset=args.preset, force=args.force):
        print(line)
    print()
    print("Paste this into AGENTS.md or CLAUDE.md:")
    print()
    print(agents_snippet_text(), end="")
    return EXIT_OK


def cmd_skill(args):
    if args.install:
        for line in install_skill(force=args.force):
            print(line)
        return EXIT_OK
    print(skill_text(), end="")
    return EXIT_OK


def cmd_agents_snippet(args):
    print(agents_snippet_text(), end="")
    return EXIT_OK


# --------------------------------------------------------------------------
# The internal watcher
# --------------------------------------------------------------------------


def cmd_watch_run(args):
    """Internal: the detached wrapper for one background run, deadline included.

    It takes `watcher.lock` so the reconcile sweep can tell a live watcher from a
    dead one, and it runs exactly the wrapper a foreground run runs.
    """
    rec = load_record(validate_run_id(args.id))
    handle = hold_run_lock(rec, "watcher.lock")
    if handle is None:
        raise DispatchError(f"{args.id} already has a watcher")
    rec["watcher_pid"] = os.getpid()
    save_record(rec)
    substrate = substrate_for(rec)
    wrapper = RunWrapper(substrate, rec)
    wrapper.attach()
    try:
        rec = wrapper.watch(deadline_seconds=rec.get("deadline_seconds")
                            or parse_deadline(policy().default_deadline))
    finally:
        release_run_lock(handle)
    append_status(Path(rec["dir"]) / "status.log",
                  f"COMPLETE {utc_now()} state={rec['state']} "
                  f"out={Path(rec['dir']) / 'out.md'}")
    return state_exit_code(rec["state"])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="dispatch",
        description="Hand work to a codex, claude, or grok worker on a fixed lane.")
    parser.add_argument("--version", action="version",
                        version=f"dispatch {__version__}")
    subs = parser.add_subparsers(dest="command")

    run = subs.add_parser("run",
                          help=f"single spawn; lane defaults to {default_lane()}")
    run.add_argument("positional", nargs="*", metavar="[lane] brief")
    run.add_argument("--dir",
                     help="working directory, and codex's sandbox root (default: cwd)")
    run.add_argument("--write", action="store_true",
                     help="allow writing, under the driver's own permissions")
    run.add_argument("--net", action="store_true",
                     help="network inside a codex write sandbox")
    run.add_argument("--add-dir", action="append",
                     help="extra writable tree (codex lanes, with --write), repeatable; "
                          "claude: additional tool directory; grok: ignored")
    run.add_argument("--schema", help="JSON output for this schema file; dispatch checks it parses, "
                          "not that it conforms")
    run.add_argument("--out", help="copy the final message here too")
    run.add_argument("--bg", action="store_true",
                     help="background; prints the run id, then the run directory")
    run.add_argument("--deadline",
                     help=f"check-in interval (default {policy().default_deadline})")
    run.add_argument("--image", help="attach an image (codex -i)")
    run.add_argument("--on", metavar="MACHINE",
                     help="run it on this machine over ssh; `local` overrides a "
                          "lane that names one")
    run.set_defaults(func=cmd_run)

    cont = subs.add_parser("continue", help="new turn on a finished run's session")
    cont.add_argument("id")
    cont.add_argument("message", nargs="?", default="")
    cont.add_argument("--message-file", metavar="PATH",
                      help="read the message from a file instead")
    cont.add_argument("--deadline", help="check-in interval for the new turn")
    cont.add_argument("--bg", action="store_true",
                      help="return once the turn is under way")
    cont.set_defaults(func=cmd_continue)

    steer = subs.add_parser(
        "steer", help="type a correction into a live worker, liveness-gated")
    steer.add_argument("id")
    steer.add_argument("message", nargs="?", default="")
    steer.add_argument("--message-file", metavar="PATH",
                       help="read the message from a file instead")
    steer.add_argument("--deadline",
                       help="check-in interval if this becomes a continue")
    steer.set_defaults(func=cmd_steer)

    status = subs.add_parser("status", help="one line per run")
    status.set_defaults(func=cmd_status)

    logs = subs.add_parser("logs", help="print or follow a run's log")
    logs.add_argument("id")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.set_defaults(func=cmd_logs)

    kill = subs.add_parser("kill",
                           help="stop the worker's process tree and close its home")
    kill.add_argument("id")
    kill.set_defaults(func=cmd_kill)

    lanes = subs.add_parser("lanes",
                            help="the lane table and what each lane expands to")
    lanes.set_defaults(func=cmd_lanes)

    watch = subs.add_parser(
        "watch",
        help="the multi-run wall, follow one run, --deep for one snapshot, "
             "or --attach to its live home")
    watch.add_argument("id", nargs="?")
    watch.add_argument("-f", "--follow", action="store_true",
                       help="redraw until the run finishes")
    watch.add_argument("--deep", action="store_true",
                       help="one snapshot: log tail, what the worker is doing, "
                            "and out.md so far (needs an id)")
    watch.add_argument("--attach", action="store_true",
                       help="hand the terminal to the run's live home (needs an id)")
    watch.set_defaults(func=cmd_watch)

    wait = subs.add_parser(
        "wait", help="block until a run ends, then print its log tail and out.md")
    wait.add_argument("id")
    wait.add_argument("--give-up",
                      help="stop waiting after this long and exit "
                           f"{EXIT_STILL_RUNNING}; the run carries on (default: "
                           "wait as long as the run takes)")
    wait.set_defaults(func=cmd_wait)

    inspect = subs.add_parser(
        "inspect", help="reopen a finished run's session and attach to it")
    inspect.add_argument("id")
    inspect.add_argument("--close", action="store_true",
                         help="close the home opened earlier and exit")
    inspect.add_argument("--keep", action="store_true",
                         help="leave the home open after you detach")
    inspect.add_argument("--no-attach", action="store_true",
                         help="open the home and print how to attach")
    inspect.set_defaults(func=cmd_inspect)

    board = subs.add_parser("board", help="the routing board, one row per slot")
    board.set_defaults(func=cmd_board)

    doctor = subs.add_parser(
        "doctor", help="each lane's CLI, each machine's ssh, and the substrate")
    doctor.set_defaults(func=cmd_doctor)

    init = subs.add_parser(
        "init", help="write the config from a preset and install the skill")
    init.add_argument("--preset", default=DEFAULT_PRESET,
                      help=f"one of: {', '.join(preset_names())} "
                           f"(default: {DEFAULT_PRESET})")
    init.add_argument("--force", action="store_true",
                      help="overwrite an existing config and skill")
    init.set_defaults(func=cmd_init)

    skill = subs.add_parser("skill", help="print the dispatch skill")
    skill.add_argument("--install", action="store_true",
                       help="install it into each agent home that exists, "
                            "leaving the config alone")
    skill.add_argument("--force", action="store_true",
                       help="with --install, replace a skill that was edited")
    skill.set_defaults(func=cmd_skill)

    snippet = subs.add_parser("agents-snippet",
                              help="print the AGENTS.md snippet")
    snippet.set_defaults(func=cmd_agents_snippet)

    internal_watch = subs.add_parser("_watch")
    internal_watch.add_argument("id")
    internal_watch.set_defaults(func=cmd_watch_run)

    return parser


def parse_cli(parser, argv):
    """`run` accepts its lane and brief wherever they land in argv.

    argparse fills a `nargs="*"` positional group once, so a positional that
    follows an optional (`run opus@medium --write brief.md`) is left over instead
    of joining the group. Fold those leftovers back in; anything flag-shaped, or
    leftovers on any other subcommand, is still an error.
    """
    args, extra = parser.parse_known_args(argv)
    if extra:
        if (getattr(args, "command", None) != "run"
                or any(a.startswith("-") for a in extra)):
            parser.error("unrecognized arguments: " + " ".join(extra))
        args.positional.extend(extra)
    return args


def main(argv=None):
    """The process entry point: install the operator's config, then run a verb.

    Config first, because it decides the default lane the parser advertises, the
    lane table every verb resolves against, and the substrate a run lands in.
    """
    try:
        load_and_apply()
    except DispatchError as exc:
        # `init --force` is the way out of a config that will not load, so it is
        # the one verb that runs on the built-in defaults instead.
        if (sys.argv[1:] if argv is None else list(argv))[:1] != ["init"]:
            print(f"dispatch: {exc}", file=sys.stderr)
            return EXIT_USAGE
    return run_verb(argv)


def run_verb(argv=None):
    """One verb, against the config already in force."""
    parser = build_parser()
    args = parse_cli(parser, argv if argv is not None else sys.argv[1:])
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except DispatchError as exc:
        print(f"dispatch: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("dispatch: interrupted", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
