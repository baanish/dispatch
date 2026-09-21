"""The headless substrate: a worker is a plain subprocess of a vendor CLI.

Headless is the fallback that is always there. It needs no daemon and no
terminal: `start_worker` runs the driver's one-shot form (`codex exec`,
`claude -p`, grok's non-interactive mode), whose prompt is an argv element
because there is no TUI to type it into, and the run is over when that process
exits.

It degrades honestly rather than pretending:

- `can_steer`: no. There is no input channel once the process is running, so
  `dispatch steer` refuses and points at `dispatch continue`.
- `can_inspect`: no. There is no live session to reopen and attach to.
  `dispatch continue` still works, through the driver's own resume flag.
- `can_read_screen`: no. `read_screen` returns what the process wrote to its
  captured output, which is a log and not a screen: nothing redraws, nothing is
  interactive, and there is no cursor an operator could be shown. The runner
  still reads it, to measure whether the worker has gone quiet and to salvage
  `out.md` when the worker wrote nothing.
- `can_answer_dialogs`: no. The one-shot forms are run with flags that mean the
  CLI never asks.

Two things this substrate gets for free that the pane ones fight for: the
environment is handed to the process directly, so `verify_environment` has
nothing to verify, and there is no shell whose exit code has to be scraped back
off a screen.

What it loses is the check-in ladder's richest signal. There is no TUI state to
read, so `status` stays untracked and liveness rests on the captured log
growing, on CPU in the worker's process tree, and on whether the deliverable
exists.

Where `out.md` comes from, per driver:

- codex: `codex exec -o <path>` writes the final message to the deliverable
  itself, so nothing has to be extracted from the output.
- claude (`-p`) and grok (`--output-format text`) print their final message on
  stdout, which lands in `pane.log`. Every brief already asks the worker to
  write `out.md`; when one does not, the runner's own salvage copies the
  captured output there.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess

from ..errors import DispatchError
from ..processes import (descendant_pids, pid_alive, popen_detached,
                         pids_cpu_percent, resolve_python, stop_pid,
                         stop_process_group)
from ..records import read_tail_bytes, run_dir, runs_root
from .base import (Substrate, SubstrateCapabilities, SubstrateError, SpawnResult,
                   Worker, WorkerProcess)

# Everything the worker printed, both streams in the order it printed them.
PANE_LOG = "pane.log"
# What a home knows about its worker between processes: the environment it was
# opened with, the argv it was started with, and the pid to look for.
STATE_FILE = "worker.json"
# The relay's one line of output.
RC_FILE = "worker.rc"

# The log is read on every poll to measure stillness, so it is read from the end
# rather than whole: a worker can print megabytes.
SCREEN_TAIL_BYTES = 65536

# The launcher of a background run exits long before its worker does, and a
# process that did not fork the CLI cannot read its exit status once the parent
# that could reap it has gone. So the CLI is started under a relay that writes
# the status down where any later process can read it. The relay is also what
# owns the process group dispatch signals, which is how a kill reaches the CLI's
# own children on both platforms.
# The status file is opened without following a link, because the worker may be
# able to write in the run directory it lands in.
RC_RELAY = ("import os,subprocess,sys; rc=subprocess.call(sys.argv[2:]); "
            "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_TRUNC|"
            "getattr(os,'O_NOFOLLOW',0),0o666); os.write(fd,str(rc).encode()); "
            "os.close(fd); sys.exit(rc)")


class HeadlessSubstrate(Substrate):
    """A worker per subprocess, with its output captured to the run directory."""

    name = "headless"
    has_tui = False
    capabilities = SubstrateCapabilities(
        can_steer=False, can_inspect=False, can_read_screen=False,
        can_answer_dialogs=False)

    # -- the home --------------------------------------------------------

    def home(self, worker):
        """A headless worker's home is its run directory: no daemon owns one."""
        return run_dir(worker.id)

    def read_state(self, worker):
        try:
            return json.loads(
                (self.home(worker) / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError, DispatchError):
            return {}

    def save_state(self, worker, state):
        (self.home(worker) / STATE_FILE).write_text(
            json.dumps(state, indent=2), encoding="utf-8")

    def open(self, label, cwd="", env=None, focus=False):
        try:
            home = run_dir(label)
            home.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SubstrateError(f"no home for {label}: {exc}") from exc
        worker = Worker(id=str(label))
        # The environment is written down rather than kept in memory: a
        # background run's watcher is a different process, and a self-update
        # respawn there has to start the CLI with the environment the launcher
        # opened this home with.
        self.save_state(worker, {"env": dict(env or {}), "cwd": str(cwd or "")})
        return worker

    def exists(self, worker):
        return self.home(worker).is_dir()

    def worker_ids(self):
        """Every home whose process is still running, read off the runs tree.

        There is no daemon to ask and a headless home is a run directory, so
        this is a scan rather than a call. It is never None: this substrate can
        always tell, and inheriting the base's "cannot tell" left the caps
        resting on the reservation, so every background run was abandoned and
        killed a minute after it started.
        """
        try:
            homes = sorted(path for path in runs_root().iterdir() if path.is_dir())
        except OSError:
            return None
        live = []
        for home in homes:
            worker = Worker(id=home.name)
            pid = self.read_state(worker).get("pid")
            if pid and self.read_exit_code(worker) is None and pid_alive(pid):
                live.append(worker.id)
        return live

    def close(self, worker, release=True):
        """Nothing to tear down but the process, if it is somehow still running."""
        self.kill_worker_tree(worker)

    # -- starting the CLI ------------------------------------------------

    def start_worker(self, worker, driver, argv):
        """Run the driver's one-shot form, with its output captured to pane.log.

        The binary is resolved here rather than left to the relay: a CLI that is
        not installed is the operator's most likely mistake, and it deserves a
        sentence rather than a traceback in the worker's log.
        """
        state = self.read_state(worker)
        binary = shutil.which(argv[0]) if argv else ""
        if not binary:
            raise SubstrateError(
                f"{(argv or ['?'])[0]} is not on PATH; the {driver.name} lane "
                "cannot run here")
        home = self.home(worker)
        log = home / PANE_LOG
        # Where this launch's output starts, so a respawn after a self-update
        # does not read the previous launch's banner as its own.
        state["log_offset"] = log.stat().st_size if log.is_file() else 0
        rc_path = home / RC_FILE
        with contextlib.suppress(OSError):
            rc_path.unlink()
        env = dict(os.environ)
        env.update(state.get("env") or {})
        # `-P`: the relay's cwd is the task directory, and without it a
        # `subprocess.py` planted there runs as the operator before the CLI starts.
        relay = [resolve_python(), "-P", "-c", RC_RELAY, str(rc_path), binary,
                 *argv[1:]]
        handle = open(log, "ab")
        try:
            popen = popen_detached(
                relay, cwd=state.get("cwd") or None, env=env,
                stdin=subprocess.DEVNULL, stdout=handle,
                # One file, both streams: what a worker complained about and
                # what it answered belong in the order they happened.
                stderr=subprocess.STDOUT)
        except OSError as exc:
            raise SubstrateError(f"could not start {argv[0]}: {exc}") from exc
        finally:
            handle.close()
        state["pid"] = popen.pid
        state["argv"] = list(argv)
        self.save_state(worker, state)
        return SpawnResult(method="subprocess", argv=list(argv))

    # -- reading ---------------------------------------------------------

    def read_screen(self, worker):
        """The tail of what the worker printed. A log, not a screen."""
        return read_tail_bytes(self.home(worker) / PANE_LOG, SCREEN_TAIL_BYTES)

    def screen_since_spawn(self, worker, run_id=""):
        offset = int(self.read_state(worker).get("log_offset") or 0)
        try:
            with open(self.home(worker) / PANE_LOG, "rb") as fh:
                fh.seek(offset)
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def process_info(self, worker):
        if not self.home(worker).is_dir():
            return None
        pid = self.read_state(worker).get("pid")
        # The relay's status file is the completion signal, ahead of the pid: a
        # pid outlives its process as a zombie until whoever forked it reaps it,
        # and the process asking here is usually not that one. Liveness is the
        # backstop, for a worker killed before it could write anything down.
        finished = self.read_exit_code(worker) is not None
        if not pid or finished or not pid_alive(pid):
            # There is no shell to hand the foreground back to, so a worker that
            # has gone is reported the way a pane substrate reports one: at the
            # prompt, with nothing running.
            return WorkerProcess(shell_pid=pid or None, at_prompt=True)
        # The relay counts as one of the worker's processes: it is alive for
        # exactly as long as the CLI is, so a `ps` that comes back empty under
        # load cannot be read as a worker that has already exited.
        return WorkerProcess(shell_pid=pid, at_prompt=False,
                             child_pids=(pid,) + tuple(descendant_pids([pid])),
                             group_id=pid)

    def cpu_percent(self, pids):
        return pids_cpu_percent(pids)

    def read_exit_code(self, worker, run_id="", log_path=None):
        """The process's own status, as the relay wrote it down."""
        try:
            return int((self.home(worker) / RC_FILE)
                       .read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    # -- typing, which this substrate cannot do --------------------------

    def refuse_typing(self, what):
        return SubstrateError(
            f"a headless worker has no terminal to {what}: it takes its brief as "
            "an argv element at launch and its answer comes back as a file")

    def send_line(self, worker, text):
        raise self.refuse_typing("run a command in")

    def send_keys(self, worker, keys):
        raise self.refuse_typing("send keys to")

    def send_tui_line(self, worker, text, enters=1, settle=None, confirm=None,
                      log_path=None):
        raise self.refuse_typing("type into")

    def deliver_prompt(self, worker, text):
        raise self.refuse_typing("prompt")

    # -- killing ---------------------------------------------------------

    def kill_worker_tree(self, worker):
        pid = self.read_state(worker).get("pid")
        if not pid or self.read_exit_code(worker) is not None:
            # The relay wrote its status and left. Anything alive wearing that
            # pid now belongs to somebody else.
            return []
        children = descendant_pids([pid])
        stop_process_group(pid)
        # Each one as well, whatever the group signal did: a descendant that
        # called setsid left the group and outlives it.
        for child in children:
            stop_pid(child)
        stop_pid(pid)
        return children + [pid]
