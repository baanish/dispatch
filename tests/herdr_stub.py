"""A stub herdr daemon, and the test case every suite here builds on.

The stub speaks the shapes recorded off a live daemon
(`herdr-protocol-19.json`), with the same framing that bites real clients: one
request per connection, then the server hangs up.

It models enough of a pane to be worth trusting: a shell, a worker that occupies
the foreground for a few polls, a screen that accumulates what was typed, and an
exit code the shell will report when asked. When a worker is prompted, the stub
writes the reply to the output path named in that prompt, which is exactly what
the real instruction tells a real worker to do. That is what lets the caps,
abort, and schema tests run against a socket instead of a mock.

No vendor CLI is ever launched. The stub models what a real one does: takes the
foreground, finishes its turn, reports done, and waits to be told to exit.
"""

import contextlib
import json
import os
import re
import signal
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatch import caps, cli, config, drivers, errors, lanes, policy  # noqa: E402
from dispatch import processes, prompt, records, runner  # noqa: E402
from dispatch.substrates import herdr  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
# A cap that only holds inside one process is not a cap, so a few cases launch
# dispatch as a real subprocess. `-m` rather than an installed console script,
# so the suite tests this checkout.
DISPATCH_ARGV = [sys.executable, "-m", "dispatch.cli"]
RECORDINGS = json.loads(
    (TESTS_DIR / "herdr-protocol-19.json").read_text(encoding="utf-8"))
RESULTS = RECORDINGS["results"]
ERRORS = RECORDINGS["errors"]

METERED_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN", "XAI_API_KEY", "GROK_API_KEY")

IS_WINDOWS = processes.IS_WINDOWS

# What dispatch types at the codex trust dialog, so the stub can recognise
# its own dialog being answered.
CODEX_TRUST_ANSWER = drivers.get_driver("codex").dialog_rules.answer_text


def launch_argv(lane, opts, session_id=""):
    """What this lane types to start a worker, without naming its driver twice."""
    return drivers.driver_for_lane(lane).launch_argv(lane, opts, session_id)


def resume_argv(lane, opts, session_id):
    """What this lane types to reopen a session."""
    return drivers.driver_for_lane(lane).resume_argv(lane, opts, session_id)

# Paths the injected prompt names for the worker's deliverable. Both separators,
# because on Windows the runs dir is `C:\Users\...\out.md` and a POSIX-only
# pattern would leave every stub worker writing nothing.
OUT_PATH_RE = re.compile(r"((?:[A-Za-z]:)?[\\/]\S+[\\/]out\.(?:md|json))")

# claude's first-visit trust dialog, verbatim off the live probe: the screen
# dispatch matches on, so the test suite reads the same words the CLI draws.
CLAUDE_TRUST_SCREEN = """\
 Accessing workspace:
 /private/tmp/dispatch-trust-probe-c3d4
 Quick safety check: Is this a project you created or one you trust? ...
 Claude Code'll be able to read, edit, and execute files here.
 ❯ 1. Yes, I trust this folder
   2. No, exit
 Enter to confirm · Esc to cancel
"""


# What each CLI leaves on the pane when it updates itself at launch and quits.
# The codex one is verbatim off the run this defect ate;
# the other two are their own updaters' strings, from the installed binaries.
CODEX_UPDATE_SCREEN = """\

Updating Codex via `npm install -g @openai/codex`...

changed 2 packages in 2s

\U0001f389 Update ran successfully! Please restart Codex.
"""

GROK_UPDATE_SCREEN = """\

Updating Grok 1.0.6
grok v1.0.6 installed successfully!
  Please restart Grok.
"""

CLAUDE_UPDATE_SCREEN = "\n\u2713 Update installed \u00b7 Restart to apply\n"


class StubPane:
    """One pane: a shell, maybe an interactive worker holding it, and a screen.

    The worker models what the real-CLI smoke found rather than what the brief
    assumed: a CLI takes the foreground, finishes its turn, reports `done`, and
    then sits there. It gives the pane back only when someone types its exit
    command, which is the driver's job.
    """

    def __init__(self, pane_id, workspace_id, shell_pid):
        self.pane_id = pane_id
        self.workspace_id = workspace_id
        self.shell_pid = shell_pid
        self.running = False     # a CLI owns the foreground
        self.turn_polls = 0      # polls left before the current turn is `done`
        self.status = ""         # herdr's agent_status for this pane
        self.screen = ""
        self.rc = 0
        self.session_id = ""     # printed on the way out, as the real CLIs do
        self.pending_text = ""
        self.agent = ""
        self.kind = ""
        self.pending_polls = 0     # polls left before a typed command forks
        self.agent_name = ""       # what herdr knows this pane's agent by
        self.startup_queue = []    # statuses agent.get serves while coming up
        self.blocked = False       # a dialog is waiting for an answer
        self.blocked_lead = 0      # looks the screen shows it before the status does
        self.trust_screen = ""     # the dialog's own text, drawn while it stands
        self.hand_polls = 0        # looks left before a human answers this one
        self.blocked_lag = 0       # polls still reporting blocked after an answer
        self.swallowed_enter = False
        self.update_exit = False   # this launch only ran the CLI's own updater
        self.exit_polls = 0        # looks left before the worker gives the pane back
        self.state_seq = 0         # herdr's state_change_seq: bumped on each change
        self.last_status = ""
        self.env = {}
        self.closed = False


class WindowsPipeServer:
    """The accept loop the stdlib does not have: a threaded named pipe server.

    `socketserver` has no named pipe transport, so this is the minimum that
    behaves like `ThreadingUnixStreamServer` for the daemon's framing: one
    instance per client, a thread per connection, and a hangup after the
    response. Instances are unlimited because a run's detached watcher is a
    second process talking to the same endpoint at the same time as the caller.

    Shutdown is a self-connect: the accept thread is parked inside
    ConnectNamedPipe, and opening the pipe as a client is what wakes it to see
    the flag. Cancelling the I/O instead would mean a second ctypes surface for
    the same result.
    """

    PIPE_ACCESS_DUPLEX = 0x00000003
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_UNLIMITED_INSTANCES = 255
    ERROR_PIPE_CONNECTED = 535
    BUFFER = 65536
    # One acceptor can only hand off one connection at a time, and everyone who
    # arrives mid-handoff is told ERROR_PIPE_BUSY and has to wait. A handful of
    # them keeps that rare under the suite's own concurrency, where a run's
    # caller and its detached watcher poll the same stub at once.
    ACCEPTORS = 4

    def __init__(self, name, handler):
        import ctypes
        from ctypes import wintypes

        self.name = name
        self.handler = handler
        self.kernel32 = ctypes.windll.kernel32
        self.kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        self.stopping = threading.Event()
        self.threads = [threading.Thread(target=self._accept_forever, daemon=True)
                        for _ in range(self.ACCEPTORS)]
        for thread in self.threads:
            thread.start()
        self.thread = self.threads[0]

    def _create_instance(self):
        import ctypes

        handle = self.kernel32.CreateNamedPipeW(
            self.name, self.PIPE_ACCESS_DUPLEX,
            self.PIPE_TYPE_BYTE | self.PIPE_READMODE_BYTE | self.PIPE_WAIT,
            self.PIPE_UNLIMITED_INSTANCES, self.BUFFER, self.BUFFER, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(f"CreateNamedPipeW({self.name}) failed: "
                          f"{ctypes.GetLastError()}")
        return handle

    def _accept_forever(self):
        import ctypes

        # The replacement instance is created before the connected one is served,
        # never after: a pipe name with zero instances does not exist at all, and
        # a client that looks into that window is told ERROR_FILE_NOT_FOUND, which
        # is indistinguishable from no daemon. Overlapping them means the worst a
        # client ever sees is ERROR_PIPE_BUSY, which it waits out.
        handle = self._create_instance()
        while not self.stopping.is_set():
            # A client that arrived before ConnectNamedPipe is already connected,
            # and Windows says so by failing with ERROR_PIPE_CONNECTED. It has to
            # be read with GetLastError, not ctypes' own `get_last_error`, which
            # stays 0 unless the library was loaded with `use_last_error`: reading
            # the wrong one hangs up on every client that wins that race.
            connected = self.kernel32.ConnectNamedPipe(handle, None)
            if not connected and ctypes.GetLastError() != self.ERROR_PIPE_CONNECTED:
                self.kernel32.CloseHandle(handle)
                handle = self._create_instance()
                continue
            if self.stopping.is_set():
                self.kernel32.CloseHandle(handle)
                return
            serving, handle = handle, self._create_instance()
            threading.Thread(target=self._serve_one, args=(serving,),
                             daemon=True).start()
        self.kernel32.CloseHandle(handle)

    def _serve_one(self, handle):
        import ctypes
        from ctypes import wintypes

        try:
            data = bytearray()
            while not data.endswith(b"\n"):
                chunk = ctypes.create_string_buffer(self.BUFFER)
                read = wintypes.DWORD(0)
                if not self.kernel32.ReadFile(handle, chunk, self.BUFFER,
                                              ctypes.byref(read), None):
                    return
                if not read.value:
                    return
                data += chunk.raw[:read.value]
            response = self.handler(bytes(data))
            written = wintypes.DWORD(0)
            self.kernel32.WriteFile(handle, response, len(response),
                                    ctypes.byref(written), None)
            self.kernel32.FlushFileBuffers(handle)
        finally:
            self.kernel32.DisconnectNamedPipe(handle)
            self.kernel32.CloseHandle(handle)

    def stop(self):
        self.stopping.set()
        for thread in self.threads:
            # One self-connect per acceptor: each is parked in its own
            # ConnectNamedPipe and only its own client wakes it.
            with contextlib.suppress(OSError):
                open(self.name, "r+b", buffering=0).close()
            thread.join(timeout=5)


class StubHerdr:
    """A herdr daemon on a temp endpoint, speaking recorded protocol 19.

    The endpoint is whichever one the driver reaches for on this platform: a unix
    socket on POSIX, a named pipe on Windows. Serving the pipe rather than
    falling back to loopback TCP is what keeps the suite exercising the real
    client transport instead of a third one that only the tests ever use, and it
    costs one ctypes accept loop (WindowsPipeServer) because the endpoint name is
    derived from `self.path` exactly as the driver derives it.
    """

    def __init__(self, directory, protocol=None):
        self.path = str(Path(directory) / "herdr.sock")
        self.protocol = herdr.PROTOCOL if protocol is None else protocol
        self.calls = []              # (method, params) in arrival order
        self.connections = 0
        self.errors = {}             # method -> error body returned instead
        self.panes = {}              # pane_id -> StubPane
        self.closed = []             # (kind, id) teardown calls
        self.reports = []            # pane.report_agent params
        self.starts = []             # agent.start params
        self.prompts = []            # text handed to a worker, in order
        self.swallow_prompts = 0     # successful writes the TUI never accepts
        self.workspaces = []         # workspace.create params
        self.focused = []            # workspace.focus ids, in order

        self.reply = "final message"  # what a prompted worker writes
        self.replies = None           # [(rc, text)] consumed in order
        self.rc = 0
        self.worker_polls = 2         # how long a worker holds the foreground
        self.hold = 0.0               # seconds a prompt "runs", for concurrency
        self.write_output = True      # False models a worker that wrote nothing
        self.self_exits = False       # True models a worker that obeys "then exit"
        self.agent_comes_alive = True  # False models the hung-before-TUI codex
        self.session_id = "11111111-2222-3333-4444-555555555555"
        # An interactive shell sources the user's rc files after the workspace
        # env is applied, and a real zshrc can export a key or drop the ladder
        # marker on the way past.
        self.rc_clobbers_env = False
        # herdr's own rule for an agent name, and how long a typed command takes
        # to actually fork: a cold CLI is not instant.
        self.enforce_agent_names = True
        self.child_delay_polls = 0
        self.prompts_while_starting = 0
        # A CLI that self-updates at launch: it comes up, prints its updater's
        # banner, is reported ready, and is gone by the time anyone looks again,
        # so the brief herdr accepts for it goes nowhere. `update_exits` is how
        # many consecutive launches do that, which is what makes the respawn cap
        # testable; `update_banner` is what they leave on the screen, and an
        # empty one models the same early exit with nothing to explain it.
        self.update_exits = 0
        self.update_banner = CODEX_UPDATE_SCREEN
        # codex's directory-trust dialog: idle at half a second, blocked three
        # seconds later, then a startup turn once it is answered.
        self.trust_dialog = False
        # Claude Code's first-visit trust dialog. dispatch never types at this
        # one, so nothing in the stub clears it either until a test plays the
        # hand: `hand_polls` is how many looks it stands before he answers.
        self.hand_dialog = False
        self.hand_polls = 0
        # The one claude form dispatch answers itself: the first-visit trust
        # dialog, recognised by its screen. herdr reports it under the same
        # generic rule as every other claude form, so the stub reports the same
        # rule and only the screen tells them apart. `trust_dialog_sticks`
        # models the dialog that will not clear no matter what is typed at it.
        self.claude_trust_dialog = False
        self.trust_dialog_sticks = False
        # How many looks the dialog is on screen (and matched by `agent explain`)
        # before `agent get` catches up and says `blocked`, which is the lag the
        # live probe measured at ~3.7 seconds.
        self.trust_lead_polls = 0
        # What herdr's `agent explain` names as the rule holding a blocked
        # agent. The trust dialog is the one dispatch answers itself, so a test
        # that wants a dialog nobody will answer says so here.
        self.blocked_rule = drivers.get_driver("codex").dialog_rules.answered_rules[0]
        self.trust_answers = []
        self.trust_lag_polls = 0      # blocked still reported after the answer
        self.composer_messages = []   # typed at a TUI with no dialog to catch it
        # A session resumed mid-turn picks that turn straight back up, so the
        # CLI is working the moment it comes up: this many looks of `working`
        # before it settles. 0 is a session that was finished when it was left.
        self.resume_turn_polls = 0
        self.ready_status = "idle"    # what a settled TUI reports
        self.never_reports_done = False  # a ghost prompt box: idle, never done
        self.status_when_prompted = ""   # status a refused prompt leaves behind
        self.prompts_while_blocked = 0
        # herdr evicts the named agent when anyone reports state for the pane,
        # and a TUI input box can eat the enter that follows its text.
        self.evicted = []
        self.tui_swallows_first_enter = False
        self.swallow_enters = 0    # a CLI that ignores this many enters outright
        self.env_reports = None       # override what `echo $AGENT_DEPTH` prints
        self.eats_exports = 0         # export lines a busy daemon drops outright
        self.vanish_next_creates = 0  # panes gone the instant they are created
        self.in_flight = 0
        self.max_in_flight = 0
        self.exits_mid_turn = 0     # exit commands that landed on a live turn
        # No daemon behind the endpoint: every call comes back with herdr's own
        # `server_not_running` body, which is what a client attached to a dead
        # daemon's address gets. A test clears it from its fake spawn to model
        # the daemon coming back up.
        self.server_down = False

        self._lock = threading.Lock()
        self._next_pane = 0
        self._next_pid = 500000
        self._serve()

    # -- server plumbing -------------------------------------------------

    def _serve(self):
        if IS_WINDOWS:
            self.server = WindowsPipeServer(herdr.pipe_name(self.path),
                                            self._handle)
            self.thread = self.server.thread
            return
        stub = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                data = b""
                while not data.endswith(b"\n"):
                    chunk = self.request.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                self.request.sendall(stub._handle(data))
                # The real daemon hangs up here; a client that reuses the socket
                # gets a broken pipe, and the driver has to be built for that.

        class Server(socketserver.ThreadingUnixStreamServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(self.path, Handler)
        # A tight poll interval: the default half-second is what `shutdown()`
        # waits for, and one stub per test made that the suite's floor.
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def _handle(self, data):
        """One request line in, one response line out. Both transports share it."""
        with self._lock:
            self.connections += 1
        response = self.respond(json.loads(data.decode("utf-8")))
        return (json.dumps(response) + "\n").encode("utf-8")

    def stop(self):
        if IS_WINDOWS:
            self.server.stop()
            return
        self.server.shutdown()
        self.server.server_close()
        with contextlib.suppress(OSError):
            os.unlink(self.path)

    # -- protocol --------------------------------------------------------

    def respond(self, request):
        method = request.get("method")
        params = request.get("params") or {}
        with self._lock:
            self.calls.append((method, params))
        if self.server_down:
            return {"id": request.get("id", ""), "error": ERRORS["server_not_running"]}
        error = self.errors.get(method)
        if error:
            if method == "agent.prompt" and self.status_when_prompted:
                # herdr timing out is not the same as the prompt not landing:
                # the real timeout came back on turns that had it and were busy.
                pane = self.agent_pane(params.get("target"))
                if pane is not None:
                    self.deliver_prompt(pane, params.get("text", ""))
                    pane.status = self.status_when_prompted
            return {"id": request.get("id", ""), "error": error}
        try:
            result = self.dispatch_method(method, params)
        except KeyError:
            return {"id": request.get("id", ""),
                    "error": {"code": "invalid_request",
                              "message": f"unknown variant `{method}`"}}
        if isinstance(result, tuple):     # (error_body,)
            return {"id": request.get("id", ""), "error": result[0]}
        return {"id": request.get("id", ""), "result": result}

    def agent_pane(self, name):
        """The pane whose agent has this name, or None: herdr's own addressing."""
        if not name:
            return None
        for pane in self.panes.values():
            if not pane.closed and pane.agent_name and pane.agent_name == name:
                return pane
        return None

    def agent_status_of(self, pane):
        """What herdr would report now, and the counter it bumps on each change.

        `state_change_seq` is real herdr: it moves only when the status does, so
        a driver can tell the `done` a brief was handed to from the `done` that
        answers it.
        """
        status = self.detect_status(pane)
        if status != pane.last_status:
            pane.last_status = status
            pane.state_seq += 1
        return status

    def detect_status(self, pane):
        """What herdr would report now: the startup script, then the turn."""
        if pane.blocked:
            if pane.blocked_lead > 0:
                # herdr's agent status lags its own screen rules: probed live,
                # claude's trust dialog is drawn and matched by `agent explain`
                # seconds before `agent get` stops answering `idle`.
                pane.blocked_lead -= 1
                return "idle"
            if pane.hand_polls:
                pane.hand_polls -= 1
                if not pane.hand_polls:
                    self.answer_by_hand(pane)
                    return self.detect_status(pane)
            return "blocked"
        if pane.blocked_lag > 0:
            # herdr catching up: the dialog is gone from the screen but its
            # status has not turned over yet.
            pane.blocked_lag -= 1
            return "blocked"
        if pane.startup_queue:
            status = pane.startup_queue.pop(0)
            if status == "blocked":
                pane.blocked = True
            return status
        status = pane.status or "unknown"
        if status == "idle" and self.ready_status != "idle":
            return self.ready_status
        if status == "done" and self.never_reports_done:
            # Claude Code's ghost prompt suggestion: herdr's live_prompt_box
            # rule reads the box as live, so it reports idle forever.
            return "idle"
        return status

    def answer_by_hand(self, pane):
        """A human clearing the driver's trust dialog, by hand.

        The only thing that ever clears this one: dispatch types nothing at it,
        so no send-text path can be what unblocks the stub either.
        """
        pane.blocked = False
        pane.hand_polls = 0
        pane.status = "idle"
        return pane

    def pane(self, pane_id):
        pane = self.panes.get(pane_id)
        if pane is None or pane.closed:
            return None
        return pane

    def dispatch_method(self, method, params):
        if method == "ping":
            return dict(RESULTS["ping"], protocol=self.protocol)
        if method == "workspace.create":
            return self.create_workspace(params)
        if method == "pane.split":
            return self.split(params)
        if method == "pane.list":
            return {"type": "pane_list",
                    "panes": [{"pane_id": p.pane_id, "workspace_id": p.workspace_id}
                              for p in self.panes.values() if not p.closed]}
        if method == "workspace.focus":
            self.focused.append(params.get("workspace_id"))
            return RESULTS["ok"]
        if method == "workspace.list":
            return {"type": "workspace_list", "workspaces": []}

        if method in ("pane.process_info", "pane.read", "pane.send_text",
                      "pane.send_keys", "pane.get"):
            pane = self.pane(params.get("pane_id"))
            if pane is None:
                return (ERRORS["pane_not_found"],)
            return self.pane_method(method, pane, params)

        if method == "pane.report_agent":
            self.reports.append(params)
            # Reporting state does not sit alongside herdr's detection, it
            # replaces it: the named agent is evicted and every agent.* probe
            # answers agent_not_found from here on.
            pane = self.pane(params.get("pane_id"))
            if pane is not None and pane.agent_name:
                self.evicted.append(pane.agent_name)
                pane.agent_name = ""
            return RESULTS["ok"]
        if method in ("pane.report_agent_session", "pane.release_agent"):
            return RESULTS["ok"]
        if method == "agent.start":
            return self.start_agent(params)
        if method in ("agent.get", "agent.prompt", "agent.explain"):
            # By NAME. herdr answers agent_not_found for a pane id here, even
            # though every pane.* method takes one.
            pane = self.agent_pane(params.get("target"))
            if pane is None:
                return ({"code": "agent_not_found",
                         "message": f"agent {params.get('target')} is not an "
                                    "active named agent"},)
            if method == "agent.get":
                return {"type": "agent_info",
                        "agent": {"pane_id": pane.pane_id, "name": pane.agent,
                                  "agent_status": self.agent_status_of(pane),
                                  "state_change_seq": pane.state_seq,
                                  "interactive_ready": pane.running}}
            if method == "agent.explain":
                # The live 0.8.0 payload names the rule under `matched_rule`,
                # and has no `rule` key at all: verified against a real claude
                # standing at its trust dialog.
                matched = {"id": self.blocked_rule, "priority": 950,
                           "region": "after_last_horizontal_rule",
                           "state": "blocked"} if pane.blocked else None
                return {"type": "agent_explain",
                        "explain": {"state": "blocked" if pane.blocked else "idle",
                                    "matched_rule": matched}}
            if pane.blocked:
                self.prompts_while_blocked += 1
            self.deliver_prompt(pane, params.get("text", ""))
            if self.status_when_prompted:
                pane.status = self.status_when_prompted
            return {"type": "agent_prompted", "agent": {"name": pane.agent}}
        if method == "pane.close":
            pane = self.panes.get(params.get("pane_id"))
            if pane:
                pane.closed = True
            self.closed.append(("pane", params.get("pane_id")))
            return RESULTS["ok"]
        if method == "workspace.close":
            for pane in self.panes.values():
                if pane.workspace_id == params.get("workspace_id"):
                    pane.closed = True
            self.closed.append(("workspace", params.get("workspace_id")))
            return RESULTS["ok"]
        raise KeyError(method)

    def create_workspace(self, params):
        self.workspaces.append(params)
        with self._lock:
            self._next_pane += 1
            index = self._next_pane
            self._next_pid += 1
            pid = self._next_pid
        workspace_id = f"w{index}"
        pane_id = f"{workspace_id}:p1"
        pane = StubPane(pane_id, workspace_id, pid)
        pane.session_id = self.session_id
        pane.env = dict(params.get("env") or {})
        if self.rc_clobbers_env:
            pane.env.pop("AGENT_DEPTH", None)
            pane.env["OPENAI_API_KEY"] = "sk-from-the-users-zshrc"
        if self.vanish_next_creates > 0:
            # The concurrent-launch race: workspace.create answers with a pane
            # that is already gone by the first pane.* call that names it.
            self.vanish_next_creates -= 1
            pane.closed = True
        self.panes[pane_id] = pane
        return {"type": "workspace_created",
                "workspace": {"workspace_id": workspace_id, "label": params.get("label")},
                "tab": {"tab_id": f"{workspace_id}:t1"},
                "root_pane": {"pane_id": pane_id, "workspace_id": workspace_id,
                              "cwd": params.get("cwd")}}

    def split(self, params):
        parent = self.pane(params.get("target_pane_id"))
        workspace_id = params.get("workspace_id") or (parent.workspace_id if parent else "w1")
        with self._lock:
            self._next_pane += 1
            self._next_pid += 1
            pane_id = f"{workspace_id}:p{self._next_pane}"
            pid = self._next_pid
        self.panes[pane_id] = StubPane(pane_id, workspace_id, pid)
        return {"type": "pane_info",
                "pane": {"pane_id": pane_id, "workspace_id": workspace_id}}

    def pane_method(self, method, pane, params):
        if method == "pane.process_info":
            return {"type": "pane_process_info", "process_info": self.process_info(pane)}
        if method == "pane.read":
            # A dialog is on the screen only while it stands: a real pane read
            # returns what is drawn now, and "the dialog left the screen" is how
            # dispatch tells an answer that landed from one that did not.
            text = pane.screen + (pane.trust_screen if pane.blocked else "")
            return {"type": "pane_read",
                    "read": dict(RESULTS["pane.read"]["read"], text=text)}
        if method == "pane.send_text":
            pane.pending_text = params.get("text", "")
            return RESULTS["ok"]
        if method == "pane.send_keys":
            if "enter" in params.get("keys", ()):
                return self.enter(pane)
            return RESULTS["ok"]
        if method == "pane.get":
            return {"type": "pane_info",
                    "pane": {"pane_id": pane.pane_id, "agent": pane.agent or None}}
        raise KeyError(method)

    def process_info(self, pane):
        """At the prompt the shell owns the foreground group; a worker gets its own."""
        if pane.pending_polls > 0:
            # Typed, not yet forked: indistinguishable from an idle shell.
            pane.pending_polls -= 1
            if pane.pending_polls == 0 and self.agent_comes_alive:
                pane.running = True
        if pane.running and pane.exit_polls > 0:
            pane.exit_polls -= 1
            if pane.exit_polls == 0:
                # A launch that only ran the CLI's own updater: it gives the
                # pane back by itself, the way any finished process does.
                pane.running = False
                pane.status = "unknown"
        if pane.running:
            if pane.turn_polls > 0:
                pane.turn_polls -= 1
                if pane.turn_polls == 0:
                    pane.status = "done"     # herdr says `done`, never `idle`
            return {"pane_id": pane.pane_id, "shell_pid": pane.shell_pid,
                    "foreground_process_group_id": pane.shell_pid + 1,
                    "foreground_processes": [
                        {"pid": pane.shell_pid + 1, "name": "worker",
                         "argv0": "worker", "argv": ["worker"], "cmdline": "worker"}]}
        return {"pane_id": pane.pane_id, "shell_pid": pane.shell_pid,
                "foreground_process_group_id": pane.shell_pid,
                "foreground_processes": [
                    {"pid": pane.shell_pid, "name": "zsh", "argv0": "zsh",
                     "argv": ["-zsh"], "cmdline": "-zsh"}]}

    def start_agent(self, params):
        pane = self.pane(params.get("pane_id"))
        if pane is None:
            return (ERRORS["pane_not_found"],)
        name = params.get("name") or ""
        if self.enforce_agent_names and not herdr.AGENT_NAME_RE.match(name):
            return ({"code": "invalid_agent_name",
                     "message": "agent name must start with a lowercase letter and "
                                "contain only lowercase letters, digits, '-' or '_' "
                                "(1-32 characters)"},)
        self.starts.append(params)
        argv = [params.get("kind")] + [str(a) for a in params.get("args", ())]
        pane.agent = params.get("name", "")
        pane.agent_name = params.get("name", "")
        pane.kind = params.get("kind", "")
        blocking = self.trust_dialog or self.hand_dialog or self.claude_trust_dialog
        pane.startup_queue = ["idle", "blocked"] if blocking else \
            ["working"] * self.resume_turn_polls
        pane.hand_polls = self.hand_polls if self.hand_dialog else 0
        if self.hand_dialog or self.claude_trust_dialog:
            self.blocked_rule = drivers.get_driver("claude").dialog_rules.handback_rules[0]
        pane.trust_screen = CLAUDE_TRUST_SCREEN if self.claude_trust_dialog else ""
        pane.blocked_lead = self.trust_lead_polls if self.claude_trust_dialog else 0
        pane.blocked = False
        pane.screen += " ".join(argv) + "\n"   # herdr types the command itself
        pane.pending_polls = self.child_delay_polls
        pane.running = self.agent_comes_alive and not pane.pending_polls
        pane.status = "idle" if self.agent_comes_alive else "unknown"
        pane.update_exit = self.update_exits > 0
        if pane.update_exit:
            self.update_exits -= 1
            pane.screen += self.update_banner
        return {"type": "agent_started", "agent": {"name": pane.agent}, "argv": argv}

    EXIT_COMMANDS = {drivers.get_driver(name).exit_command[0]
                     for name in drivers.driver_names()}

    def enter(self, pane):
        """Run whatever was typed, the way a shell (or a TUI) would.

        codex swallows the first enter after a slash command: its autocomplete
        popup takes it, leaving the command typed but unsent. A driver that sends
        one enter to a codex pane hangs here, which is the point.
        """
        # Only a running CLI can eat an enter: the env exports and the rc probe
        # are typed at a shell, before and after the TUI owns the pane.
        if self.swallow_enters > 0 and pane.running:
            # A TUI that is not listening: the command sits in the box.
            self.swallow_enters -= 1
            return RESULTS["ok"]
        swallow = (pane.kind == "codex" and pane.pending_text.startswith("/")) or (
            self.tui_swallows_first_enter and pane.pending_text and pane.running)
        if swallow and not pane.swallowed_enter:
            pane.swallowed_enter = True
            return RESULTS["ok"]
        text, pane.pending_text = pane.pending_text, ""
        pane.swallowed_enter = False
        pane.screen += text + "\n"
        marker = herdr.RC_MARKER
        env_marker = herdr.ENV_MARKER
        # What the pane's own shell would make of the line, in its own dialect:
        # bash or zsh on POSIX, PowerShell on Windows. The dialect answers, so
        # this simulation cannot drift from the lines dispatch actually types.
        kind, payload = herdr.PANE_SHELL.parse_line(text)
        if kind == "rc-probe":
            pane.screen += f"{marker}-{payload}={pane.rc}\n"
        elif kind == "env-probe":
            # The shell expands the rung, then a marker for each metered key
            # that is not blank.
            value = pane.env.get("AGENT_DEPTH", "")
            keys = "".join("set" for var in METERED_VARS if pane.env.get(var))
            value = f"{value}:{keys}"
            if self.env_reports is not None:
                value = self.env_reports
            pane.screen += f"{env_marker}-{payload}={value}\n"
        elif kind == "export":
            if self.eats_exports > 0:
                self.eats_exports -= 1
            else:
                for name, value in payload:
                    pane.env[name] = value
        elif pane.blocked and pane.trust_screen and not text:
            # "Enter to confirm" with option 1 preselected: the bare enter is
            # the only thing that answers claude's trust dialog, and typed text
            # would select by digit instead (below).
            self.trust_answers.append("enter")
            if not self.trust_dialog_sticks:
                pane.blocked = False
                pane.blocked_lag = self.trust_lag_polls
                # `done`, not `idle`: herdr reports a settled prompt box with
                # anything behind it as done, and a claude that has just had its
                # trust dialog answered stays there until it is prompted.
                pane.status = "done"
        elif pane.blocked:
            self.trust_answers.append(text)
            if text == drivers.get_driver("codex").dialog_rules.answer_text:
                pane.blocked = False
                # The startup turn a real codex runs once it is trusted.
                pane.startup_queue = ["idle", "working"]
            else:
                # A numbered selector: anything else picks another option, and
                # option 2 is "No, quit".
                self.exit_session(pane)
        elif text in self.EXIT_COMMANDS:
            self.exit_session(pane)
        elif pane.running and text == CODEX_TRUST_ANSWER:
            # No dialog left to catch it: codex reads it as a message.
            self.composer_messages.append(text)
            pane.status = "done"
        elif "prompt.txt" in text:
            # The fallback types a one-line reference; a real worker opens the
            # file and follows it, so the stub does too.
            reference = text.split()[-1].strip("'\"")
            with contextlib.suppress(OSError):
                self.deliver_prompt(pane, Path(reference).read_text(encoding="utf-8"))
        elif pane.running or text.startswith("Read and follow the brief") \
                or "Course correction" in text or "does not satisfy the contract" in text:
            self.deliver_prompt(pane, text)          # the typed-in fallback prompt
        elif text:
            pane.pending_polls = self.child_delay_polls
            pane.running = not pane.pending_polls
            pane.turn_polls = self.worker_polls
        return RESULTS["ok"]

    def exit_session(self, pane):
        """What a CLI does on its exit command: print the resume line and go.

        A worker still holding a turn when the exit lands is counted, because
        that is the failure a driver reading someone else's `done` produces and
        the run record cannot show it: the deliverable is already on disk and
        the run looks finished.
        """
        if pane.status == "working" or pane.turn_polls > 0:
            self.exits_mid_turn += 1
        pane.running = False
        pane.turn_polls = 0
        pane.status = "unknown"
        if pane.session_id:
            # Two shapes in the wild, both scraped from the exit screen.
            if pane.kind == "codex":
                pane.screen += f"To continue this session, run codex resume {pane.session_id}\n"
            else:
                pane.screen += (f"Resume this session with: {pane.kind or 'claude'} "
                                f"--resume {pane.session_id}\n")

    def deliver_prompt(self, pane, text):
        """A prompted worker does its work: write the deliverable, then exit.

        The output path comes out of the prompt itself, which is how a real
        worker learns it too.
        """
        self.prompts.append(text)
        if self.swallow_prompts:
            self.swallow_prompts -= 1
            return
        if pane.update_exit:
            # herdr accepted the prompt for an agent whose process had already
            # quit for its update, which is what the live occurrence recorded:
            # nothing reads it, and the shell owns the pane at the next look.
            pane.exit_polls = 1
            return
        if pane.blocked:
            self.prompts_while_blocked += 1
        if not pane.running:
            # A brief delivered before the worker forked goes nowhere.
            self.prompts_while_starting += 1
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.hold:
                time.sleep(self.hold)
            with self._lock:
                if self.replies:
                    rc, reply = self.replies.pop(0)
                else:
                    rc, reply = self.rc, self.reply
            pane.rc = rc
            pane.running = True
            pane.status = "working"
            pane.turn_polls = self.worker_polls
            if self.self_exits:
                # The bonus path: a worker that actually obeys "then exit".
                pane.running = False
                pane.status = "unknown"
            match = OUT_PATH_RE.search(text)
            if match and self.write_output:
                Path(match.group(1)).write_text(reply, encoding="utf-8")
        finally:
            with self._lock:
                self.in_flight -= 1

    def finish_turn(self, pane_id):
        """Mark a worker's turn complete, the way a real CLI would report it."""
        pane = self.panes[pane_id]
        pane.turn_polls = 0
        pane.status = "done"
        return pane

    # -- assertions helpers ----------------------------------------------

    def methods(self):
        return [m for m, _ in self.calls]

    def params_for(self, method):
        return [p for m, p in self.calls if m == method]

    def started_argv(self, index=0):
        """The full command line herdr was asked to run for a worker."""
        start = self.starts[index]
        return [start["kind"]] + [str(a) for a in start.get("args", ())]

    def typed(self):
        """Everything typed into any pane, in order."""
        return [p["text"] for p in self.params_for("pane.send_text")]


class HerdrStubTestCase(unittest.TestCase):
    """Isolated ~/.dispatch, depth 0, no metered keys, and a stub daemon."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # resolve(): macOS hands out /var/... but reports /private/var/... as cwd.
        self.root = Path(self.tmp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.brief = self.work / "brief.md"
        self.brief.write_text("do the thing\n", encoding="utf-8")

        self.env_backup = dict(os.environ)
        self.home = self.root / "dispatch-home"
        os.environ["DISPATCH_HOME"] = str(self.home)
        # codex's rollout store, isolated for the same reason DISPATCH_HOME is:
        # `capture_codex_session` reads it with no cwd filter when the record
        # carries no cwd, so a rollout a real codex wrote on this machine while
        # the suite ran handed an inspect-refusal test a session id to reopen.
        os.environ["CODEX_HOME"] = str(self.root / "codex")
        # An empty file rather than an absent one: `dispatch` reads the
        # operator's config at startup, and a suite that fell through to the
        # real `~/.config/dispatch/config.toml` would run on that machine's
        # lane table instead of the shipped one.
        empty_config = self.root / "config.toml"
        empty_config.write_text("", encoding="utf-8")
        os.environ[config.CONFIG_ENV] = str(empty_config)
        os.environ["AGENT_DEPTH"] = "0"
        # The transport self-heal spawns a real `herdr server` when it finds
        # nothing listening, and several cases point the client at a socket that
        # never existed. Tests that want the repair path patch
        # `spawn_herdr_daemon` themselves.
        os.environ[herdr.SPAWN_ENV] = "0"
        for var in METERED_VARS:
            os.environ.pop(var, None)

        self.cwd_backup = os.getcwd()
        os.chdir(self.work)

        self.stub = StubHerdr(self.root)
        os.environ[herdr.SOCKET_ENV] = self.stub.path
        # Poll fast: the suite must not pay for the production pacing.
        self.poll_backup = runner.POLL_SECONDS
        self.idle_backup = runner.OUTPUT_IDLE_SECONDS
        self.quiet_backup = runner.DELIVERABLE_QUIET_SECONDS
        self.child_backup = herdr.CHILD_APPEAR_SECONDS
        # A cold CLI gets a minute in production; the suite proves the rule.
        herdr.CHILD_APPEAR_SECONDS = 3.0
        self.ready_backup = runner.READY_IDLE_SECONDS
        # TUI typing pacing: real terminals need these, the suite does not.
        self.tui_backup = (herdr.TUI_SETTLE_SECONDS,
                           herdr.TUI_SUBMIT_SECONDS,
                           herdr.TUI_ENTER_BEAT_SECONDS)
        herdr.TUI_SETTLE_SECONDS = 0.05
        herdr.TUI_SUBMIT_SECONDS = 0.1
        herdr.TUI_ENTER_BEAT_SECONDS = 0.01
        # Production waits two seconds of settled idle; the suite proves the
        # rule, not the wall clock.
        runner.READY_IDLE_SECONDS = 0.05
        runner.POLL_SECONDS = 0.01
        # A deliverable counts as finished once it stops changing; the suite
        # proves the rule rather than the five seconds production waits.
        runner.DELIVERABLE_QUIET_SECONDS = 0.05
        # The production backstop waits two minutes of total silence before it
        # believes a worker with no deliverable is finished; the suite proves the
        # rule, not the wall clock.
        runner.OUTPUT_IDLE_SECONDS = 0.05
        self.policy_backup = policy.policy()
        self.addCleanup(self._restore)

    def _restore(self):
        # Detached watchers outlive the process that launched them by design, so
        # the suite has to take its own down or they poll a dead socket forever.
        for rec in records.all_records():
            self.stop_watcher(rec.get("watcher_pid"))
        runner.POLL_SECONDS = self.poll_backup
        runner.OUTPUT_IDLE_SECONDS = self.idle_backup
        runner.DELIVERABLE_QUIET_SECONDS = self.quiet_backup
        herdr.CHILD_APPEAR_SECONDS = self.child_backup
        runner.READY_IDLE_SECONDS = self.ready_backup
        (herdr.TUI_SETTLE_SECONDS, herdr.TUI_SUBMIT_SECONDS,
         herdr.TUI_ENTER_BEAT_SECONDS) = self.tui_backup
        policy.set_policy(self.policy_backup)
        self.stub.stop()
        for handle in list(records.HELD_LOCKS):
            records.release_run_lock(handle)
        os.chdir(self.cwd_backup)
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def stop_watcher(self, pid):
        """Stop a detached watcher this process spawned, and reap it.

        Reaping matters: an unreaped child stays a zombie, and a zombie still
        answers `kill -0`, so every liveness check about it would wait out the
        full kill grace before believing it was gone.
        """
        if not pid:
            return
        with contextlib.suppress(OSError):
            os.kill(int(pid), signal.SIGTERM)
        with contextlib.suppress(OSError):
            os.waitpid(int(pid), 0)

    def unwatched_bg_run(self, lane="opus@high"):
        """A `--bg` run stuck mid-turn whose watcher has been stopped.

        Two things at once, both deliberate: it gives the reconcile tests a run
        nobody else is driving, and stopping the watcher *is* the wrapper-death
        case the reconcile sweep exists to cover.
        """
        self.stub.worker_polls = 10_000      # the turn never completes on its own
        self.run_cli("run", lane, str(self.brief), "--bg")
        rec = records.load_record(self.only_record()["id"])
        self.stop_watcher(rec.get("watcher_pid"))
        deadline = time.time() + 30
        while time.time() < deadline and caps.run_is_watched(rec):
            time.sleep(0.05)
        self.assertFalse(caps.run_is_watched(rec), "the watcher would not die")
        return rec

    def stuck_bg_run(self, lane="opus@high"):
        """An unwatched `--bg` run whose worker is demonstrably not working.

        A deadline is a check-in rather than a kill, so a test that wants a kill
        has to build a worker there is nothing left to extend: a TUI herdr no
        longer reports as working, nothing written, and a screen that has been
        still since before the last check-in.

        The first check-in is spent here on purpose. A process that has looked
        at a pane once cannot measure how long its screen has been still, so the
        first look always comes back `working`; it is the fingerprint it leaves
        on the record that lets the next one judge.
        """
        rec = self.unwatched_bg_run(lane)
        self.stub.panes[rec["worker_id"]].status = "idle"
        with contextlib.suppress(OSError):
            (Path(rec["dir"]) / "out.md").unlink()
        rec = records.load_record(rec["id"])
        rec["deadline_seconds"] = 1
        rec["started_at"] = time.time() - 30
        records.save_record(rec)
        runner.reconcile_run(records.load_record(rec["id"]), self.substrate())
        return self.freeze_checkin_screen(rec)

    def freeze_checkin_screen(self, rec, seconds=300):
        """Backdate the screen the last check-in saw: stillness the suite cannot wait."""
        rec = records.load_record(rec["id"])
        rec["checkin_screen_since"] = time.time() - seconds
        records.save_record(rec)
        return rec

    def run_cli(self, *argv):
        """One verb, with the config this process already has.

        `cli.main` reloads the operator's config first, which would undo a
        policy or lane table the case set for itself.
        """
        return cli.run_verb(list(argv))

    def blank_metered_keys(self):
        """Turn the opt-in key blanking on for this test, and off again after.

        It ships off, so every case that exercises it says so rather than
        inheriting it from a default that is not there.
        """
        previous = policy.with_policy(blank_metered_keys=True)
        self.addCleanup(policy.set_policy, previous)

    def substrate(self):
        """A herdr substrate pointed at this test's stub daemon."""
        return herdr.HerdrSubstrate(herdr.HerdrClient(self.stub.path))

    def sweep(self):
        """What `live_records` reconciles through."""
        return runner.SubstrateSweep(self.substrate())

    def worker_of(self, rec):
        """The substrate handle a record points at."""
        return runner.record_worker(rec)

    def only_record(self):
        found = records.all_records()
        self.assertEqual(len(found), 1, found)
        return found[0]

    def records_of_kind(self, kind):
        return [r for r in records.all_records() if r.get("kind") == kind]

    def make_live_record(self, lane="sol@medium", **fields):
        """A record that reads as live the way a real run does: by a live worker."""
        rec_id = f"{lane}-{len(records.all_records()):06d}-live"
        directory = records.runs_root() / rec_id
        directory.mkdir(parents=True)
        pane = self.stub.create_workspace({"label": f"dispatch-{rec_id}"})
        rec = {"id": rec_id, "kind": "run", "dir": str(directory), "lane": lane,
               "driver": lanes.resolve_lane(lane).driver,
               "substrate": "herdr", "state": "running",
               "session": caps.session_key(),
               "worker_id": pane["root_pane"]["pane_id"],
               "worker_group": pane["workspace"]["workspace_id"],
               "agent": herdr.agent_name(rec_id),
               "started_at": time.time(),
               "reserved_at": time.time(), "created": rec_id}
        rec.update(fields)
        records.save_record(rec)
        return rec

    def capture_stdout(self, *argv):
        import contextlib as _contextlib
        import io as _io
        buffer = _io.StringIO()
        with _contextlib.redirect_stdout(buffer):
            code = self.run_cli(*argv)
        return code, buffer.getvalue()

    def capture_stderr(self, *argv):
        import contextlib as _contextlib
        import io as _io
        buffer = _io.StringIO()
        with _contextlib.redirect_stderr(buffer):
            code = self.run_cli(*argv)
        return code, buffer.getvalue()
