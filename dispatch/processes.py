"""Cross-platform process facts: is it alive, what descends from it, stop it.

Nothing here knows about runs or substrates. It exists because a worker's real
process tree is the only honest answer to "is this thing still working", and
because a process group is not a family tree: a child that calls setsid, or that
outlives its parent and is reparented to init, keeps running when the group is
signalled.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
# Windows OpenSSH runs a session's processes inside a job object it closes at
# logout, and a detached child is still a member of it. Breaking away is what
# lets a daemon or watcher spawned over ssh outlive the ssh session.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB

# SIGTERM, then SIGKILL after this long.
KILL_GRACE_SECONDS = 10

# The widest a process id can be: pid_t is signed on POSIX, a DWORD on Windows.
MAX_PID = 0xFFFFFFFF if IS_WINDOWS else 0x7FFFFFFF


def checked_pid(pid):
    """One real process, or None. Every query and every signal goes through here.

    Most pids here come back off a record a worker can write, and `os.kill` reads
    the whole integer line as targets rather than as ids: 0 is the caller's own
    process group, anything below -1 is some other group, and -1 is every process
    the operator may signal. 1 is init. A bool is an int in Python, and a digit
    string coerces to any of those, so the type is checked as narrowly as the
    range.
    """
    if type(pid) is not int or not 1 < pid <= MAX_PID:
        return None
    return pid


def popen_detached(argv, **kwargs):
    """Start `argv` outside this process's lifetime, and outside its job where allowed.

    A job that forbids breakaway refuses the flag with ERROR_ACCESS_DENIED, so
    the spawn falls back to a plain detached child there: it outlives this
    process and ends with the job.
    """
    if not IS_WINDOWS:
        return subprocess.Popen(argv, start_new_session=True, **kwargs)
    try:
        return subprocess.Popen(argv, creationflags=DETACHED_FLAGS, **kwargs)
    except PermissionError:
        return subprocess.Popen(
            argv, creationflags=DETACHED_FLAGS & ~CREATE_BREAKAWAY_FROM_JOB, **kwargs)


def pid_alive(pid):
    pid = checked_pid(pid)
    if pid is None:
        return False
    if IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pid_is_zombie(pid):
    """Exited and not yet reaped: `pid_alive` says yes to it, and it runs nothing."""
    pid = checked_pid(pid)
    if IS_WINDOWS or pid is None:
        return False
    try:
        found = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return found.stdout.strip().startswith("Z")


def terminate_pid(pid):
    """Graceful stop. Windows has no SIGTERM; the group break is the analog.

    The break only reaches a process put in a group of its own, which is the
    detached watcher and nothing else. A worker inside a terminal multiplexer
    belongs to that daemon's console, where `os.kill` raises SystemError rather
    than failing quietly, so that one is left to `kill_pid`'s taskkill on the
    caller's next pass.
    """
    pid = checked_pid(pid)
    if pid is None:
        return
    try:
        if IS_WINDOWS:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, AttributeError, SystemError):
        pass


def kill_pid(pid):
    pid = checked_pid(pid)
    if pid is None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def stop_pid(pid):
    """SIGTERM, then SIGKILL after the grace window."""
    if not pid_alive(pid):
        return
    terminate_pid(pid)
    deadline = time.time() + KILL_GRACE_SECONDS
    while pid_alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    if pid_alive(pid):
        kill_pid(pid)


def posix_process_parents():
    """(pid, ppid) for every live process, off `ps`. Empty when it cannot run."""
    try:
        probe = subprocess.run(["ps", "-eo", "pid=,ppid="], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    pairs = []
    for line in (probe.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pairs.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return pairs


def windows_process_parents():
    """The same pairs off a Toolhelp snapshot, which is the whole table at once.

    ctypes rather than a `Get-CimInstance` subprocess because this is asked once
    per poll of every live run: a snapshot is microseconds where spawning
    PowerShell is the better part of a second.
    """
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return []
    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(ProcessEntry32)
    pairs = []
    try:
        found = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while found:
            pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            found = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return pairs


def descendant_pids(pids):
    """Every process descended from these, by walking ppid links once."""
    # `True` would otherwise index the table at 1 and return the whole orphan
    # population of the machine, which the callers go on to signal.
    roots = [pid for pid in map(checked_pid, pids) if pid is not None]
    children = {}
    for child, parent in (windows_process_parents() if IS_WINDOWS
                          else posix_process_parents()):
        children.setdefault(parent, []).append(child)
    found, queue, seen = [], list(roots), set(roots)
    while queue:
        for child in children.get(queue.pop(), ()):
            if child not in seen:
                seen.add(child)
                found.append(child)
                queue.append(child)
    return found


def stop_process_group(pgid):
    """SIGTERM then SIGKILL a whole foreground group. False where unsupported."""
    pgid = checked_pid(pgid)
    if IS_WINDOWS or pgid is None or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + KILL_GRACE_SECONDS
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    return True


def process_cpu_percent(pid):
    """Recent CPU for one pid, or None where it cannot be read.

    A free signal: no tokens. POSIX-only, because Windows has no equivalent that
    is one cheap read (its counters are cumulative, so a percent costs two
    samples and a sleep inside the poll loop). The liveness judge degrades to
    output activity there rather than paying that on every look.
    """
    pid = checked_pid(pid)
    if IS_WINDOWS or pid is None:
        return None
    try:
        probe = subprocess.run(["ps", "-o", "%cpu=", "-p", str(pid)],
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float((probe.stdout or "").strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def pids_cpu_percent(pids):
    """Recent CPU across a worker's whole process tree, or None if unreadable.

    The sum, not the maximum: a CLI often idles while a tool subprocess of its
    own does the work, and either one burning cycles means the worker is alive.
    """
    readings = [process_cpu_percent(pid) for pid in pids]
    readings = [reading for reading in readings if reading is not None]
    return sum(readings) if readings else None


def resolve_python():
    """Interpreter for the detached watcher.

    On Windows the WindowsApps `python.exe` is a Store stub that exits without
    running anything, so a candidate only counts once `--version` prints a real
    Python 3.
    """
    if not IS_WINDOWS:
        return sys.executable or "python3"
    candidates = []
    if sys.executable and "WindowsApps" not in sys.executable:
        candidates.append(sys.executable)
    candidates += ["python3.exe", "python.exe", "py.exe"]
    for candidate in candidates:
        try:
            probe = subprocess.run([candidate, "--version"], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=10,
                                   check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        blob = (probe.stdout or "") + (probe.stderr or "")
        if probe.returncode == 0 and blob.strip().startswith("Python 3"):
            return candidate
    from .errors import DispatchError
    raise DispatchError("no working python3 found (the WindowsApps stub does not count)")
