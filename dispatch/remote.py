"""Machines: running a worker on a box this one reaches over ssh.

Placement is one line of policy and no scheduler: `--on <machine>` wins, then
the lane's own `machine`, then local. A run goes where the operator said, or
where its lane always goes.

Two modes, because remote boxes come in two shapes.

`dispatch` mode is the default and expects a full install on the far side. The
brief and any schema or image are copied into a staging directory under the
remote `DISPATCH_HOME`, the remote's own `dispatch run --bg` launches the
worker, and the local record keeps the remote run id. While the run is live,
`status`, `wait`, `logs`, `watch`, `kill`, `steer`, and `continue` are questions
asked over ssh; at terminal state the artifacts are copied down once and the
local record answers from then on. The far side owns the worker, so a dropped
connection costs nothing: the run carries on and the next poll finds it.

`shell` mode expects only tmux and a vendor CLI on the far side. There is no
remote dispatch: the local process drives a remote tmux through the same tmux
substrate a local run uses, with every tmux invocation wrapped in ssh over one
persistent control connection. The local process is the run's supervisor, so an
ssh drop it cannot re-establish loses the run, the way closing a laptop on a
local foreground run does.

That machine shares no filesystem with this one, so a shell-mode run has a
directory on both. The brief and the prompt are staged into the machine's, every
path the worker is given is one of the machine's, and the deliverable it writes
there is mirrored down on every poll. The record, the status log, and the screen
stay here, which is what keeps `status`, `wait`, and `logs` local questions.

ssh configuration is the operator's. Hosts, users, keys, ports, and jump hosts
all come from their ssh config; dispatch adds `BatchMode=yes`, because a
password prompt in a background run hangs forever, and the control settings that
keep one connection open across the dozens of calls a live run makes. Nothing
sensitive is ever an argv element: briefs, schemas, images, and steer messages
travel as files and are named by path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .caps import reserve_slot, session_key
from .errors import DispatchError
from .policy import current_depth, parse_deadline, policy
from .records import (append_status, dispatch_home, new_run_id, out_copy_dir,
                      out_copy_path, release_run_lock, run_dir, save_record,
                      utc_now, validate_run_id, write_out_copy,
                      write_status_header)

# A machine name that always means "here", so a lane pinned to a machine can
# still be run locally with `--on local`.
LOCAL = "local"

DISPATCH_MODE = "dispatch"
SHELL_MODE = "shell"
MACHINE_MODES = (DISPATCH_MODE, SHELL_MODE)
REMOTE_SHELLS = ("posix", "powershell")
POWERSHELL_PREFIX = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                     "$env:PYTHONIOENCODING = 'utf-8'; "
                     "$ErrorActionPreference = 'Stop'; ")

DEFAULT_REMOTE_HOME = "~/.dispatch"
DEFAULT_REMOTE_DISPATCH = "dispatch"

# How long the multiplexed connection outlives the command that opened it. Long
# enough that a poll loop reuses one connection, short enough that an idle
# machine is not held open all day.
CONTROL_PERSIST = "60"

# ssh's own connect timeout. The operator's config may set a longer one; this is
# only the floor that keeps an unplugged machine from hanging a poll.
CONNECT_TIMEOUT = "10"

SSH_SECONDS = 60
# The deliverable mirror runs inside a poll, so it is bounded like a poll and not
# like a transfer: a run whose heartbeat stops for five minutes because a copy is
# hanging looks exactly like a run whose worker died.
FETCH_SECONDS = 30
# A remote launch opens a home, starts a CLI, and delivers the brief before it
# prints the id, so it gets the room a local spawn gets.
LAUNCH_SECONDS = 600
SCP_SECONDS = 300

POLL_SECONDS = 2.0

# Copied down once, at terminal state. `run.json` lands beside them under its
# own name: the local record is this run's identity, and overwriting it with the
# remote's would lose the machine, the local directory, and the local id.
MIRRORED_FILES = ("out.md", "out.json", "status.log", "screen.log")
REMOTE_RECORD_FILE = "remote-run.json"

# What a poll takes from the remote record. Deliberately not `worker_id`: the
# worker exists in the remote's substrate, and a local sweep that found the id
# in a record would look for it here and call the run orphaned.
POLLED_FIELDS = ("state", "rc", "finished", "error", "session_id", "needs_hand",
                 "checkins", "checkin_verdict", "aborted", "started_at",
                 "deliverable_written")


@dataclass(frozen=True)
class Machine:
    """One `[machines.<name>]` table: where it is, and what is installed on it."""

    name: str
    ssh: str                          # user@host, as ssh takes it
    mode: str = DISPATCH_MODE
    home: str = DEFAULT_REMOTE_HOME   # the remote DISPATCH_HOME
    dispatch: str = DEFAULT_REMOTE_DISPATCH  # the remote dispatch command
    shell: str = "posix"               # the ssh server's command shell


_MACHINES = {}


def machine_table():
    """The machines in force: none until config loads some."""
    return _MACHINES


def set_machines(table):
    """Replace the machine table wholesale. Returns the old one."""
    global _MACHINES
    previous = _MACHINES
    _MACHINES = dict(table)
    return previous


def machine_for(name):
    table = machine_table()
    machine = table.get(name)
    if machine is None:
        known = ", ".join(sorted(table)) or "none configured"
        raise DispatchError(f"no machine named {name!r}; known machines: {known}")
    return machine


def place_run(on, lane):
    """Where this run goes: `--on`, then the lane's machine, then local (None)."""
    name = (on or "").strip() or (getattr(lane, "machine", "") or "").strip()
    if not name or name == LOCAL:
        return None
    return machine_for(name)


def machine_of_record(rec):
    """The machine a recorded run was placed on, or None for a local run.

    Rebuilt from the record rather than looked up, so a run stays killable and
    readable after its machine is renamed or dropped from config.
    """
    if not rec.get("machine"):
        return None
    return Machine(name=rec["machine"], ssh=rec.get("machine_ssh", ""),
                   mode=rec.get("machine_mode", DISPATCH_MODE),
                   home=rec.get("remote_home") or DEFAULT_REMOTE_HOME,
                   dispatch=rec.get("remote_dispatch") or DEFAULT_REMOTE_DISPATCH,
                   shell=rec.get("remote_shell", "posix"))


# --------------------------------------------------------------------------
# ssh and scp
# --------------------------------------------------------------------------


def control_socket(machine):
    """The multiplexing socket for this machine, under DISPATCH_HOME.

    Named by a short digest rather than ssh's own `%C`, because the whole path
    has to fit in a unix socket address (104 bytes on the BSDs) and the home it
    sits under is not ours to shorten.
    """
    directory = dispatch_home() / "ssh"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(machine.ssh.encode("utf-8")).hexdigest()[:8]
    return directory / digest


def ssh_options(machine):
    """The only options dispatch adds; everything else is the operator's config."""
    return ["-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control_socket(machine)}",
            "-o", f"ControlPersist={CONTROL_PERSIST}"]


def quote_remote(path):
    """Quote a remote path, leaving a leading `~/` for the remote shell.

    `~` is the shell's, not ssh's: quoting it whole turns `~/.dispatch` into a
    directory with a literal tilde in its name.
    """
    text = str(path)
    if text == "~":
        return text
    if text.startswith("~/"):
        return "~/" + shlex.quote(text[2:])
    return shlex.quote(text)


def quote_powershell(value):
    text = str(value)
    if text == "~":
        return "$HOME"
    if text.startswith("~/"):
        return "($HOME + " + quote_powershell(text[1:]) + ")"
    return "'" + text.replace("'", "''") + "'"


def remote_command(argv, env=None, shell="posix"):
    """One argv, and any environment for it, as a remote shell command string.

    Every word is quoted for the remote shell, which is what keeps a path with a
    space in it one argument. A leading `~/` is the exception and stays bare:
    only that shell knows where the machine's home directory is, and a quoted
    tilde reaches dispatch there as a directory with a tilde in its name.
    """
    if shell == "powershell":
        parts = [f"$env:{key} = {quote_powershell(value)}"
                  for key, value in (env or {}).items()]
        parts.append("& " + " ".join(quote_powershell(word) for word in argv))
        return POWERSHELL_PREFIX + "; ".join(parts)
    parts = [f"{key}={quote_remote(value)}" for key, value in (env or {}).items()]
    parts += [quote_remote(word) for word in argv]
    return " ".join(parts)


def mkdir_command(machine, path):
    if machine.shell == "powershell":
        return (POWERSHELL_PREFIX
                + f"[void][System.IO.Directory]::CreateDirectory({quote_powershell(path)})")
    # The staging directory holds the brief, and the machine may be shared.
    return "umask 077; " + remote_command(["mkdir", "-p", path])


def read_command(machine, path):
    if machine.shell == "powershell":
        return (POWERSHELL_PREFIX
                + "Get-Content -Raw -Encoding UTF8 -ErrorAction Stop -LiteralPath "
                + quote_powershell(path))
    return remote_command(["cat", path])


def scp_path(machine, path):
    if machine.shell == "powershell":
        # SFTP accepts drive paths, but shell quotes become literal filename bytes.
        return str(path).replace("\\", "/")
    return quote_remote(path)


def ssh_argv(machine, command):
    return ["ssh", *ssh_options(machine), machine.ssh, command]


def run_ssh(machine, command, seconds=SSH_SECONDS, check=False):
    """One ssh call. `check` turns a non-zero exit into an operator-facing error."""
    return run_program(machine, ssh_argv(machine, command), seconds, check)


def run_program(machine, argv, seconds, check):
    """One ssh or scp invocation, with its failures turned into dispatch's."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=seconds)
    except FileNotFoundError as exc:
        raise DispatchError(
            f"machine {machine.name}: {argv[0]} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(
            f"machine {machine.name}: {argv[0]} timed out after {seconds}s") from exc
    if check and proc.returncode:
        raise DispatchError(ssh_failure(machine, proc))
    return proc


def missing_dispatch(proc):
    """Did the remote shell fail to find the dispatch command itself?

    127 is the shell's own answer and ssh passes it through. The text is checked
    too, for the case where something between here and there ate the status.
    """
    text = (proc.stderr or "") + (proc.stdout or "")
    return (proc.returncode == 127 or "command not found" in text
            or "is not recognized" in text)


def ssh_failure(machine, proc):
    lines = [line for line in (proc.stderr or proc.stdout or "").splitlines() if line]
    tail = lines[-1] if lines else f"exit {proc.returncode}"
    if proc.returncode == 255:
        return (f"machine {machine.name} ({machine.ssh}) is not reachable: {tail}. "
                f"dispatch uses your ssh config, so `ssh {machine.ssh} exit 0` is the "
                "same connection.")
    if missing_dispatch(proc):
        return (f"machine {machine.name} has no `{machine.dispatch}` on PATH: {tail}. "
                "Install dispatch there, or give the machine "
                f'mode = "{SHELL_MODE}", which needs only tmux and the vendor CLI. '
                "A login over ssh may also have a shorter PATH than a terminal "
                "does, in which case `dispatch = \"/full/path/to/dispatch\"` "
                "settles it.")
    return f"machine {machine.name}: {tail}"


def scp_up(machine, paths, remote_dir):
    """Copy local files into a directory on the machine."""
    if not paths:
        return
    argv = ["scp", *ssh_options(machine), *[str(p) for p in paths],
            f"{machine.ssh}:{scp_path(machine, remote_dir)}/"]
    run_program(machine, argv, SCP_SECONDS, check=True)


def scp_down(machine, remote_path, local_path, seconds=SCP_SECONDS):
    """Copy one file off the machine. False when it was not there."""
    argv = ["scp", *ssh_options(machine),
            f"{machine.ssh}:{scp_path(machine, remote_path)}", str(local_path)]
    return run_program(machine, argv, seconds, check=False).returncode == 0


# --------------------------------------------------------------------------
# dispatch mode: launching
# --------------------------------------------------------------------------


def is_remote(rec):
    """Is this run supervised by another machine's dispatch?

    True only in `dispatch` mode. A shell-mode run is placed on a machine but
    driven from here, so it is watched, reconciled, and killed like a local one.
    """
    return bool(rec.get("remote_supervised"))


def staging_dir(machine, run_id):
    """Where this run's inputs live on the machine."""
    return posixpath.join(machine.home, "remote", run_id)


def machine_fields(machine, run_id):
    """The record fields a run placed on a machine carries, whichever mode it is.

    `remote_staging` is the run's directory on the machine. In dispatch mode it
    holds the inputs the machine's own dispatch is handed; in shell mode it is
    the whole of the run as the worker sees it, because that worker's pane can
    open no path of this machine's.
    """
    return {"machine": machine.name,
            "machine_ssh": machine.ssh,
            "machine_mode": machine.mode,
            "remote_home": machine.home,
            "remote_shell": machine.shell,
            "remote_staging": staging_dir(machine, run_id)}


def prepare_remote_run(machine, lane, brief_text, opts, kind="run", parent=""):
    """The local record for a run that will happen somewhere else.

    Always called with the runs lock held: like a local reservation, the record
    is the slot, so it exists before anything is started. It carries no argv and
    no prompt, because the machine's own dispatch builds both.
    """
    rec_id = new_run_id(lane.name)
    directory = run_dir(rec_id)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "brief.md").write_text(brief_text, encoding="utf-8")
    rec = {
        "id": rec_id,
        "kind": kind,
        "lane": lane.name,
        "driver": lane.driver,
        "substrate": "",          # the machine's, not this one's
        "model": lane.model,
        "effort": lane.effort,
        "tier": lane.tier,
        "dir": str(directory),
        "cwd": opts.dir,          # a path on the machine, not on this one
        "write": opts.write,
        "net": opts.net,
        "add_dirs": list(opts.add_dirs),
        "schema": opts.schema,
        "out_copy": out_copy_path(opts.out),
        "out_copy_dir": out_copy_dir(opts.out),
        "image": opts.image,
        "argv": [],
        "prompt": "",
        # The machine always runs it in the background; `--bg` here decides only
        # whether this process waits for it.
        "background": opts.bg,
        "foreground": not opts.bg,
        "deadline": opts.deadline,
        "deadline_seconds": parse_deadline(opts.deadline) if opts.deadline else None,
        "session_id": "",
        "resumed_from_session": "",
        "parent": parent,
        "transcript": "",
        "launcher_depth": current_depth(),
        "depth": current_depth(),
        "session": session_key(),
        "worker_id": "",
        "worker_group": "",
        "agent": "",
        "owner_pid": os.getpid(),
        "reserved_at": time.time(),
        "state": "reserved",
        "rc": None,
        "created": utc_now(),
        "finished": "",
        **machine_fields(machine, rec_id),
        "remote_supervised": True,
        "remote_dispatch": machine.dispatch,
        "remote_id": "",
        "remote_dir": "",
    }
    save_record(rec)
    return rec


def stage_file(rec, machine, name, text):
    """Write one input into the run's directory and copy it to the machine.

    Everything the worker is given crosses as a file, and the argv names it by
    path: a brief, a schema, an image, a steer message.
    """
    local = Path(rec["dir"]) / name
    local.write_text(text, encoding="utf-8")
    scp_up(machine, [local], rec["remote_staging"])
    return posixpath.join(rec["remote_staging"], name)


def remote_env(rec, machine):
    """What the machine's dispatch runs with.

    The ladder rung crosses the connection unchanged: the remote dispatch is this
    seat reaching further rather than a worker, so the worker it starts lands one
    rung below this process and a depth-1 worker cannot buy a spawn by going
    remote. The session key crosses too, which is what makes the per-session cap
    count an operator's runs instead of each machine's sshd sessions.
    """
    return {"DISPATCH_HOME": machine.home,
            "AGENT_DEPTH": str(current_depth()),
            "DISPATCH_SESSION": rec["session"]}


def remote_run_argv(machine, rec, brief, schema="", image=""):
    """The `dispatch run` the machine will execute. Every path is the machine's."""
    argv = [machine.dispatch, "run", rec["lane"], brief, "--bg"]
    if rec["cwd"]:
        argv += ["--dir", rec["cwd"]]
    if rec["write"]:
        argv.append("--write")
    if rec["net"]:
        argv.append("--net")
    for extra in rec["add_dirs"]:
        argv += ["--add-dir", extra]
    if schema:
        argv += ["--schema", schema]
    if image:
        argv += ["--image", image]
    if rec["deadline"]:
        argv += ["--deadline", rec["deadline"]]
    return argv


def launch_output(machine, proc):
    """`dispatch run --bg` prints the run id, then the run's directory."""
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if len(lines) < 2:
        detail = (proc.stderr or proc.stdout or "").strip() or "no output"
        raise DispatchError(
            f"machine {machine.name} did not report a run id: {detail}")
    try:
        run_id = validate_run_id(lines[0])
    except DispatchError as exc:
        raise DispatchError(
            f"machine {machine.name} reported {lines[0]!r} as a run id: "
            f"{exc}") from exc
    return run_id, lines[1]


def start_on_machine(rec, machine, argv):
    """Run a backgrounding `dispatch` verb there, and record the run it started."""
    write_status_header(Path(rec["dir"]) / "status.log", rec["lane"], os.getpid(),
                        rec["cwd"] or "-", argv, utc_now())
    proc = run_ssh(machine, remote_command(argv, remote_env(rec, machine), machine.shell),
                   seconds=LAUNCH_SECONDS, check=True)
    rec["remote_id"], rec["remote_dir"] = launch_output(machine, proc)
    rec["argv"] = argv
    rec["state"] = "running"
    rec["started_at"] = time.time()
    save_record(rec)
    append_status(Path(rec["dir"]) / "status.log",
                  f"REMOTE {utc_now()} {machine.name} {rec['remote_id']}")
    return rec


def launch_remote_run(rec, machine, brief_path):
    """Ship the inputs and start the run on the machine."""
    run_ssh(machine, mkdir_command(machine, rec["remote_staging"]),
            check=True)
    uploads = [brief_path]
    schema = image = ""
    if rec["schema"]:
        uploads.append(rec["schema"])
        schema = posixpath.join(rec["remote_staging"], Path(rec["schema"]).name)
    if rec["image"]:
        uploads.append(rec["image"])
        image = posixpath.join(rec["remote_staging"], Path(rec["image"]).name)
    names = [Path(path).name for path in uploads]
    if len(set(names)) != len(names):
        raise DispatchError(
            "the brief, the schema, and the image are staged side by side on "
            f"{machine.name}, so they need different file names: {', '.join(names)}")
    scp_up(machine, uploads, rec["remote_staging"])
    brief = posixpath.join(rec["remote_staging"], Path(brief_path).name)
    return start_on_machine(rec, machine,
                            remote_run_argv(machine, rec, brief, schema, image))


def run_remote(machine, lane, brief_text, opts, brief_path):
    """Place a run on a machine and return its record.

    A foreground run then waits on the machine; a `--bg` one is the caller's to
    poll from here on.
    """
    rec, handle = reserve_slot(
        lambda: prepare_remote_run(machine, lane, brief_text, opts))
    try:
        rec = launch_remote_run(rec, machine, brief_path)
    except BaseException:
        close_failed_launch(rec, handle)
        raise
    release_run_lock(handle)
    return rec if opts.bg else wait_for_remote(rec, machine)


def close_failed_launch(rec, handle):
    """Close a reservation whose launch never happened.

    Nothing was started anywhere, so the slot is a slot with nothing in it and
    the record says so rather than being left `reserved` for a sweep to guess at.
    """
    rec["state"] = "failed"
    rec["finished"] = utc_now()
    rec["closed_by"] = "launch"
    save_record(rec)
    release_run_lock(handle)


# --------------------------------------------------------------------------
# dispatch mode: proxying a live run
# --------------------------------------------------------------------------


def remote_path(rec, name):
    return posixpath.join(rec["remote_dir"], name)


def poll_command(rec, machine):
    """Reconcile on the far side, then take the record it wrote.

    The machine has no supervisor process either: a background run's exit code
    lands when something there reads its status. So one round trip triggers that
    read and collects the record it settled.
    """
    reconcile = remote_command([machine.dispatch, "status"],
                               remote_env(rec, machine), machine.shell)
    record = read_command(machine, remote_path(rec, "run.json"))
    sink = "$null" if machine.shell == "powershell" else "/dev/null"
    return f"{reconcile} >{sink} 2>&1; {record}"


def poll_remote_run(rec, machine=None):
    """Ask the machine how the run is going, and fold the answer into the record.

    Raises when the machine cannot be reached: a poll that swallowed that would
    report a run whose machine is gone as a healthy one.
    """
    machine = machine or machine_of_record(rec)
    if not rec.get("remote_dir"):
        return rec
    proc = run_ssh(machine, poll_command(rec, machine), check=True)
    try:
        reported = json.loads(proc.stdout)
    except ValueError as exc:
        raise DispatchError(
            f"machine {machine.name}: {rec['remote_id']} has no readable record "
            f"({exc})") from exc
    for field in POLLED_FIELDS:
        if field in reported:
            rec[field] = reported[field]
    rec["polled"] = utc_now()
    rec.pop("remote_error", None)
    save_record(rec)
    if rec.get("state") in policy().terminal_states:
        rec = mirror_remote_run(rec, machine)
    return rec


def reconcile_remote_run(rec):
    """One poll of a remote run nobody is waiting on. Never raises.

    A machine that is asleep, off the network, or behind a VPN that dropped is
    the ordinary case here, and none of those mean the run is over. The failure
    is recorded so `status` can report it, and the run keeps its state.
    """
    try:
        return poll_remote_run(rec)
    except DispatchError as exc:
        rec["remote_error"] = str(exc)
        save_record(rec)
        return rec


def mirror_remote_run(rec, machine=None):
    """Copy a finished run's artifacts down, once.

    The machine's own `run.json` lands under its own name. The local record is
    this run's identity, and overwriting it would lose the local id, the local
    directory, and the machine the run was placed on.
    """
    if rec.get("mirrored"):
        return rec
    machine = machine or machine_of_record(rec)
    directory = Path(rec["dir"])
    # The machine's record always exists by now, and so does the out.md of a run
    # that ended `done`, so failing to copy either is the connection failing, not
    # a file being absent. Unmarked, so a later poll tries again instead of
    # reading a mirror with no answer in it as the run's answer.
    copied = scp_down(machine, remote_path(rec, "run.json"),
                      directory / REMOTE_RECORD_FILE)
    answered = scp_down(machine, remote_path(rec, "out.md"), directory / "out.md")
    if not copied or (rec.get("state") == "done" and not answered):
        rec["remote_error"] = (f"machine {machine.name}: the run ended but its "
                               "files could not be copied down yet")
        rec["mirror_pending"] = True
        save_record(rec)
        return rec
    rec.pop("mirror_pending", None)
    for name in MIRRORED_FILES:
        if name != "out.md":
            scp_down(machine, remote_path(rec, name), directory / name)
    if rec.get("out_copy"):
        deliverable = directory / "out.md"
        if deliverable.is_file():
            write_out_copy(rec, deliverable.read_text(encoding="utf-8",
                                                      errors="replace"))
    rec["mirrored"] = utc_now()
    save_record(rec)
    return rec


def wait_for_remote(rec, machine=None, poll_seconds=None):
    """Block until the machine says the run is over, mirroring what it produced."""
    machine = machine or machine_of_record(rec)
    while True:
        rec = poll_remote_run(rec, machine)
        if rec.get("state") in policy().terminal_states:
            return rec
        time.sleep(POLL_SECONDS if poll_seconds is None else poll_seconds)


def remote_log(rec, machine=None):
    """The run's status.log: the machine's while it is live, the mirror after."""
    local = Path(rec["dir"]) / "status.log"
    if rec.get("mirrored") or not rec.get("remote_dir"):
        return local.read_text(encoding="utf-8", errors="replace") \
            if local.is_file() else ""
    machine = machine or machine_of_record(rec)
    return run_ssh(machine,
                   read_command(machine, remote_path(rec, "status.log")),
                   check=True).stdout


def kill_remote_run(rec, machine=None):
    """Stop the run on the machine. Returns what its dispatch reported."""
    machine = machine or machine_of_record(rec)
    proc = run_ssh(machine,
                   remote_command([machine.dispatch, "kill", rec["remote_id"]],
                                  remote_env(rec, machine), machine.shell),
                   check=True)
    return (proc.stdout or "").strip()


def steer_remote(rec, message, deadline=""):
    """Type a correction into the live worker on the machine.

    The message is a prompt, and prompts cross as files for the same reason
    briefs do: an argv element is readable in the machine's process table by
    every account on it.
    """
    machine = machine_of_record(rec)
    path = stage_file(rec, machine, f"steer-{time.strftime('%H%M%S')}.md", message)
    argv = [machine.dispatch, "steer", rec["remote_id"], "--message-file", path]
    if deadline:
        argv += ["--deadline", deadline]
    proc = run_ssh(machine, remote_command(argv, remote_env(rec, machine), machine.shell),
                   seconds=LAUNCH_SECONDS, check=True)
    return (proc.stdout or "").strip()


def continue_remote(rec, lane, message, opts):
    """A new turn on the machine, recorded here as a child of this run.

    The machine's `continue` starts a run of its own, with its own id, so this
    side gets its own record too: one local record per remote run keeps `status`,
    `kill`, and the mirror pointing at exactly one thing.
    """
    machine = machine_of_record(rec)
    child, handle = reserve_slot(lambda: prepare_remote_run(
        machine, lane, message, opts, kind="continue", parent=rec["id"]))
    try:
        run_ssh(machine, mkdir_command(machine, child["remote_staging"]),
                check=True)
        path = stage_file(child, machine, "message.md", message)
        argv = [machine.dispatch, "continue", rec["remote_id"],
                "--message-file", path, "--bg"]
        if opts.deadline:
            argv += ["--deadline", opts.deadline]
        child = start_on_machine(child, machine, argv)
    except BaseException:
        close_failed_launch(child, handle)
        raise
    release_run_lock(handle)
    return child if opts.bg else wait_for_remote(child, machine)


def abandon_unreachable(rec, error):
    """Settle a remote run here when the operator gives up on reaching its machine.

    Only ever the operator's call, and it is loud: this record is a pointer, so
    closing it frees a cap slot for a worker that may still be running on a
    machine this one cannot currently see.
    """
    rec["state"] = "orphaned"
    rec["finished"] = utc_now()
    rec["closed_by"] = "kill"
    rec["remote_error"] = error
    save_record(rec)
    return rec


# --------------------------------------------------------------------------
# shell mode
# --------------------------------------------------------------------------


def remote_tmux_command(machine):
    """Wrap a tmux argv so it runs on the machine instead of this one.

    The tmux substrate takes this as its `command_builder` and is otherwise the
    substrate a local run uses: same panes, same screen reads, same exit-code
    probe, one ssh in front of each call. The argv is quoted into a single remote
    command because ssh hands its arguments to a shell, which would otherwise
    re-split a pane title or a line of typed text on its spaces.
    """
    def build(argv):
        return ssh_argv(machine, remote_command(argv))

    return build


def shell_substrate(machine):
    """The tmux substrate, pointed at the machine's tmux over one ssh connection."""
    from .substrates import get_substrate

    try:
        return get_substrate("tmux", command_builder=remote_tmux_command(machine))
    except NotImplementedError as exc:
        raise DispatchError(
            f'machine {machine.name} has mode = "{SHELL_MODE}", which drives a '
            f"remote tmux: {exc}") from exc


def check_shell_options(machine, opts):
    """Shell mode shares no filesystem with this machine, so it needs a `--dir`.

    A local run defaults the sandbox root to the current directory. That path
    means nothing on the machine, and assuming it exists there is how a worker
    ends up sandboxed to a directory nobody meant.

    `--image` is refused for the same reason and cannot be staged around it: the
    path is an argv element of the CLI's own command line, built before the run
    has a directory on the machine to put the file in.
    """
    if not opts.dir:
        raise DispatchError(
            f"machine {machine.name} has no directory in common with this one; "
            "pass --dir <path on the machine>")
    if opts.image:
        raise DispatchError(
            f'machine {machine.name} has mode = "{SHELL_MODE}", which shares no '
            "filesystem with this one, so --image has no path there; place the "
            f'run on a machine with mode = "{DISPATCH_MODE}", or run it here')


def stage_shell_run(rec):
    """Put a shell-mode run's brief and prompt on the machine, before it starts.

    The pane is on a box with a filesystem of its own, so a brief left here is a
    brief the worker cannot read and a prompt naming this machine's paths is a
    prompt it cannot follow. Both files go up, and the prompt built for this run
    already names their paths there.
    """
    staging = rec.get("remote_staging")
    if not staging:
        return ""
    machine = machine_of_record(rec)
    run_ssh(machine, mkdir_command(machine, staging), check=True)
    directory = Path(rec["dir"])
    scp_up(machine, [directory / "brief.md", directory / "prompt.txt"], staging)
    append_status(directory / "status.log",
                  f"STAGED {utc_now()} {machine.name}:{staging}")
    return staging


def fetch_deliverable(rec):
    """Mirror a shell-mode run's deliverable down. True when it changed.

    Everything dispatch asks about a deliverable (has it landed, has it stopped
    changing, was it written during this turn) is a question about a local file,
    and the worker's answer is on the machine. So the local copy is refreshed
    on every poll, and rewritten only when the bytes differ: its mtime is what
    the quiet window is measured with, and a copy taken every poll would restart
    that window every poll.

    A mirror that cannot be taken is not a run that has failed: the poll after
    it asks again, and if the machine is really gone the pane calls that before
    a missing copy would.
    """
    staging = rec.get("remote_staging")
    if not staging or rec.get("remote_supervised"):
        return False
    name = "out.json" if rec.get("schema") else "out.md"
    target = Path(rec["dir"]) / name
    scratch = Path(rec["dir"]) / f".{name}.fetched"
    try:
        if not scp_down(machine_of_record(rec), posixpath.join(staging, name),
                        scratch, seconds=FETCH_SECONDS):
            return False
        fetched = scratch.read_bytes()
        if target.is_file() and target.read_bytes() == fetched:
            return False
        target.write_bytes(fetched)
        return True
    except (DispatchError, OSError):
        return False
    finally:
        with contextlib.suppress(OSError):
            scratch.unlink()
