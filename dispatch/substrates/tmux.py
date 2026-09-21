"""The tmux substrate: one detached tmux session per run, the worker in its pane.

Every run gets a session of its own, named `dispatch-<run id>`, holding one
window with one pane. The session is created detached and dispatch never
attaches the operator to it: a live run is followed with `dispatch watch`, which
reads the pane. To look at one by hand:

    tmux attach -t dispatch-<run id>

and leave it again with the prefix key followed by `d`. `dispatch inspect` runs
that attach for the operator.

What tmux offers, and what it costs:

- The screen is `capture-pane`, so the driver's own dialog markers, its session
  id, and its update banners are all readable. Scrollback is asked for only
  where it is needed, because the visible pane is what "what is on screen right
  now" means.
- Typing is `send-keys`, with `-l` for text so that a brief containing the word
  `Enter` is typed rather than pressed.
- There is no detection engine and no agent channel: nothing in tmux tells a TUI
  from a shell. So `status` stays untracked, `blocked_rule` stays empty, and
  dispatch answers only the dialog the driver names in its own words on screen.
  Dialogs count as answerable because both halves are here (the screen to read
  and the keys to send), and never because tmux has an opinion about them.
- Panes carry no exit code, so the real one is read back out of the shell the
  way the herdr substrate reads it: type an echo of the status and parse a
  per-run marker off the screen.
- Process facts come from the pane's pid and the tty it owns. The foreground
  process group is the signal, not the process list: a shell at its prompt forks
  subshells for prompt hooks continuously, and reading those as a worker would
  mean no run ever looks finished.

Two tmux behaviours this file is written around. The server exits once its last
session is gone, so a command after the run's own teardown answers "no server
running", which is the same fact as "the worker is gone". And `display-message`
against a pane that no longer exists prints an empty line and exits 0 rather
than failing, so an empty answer is what absence looks like.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import subprocess
import time

from .. import processes
from ..processes import KILL_GRACE_SECONDS, descendant_pids, process_cpu_percent
from ..records import append_status, utc_now
from .base import (Substrate, SubstrateCapabilities, SubstrateError, SpawnResult,
                   Worker, WorkerProcess, WorkerSetupError)
from .paneshell import (ENV_MARKER, PANE_SHELL, RC_MARKER, anchor_after,
                        parse_env_echo, parse_rc_echo, rc_token)

SESSION_PREFIX = "dispatch-"
# tmux takes longer names, but a session name is typed by hand in the attach
# line, and a run id plus this prefix fits well inside it.
SESSION_NAME_MAX = 64

# A detached session is 80x24 unless it is told otherwise, and a CLI's session
# id, its trust dialog and its update banner all have to survive on a screen
# that is only ever read one pane at a time.
PANE_WIDTH = 200
PANE_HEIGHT = 50
HISTORY_LINES = 2000

# `new-session -e` landed in tmux 3.2. Older ones get the environment from a
# shell command that exports it and execs the login shell.
ENV_FLAG_VERSION = (3, 2)
VERSION_RE = re.compile(r"(\d+)\.(\d+)")

COMMAND_TIMEOUT = 20.0       # one tmux invocation
SHELL_SETTLE_SECONDS = 30    # wait for a fresh pane's shell rc files to finish
PROBE_SECONDS = 10.0         # how long an echoed marker has to appear on screen
ENV_ASSERT_ATTEMPTS = 3      # a busy pane can drop the export line; re-assert it
CHILD_APPEAR_SECONDS = 60.0  # how long a typed command has to produce a process

# Typing into a TUI is not typing into a shell. A shell reads its line
# discipline; a TUI input box has to render the text before its key handler will
# treat the next enter as a submit, and back-to-back text plus enter leaves the
# line sitting unsent. So: type, let it draw, submit, then check that it went,
# because an unsubmitted brief looks exactly like a working worker.
TUI_SETTLE_SECONDS = 0.5
TUI_SUBMIT_SECONDS = 3.0
TUI_ENTER_BEAT_SECONDS = 0.3

# The key names a driver asks for, in tmux's spelling. A closed set on purpose:
# tmux types an unrecognised key name as literal text, so a typo would put the
# word `escpe` into a worker's composer instead of failing.
KEY_NAMES = {
    "enter": "Enter", "escape": "Escape", "tab": "Tab", "space": "Space",
    "backspace": "BSpace", "up": "Up", "down": "Down", "left": "Left",
    "right": "Right",
}

# What tmux says on every command once the last session is gone, which is the
# ordinary end of a run rather than a failure.
NO_SERVER = "no server running"


class TmuxError(SubstrateError):
    """tmux substrate failure, operator-facing like every other DispatchError."""


def session_name(label):
    """The tmux session name for a run id or an inspect label.

    tmux forbids `.` and `:` in a session name and a lane string carries an `@`,
    so the label is sanitized. A label too long to keep whole keeps its tail,
    because the timestamp and random suffix are what tell two runs apart, plus a
    digest of the whole label so that two labels sharing a tail (a run, and the
    `inspect-` home reopening it) can never share a session.
    """
    text = re.sub(r"[^A-Za-z0-9_-]", "-", str(label)).strip("-") or "run"
    body = SESSION_NAME_MAX - len(SESSION_PREFIX)
    if len(text) > body:
        digest = hashlib.sha1(str(label).encode("utf-8")).hexdigest()[:6]
        text = digest + "-" + text[-(body - 7):]
    return SESSION_PREFIX + text


def version_at_least(release, wanted):
    """Is this `tmux -V` string at or past a version? An unreadable one is not.

    Unparseable reads as older rather than newer: the fallback path works
    everywhere, and guessing the other way would pass a flag the binary rejects
    at every single launch.
    """
    found = VERSION_RE.search(str(release or ""))
    if not found:
        return False
    return (int(found.group(1)), int(found.group(2))) >= wanted


def tty_process_rows(tty, command_builder=None):
    """(pid, pgid, tpgid) for every process on a pane's tty. Empty when unreadable.

    `tpgid` is the terminal's foreground process group, which is the one fact
    that separates a shell sitting at its prompt from a shell running something:
    both of them have children.

    The probe goes through the same `command_builder` the tmux calls do, because
    the tty belongs to whichever machine holds the pane, and this machine's
    process table knows nothing about it.
    """
    name = str(tty or "").replace("/dev/", "", 1)
    if not name:
        return []
    argv = ["ps", "-o", "pid=,pgid=,tpgid=", "-t", name]
    if command_builder is not None:
        argv = command_builder(argv)
    try:
        probe = subprocess.run(argv,
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in (probe.stdout or "").splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            rows.append((int(fields[0]), int(fields[1]), int(fields[2])))
        except ValueError:
            continue
    return rows


def pane_process(shell_pid, rows):
    """What a pane is running, from its shell's pid and its tty's process rows.

    None when the tty says nothing at all, which is a pane whose shell has gone:
    an empty foreground reading must never be reported as a prompt, because at
    the prompt is what the runner reads as a finished worker.
    """
    if not rows:
        return None
    foreground = rows[0][2]
    shell_group = next((pgid for pid, pgid, _ in rows if pid == shell_pid),
                       shell_pid)
    if foreground == shell_group:
        return WorkerProcess(shell_pid=shell_pid, at_prompt=True,
                             group_id=foreground)
    children = tuple(pid for pid, pgid, _ in rows
                     if pgid == foreground and pid != shell_pid)
    return WorkerProcess(shell_pid=shell_pid, at_prompt=False,
                         child_pids=children, group_id=foreground)


class TmuxSubstrate(Substrate):
    """A worker per tmux pane, in a detached session dispatch owns."""

    name = "tmux"
    capabilities = SubstrateCapabilities(
        can_steer=True, can_inspect=True, can_read_screen=True,
        can_answer_dialogs=True)

    def __init__(self, binary="", command_builder=None):
        """Resolve tmux, and prove a server can be reached or started.

        Both halves are the detection: a box with no tmux and a box whose tmux
        cannot bind its socket must both fall through to the next substrate
        rather than fail a run halfway in. `start-server` is the probe because it
        answers the question without leaving anything behind, tmux exiting an
        empty server as soon as it has started one.

        `command_builder` turns the argv this wanted to run into the argv that is
        actually run, and is how the same substrate drives a tmux on another
        machine: `remote.remote_tmux_command` returns one that wraps each call in
        ssh. With one in force the binary is left unresolved, since this
        machine's PATH says nothing about that one's, and the detection probes
        answer for the far tmux rather than for a local one.
        """
        self.command_builder = command_builder
        if command_builder is not None:
            self.binary = binary or "tmux"
        else:
            self.binary = binary or shutil.which("tmux") or ""
        if not self.binary:
            raise TmuxError("no tmux on PATH")
        self.release = self.probe(["-V"], "tmux -V")
        self.env_flag = version_at_least(self.release, ENV_FLAG_VERSION)
        self.probe(["start-server"], "tmux start-server")

    def probe(self, args, what):
        done = self.invoke(args, check=False)
        if done.returncode != 0:
            raise TmuxError(f"{what} failed: "
                            f"{(done.stderr or done.stdout or '').strip()}")
        return (done.stdout or "").strip()

    # -- the tmux command line -------------------------------------------

    def invoke(self, args, check=True, timeout=COMMAND_TIMEOUT):
        """Run one tmux command. Raises TmuxError unless the caller reads `check`."""
        argv = self.build_command([self.binary] + [str(a) for a in args])
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TmuxError(f"tmux {args[0]} did not return in {timeout}s; the "
                            "server may be wedged") from exc
        except OSError as exc:
            raise TmuxError(f"could not run {self.binary}: {exc}") from exc
        if check and done.returncode != 0:
            raise TmuxError(f"tmux {' '.join(str(a) for a in args)} failed: "
                            f"{(done.stderr or done.stdout or '').strip()}")
        return done

    def build_command(self, argv):
        """The argv actually run: the caller's, unless a builder rewrites it."""
        if self.command_builder is None:
            return argv
        return list(self.command_builder(argv))

    def tolerate_absence(self, args):
        """Closing something already gone is success, not an error to propagate.

        A worker that runs `exit` takes its pane, its session, and, as the last
        session, the whole server with it, so teardown routinely races the run's
        own shell.
        """
        done = self.invoke(args, check=False)
        if done.returncode == 0:
            return True
        blob = (done.stderr or "") + (done.stdout or "")
        if NO_SERVER in blob or "can't find" in blob:
            return False
        raise TmuxError(f"tmux {' '.join(str(a) for a in args)} failed: "
                        f"{blob.strip()}")

    # -- lifecycle -------------------------------------------------------

    def open(self, label, cwd="", env=None, focus=False):
        """Create the run's session, detached, with its environment in place.

        `focus` is ignored: a detached session has no client to bring to the
        front, which is the trade tmux makes for a home that outlives every
        dispatch process. The operator reaches it with the attach line instead.
        """
        name = session_name(label)
        assignments = sorted(dict(env or {}).items())
        args = ["new-session", "-d", "-s", name,
                "-x", PANE_WIDTH, "-y", PANE_HEIGHT]
        if cwd:
            args += ["-c", str(cwd)]
        if self.env_flag:
            for key, value in assignments:
                args += ["-e", f"{key}={value}"]
        args += ["-P", "-F", "#{pane_id} #{pane_pid} #{pane_tty}"]
        if not self.env_flag and assignments:
            # tmux runs a shell-command through `sh -c`, so the exports land
            # before the login shell that inherits them. `verify_environment`
            # re-asserts them either way; this is what the worker's environment
            # rests on until it does.
            args.append(f"{PANE_SHELL.export_line(assignments)}; "
                        'exec "${SHELL:-/bin/sh}" -l')
        done = self.invoke(args, check=False)
        if done.returncode != 0:
            raise WorkerSetupError(
                f"tmux could not create session {name}: "
                f"{(done.stderr or done.stdout or '').strip()}")
        fields = (done.stdout or "").split()
        if len(fields) < 3:
            raise WorkerSetupError(
                f"tmux created session {name} but reported no pane: "
                f"{(done.stdout or '').strip()!r}")
        return Worker(id=fields[0], group=name)

    def close(self, worker, release=True):
        """Kill the run's session. `release` is an agent handover tmux has none of."""
        if worker.group:
            self.tolerate_absence(["kill-session", "-t", worker.group])
        else:
            self.tolerate_absence(["kill-pane", "-t", worker.id])

    def exists(self, worker):
        return self.pane_facts(worker) is not None

    def worker_ids(self):
        """Every live pane id in a dispatch session, or None when tmux cannot say.

        No server is not "cannot tell": tmux keeps a server only while a session
        is alive, so that answer is a definite nothing.
        """
        done = self.invoke(["list-panes", "-a", "-F", "#{session_name} #{pane_id}"],
                           check=False)
        if done.returncode != 0:
            if NO_SERVER in ((done.stderr or "") + (done.stdout or "")):
                return set()
            return None
        found = set()
        for line in (done.stdout or "").splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0].startswith(SESSION_PREFIX):
                found.add(fields[1])
        return found

    def version(self):
        return self.release

    def agent_name(self, run_id):
        """What this substrate knows a run's worker by: its session name.

        The same name `open` gives the session, so the attach line a run prints
        and the session it closes cannot drift apart.
        """
        return session_name(run_id)

    def attach_hint(self, worker):
        """The line an operator types to watch this worker themselves."""
        return f"tmux attach -t {worker.group or session_name(worker.id)}"

    # -- typing ----------------------------------------------------------

    def send_text(self, worker, text):
        """Type text literally. `-l` is what keeps a brief from being read as keys.

        `--` because a brief may begin with a dash, and tmux would otherwise
        parse the first word of the worker's instructions as its own flag.
        """
        self.invoke(["send-keys", "-t", worker.id, "-l", "--", text])

    def send_keys(self, worker, keys):
        named = []
        for key in keys:
            found = KEY_NAMES.get(str(key).strip().lower())
            if not found:
                raise TmuxError(f"no tmux key name for {key!r}; add it to KEY_NAMES "
                                "rather than letting tmux type it as text")
            named.append(found)
        self.invoke(["send-keys", "-t", worker.id] + named)

    def send_line(self, worker, text):
        """Type a command and run it. Two calls, deliberately.

        Sending the text with a trailing newline is not the same thing: TUIs bind
        enter, and the pair is what both a real shell and a real TUI take.
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
            if needle and needle in self.read_screen(worker):
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

    def deliver_prompt(self, worker, text):
        """tmux has no write channel of its own, and says so.

        Nothing in tmux knows which pane holds a TUI, so there is no
        liveness-gated write to make and no agent to make it to. The runner
        answers this by typing one line naming the brief's path, which is the
        right shape for keystrokes that may be landing in a shell.
        """
        raise TmuxError(
            f"the tmux substrate has no agent channel to write into pane "
            f"{worker.id}; the brief goes in as keystrokes")

    # -- reading ---------------------------------------------------------

    def read_screen(self, worker, history=False):
        """What the pane has drawn, or empty once it is gone.

        `-J` joins the lines tmux wrapped at the pane's width, so a marker that
        wrapped is still one string. History is asked for only by the callers
        that need it: what is on screen right now is the question that readiness,
        dialogs, and screen movement are all asking.
        """
        args = ["capture-pane", "-p", "-J"]
        if history:
            args += ["-S", f"-{HISTORY_LINES}"]
        args += ["-t", worker.id]
        done = self.invoke(args, check=False)
        if done.returncode != 0:
            return ""
        # capture-pane pads its answer out to the height of the pane, and that
        # padding would otherwise be most of what a screen comparison compares.
        return (done.stdout or "").rstrip()

    def screen_since_spawn(self, worker, run_id=""):
        """The pane's screen since this spawn, out of its scrollback.

        `verify_environment` types its probe immediately before every spawn and
        every respawn, so the echo it leaves is exactly where this attempt's
        output starts, and by then it has usually scrolled off the pane.
        """
        screen = self.read_screen(worker, history=True)
        token = rc_token(run_id) if run_id else ""
        since = anchor_after(screen, f"{ENV_MARKER}-{token}=") if token else None
        return screen if since is None else since

    def blocked_rule(self, worker, known_rules=()):
        """Always empty: no rule engine here can name the dialog on a screen.

        Empty is what keeps dispatch from typing at a dialog it cannot read. The
        driver's own trust markers are matched against `read_screen` by the
        runner, and that is the only dialog answered here.
        """
        return ""

    def pane_facts(self, worker):
        """(shell pid, tty) for a pane, or None once it is gone.

        tmux answers `display-message` for a pane that does not exist with an
        empty line and a zero exit status, so an empty answer is the absence.
        """
        done = self.invoke(["display-message", "-p", "-t", worker.id,
                            "#{pane_pid} #{pane_tty}"], check=False)
        if done.returncode != 0:
            return None
        fields = (done.stdout or "").split()
        if len(fields) < 2:
            return None
        try:
            return (int(fields[0]), fields[1])
        except ValueError:
            return None

    def process_info(self, worker):
        facts = self.pane_facts(worker)
        if facts is None:
            return None
        shell_pid, tty = facts
        return pane_process(shell_pid,
                            tty_process_rows(tty, self.command_builder))

    def cpu_percent(self, pids):
        readings = [process_cpu_percent(pid) for pid in pids]
        readings = [reading for reading in readings if reading is not None]
        return sum(readings) if readings else None

    # -- spawning --------------------------------------------------------

    def start_worker(self, worker, driver, argv):
        """Type the lane's command at the pane's shell, and wait for the fork.

        Typing is not a fallback here the way it is in a substrate that can spawn
        a known agent kind. tmux has one way to start a program in a pane that
        already holds a shell, so there is no better path for a run to have
        missed and nothing to flag.
        """
        self.send_line(worker, PANE_SHELL.command_line(driver.shell_prefix, argv))
        self.wait_for_child(worker)
        return SpawnResult(method="send-keys", argv=list(argv))

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
                raise TmuxError(f"pane {worker.id} died before its worker started")
            if info.child_pids:
                return info
            time.sleep(0.25)
        raise TmuxError(
            f"pane {worker.id}: nothing started in {seconds}s after the lane "
            "command was typed, so there is no worker to brief")

    def wait_for_shell(self, worker, seconds=None):
        """Block until the pane has a shell that owns its own foreground.

        A pane is not typed into the instant it is created: the shell has to
        exist first, and anything the profile started in a group of its own has
        to finish, or the command lands in another program's stdin.
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

        `new-session -e` does reach the pane's shell, but the shell then sources
        the user's rc files, and an `export` or an `unset` in one wins on the way
        past. Neither the depth ladder nor metered key blanking is a per-run
        preference, so both are re-asserted here, and on a tmux too old for `-e`
        this is also where the environment first arrives.

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
            # The export and the probe are two separate sends, and a pane still
            # sourcing its rc files can swallow the first while taking the
            # second. Re-asserting is free; the guard below still refuses if the
            # pane never takes it.
            if attempt < ENV_ASSERT_ATTEMPTS:
                if log_path is not None:
                    append_status(log_path,
                                  f"RETRY {utc_now()} env assert #{attempt}: pane "
                                  f"reports {depth_var}={reported!r}, re-asserting")
                continue
            raise TmuxError(
                f"{run_id or worker.id}: the pane reports {depth_var}={reported!r}, "
                f"not {depth!r} after {ENV_ASSERT_ATTEMPTS} attempts; refusing to "
                "start a worker that would sit at the wrong rung of the depth ladder")
        if live_keys:
            raise TmuxError(
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

        A pane carries no exit status at all, so the shell is asked for it in its
        own dialect and the echoed marker is parsed off the screen. The typed
        line cannot be mistaken for the answer, because it still reads `=$?`
        where the answer reads `=<n>`.
        """
        token = rc_token(run_id or worker.id)
        probe = PANE_SHELL.rc_probe_line(RC_MARKER, token)
        if log_path is not None:
            append_status(log_path,
                          f"RC-PROBE {utc_now()} reading the status at the prompt")
        try:
            self.send_line(worker, probe)
        except TmuxError as exc:
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
        """Stop what the pane is running, leaving its shell alive."""
        info = self.process_info(worker)
        if info is None or not info.child_pids:
            return []
        children = list(info.child_pids)
        if self.command_builder is not None:
            return self.kill_where_the_pane_is(info, children)
        # Descendants first, while their parents are still alive to name them.
        strays = [pid for pid in descendant_pids(children) if pid not in children]
        group = info.group_id
        if not (group and group != info.shell_pid
                and processes.stop_process_group(group)):
            for pid in children:
                processes.stop_pid(pid)
        for pid in strays:
            processes.stop_pid(pid)
        return children + strays

    def kill_where_the_pane_is(self, info, children):
        """Kill through the builder, for a pane this process cannot signal.

        These pids number processes on the machine holding the pane. Signalling
        them here would hit whatever local process happens to share a number, so
        the signals are sent where the processes are, and the escalation is the
        same TERM then KILL.
        """
        group = info.group_id
        targets = ([f"-{group}"] if group and group != info.shell_pid
                   else [str(pid) for pid in children])
        for signal_name in ("TERM", "KILL"):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(self.build_command(["kill", f"-{signal_name}",
                                                   *targets]),
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=COMMAND_TIMEOUT, check=False)
            if signal_name == "TERM":
                time.sleep(KILL_GRACE_SECONDS)
        return children

    # -- presence --------------------------------------------------------

    def attach(self, worker):
        """Hand this terminal to the run's session until the operator detaches."""
        name = worker.group or session_name(worker.id)
        try:
            return subprocess.run(
                self.build_command([self.binary, "attach", "-t", name]),
                check=False).returncode
        except OSError as exc:
            raise TmuxError(f"could not attach to {name}: {exc}") from exc
