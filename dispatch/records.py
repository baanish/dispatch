"""Run records: the durable file every other module reads and writes.

One run is one directory under `~/.dispatch/runs/<id>` holding `run.json`, the
brief, the prompt, the deliverable, and `status.log`. The record is the source
of truth about what happened; a substrate's own view of a pane is what says so
while it is happening.

Two invariants shape this module:

- A record is replaced atomically and never edited in place, because a run has
  concurrent readers by design (its watcher, a reconcile sweep, whoever ran
  `kill`).
- Liveness is an advisory file lock held by whichever process owns a run, never
  a bare pid. Pids get reused; a lock dies with its holder, so a crashed run
  frees its slot and a recycled pid is never signalled by mistake.
"""

from __future__ import annotations

import calendar
import contextlib
import json
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .errors import DispatchError
from .processes import IS_WINDOWS

# Run ids and session ids both end up as filesystem path segments and as argv
# elements, so both are validated at every entry point: no separators, no
# leading dash, no glob metacharacters.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,127}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Session ids on disk and on screen are uuids; matching loosely here would pick
# the date out of `rollout-2026-<uuid>.jsonl` and call it a session.
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# How long a Windows reader may hold a record open before a write gives up.
RECORD_REPLACE_SECONDS = 5.0

# The pace the run loop writes heartbeats at.
HEARTBEAT_SECONDS = 15

# How long a command waits for runs.lock before refusing to go on without it.
RUNS_LOCK_SECONDS = 120

# A worker that refuses its brief writes this first. Terminal and distinct from
# failure: nothing retries it.
ABORT_MARKER = "ABORT:"

COMPLETE_MARKER = "COMPLETE "


@dataclass
class RunOptions:
    """What the caller asked for, beyond the lane.

    It lives beside the record rather than beside the lane because these are the
    fields a run is recorded with: a reader of `run.json` is reading these back.
    """

    dir: str = ""
    write: bool = False
    net: bool = False
    add_dirs: tuple = ()
    schema: str = ""
    out: str = ""
    bg: bool = False
    deadline: str = ""
    image: str = ""


def dispatch_home():
    """Durable, never a tempdir.

    DISPATCH_HOME relocates it for tests and one-off ops work. It is deliberately
    not a trust boundary: anything able to set it can equally set AGENT_DEPTH.
    A relocation from inside a worker gets a warning on stderr.
    """
    override = os.environ.get("DISPATCH_HOME")
    if not override:
        return Path.home() / ".dispatch"
    if (os.environ.get("AGENT_DEPTH") or "0").strip() not in ("", "0"):
        import sys
        print(f"dispatch: warning: DISPATCH_HOME={override} inside a depth>0 worker; "
              "cap and status are relative to that tree, not ~/.dispatch",
              file=sys.stderr)
    return Path(override)


def runs_root():
    """Owner-only, whatever the umask: briefs, prompts, and answers live below."""
    root = dispatch_home() / "runs"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def stamp_epoch(text):
    """A `utc_now()` stamp back to epoch seconds, or None.

    timegm, not mktime: the stamps are UTC, and reading them as local time would
    age every finished run by the timezone offset.
    """
    try:
        return calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def new_run_id(lane_name):
    """`<lane>-<hhmmss>-<4 hex>`, sanitized so it is a legal Windows dirname."""
    safe = re.sub(r"[^A-Za-z0-9@._-]", "-", lane_name)
    return f"{safe}-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}"


def validate_run_id(run_id):
    """Every command that takes an id goes through here.

    `dispatch logs ../../elsewhere` would otherwise read, kill, or resume records
    outside the runs tree entirely.
    """
    text = str(run_id or "")
    if not RUN_ID_RE.match(text) or ".." in text:
        raise DispatchError(f"not a run id: {run_id!r}")
    return text


def validate_session_id(session_id):
    """A session id becomes both an argv element and a glob, so it needs a shape.

    An id discovered from a transcript filename is still worker-influenced
    input: `*` as a session id would walk the whole sessions tree.
    """
    text = str(session_id or "")
    if not SESSION_ID_RE.match(text):
        raise DispatchError(f"not a session id: {session_id!r}")
    return text


def run_dir(run_id):
    directory = runs_root() / validate_run_id(run_id)
    root = os.path.realpath(runs_root())
    resolved = os.path.realpath(directory)
    if os.path.dirname(resolved) != root:
        raise DispatchError(f"run id escapes the runs tree: {run_id!r}")
    return Path(directory)


def load_record(run_id):
    path = run_dir(run_id) / "run.json"
    if not path.is_file():
        raise DispatchError(f"no such run: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_replace(tmp, path):
    """Move a written temp file over its target, waiting out a Windows reader.

    `replace` is atomic on both platforms, but on Windows it is refused outright
    (`ERROR_ACCESS_DENIED`) for as long as any other handle holds the destination
    open, and a run record has concurrent readers by design. Their opens are
    microseconds, so waiting one out is the whole fix. On POSIX the first attempt
    always wins and this never sleeps.
    """
    deadline = time.time() + RECORD_REPLACE_SECONDS
    while True:
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if time.time() >= deadline:
                raise
            time.sleep(0.02)


def replace_text(path, text):
    """Write a file in a run directory by replacing it, never through a link.

    A worker writes here too, so the name may be a symlink to a file of the
    operator's by the time dispatch writes it.
    """
    replace_bytes(path, text.encode("utf-8"))


def replace_bytes(path, data):
    """`replace_text` for bytes."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(tmp, flags, 0o666), "wb") as handle:
        handle.write(data)
    atomic_replace(tmp, path)


def open_append(path):
    """Open a log in a run directory for appending bytes, never through a link."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o666), "ab")


def out_copy_path(out):
    """The `--out` destination with its directory resolved, as recorded at launch."""
    if not out:
        return ""
    return str(Path(os.path.realpath(Path(out).parent)) / Path(out).name)


def out_copy_dir(out):
    """Which directory `--out` lands in, as `[device, inode]`, recorded at launch."""
    if not out:
        return []
    found = os.stat(Path(out).parent)
    return [found.st_dev, found.st_ino]


def write_out_copy(rec, text):
    """Write the `--out` copy without following anything a worker left there.

    The destination may sit inside the worker's writable tree, and this write
    happens as the operator, outside any sandbox. The directory is opened once
    and has to be the one recorded at launch, by inode, so a directory renamed
    away and replaced with a link refuses the copy however it is timed. Inside
    it the leaf is replaced, never written through, so a leaf that has become a
    symlink is simply lost. An existing file keeps its mode. False when
    refused: the answer is still in the run directory.
    """
    target = Path(rec["out_copy"])
    tmp = f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    anchored = os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd
    if not anchored:
        # Windows has no directory descriptors, and no unprivileged symlinks.
        if os.path.realpath(target.parent) != str(target.parent):
            return _refuse_out_copy(rec, target)
        with os.fdopen(os.open(target.with_name(tmp), flags, 0o666), "w",
                       encoding="utf-8") as handle:
            handle.write(text)
        atomic_replace(target.with_name(tmp), target)
        return True
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        found = os.fstat(directory)
        if [found.st_dev, found.st_ino] != list(rec.get("out_copy_dir") or []):
            return _refuse_out_copy(rec, target)
        with os.fdopen(os.open(tmp, flags, 0o666, dir_fd=directory), "w",
                       encoding="utf-8") as handle:
            handle.write(text)
            with contextlib.suppress(OSError):
                old = os.stat(target.name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(old.st_mode):
                    os.fchmod(handle.fileno(), stat.S_IMODE(old.st_mode))
        os.replace(tmp, target.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        os.close(directory)
    return True


def _refuse_out_copy(rec, target):
    append_status(Path(rec["dir"]) / "status.log",
                  f"OUT-REFUSED {utc_now()} {target.parent} is not the directory "
                  "it was at launch")
    return False


def save_record(rec):
    """Publish a run record, atomically and without colliding with a co-writer.

    A record has several writers by design: whoever is watching or reconciling
    the run, and whatever verb the operator ran. One temp name shared between
    them is one file two of them write at once, and the first to replace it
    publishes the other's half-written copy or finds it already gone, so the
    scratch file is this writer's alone.
    """
    path = Path(rec["dir"]) / "run.json"
    tmp = path.with_name(f"run.json.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with runs_lock():
        keep_the_ending_on_disk(rec, path)
        tmp.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        atomic_replace(tmp, path)


# How a run ended. Written once, by whoever ended it, and carried by every
# later write of that record.
OUTCOME_FIELDS = ("state", "rc", "finished", "closed_by", "error")


def keep_the_ending_on_disk(rec, path):
    """Once a run has ended on disk, no later write changes how it ended.

    Every writer publishes the whole record from the copy it holds, and those
    copies go stale: a watcher mid-poll still holds `running` after `kill` has
    written `killed`, and a second finalizer still holds its own verdict after
    the first has published `done`. Atomic replace keeps the file whole and does
    nothing about that. `orphaned` is the exception, because it is dispatch's
    guess that nobody is left, and a process that then reports the real ending
    knows better.
    """
    from .policy import policy

    try:
        on_disk = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    ended = on_disk.get("state")
    if ended in policy().terminal_states and ended != "orphaned":
        for field in OUTCOME_FIELDS:
            if field in on_disk:
                rec[field] = on_disk[field]
            else:
                rec.pop(field, None)


def save_final_record(rec):
    """Close a run. The first ending on disk stands: see `save_record`."""
    save_record(rec)
    return rec


def save_heartbeat(rec):
    """Stamp a run as seen alive, without republishing the copy in memory.

    Same hazard as `save_final_record`, at the other end of a run's life: a
    heartbeat is the one write that carries nothing but its own timestamp, so
    writing the whole record back would undo whatever another process journaled
    while this one was polling. Read and written under the runs lock, which is
    reentrant, so a poll inside a check-and-reserve takes it again.
    """
    stamp = utc_now()
    rec["heartbeat"] = stamp
    path = Path(rec["dir"]) / "run.json"
    with runs_lock():
        try:
            on_disk = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            on_disk = dict(rec)
        on_disk["heartbeat"] = stamp
        save_record(on_disk)
    return stamp


def all_records():
    out = []
    for entry in sorted(runs_root().glob("*/run.json")):
        try:
            out.append(json.loads(entry.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def read_tail_bytes(path, limit):
    """The last `limit` bytes of a file, decoded. Never the whole file."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def iter_json_lines(path):
    if not Path(path).is_file():
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


# --------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------


def lock_handle(path, blocking):
    """Take an exclusive advisory lock, or return None if someone else holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if IS_WINDOWS:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(),
                           msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), flags)
    except OSError:
        handle.close()
        return None
    return handle


def release_handle(handle):
    if handle is None:
        return
    try:
        if IS_WINDOWS:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


_runs_lock_owner = None
_runs_lock_depth = 0


@contextlib.contextmanager
def runs_lock():
    """The cross-process lock around check-and-reserve, and around finalizing.

    Reentrant for the thread holding it, because those two nest: the
    check-and-reserve sweeps the runs it counts, and a sweep that adopts an
    orphan finalizes it from inside the reservation. A second `flock` on a
    fresh descriptor blocks against the first whether or not the same process
    holds it, so the nested take is counted here instead of taken again.
    """
    global _runs_lock_owner, _runs_lock_depth
    me = threading.get_ident()
    if _runs_lock_owner == me:
        _runs_lock_depth += 1
        try:
            yield
        finally:
            _runs_lock_depth -= 1
        return
    home = dispatch_home()
    home.mkdir(parents=True, exist_ok=True)
    # Never entered without the lock. A blocking acquire still comes back empty:
    # Windows gives up after ten one-second tries, and a holder can be inside a
    # reconcile for longer than that.
    give_up = time.time() + RUNS_LOCK_SECONDS
    handle = lock_handle(home / "runs.lock", blocking=True)
    while handle is None:
        if time.time() >= give_up:
            raise DispatchError(
                f"could not take {home / 'runs.lock'} in {RUNS_LOCK_SECONDS}s; "
                "refusing to count or change runs without it")
        time.sleep(0.1)
        handle = lock_handle(home / "runs.lock", blocking=True)
    _runs_lock_owner, _runs_lock_depth = me, 1
    try:
        yield
    finally:
        _runs_lock_owner, _runs_lock_depth = None, 0
        release_handle(handle)


def lock_is_held(path):
    """True when some open descriptor (this process's or another's) holds it."""
    if not Path(path).exists():
        return False
    handle = lock_handle(path, blocking=False)
    if handle is None:
        return True
    release_handle(handle)
    return False


# Owner locks stay held for the life of the process that took them; keeping the
# handles here stops the garbage collector from closing them early.
HELD_LOCKS = []


def hold_run_lock(rec, name):
    handle = lock_handle(Path(rec["dir"]) / name, blocking=False)
    if handle is not None:
        HELD_LOCKS.append(handle)
    return handle


def release_run_lock(handle):
    if handle in HELD_LOCKS:
        HELD_LOCKS.remove(handle)
    release_handle(handle)


# --------------------------------------------------------------------------
# status.log
# --------------------------------------------------------------------------


def sanitize_log_line(text):
    """Briefs reach the log through argv; a newline in one must not forge a line."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(text))


def write_status_header(status_path, lane_name, pid, cwd, argv, started):
    """The fixed 5-line header a supervising reader surveys with `head -n 5`."""
    cmd = sanitize_log_line(" ".join(argv))
    lines = [
        f"lane: {sanitize_log_line(lane_name)}",
        f"pid: {pid}",
        f"cwd: {sanitize_log_line(cwd)}",
        f"cmd: {cmd[:200]}",
        f"started: {started}",
    ]
    with open_append(status_path) as fh:
        fh.write(("\n".join(lines) + f"\nSTART {started} {cmd}\n").encode("utf-8"))


def append_status(status_path, line):
    # Never through a link: a worker that can write in its run directory could
    # otherwise aim this append at a file of the operator's.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(status_path, flags, 0o666), "a", encoding="utf-8") as fh:
        fh.write(sanitize_log_line(line) + "\n")


def read_status_head(status_path, count=5):
    if not Path(status_path).is_file():
        return {}
    head = {}
    with open(status_path, "r", encoding="utf-8", errors="replace") as fh:
        for _ in range(count):
            line = fh.readline()
            if not line:
                break
            if ": " in line:
                key, value = line.rstrip("\n").split(": ", 1)
                head[key] = value
    return head


def last_heartbeat_age(status_path):
    """Seconds since the last heartbeat line, or None. Bounded tail read."""
    path = Path(status_path)
    if not path.is_file():
        return None
    tail = read_tail_bytes(path, 4096)
    stamp = None
    for line in tail.splitlines():
        if line.startswith(("HEARTBEAT ", "START ", "EXIT ")):
            parts = line.split()
            if len(parts) >= 2:
                stamp = parts[1]
    if not stamp:
        return None
    seen = stamp_epoch(stamp)
    if seen is None:
        return None
    return max(0, int(time.time() - seen))


def deliverable_path(rec):
    """The file the worker was told to write: a schema run's is out.json."""
    return Path(rec["dir"]) / ("out.json" if rec.get("schema") else "out.md")


def read_output(rec):
    path = Path(rec["dir"]) / "out.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")
