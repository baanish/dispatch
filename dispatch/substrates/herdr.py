"""The herdr substrate: workers live in panes owned by a terminal multiplexer.

herdr is a terminal multiplexer whose daemon owns real PTYs. A worker is an
interactive CLI in a pane, which is why it outlives every dispatch process and
why a run can be reconciled later by a command that did not launch it.

Three facts about the control socket shape this whole file:

- The daemon closes the connection after every response, so a client is
  connect-per-call. Anything holding one socket open for a second request gets a
  broken pipe.
- Panes carry no exit codes, so the real one is read back out of the shell:
  wait for the prompt to return, type an `echo` of the status, and parse a
  per-run marker off the screen.
- `pane.report_agent` from a source outside herdr's detection allowlist replaces
  detection rather than sitting beside it: one report evicts the named agent and
  every `agent.*` probe answers agent_not_found afterwards. So dispatch reports
  once, at the end. During the turn herdr's detection is the status of record;
  dispatch is still the lifecycle authority for what matters, and the final
  report is what puts the ending on the wall.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from ..errors import DispatchError
from .. import processes
from ..processes import (IS_MACOS, IS_WINDOWS, descendant_pids, popen_detached,
                         pids_cpu_percent)
from ..records import append_status, utc_now
from .base import (Substrate, SubstrateCapabilities, SubstrateError, SpawnResult,
                   Worker, WorkerProcess, WorkerSetupError, WorkerStatus)
from .paneshell import (ENV_MARKER, PANE_SHELL, RC_MARKER, anchor_after,
                        parse_env_echo, parse_rc_echo, rc_token)

# Pinned: protocol drift is a loud refusal, never a guess. Both entries have
# been verified live, call by call (workspace.create, pane.split,
# pane.send_text/keys, pane.read, pane.process_info, agent.prompt,
# workspace.close). A fleet mid-upgrade may run either; anything else refuses.
PROTOCOL = 22
PROTOCOLS = (20, PROTOCOL)

SOCKET_ENV = "HERDR_SOCKET_PATH"   # herdr's own override, honored by its CLI
POSIX_CONFIG_DIR = "~/.config/herdr"
WINDOWS_CONFIG_DIR = "herdr"       # under %APPDATA%
SOCKET_NAME = "herdr.sock"
PIPE_PREFIX = "\\\\.\\pipe\\"

# ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND: nothing is listening on the name,
# which is the only code that means no daemon. Busy is a daemon that is up, and
# is waited out rather than reported.
WINDOWS_PIPE_MISSING = (2, 3)
WINDOWS_PIPE_BUSY = 231
WINDOWS_PIPE_BROKEN = 109                # ERROR_BROKEN_PIPE: the daemon hung up
# ERROR_ACCESS_DENIED: the pipe is there and will not open for us.
WINDOWS_PIPE_DENIED = 5

CALL_TIMEOUT = 20.0        # one socket call; `agent.start` passes its own
# A reply is one line of JSON: a screen is the largest of them and herdr caps
# what it will send. Past this, whatever holds the socket path is feeding this
# process rather than answering it.
MAX_REPLY_BYTES = 8 * 1024 * 1024
SOURCE = "dispatch"        # report_agent source: outside herdr's allowlist
LABEL_PREFIX = "dispatch-"  # every workspace dispatch creates is labelled

# Transport self-heal. Two ways the control endpoint fails that a run should not
# simply die on, and they want opposite treatment:
#
# - Nothing is listening. dispatch starts the daemon itself: one detached spawn,
#   one bounded wait, one retry of the call that found it down.
# - Something is listening and will not talk to us. The daemon belongs to
#   another security context, so retrying is spam and only a session with rights
#   over it can bounce it. That class is refused with the diagnosis and the
#   one-line bounce command in the message.
#
# The spawn is once per dispatch invocation, process-wide. A second attempt
# inside one invocation means the first daemon did not come up, and a loop of
# detached spawns is how a box ends up with six herdrs and no working one.
SERVER_DOWN_CODE = "server_not_running"   # herdr's own code for it
SPAWN_ARGV = ("herdr", "server")
SPAWN_WAIT_SECONDS = 5.0
SPAWN_POLL_SECONDS = 0.2
# The opt-out, for a caller that must never start a daemon: a test suite that
# points the client at a socket which was never there.
SPAWN_ENV = "DISPATCH_HERDR_SPAWN"
# Homebrew installs herdr as a LaunchAgent on macOS, so the bounce there goes
# through launchctl and not `kill`: launchd would restart a killed daemon
# underneath the operator, with the same ownership it had before.
MACOS_SERVICE = "homebrew.mxcl.herdr"

# Attaching is plain `herdr`: it attaches to the persistent session, and the
# workspace dispatch focused is the one it lands on. There is no per-pane attach
# verb, and `herdr session attach <name>` is for named sessions, which dispatch
# does not create.
ATTACH_ARGV = ("herdr",)

SHELL_SETTLE_SECONDS = 30   # wait for a fresh pane's shell rc files to finish
PROBE_SECONDS = 10.0        # how long an echoed marker has to appear on screen
ENV_ASSERT_ATTEMPTS = 3     # a busy pane can drop the export line; re-assert it
CHILD_APPEAR_SECONDS = 60.0  # how long a typed command has to produce a process
AGENT_START_TIMEOUT_MS = 60000  # herdr's own TUI-detection window
PROMPT_TIMEOUT = 120.0
STALLED_CODE = "agent_prompt_stalled"

# Typing into a TUI is not typing into a shell. A shell reads its line
# discipline; a TUI input box has to render the text before its key handler will
# treat the next enter as a submit, and back-to-back text plus enter leaves the
# line sitting unsent. So: type, let it draw, submit, then check that it went,
# because an unsubmitted brief looks exactly like a working worker.
TUI_SETTLE_SECONDS = 0.5
TUI_SUBMIT_SECONDS = 3.0
TUI_ENTER_BEAT_SECONDS = 0.3

# herdr refuses an agent name that is not `[a-z][a-z0-9_-]{0,31}`, and a run id
# is not one: a lane string has an `@` in it.
AGENT_NAME_MAX = 32
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

FALLBACK_FLAG = (
    "HERDR FALLBACK: `agent start` (kind {kind}) failed on pane {pane}: {error}. "
    "dispatch typed the lane argv into the pane instead, so the run continues but "
    "herdr never detected the agent: its pane wall, prompting, and detection are "
    "degraded for this run. Investigate before the next batch.")

UNCONFIRMED_FLAG = (
    "HERDR SPAWN UNCONFIRMED: `agent start` on pane {pane} failed after the "
    "pane had already started a process: {error}. dispatch adopted what is "
    "running rather than typing the lane argv into it, so the run continues "
    "but herdr may never have detected the agent: its pane wall, prompting, "
    "and detection may be degraded for this run. Investigate before the next "
    "batch.")

DRIFT_MESSAGE = (
    "herdr protocol drift: expected {expected}, got {actual}; finish the run by "
    "hand if it is urgent, and update dispatch's pinned protocol")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class HerdrError(SubstrateError):
    """herdr substrate failure, operator-facing like every other DispatchError."""


class HerdrCallError(HerdrError):
    """An error body from the daemon; `code` is herdr's machine-readable one."""

    def __init__(self, method, code, message):
        super().__init__(f"herdr {method} failed [{code}]: {message}")
        self.method = method
        self.code = code
        self.message = message


class HerdrDaemonDown(HerdrError):
    """Nothing is listening on the control endpoint.

    The repairable class: dispatch starts the daemon itself and retries once.
    Separate from a refused connection, which looks the same to a caller and
    must never be retried.
    """


class HerdrAccessDenied(HerdrError):
    """The endpoint is there and refused us: another security context owns it.

    Never repaired here. Starting a second daemon cannot take a name that is
    already bound, and retrying cannot acquire rights this process does not
    have, so the message carries the bounce command instead.
    """


# --------------------------------------------------------------------------
# Endpoint discovery and daemon repair
# --------------------------------------------------------------------------


def config_dir():
    """Where herdr keeps its endpoint and session state on this platform."""
    if IS_WINDOWS:
        appdata = (os.environ.get("APPDATA") or "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / WINDOWS_CONFIG_DIR
    return Path(POSIX_CONFIG_DIR).expanduser()


def socket_path():
    override = (os.environ.get(SOCKET_ENV) or "").strip()
    if override:
        return Path(override)
    return config_dir() / SOCKET_NAME


def pipe_name(path):
    """The Windows endpoint for a socket path: the pipe prefix plus the path.

    herdr does not shorten or hash the name, it prefixes the same path it would
    have bound a unix socket at, drive letter and colon included. Taking the
    name from `socket_path` rather than hardcoding it is what keeps the socket
    environment override working on Windows too.
    """
    text = str(path)
    if text.startswith(PIPE_PREFIX):
        return text
    return PIPE_PREFIX + text


def missing_daemon_message(address, exc):
    """The one message an operator sees when nothing is listening."""
    start = "run `herdr`" if IS_WINDOWS else "start it with `brew services start herdr`"
    return f"no herdr daemon on {address} ({exc}); {start}"


def daemon_process():
    """(pid, start time) of the herdr daemon on this box, or (None, "").

    Best effort by design, and only ever used to make the access-denied message
    more specific: the message still names the repair without a pid, so a
    listing that does not parse is not an error worth raising.
    """
    if IS_WINDOWS:
        argv = ["powershell", "-NoProfile", "-Command",
                "Get-Process herdr -ErrorAction SilentlyContinue | "
                "Select-Object -First 1 | ForEach-Object "
                "{ \"$($_.Id) $($_.StartTime)\" }"]
    else:
        argv = ["ps", "-Ao", "pid=,lstart=,comm="]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return (None, "")
    for line in done.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        if IS_WINDOWS:
            return (fields[0], " ".join(fields[1:]))
        if os.path.basename(fields[-1]) == "herdr":
            return (fields[0], " ".join(fields[1:-1]))
    return (None, "")


def bounce_command(pid):
    """The one line that restarts a daemon this session is not allowed to touch."""
    if IS_WINDOWS:
        return (f"Stop-Process -Id {pid or '<pid>'} -Force; "
                "Start-Process herdr -ArgumentList 'server'")
    if IS_MACOS:
        return f"launchctl kickstart -k gui/{os.getuid()}/{MACOS_SERVICE}"
    return f"kill {pid or '<pid>'} && herdr server"


def access_denied_message(address, exc):
    """The message for an endpoint that is up and owned by somebody else.

    It carries the repair, not just the diagnosis: whoever hits this is inside a
    session that by definition cannot fix it, so what they need in hand is one
    line to paste into a session that can.
    """
    pid, started = daemon_process()
    who = f"pid {pid}" if pid else "pid unreadable from here"
    when = f", started {started}" if started else ""
    return (f"herdr's endpoint at {address} exists but refused this connection "
            f"({exc}): the daemon ({who}{when}) belongs to another security "
            "context, so this session can neither call it nor bounce it, and "
            "retrying will not change that. Bounce it from a session that owns "
            f"it: {bounce_command(pid)}")


_SPAWN_LOCK = threading.Lock()
_SPAWNED = False


def spawn_daemon():
    """Start the herdr daemon detached, at most once per dispatch invocation.

    Detached because the daemon outlives whichever dispatch process noticed it
    was missing, and a run's watcher must not end up as its parent.

    Returns False when there is nothing left to try, which is the whole loop
    guard: a second spawn in one invocation means the first daemon never came
    up, and the answer to that is an error an operator reads.
    """
    global _SPAWNED
    if (os.environ.get(SPAWN_ENV) or "").strip() in ("0", "no", "false"):
        return False
    with _SPAWN_LOCK:
        if _SPAWNED:
            return False
        _SPAWNED = True
    try:
        popen_detached(list(SPAWN_ARGV), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise HerdrError(f"no herdr daemon was running and "
                         f"`{' '.join(SPAWN_ARGV)}` could not be "
                         f"started: {exc}") from exc
    return True


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------


class UnixTransport:
    """One request per connection over herdr's unix socket.

    The framing is the daemon's: write one JSON line, read one back, and the
    daemon hangs up. Reconnecting per call is not a missed optimization, it is
    the only thing the daemon supports.
    """

    def __init__(self, path, timeout=CALL_TIMEOUT):
        self.address = str(path)
        self.timeout = timeout

    def request(self, payload, timeout=None):
        if not hasattr(socket, "AF_UNIX"):
            raise HerdrError(
                f"this platform has no AF_UNIX, so {self.address} cannot be a "
                "socket; transport_for should have chosen a pipe here")
        seconds = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + seconds
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(seconds)
        try:
            sock.connect(self.address)
        except OSError as exc:
            sock.close()
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise HerdrAccessDenied(
                    access_denied_message(self.address, exc)) from exc
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                raise HerdrDaemonDown(
                    missing_daemon_message(self.address, exc)) from exc
            raise HerdrError(missing_daemon_message(self.address, exc)) from exc
        try:
            sock.sendall(payload)
            return self._read_reply(sock, deadline, seconds)
        finally:
            sock.close()

    def _read_reply(self, sock, deadline, seconds):
        """One line back, inside one deadline and one size.

        A socket timeout bounds a single recv, and something answering a byte
        at a time renews it on every one: the budget that means anything is the
        caller's, and it covers connecting, sending, and being answered.
        """
        wedged = (f"herdr did not finish answering on {self.address} within "
                  f"{seconds}s; the daemon may be wedged")
        reply = bytearray()
        while not reply.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrError(wedged)
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(65536)
            except TimeoutError as exc:
                raise HerdrError(wedged) from exc
            if not chunk:                  # the daemon hung up, as it does
                break
            reply += chunk
            if len(reply) > MAX_REPLY_BYTES:
                raise HerdrError(
                    f"herdr sent more than {MAX_REPLY_BYTES} bytes on "
                    f"{self.address} without ending the line")
        return bytes(reply)


class PipeTransport:
    """The same one-request-per-connection framing over a Windows named pipe.

    Opening goes through CreateFileW rather than the builtin `open`, which also
    reaches the pipe but arrives through the CRT's `_wopen`: that sets errno and
    leaves `winerror` empty, which collapses "no daemon" and "every pipe instance
    is busy" into one indistinguishable error. Both have to be told apart,
    because a busy pipe is normal (a run's detached watcher polls the same daemon
    as its caller) and is worth waiting out, while a missing one is an operator
    error worth naming.

    The timeout has to be a thread join, because a synchronous pipe handle has no
    read timeout to set: Windows offers one only through overlapped I/O. A call
    that outruns its deadline leaves one blocked thread behind and never a wrong
    answer.
    """

    def __init__(self, path, timeout=CALL_TIMEOUT):
        self.address = pipe_name(path)
        self.timeout = timeout

    def _exchange(self, payload, deadline):
        handle = self._open(deadline)
        try:
            handle.write(payload)
            handle.flush()
            line = bytearray()
            while not line.endswith(b"\n"):
                if time.monotonic() > deadline:
                    raise HerdrError(
                        f"herdr did not finish answering on {self.address} "
                        "before the call's deadline")
                if len(line) > MAX_REPLY_BYTES:
                    raise HerdrError(
                        f"herdr sent more than {MAX_REPLY_BYTES} bytes on "
                        f"{self.address} without ending the line")
                try:
                    chunk = handle.read(65536)
                except OSError as exc:
                    # The daemon hanging up is how a response ends, not an error.
                    if getattr(exc, "winerror", None) != WINDOWS_PIPE_BROKEN \
                            and exc.errno != errno.EPIPE:
                        raise
                    break
                if not chunk:
                    break
                line += chunk
            return bytes(line)
        finally:
            with contextlib.suppress(OSError):
                handle.close()

    def _open(self, deadline):
        """Open the pipe, waiting out a daemon whose instances are all in use.

        Busy is retried until the caller's own deadline rather than for some
        shorter window of its own: a busy pipe is a daemon that is up and
        saturated, so the call's timeout is the only budget that should decide
        when to give up on it.
        """
        import ctypes
        import msvcrt
        from ctypes import wintypes

        GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING = 0x80000000, 0x40000000, 3
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = wintypes.HANDLE
        invalid = ctypes.c_void_p(-1).value
        while True:
            handle = kernel32.CreateFileW(self.address, GENERIC_READ | GENERIC_WRITE,
                                          0, None, OPEN_EXISTING, 0, None)
            if handle != invalid:
                # The fd owns the handle from here, and closing the file closes both.
                return os.fdopen(msvcrt.open_osfhandle(handle, os.O_BINARY),
                                 "r+b", buffering=0)
            code = ctypes.GetLastError()
            if code == WINDOWS_PIPE_BUSY:
                if time.monotonic() < deadline:
                    time.sleep(0.05)
                    continue
                raise HerdrError(
                    f"every instance of herdr's pipe at {self.address} was busy "
                    "for the whole call; the daemon is up but saturated")
            error = OSError(f"CreateFileW({self.address}) failed: {code}")
            if code in WINDOWS_PIPE_MISSING:
                raise HerdrDaemonDown(missing_daemon_message(self.address, error))
            if code == WINDOWS_PIPE_DENIED:
                raise HerdrAccessDenied(access_denied_message(self.address, error))
            raise error

    def request(self, payload, timeout=None):
        seconds = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + seconds
        answer = {}

        def exchange():
            try:
                answer["line"] = self._exchange(payload, deadline)
            except BaseException as exc:            # re-raised on the caller's thread
                answer["error"] = exc

        worker = threading.Thread(target=exchange, daemon=True)
        worker.start()
        worker.join(seconds)
        if worker.is_alive():
            raise HerdrError(
                f"herdr did not answer on {self.address} within {seconds}s; the "
                "daemon may be wedged")
        if "error" in answer:
            raise answer["error"]
        return answer.get("line", b"")


def transport_for(path, timeout=CALL_TIMEOUT):
    """The control-plane transport for this platform, socket or pipe."""
    if IS_WINDOWS:
        return PipeTransport(path, timeout)
    return UnixTransport(path, timeout)


class HerdrClient:
    """JSON-RPC over herdr's control endpoint: one line out, one line back, per call.

    Not a connection pool by choice: the daemon hangs up after each response, so
    reusing the connection is not an optimization available to any client.

    `transport` is injectable so a test can drive the real client against
    something other than this platform's endpoint.
    """

    def __init__(self, path=None, timeout=CALL_TIMEOUT, transport=None):
        self.socket_path = str(path or socket_path())
        self.timeout = timeout
        self.transport = transport or transport_for(self.socket_path, timeout)
        self.protocol = None      # filled by check_protocol, then trusted
        # Where a repair says what it did. A run's wrapper points this at its
        # status.log; a client with no run behind it repairs just as silently.
        self.log_path = None
        self._lock = threading.Lock()
        self._seq = 0

    def _next_id(self, method):
        """Mirrors herdr's own `cli:pane:split` convention, with our name on it."""
        with self._lock:
            self._seq += 1
            return f"{SOURCE}:{method.replace('.', ':')}:{self._seq}"

    def call(self, method, params=None, timeout=None):
        """Return the `result` body, or raise HerdrCallError on an error body.

        The protocol pin happens here, on the client's first call, rather than
        in whichever method a caller happened to reach first: verbs touch panes
        of runs they did not create, and a drifting daemon has to refuse those
        too. A daemon that is down is repaired here for the same reason: every
        herdr call funnels through this method, and a repair living in one of
        them would leave the other forty failing opaquely.
        """
        if self.protocol is None and method != "ping":
            self.check_protocol()
        request_id = self._next_id(method)
        payload = json.dumps({"id": request_id, "method": method,
                              "params": params or {}}) + "\n"
        try:
            message = self.transact(method, payload, timeout, request_id)
        except HerdrDaemonDown as exc:
            self.repair_daemon(exc)     # raises if there is nothing left to try
            self.note(f"RETRY {utc_now()} herdr answered after the repair; "
                      f"retrying {method}")
            message = self.transact(method, payload, timeout, request_id)
        except HerdrAccessDenied as exc:
            self.note(f"REPAIR-REFUSED {utc_now()} {exc}")
            raise
        if "error" in message:
            body = message["error"]
            raise HerdrCallError(method, body.get("code", "unknown"),
                                 body.get("message", ""))
        result = message.get("result") or {}
        if not isinstance(result, dict):
            raise HerdrError(f"herdr {method} answered with a "
                             f"{type(result).__name__} where its result body "
                             "should be")
        return result

    def transact(self, method, payload, timeout, request_id):
        """One request on the wire, parsed into the reply to it.

        Raises HerdrDaemonDown for either way herdr says nothing is serving: the
        transport not reaching the endpoint, and an endpoint that answers
        `server_not_running`. Both are the same operator problem and take the
        same repair.

        Whatever is listening on that socket path has to answer this request
        and carry exactly one of a reply's two arms. A pane's contents, a
        worker's status, and a run's exit code are all read out of what comes
        back, so a reply that is not to this request is not this call's answer.
        """
        try:
            line = self.transport.request(payload.encode("utf-8"), timeout)
        except OSError as exc:
            raise HerdrError(f"herdr {method} failed: {exc}") from exc
        if not line:
            raise HerdrError(
                f"herdr closed the socket without answering {method}; the daemon "
                "may have died mid-call")
        try:
            message = json.loads(line.decode("utf-8", "replace"))
        except ValueError as exc:
            raise HerdrError(
                f"herdr {method} returned non-JSON: {line[:200]!r}") from exc
        if not isinstance(message, dict):
            raise HerdrError(
                f"herdr {method} returned {line[:200]!r}, which is not a reply")
        body = message.get("error")
        # Before the request id is checked: an endpoint with no daemon behind it
        # has no request to answer, and the repair is the same either way.
        if isinstance(body, dict) and body.get("code") == SERVER_DOWN_CODE:
            raise HerdrDaemonDown(f"herdr {method} failed "
                                  f"[{SERVER_DOWN_CODE}]: "
                                  f"{body.get('message', '')}")
        if message.get("id") != request_id:
            raise HerdrError(
                f"herdr answered {method} ({request_id}) with a reply to "
                f"{message.get('id')!r}")
        if "error" in message and "result" in message:
            raise HerdrError(f"herdr {method} answered with a result and an "
                             "error at once, and only one of them can be true")
        if "error" not in message and "result" not in message:
            raise HerdrError(f"herdr {method} answered with neither a result "
                             f"nor an error: {line[:200]!r}")
        if "error" in message and not isinstance(body, dict):
            raise HerdrError(f"herdr {method} failed and gave {body!r} as the "
                             "reason, which is not an error body")
        return message

    def repair_daemon(self, exc):
        """Start the daemon this call could not reach, and wait for it to answer.

        Raises the caller's own failure rather than a new one when there is
        nothing left to try: the operator's problem is still "no herdr", and a
        second message about a spawn that already happened buries it.
        """
        if not spawn_daemon():
            raise exc
        self.note(f"REPAIR {utc_now()} no herdr daemon on {self.socket_path}; "
                  f"started `{' '.join(SPAWN_ARGV)}` detached")
        if not self.wait_for_daemon():
            raise HerdrError(
                f"{exc}; dispatch started `{' '.join(SPAWN_ARGV)}` and it "
                f"did not answer on {self.socket_path} within "
                f"{SPAWN_WAIT_SECONDS}s")
        # What answers now is a different process, so the pin the dead daemon
        # earned says nothing about it and is taken again before the retry.
        self.protocol = None
        self.check_protocol()

    def wait_for_daemon(self):
        """Poll the endpoint until a ping comes back, bounded. True if it did."""
        deadline = time.time() + SPAWN_WAIT_SECONDS
        request_id = self._next_id("ping")
        payload = json.dumps({"id": request_id, "method": "ping",
                              "params": {}}) + "\n"
        while True:
            remaining = deadline - time.time()
            with contextlib.suppress(HerdrError, OSError):
                self.transact("ping", payload, max(SPAWN_POLL_SECONDS, remaining),
                              request_id)
                return True
            if time.time() >= deadline:
                return False
            time.sleep(SPAWN_POLL_SECONDS)

    def note(self, line):
        """Say what the transport did, into the run's log when one is attached."""
        if self.log_path is not None:
            append_status(self.log_path, line)

    def ping(self):
        return self.call("ping", {})

    def check_protocol(self):
        """Pin the verified protocols. Drift refuses and names the escape hatch.

        Cached: the daemon cannot change protocol without restarting, and a
        per-call handshake would double every request on the wire.
        """
        if self.protocol is not None:
            return self.protocol
        pong = self.ping()
        actual = pong.get("protocol")
        if actual not in PROTOCOLS:
            raise HerdrError(DRIFT_MESSAGE.format(expected=PROTOCOL, actual=actual))
        self.protocol = actual
        return actual


# --------------------------------------------------------------------------
# Pane process facts
# --------------------------------------------------------------------------


def pane_shell_pid(info):
    return (info or {}).get("shell_pid")


def pane_at_prompt(info):
    """True when the shell owns the pane's foreground again: the worker exited.

    The foreground process *group*, not the process list, is the signal. A shell
    sitting at its prompt is never alone: prompt hooks, rc files, and version
    managers fork short-lived subshells continuously, and every one of them is
    inside the shell's own group. A command the user (or dispatch) runs gets a
    new group, which is exactly the distinction being read here.
    """
    shell = pane_shell_pid(info)
    if not info or not shell:
        return False
    return info.get("foreground_process_group_id") == shell


def pane_worker_pids(info):
    """The foreground group's pids while the pane runs something, else empty."""
    if not info or pane_at_prompt(info):
        return []
    shell = pane_shell_pid(info)
    return [p.get("pid") for p in info.get("foreground_processes", ())
            if p.get("pid") and p.get("pid") != shell]


def fill_pane_processes(info):
    """Put the pane's own children back into what herdr reported, on Windows.

    herdr on Windows answers `pane.process_info` with the pane's shell and
    nothing under it, even with a child genuinely running. Left alone, every
    pane reads as sitting at its prompt, which means waiting for a spawned child
    times out on every launch and every worker reads as finished before it has
    started.

    So the tree is walked locally from the one pid herdr does report, and only
    when herdr reported no worker: a daemon that names the foreground itself is
    the better authority.
    """
    if not IS_WINDOWS or not info or pane_worker_pids(info):
        return info
    shell = pane_shell_pid(info)
    children = descendant_pids([shell]) if shell else []
    if not children:
        return info
    info = dict(info)
    info["foreground_process_group_id"] = children[0]
    info["foreground_processes"] = [{"pid": pid} for pid in children]
    return info


def agent_name(run_id):
    """A herdr-legal agent name for a run id.

    The tail is what survives a long id, because the timestamp and random suffix
    are what tell two runs apart; the lane is already on the record and in the
    workspace label.
    """
    text = re.sub(r"[^a-z0-9_-]", "-", str(run_id).lower())
    text = text[-(AGENT_NAME_MAX - 1):].strip("-_")
    if not text or not text[0].isalpha():
        # Must begin with a lowercase letter; `d` for dispatch.
        text = "d" + text
    return text[:AGENT_NAME_MAX]


class HerdrSubstrate(Substrate):
    """One workspace per run, closed when the run ends.

    Panes do not outlive their run: the vendor CLIs' own session stores make a
    finished run resumable, so keeping sixteen dead TUIs on screen buys nothing.
    """

    name = "herdr"
    capabilities = SubstrateCapabilities(
        can_steer=True, can_inspect=True, can_read_screen=True,
        can_answer_dialogs=True)

    def __init__(self, client=None):
        self.client = client or HerdrClient()

    # -- lifecycle -------------------------------------------------------

    def check_protocol(self):
        return self.client.check_protocol()

    def available(self):
        """A daemon that answers `ping` with a protocol dispatch speaks.

        The same handshake the first real call would make, paid here so that
        auto-detection can move on to the next substrate instead of failing a
        run on a machine where herdr is simply not running.
        """
        try:
            self.check_protocol()
        except (HerdrError, OSError):
            return False
        return True

    def open(self, label, cwd="", env=None, focus=False):
        self.check_protocol()
        params = {"label": f"{LABEL_PREFIX}{label}", "focus": bool(focus),
                  "env": dict(env or {})}
        if cwd:
            params["cwd"] = str(cwd)
        result = self.client.call("workspace.create", params)
        workspace = result.get("workspace") or {}
        root = result.get("root_pane") or {}
        worker = Worker(id=root.get("pane_id", ""),
                        group=workspace.get("workspace_id", ""))
        if not worker.id or not worker.group:
            raise HerdrError(f"herdr workspace.create returned no pane: {result}")
        return worker

    def close(self, worker, release=True):
        if release and worker.agent:
            with contextlib.suppress(HerdrError):
                self._tolerate_absence(
                    "pane.release_agent",
                    {"pane_id": worker.id, "source": SOURCE, "agent": worker.agent})
        self._tolerate_absence("pane.close", {"pane_id": worker.id})
        if worker.group:
            self._tolerate_absence("workspace.close", {"workspace_id": worker.group})

    def exists(self, worker):
        return self.process_info(worker) is not None

    def worker_ids(self):
        try:
            result = self.client.call("pane.list", {})
        except HerdrError:
            return None
        return {pane.get("pane_id") for pane in result.get("panes", ())}

    def _tolerate_absence(self, method, params):
        """Closing something already gone is success, not an error to propagate.

        A worker that runs `exit` takes its pane and, if it was the last one,
        its whole workspace with it, so teardown routinely races the run's own
        shell.
        """
        try:
            return self.client.call(method, params)
        except HerdrCallError as exc:
            if exc.code in ("pane_not_found", "workspace_not_found"):
                return {}
            raise

    # -- typing ----------------------------------------------------------

    def send_text(self, worker, text):
        return self.client.call("pane.send_text",
                                {"pane_id": worker.id, "text": text})

    def send_keys(self, worker, keys):
        return self.client.call("pane.send_keys",
                                {"pane_id": worker.id, "keys": list(keys)})

    def send_line(self, worker, text):
        """Type a command and run it. Two calls, deliberately.

        `send_text` with a trailing newline is not the same thing: TUIs bind
        enter, and the pair is what was verified end to end against a real shell.
        """
        self.send_text(worker, text)
        self.send_keys(worker, ["enter"])

    def send_tui_line(self, worker, text, enters=1, settle=None, confirm=None,
                      log_path=None):
        settle = TUI_SETTLE_SECONDS if settle is None else settle
        confirm = TUI_SUBMIT_SECONDS if confirm is None else confirm
        self.send_text(worker, text)
        needle = text.strip()[-40:]
        deadline = time.time() + settle
        while time.time() < deadline:
            screen = self.read_screen(worker)
            if needle and needle in screen:
                break
            time.sleep(0.1)
        before = self.read_screen(worker)
        for index in range(max(1, enters)):
            if index:
                # A slash-command popup closes on the first enter and the box
                # redraws; a second enter arriving inside that redraw goes
                # nowhere.
                time.sleep(TUI_ENTER_BEAT_SECONDS)
            self.send_keys(worker, ["enter"])
        if not confirm:
            # The caller has a better receipt than a screen delta; an exit
            # command's is the shell coming back, which it checks for itself.
            return True
        if self.submission_landed(worker, before, confirm):
            return True
        # The TUI ate it. One more enter, said out loud: a brief that never
        # submitted is indistinguishable from a worker that is thinking.
        if log_path is not None:
            append_status(log_path,
                          f"SUBMIT-RETRY {utc_now()} the input box did not clear; "
                          "pressing enter again")
        self.send_keys(worker, ["enter"])
        return self.submission_landed(worker, before, confirm)

    def submission_landed(self, worker, before, seconds):
        """Did the screen move after enter? That is what submitted looks like."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.read_screen(worker) != before:
                return True
            time.sleep(0.1)
        return False

    # -- reading ---------------------------------------------------------

    def read_screen(self, worker, source="recent_unwrapped", lines=None):
        """Screen text. `recent_unwrapped` because `recent` hard-wraps at pane width.

        `lines` is accepted but not passed by default: `--lines` was seen
        returning nothing where the bare read worked, so nothing depends on it.
        """
        params = {"pane_id": worker.id, "source": source, "format": "text",
                  "strip_ansi": True}
        if lines:
            params["lines"] = int(lines)
        try:
            result = self.client.call("pane.read", params)
        except HerdrError:
            return ""
        return (result.get("read") or {}).get("text", "")

    def screen_since_spawn(self, worker, run_id=""):
        """The pane's screen since this spawn.

        `verify_environment` types its probe immediately before every spawn and
        every respawn, so the echo it leaves is exactly where this attempt's
        output starts.
        """
        screen = self.read_screen(worker)
        token = rc_token(run_id) if run_id else ""
        since = anchor_after(screen, f"{ENV_MARKER}-{token}=") if token else None
        return screen if since is None else since

    def version(self):
        return self.client.check_protocol()

    def agent_name(self, run_id):
        return agent_name(run_id)

    def raw_process_info(self, worker):
        """herdr's own `pane.process_info` body, or None once the pane is gone."""
        try:
            result = self.client.call("pane.process_info", {"pane_id": worker.id})
        except HerdrCallError as exc:
            if exc.code == "pane_not_found":
                return None
            raise
        return fill_pane_processes(result.get("process_info") or {})

    def process_info(self, worker):
        info = self.raw_process_info(worker)
        if info is None:
            return None
        return WorkerProcess(shell_pid=pane_shell_pid(info),
                             at_prompt=pane_at_prompt(info),
                             child_pids=tuple(pane_worker_pids(info)),
                             group_id=info.get("foreground_process_group_id"))

    def cpu_percent(self, pids):
        return pids_cpu_percent(pids)

    # -- detection -------------------------------------------------------

    def agent_info(self, worker):
        """herdr's own view of an agent, or None when it has no opinion.

        The target is the agent's NAME. herdr answers `agent_not_found` for a
        pane id here even though `pane.*` takes one.
        """
        if not worker.agent:
            return None
        try:
            result = self.client.call("agent.get", {"target": worker.agent})
        except HerdrCallError:
            return None
        return result.get("agent") or {}

    def status(self, worker):
        info = self.agent_info(worker)
        if info is None:
            return WorkerStatus(tracked=False)
        return WorkerStatus(tracked=True,
                            status=info.get("agent_status") or "",
                            ready=bool(info.get("interactive_ready")),
                            seq=info.get("state_change_seq"))

    def explain_agent(self, worker):
        """herdr's detection reasoning: which rule fired, and why.

        The payload is free-form in the bundled schema, so the rule is read from
        `matched_rule.id`, which is where herdr actually puts it, and failing
        that a `rule` key. Deliberately nothing else: the payload also lists
        every rule that was evaluated and did not match, and a dialog dispatch
        must not touch is named there as plainly as the one that fired.
        """
        if not worker.agent:
            return {}
        try:
            result = self.client.call("agent.explain", {"target": worker.agent})
        except HerdrCallError:
            return {}
        return result.get("explain") or {}

    def screen_state(self, worker):
        explain = self.explain_agent(worker)
        state = explain.get("state") if isinstance(explain, dict) else ""
        return state if isinstance(state, str) else ""

    def blocked_rule(self, worker):
        explain = self.explain_agent(worker)
        matched = explain.get("matched_rule") if isinstance(explain, dict) else None
        if isinstance(matched, dict) and isinstance(matched.get("id"), str):
            return matched["id"]
        rule = explain.get("rule") if isinstance(explain, dict) else ""
        if isinstance(rule, str) and rule:
            return rule.split()[0]
        return ""

    # -- the agent channel -----------------------------------------------

    def deliver_prompt(self, worker, text):
        """herdr's liveness-gated write into a detected agent.

        Called without `wait`: with it, the call does not return until the turn
        ends, which blocks a background launch for minutes, blocks a steer behind
        the turn it is steering, and spends a deadline before the watcher starts.
        Detecting the end of a turn is the runner's job and it does it from the
        pane.
        """
        self.client.call("agent.prompt", {"target": worker.agent, "text": text},
                         timeout=PROMPT_TIMEOUT)
        return "agent.prompt"

    def report_state(self, worker, state, message="", seq=None):
        """Authoritative state for herdr's UI: this source overrides detection."""
        if state not in ("idle", "working", "blocked", "unknown"):
            raise HerdrError(f"not a herdr agent state: {state!r}")
        params = {"pane_id": worker.id, "source": SOURCE, "agent": worker.agent,
                  "state": state}
        if message:
            params["message"] = message
        if seq is not None:
            params["seq"] = int(seq)
        return self.client.call("pane.report_agent", params)

    def report_session(self, worker, session_id, session_path="", seq=None):
        params = {"pane_id": worker.id, "source": SOURCE, "agent": worker.agent,
                  "agent_session_id": session_id}
        if session_path:
            params["agent_session_path"] = session_path
        if seq is not None:
            params["seq"] = int(seq)
        return self.client.call("pane.report_agent_session", params)

    # -- spawning --------------------------------------------------------

    def start_agent(self, worker, name, kind, args=(), timeout_ms=None):
        """herdr's own spawn: types the kind's command and confirms its TUI."""
        params = {"pane_id": worker.id, "name": name, "kind": kind,
                  "args": [str(a) for a in args]}
        params["timeout_ms"] = int(timeout_ms or AGENT_START_TIMEOUT_MS)
        # herdr blocks until it detects the TUI, so the socket call has to
        # outlive its own detection window rather than time out underneath it.
        wait = (params["timeout_ms"] / 1000.0) + CALL_TIMEOUT
        return self.client.call("agent.start", params, timeout=wait)

    def start_worker(self, worker, driver, argv):
        """`agent start` first, raw primitives on failure, and a loud flag.

        `agent.start` runs the kind's own command and appends `args`, so the
        lane's flags have to go in as args or herdr launches a bare CLI with
        none of the model, effort, or sandbox this lane means.
        """
        kind = driver.agent_kind
        agent_args = list(argv[1:])
        spawn = SpawnResult(method="agent.start", argv=list(argv))
        try:
            self.start_agent(worker, name=worker.agent, kind=kind,
                             args=agent_args)
            # herdr only returns once it has recognised the TUI, so the process
            # exists by now and this returns on its first look. It is here so
            # that both spawn paths answer "is there a worker yet" the same way.
            self.wait_for_child(worker)
            return spawn
        except HerdrError as exc:
            error = getattr(exc, "message", "") or str(exc)
            spawn.error = f"{getattr(exc, 'code', 'error')}: {error}"
            # A lost answer is not a lost spawn: the CLI may be up already, and
            # typing the lane command then submits it as that CLI's own input.
            # Only a pane back at its shell prompt can be typed into.
            info = self.process_info(worker)
            if info is not None and info.child_pids:
                spawn.flag = UNCONFIRMED_FLAG.format(pane=worker.id,
                                                     error=spawn.error)
                return spawn
            if info is None or not info.at_prompt:
                raise
            spawn.method = "send-text"
            spawn.flag = FALLBACK_FLAG.format(kind=kind, pane=worker.id,
                                              error=spawn.error)
        # Raw primitives. If these fail too there is no substrate left, so that
        # error propagates.
        self.send_line(worker, PANE_SHELL.command_line(driver.shell_prefix, argv))
        # herdr confirms a TUI before `agent.start` returns; nothing confirms a
        # typed command, so this path has to wait for the fork itself.
        self.wait_for_child(worker)
        return spawn

    def wait_for_child(self, worker, seconds=None):
        """Block until the pane runs something other than its shell.

        Typing a command is not starting one. A cold CLI can take seconds to
        fork, and until it does the pane looks exactly like an idle shell, which
        is also what a finished worker looks like. Prompting into that window
        types the brief at a shell prompt, and polling it reads the run as
        already over.
        """
        seconds = CHILD_APPEAR_SECONDS if seconds is None else seconds
        deadline = time.time() + seconds
        while time.time() < deadline:
            info = self.process_info(worker)
            if info is None:
                raise HerdrError(f"pane {worker.id} died before its worker started")
            if info.child_pids:
                return info
            time.sleep(0.25)
        raise HerdrError(
            f"pane {worker.id}: nothing started in {seconds}s after the lane "
            "command was typed, so there is no worker to brief")

    def wait_for_shell(self, worker, seconds=None):
        """Block until the pane has a shell that owns its own foreground.

        A pane is not typed into the instant it is created: the shell has to
        exist first, and anything herdr or the profile started in a group of its
        own has to finish, or the command lands in another program's stdin.
        """
        seconds = SHELL_SETTLE_SECONDS if seconds is None else seconds
        deadline = time.time() + seconds
        while time.time() < deadline:
            info = self.process_info(worker)
            if info is None:
                raise WorkerSetupError(
                    f"pane {worker.id} died before its shell reached a prompt")
            if info.at_prompt:
                return info
            time.sleep(0.25)
        raise WorkerSetupError(
            f"pane {worker.id} never reached a shell prompt in {seconds}s")

    # -- environment -----------------------------------------------------

    def verify_environment(self, worker, assignments, depth_var, depth,
                           blank_keys=(), log_path=None, run_id=""):
        """Re-assert the run's environment in the pane, then read it back.

        `workspace.create --env` does reach the pane's shell, but the shell then
        sources the user's rc files, and an `export` or an `unset` in one wins on
        the way past. Neither the depth ladder nor metered key blanking is a
        per-run preference, so both are re-asserted here.

        The read-back is the point. Exporting and hoping would leave the same
        silence as before, so the shell is asked what it actually has, and a pane
        whose ladder marker is wrong never gets a CLI started in it.
        """
        depth = str(depth)
        token = rc_token(run_id or worker.id)
        live_keys = ""
        for attempt in range(1, ENV_ASSERT_ATTEMPTS + 1):
            self.send_line(worker, PANE_SHELL.export_line(assignments))
            # One probe line for both answers: the ladder rung, then a marker per
            # metered key that came back non-empty, so a lane whose keys are all
            # blank reads as `<depth>:`. Re-typing the same probe on a retry
            # self-anchors, so a stale echo cannot answer for this attempt.
            probe = PANE_SHELL.env_probe_line(ENV_MARKER, token, depth_var,
                                              blank_keys)
            self.send_line(worker, probe)
            seen = None
            deadline = time.time() + PROBE_SECONDS
            while time.time() < deadline:
                seen = parse_env_echo(self.read_screen(worker), token, after=probe)
                if seen is not None:
                    break
                time.sleep(0.25)
            reported, _, live_keys = (seen or "").partition(":")
            if reported == depth:
                break
            # The export and the probe travel as separate sends, and a daemon
            # busy with a burst of same-second launches can drop the first while
            # landing the second. Re-asserting is free; the guard still refuses
            # if the pane never takes it.
            if attempt < ENV_ASSERT_ATTEMPTS:
                if log_path is not None:
                    append_status(log_path,
                                  f"RETRY {utc_now()} env assert #{attempt}: pane "
                                  f"reports {depth_var}={reported!r}, re-asserting")
                continue
            raise HerdrError(
                f"{run_id or worker.id}: the pane reports {depth_var}={reported!r}, "
                f"not {depth!r} after {ENV_ASSERT_ATTEMPTS} attempts; refusing to "
                "start a worker that would sit at the wrong rung of the depth ladder")
        if live_keys:
            raise HerdrError(
                f"{run_id or worker.id}: a metered key is still set in the pane "
                "after being blanked, so this subscription lane would bill per "
                "token; something in the shell's startup re-exports it")
        if log_path is not None:
            append_status(log_path,
                          f"ENV {utc_now()} {depth_var}={depth} verified"
                          + (", keys blank" if blank_keys else ""))
        return depth

    def read_exit_code(self, worker, run_id="", log_path=None):
        """Read the worker's real exit code back out of its own shell.

        herdr's panes carry no exit status at all, so the shell is asked for it
        in its own dialect and the echoed marker is parsed off the screen. The
        typed line cannot be mistaken for the answer because it still reads
        `=$...` where the answer reads `=<n>`.
        """
        token = rc_token(run_id or worker.id)
        probe = PANE_SHELL.rc_probe_line(RC_MARKER, token)
        if log_path is not None:
            append_status(log_path, f"RC-PROBE {utc_now()} reading the status at the prompt")
        try:
            self.send_line(worker, probe)
        except HerdrError as exc:
            if log_path is not None:
                append_status(log_path, f"RC-PROBE-FAILED {utc_now()} {exc}")
            return None
        deadline = time.time() + PROBE_SECONDS
        while time.time() < deadline:
            rc = parse_rc_echo(self.read_screen(worker), token, after=probe)
            if rc is not None:
                return rc
            time.sleep(0.25)
        if log_path is not None:
            append_status(log_path, f"RC-PROBE-TIMEOUT {utc_now()}")
        return None

    # -- killing ---------------------------------------------------------

    def kill_worker_tree(self, worker):
        info = self.raw_process_info(worker)
        if info is None:
            return []
        children = pane_worker_pids(info)
        if not children:
            return []
        signalled = list(children)
        # Descendants first, while their parents are still alive to name them.
        strays = [pid for pid in descendant_pids(children) if pid not in signalled]
        group = info.get("foreground_process_group_id")
        if not (group and group != pane_shell_pid(info)
                and processes.stop_process_group(group)):
            for pid in children:
                processes.stop_pid(pid)
        for pid in strays:
            processes.stop_pid(pid)
        return signalled + strays

    # -- presence --------------------------------------------------------

    def focus(self, worker):
        if not worker.group:
            return {}
        return self._tolerate_absence("workspace.focus",
                                      {"workspace_id": worker.group})

    def attach(self, worker):
        try:
            return subprocess.run(list(ATTACH_ARGV), check=False).returncode
        except OSError as exc:
            raise DispatchError(
                f"could not attach with {' '.join(ATTACH_ARGV)}: {exc}") from exc
