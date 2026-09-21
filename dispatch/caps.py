"""Caps: how many workers may be live, and the reservation that enforces it.

Two limits, checked and reserved under one cross-process lock, with the
reservation written before the worker is spawned. Anything that counts runs only
after spawning has a window in which the caps do not exist.

Nothing here knows what a substrate is. Liveness needs one fact this module
cannot produce, "does this run's worker still exist", so it is asked of a
`Sweep`: `runner.SubstrateSweep` answers it from a real substrate, and the
default answers "cannot tell", which keeps the caps conservative rather than
opening the machine to unlimited spawns when a daemon blinks.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .errors import DispatchError
from .policy import policy
from .processes import IS_WINDOWS, pid_alive
from .records import (all_records, append_status, lock_is_held, runs_lock,
                      save_record, utc_now)

# A reservation with no worker yet still counts against both caps for this long:
# it covers the window between reserving a slot and the worker existing to be
# counted.
RESERVATION_GRACE_SECONDS = 60

# How long a `--bg` run counts as watched before its detached watcher has taken
# watcher.lock: a cold interpreter start, generously.
WATCHER_START_GRACE_SECONDS = 30

# How often a queued caller retries the caps.
SLOT_POLL_SECONDS = 0.25


class Sweep:
    """What `live_records` needs from outside the run records.

    The default implementation has no substrate, so it can neither see workers
    nor close them; it settles a record and nothing more. Subclass it to make
    reconciling real (see `runner.SubstrateSweep`).
    """

    def worker_ids(self):
        """Every live worker id the substrate knows of, or None when unreachable."""
        return None

    def covers(self, rec):
        """Can this sweep answer for this run at all?

        A sweep holds one substrate on one machine, and a worker id means
        something only inside the substrate that issued it: two of them number
        their panes the same way, so an id from one names a stranger's worker in
        the other. A run this sweep does not cover keeps its record and its
        home, which is the same conservative answer an unreachable substrate
        gets: the dispatch invocation that does run that substrate settles it.
        """
        return False

    def reconcile(self, rec):
        """One poll of an unwatched run. Returns the record, possibly terminal."""
        return rec

    def home_remains(self, rec):
        """Is the worker's home still there to read an ending from?"""
        return False

    def reconcile_remote(self, rec):
        """One poll of an unwatched run placed on another machine."""
        return rec

    def close_finished(self, rec, ids):
        """Close the home of a run that is over but whose worker home is still up."""
        return False

    def abandon(self, rec, ids):
        """Close out a run nobody is driving and nothing ever finished."""
        return abandon_record(rec)


def abandon_record(rec, error=""):
    """Settle a run whose owner is gone, in the record alone.

    `orphaned` keeps its meaning: a record whose worker was already gone, so
    nothing was lost here that was not lost before. `failed` is for a run that
    still held a live worker somebody had to take down, and that caller passes
    the reason.
    """
    rec["state"] = "failed" if error else "orphaned"
    if error:
        rec["error"] = rec.get("error") or error
    rec["finished"] = utc_now()
    rec["closed_by"] = "reconcile"
    save_record(rec)
    return rec


def session_key():
    """Which parent session a worker belongs to, for the per-session cap.

    `DISPATCH_SESSION` when the caller sets one, so runs can be grouped
    deliberately; otherwise the OS session (POSIX `getsid`, and the parent pid on
    Windows, which has no sessions). The key is stored in the run record rather
    than recomputed, because a run reconciled later by another dispatch process
    would otherwise be counted against that process's allowance instead.
    """
    explicit = (os.environ.get("DISPATCH_SESSION") or "").strip()
    if explicit:
        return explicit
    try:
        if not IS_WINDOWS:
            return f"sid:{os.getsid(0)}"
    except (AttributeError, OSError):
        pass
    return f"ppid:{os.getppid()}"


def reserved_recently(rec):
    try:
        return (time.time() - float(rec.get("reserved_at") or 0)) \
            < RESERVATION_GRACE_SECONDS
    except (TypeError, ValueError):
        return False


def run_is_live(rec, worker_ids=None):
    """Liveness by live worker, never by bare pid.

    The worker's home outlives every dispatch process, so its existence is the
    fact and the lock only covers the window before there is one to see.
    """
    if rec.get("state") in policy().terminal_states:
        return False
    if rec.get("remote_supervised"):
        # Liveness is that machine's to answer, and the poll that asked it wrote
        # the answer into this record. Falling through to the local checks would
        # report every live remote run as orphaned, because none of them has a
        # worker or an owner here.
        return True
    worker_id = rec.get("worker_id")
    if worker_id and worker_ids is not None:
        return worker_id in worker_ids
    # The substrate is unreachable, nobody asked it, or there is no worker yet.
    # A held lock is a live process driving the run, the launcher's or the
    # watcher's. Without the watcher's, a healthy background run read as finished
    # once its reservation aged out, and `steer` and `continue` opened a second
    # turn on a session that was still working.
    directory = Path(rec.get("dir", ""))
    if lock_is_held(directory / "owner.lock") \
            or lock_is_held(directory / "watcher.lock"):
        return True
    return reserved_recently(rec)


def run_never_started(rec):
    """Reserved and then dropped before its worker ever began.

    `reserved` is the state a prepared run carries; anything that got as far as
    starting a worker moves past it. Combined with nobody holding it, that is a
    launcher which died between reserving a slot and using it.
    """
    return rec.get("state") == "reserved" and not rec.get("started_at")


def run_is_watched(rec):
    """Is some process already driving this run to completion?

    Foreground runs are watched by their owner; background runs by their launcher
    first and then by the detached watcher. All of them advertise it with a held
    lock, because a lock dies with its holder and a pid does not.

    The grace covers the one window a lock cannot: a freshly spawned watcher has
    not started Python yet, and a reconcile sweep in that gap would adopt a run
    that is about to be driven properly. A pid is only ever used to revoke the
    grace early, never to grant liveness.
    """
    directory = Path(rec.get("dir", ""))
    # The launcher holds owner.lock for the whole of opening the worker's home,
    # starting the CLI, and delivering the brief. That window belongs to the
    # launcher whether or not the run is a background one, so the owner lock is
    # checked first and unconditionally.
    if lock_is_held(directory / "owner.lock"):
        return True
    if rec.get("foreground"):
        return False
    if lock_is_held(directory / "watcher.lock"):
        return True
    started = rec.get("watcher_started_at") or 0
    try:
        starting = (time.time() - float(started)) < WATCHER_START_GRACE_SECONDS
    except (TypeError, ValueError):
        return False
    if not starting:
        return False
    pid = rec.get("watcher_pid")
    return not pid or pid_alive(pid)


def live_records(sweep=None):
    """Runs still holding a slot, settling the ones nobody is watching.

    Reconciling is what makes a detached run's exit code land and its cap slot
    free without a supervisor process: every command that counts runs also polls
    the ones whose workers are still up. Pass no sweep to count without settling.
    """
    records = all_records()
    reconcile = sweep is not None
    sweep = sweep or Sweep()
    ids = sweep.worker_ids() if any(r.get("worker_id") for r in records) else None
    live = []
    for rec in records:
        if rec.get("remote_supervised"):
            # The worker lives on another machine, which runs its own dispatch
            # and drives it. Nothing here can see that worker, so the local
            # checks below would read a healthy run as one whose worker had
            # vanished and abandon it. The sweep asks the machine instead.
            if reconcile and not run_is_watched(rec) and (
                    rec.get("state") not in policy().terminal_states
                    or rec.get("mirror_pending")):
                rec = sweep.reconcile_remote(rec)
            if rec.get("state") not in policy().terminal_states:
                live.append(rec)
            continue
        terminal = rec.get("state") in policy().terminal_states
        # A sweep that cannot answer for this run must not act on it: its ids
        # belong to another substrate's numbering, and the worker it would close
        # is somebody else's.
        mine = sweep.covers(rec)
        if terminal:
            if reconcile and mine:
                sweep.close_finished(rec, ids)
            continue
        # Somebody is already watching most runs. Reconciling underneath a
        # watcher would race it for the exit code and close its worker's home.
        # Once that watcher is gone the worker is nobody's, and this adopts it,
        # which is what keeps a crashed launcher from leaking a cap slot and
        # stranding a finished TUI.
        watched = run_is_watched(rec)
        # A watcher holds its lock for exactly as long as it is driving a live
        # worker, so it is evidence of liveness in its own right. Without it, a
        # background run whose substrate cannot list workers is abandoned the
        # moment its reservation goes stale, out from under the watcher.
        if run_is_live(rec, ids if mine else None) or watched:
            if reconcile and mine and not watched and run_never_started(rec):
                sweep.abandon(rec, ids)
                continue
            if reconcile and mine and rec.get("worker_id") and not watched:
                rec = sweep.reconcile(rec)
                if rec.get("state") in policy().terminal_states:
                    continue
            live.append(rec)
        elif reconcile and mine:
            if rec.get("worker_id") and sweep.home_remains(rec):
                # Not listed is not always gone: a headless worker leaves the
                # list when it exits, with its status written and its answer on
                # disk. One look reads that ending instead of calling a run that
                # finished well orphaned.
                rec = sweep.reconcile(rec)
                if rec.get("state") in policy().terminal_states:
                    continue
            sweep.abandon(rec, ids)
    return live


def cap_refusal(over_session, key, blocked):
    ids = ", ".join(r["id"] for r in blocked)
    limits = policy()
    if over_session:
        return (f"session cap reached: {len(blocked)} workers live for session {key} "
                f"(limit {limits.session_cap} per parent session): {ids}. "
                "Wait for one, or `dispatch kill <id>`.")
    return (f"machine cap reached: {len(blocked)} workers live "
            f"(hard limit {limits.machine_cap} machine-wide): {ids}. "
            "Wait for one, or `dispatch kill <id>`.")


def reserve_slot(prepare, sweep=None, block=False):
    """Check both caps and write the reservation under one lock, before spawning.

    `prepare` is called with the lock held and returns the new run record; it is
    a callback rather than a record because the reservation and the record it
    reserves have to be written inside the same critical section.

    Returns (record, lock handle). `block=True` queues instead of refusing.
    """
    from .records import hold_run_lock

    key = session_key()
    limits = policy()
    while True:
        with runs_lock():
            live = live_records(sweep)
            mine = [r for r in live if r.get("session") == key]
            if len(mine) < limits.session_cap and len(live) < limits.machine_cap:
                rec = prepare()
                handle = hold_run_lock(rec, "owner.lock")
                return rec, handle
            # Name the limit the caller can act on: their own session's runs are
            # theirs to kill, so that one is reported when both are saturated.
            over_session = len(mine) >= limits.session_cap
            refusal = cap_refusal(over_session, key, mine if over_session else live)
        if not block:
            raise DispatchError(refusal)
        time.sleep(SLOT_POLL_SECONDS)


def note_abandoned(rec, line):
    append_status(Path(rec["dir"]) / "status.log", line)
