"""Fake `ssh` and `scp` executables, and the test case that installs them.

No test reaches a real machine, so the two programs dispatch shells out to are
replaced by scripts on PATH. They are not stubs that only record: the pair
models one remote filesystem in a temp directory, so `mkdir -p`, `cat`, and both
directions of `scp` really move files. That is what lets a test assert on what
was staged and on what came back at terminal state, rather than on argv alone.

Anything else the remote shell is asked to run (`dispatch run --bg`, `kill`,
`steer`) answers from a scripted reply table, matched on a substring of the
command. Each call is appended to a log the test reads back as JSON lines.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dispatch import caps, cli, config, policy, records, remote  # noqa: E402
from dispatch.substrates import herdr  # noqa: E402

LOG_ENV = "FAKE_SSH_LOG"
REPLIES_ENV = "FAKE_SSH_REPLIES"
REMOTE_ROOT_ENV = "FAKE_REMOTE_ROOT"

# The two programs are one script under two names: both log, both resolve remote
# paths the same way, and which half runs is decided by argv[0].
FAKE_PROGRAM = '''#!{python}
import json, os, shlex, shutil, sys
from pathlib import Path

TOOL = Path(sys.argv[0]).name
ARGV = sys.argv[1:]
ROOT = Path(os.environ["{remote_root}"])


def log(entry):
    entry["tool"] = TOOL
    with open(os.environ["{log}"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\\n")


def local_path(remote):
    text = remote[2:] if remote.startswith("~/") else remote.lstrip("/")
    return ROOT / text


def replies():
    path = os.environ.get("{replies}", "")
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def run_ssh():
    command = ARGV[-1] if ARGV else ""
    log({{"argv": ARGV, "command": command}})
    # The last stage of a compound command is the one that produces output; the
    # reconcile in front of it is discarded on the far side too.
    tail = command.rsplit("; ", 1)[-1].strip()
    words = shlex.split(tail)
    if words[:1] == ["cat"]:
        path = local_path(words[1])
        if not path.is_file():
            sys.stderr.write("cat: %s: No such file or directory\\n" % words[1])
            return 1
        sys.stdout.write(path.read_text(encoding="utf-8"))
        return 0
    if words[:2] == ["mkdir", "-p"]:
        local_path(words[2]).mkdir(parents=True, exist_ok=True)
        return 0
    for match, reply in replies():
        if match in command:
            sys.stdout.write(reply.get("stdout", ""))
            sys.stderr.write(reply.get("stderr", ""))
            return reply.get("rc", 0)
    return 0


def split_spec(spec):
    # scp speaks SFTP, so the remote half is a literal pathname, not a shell word.
    if ":" not in spec:
        return "", spec
    host, path = spec.split(":", 1)
    return host, path


def run_scp():
    paths = [a for a in ARGV if not a.startswith("-")]
    # Every option dispatch passes takes a value, so the values are dropped with
    # them; what is left is the sources and the destination.
    values = set()
    for index, item in enumerate(ARGV):
        if item.startswith("-") and index + 1 < len(ARGV):
            values.add(ARGV[index + 1])
    paths = [p for p in paths if p not in values]
    log({{"argv": ARGV, "paths": paths}})
    sources, dest = paths[:-1], paths[-1]
    dest_host, dest_path = split_spec(dest)
    for source in sources:
        source_host, source_path = split_spec(source)
        origin = local_path(source_path) if source_host else Path(source_path)
        if not origin.is_file():
            sys.stderr.write("scp: %s: No such file or directory\\n" % source)
            return 1
        target = local_path(dest_path) if dest_host else Path(dest_path)
        if dest.endswith("/") or target.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            target = target / origin.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, target)
    return 0


sys.exit(run_scp() if TOOL == "scp" else run_ssh())
'''


class FakeSshTestCase(unittest.TestCase):
    """An isolated DISPATCH_HOME, one fake machine, and no real ssh anywhere."""

    machine_name = "box"
    machine_ssh = "operator@box"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.brief = self.work / "brief.md"
        self.brief.write_text("do the thing\n", encoding="utf-8")

        self.env_backup = dict(os.environ)
        self.cwd_backup = os.getcwd()
        self.home = self.root / "dispatch-home"
        os.environ["DISPATCH_HOME"] = str(self.home)
        os.environ["AGENT_DEPTH"] = "0"
        os.environ["DISPATCH_SESSION"] = "test-session"
        # Nothing in these cases needs a substrate, and a real herdr must never
        # be started to find that out.
        os.environ[herdr.SPAWN_ENV] = "0"
        os.environ[herdr.SOCKET_ENV] = str(self.root / "no-such-herdr.sock")

        self.remote_root = self.root / "remote-fs"
        self.remote_root.mkdir()
        self.ssh_log = self.root / "ssh.log"
        self.replies_path = self.root / "replies.json"
        self.set_replies([])
        self.install_fakes()

        self.config_path = self.root / "config.toml"
        os.environ[config.CONFIG_ENV] = str(self.config_path)
        self.machines_backup = remote.machine_table()
        self.policy_backup = policy.policy()
        self.write_config()
        self.machine = remote.machine_for(self.machine_name)
        os.chdir(self.work)
        self.addCleanup(self._restore)

    def write_config(self, extra=""):
        """The config file this process and `cli.main` both read.

        A machine is described in config and nowhere else, so a case that wants
        a second one writes it here rather than installing a table of its own:
        `cli.main` reloads the file, and would drop anything not in it.
        """
        self.config_path.write_text(
            f"[machines.{self.machine_name}]\n"
            f'ssh = "{self.machine_ssh}"\nhome = "~/.dispatch"\n' + extra,
            encoding="utf-8")
        return config.load_and_apply()

    def _restore(self):
        remote.set_machines(self.machines_backup)
        policy.set_policy(self.policy_backup)
        for handle in list(records.HELD_LOCKS):
            records.release_run_lock(handle)
        os.chdir(self.cwd_backup)
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    # -- fakes -----------------------------------------------------------

    def install_fakes(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        program = FAKE_PROGRAM.format(python=sys.executable, log=LOG_ENV,
                                      replies=REPLIES_ENV,
                                      remote_root=REMOTE_ROOT_ENV)
        for name in ("ssh", "scp"):
            path = bin_dir / name
            path.write_text(program, encoding="utf-8")
            path.chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        os.environ[LOG_ENV] = str(self.ssh_log)
        os.environ[REPLIES_ENV] = str(self.replies_path)
        os.environ[REMOTE_ROOT_ENV] = str(self.remote_root)

    def set_replies(self, replies):
        """`[[match, {stdout, stderr, rc}], ...]`, first match wins."""
        self.replies_path.write_text(json.dumps(replies), encoding="utf-8")

    def calls(self, tool=None):
        if not self.ssh_log.is_file():
            return []
        entries = [json.loads(line) for line in
                   self.ssh_log.read_text(encoding="utf-8").splitlines() if line]
        return [e for e in entries if tool is None or e["tool"] == tool]

    def commands(self):
        return [call["command"] for call in self.calls("ssh")]

    # -- the machine's side ----------------------------------------------

    def remote_file(self, path, text):
        """Put a file on the fake machine, at a path as the remote shell sees it."""
        target = self.remote_root / (path[2:] if path.startswith("~/")
                                     else path.lstrip("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def remote_record(self, run_id, **fields):
        """Write the run record the machine's own dispatch would have written."""
        rec = {"id": run_id, "lane": "astra@medium", "state": "running",
               "dir": f"~/.dispatch/runs/{run_id}", "rc": None, "finished": ""}
        rec.update(fields)
        self.remote_file(f"~/.dispatch/runs/{run_id}/run.json",
                         json.dumps(rec, indent=2) + "\n")
        return rec

    def launched(self, run_id="astra@medium-120000-ab12", extra=()):
        """Answer the launch, and give the machine a record for what it started."""
        remote_dir = f"~/.dispatch/runs/{run_id}"
        self.set_replies([["dispatch run", {"stdout": f"{run_id}\n{remote_dir}\n"}],
                          *extra])
        self.remote_record(run_id)
        return run_id, remote_dir

    def run_remote_bg(self, *extra_argv):
        """`dispatch run --on box --bg`, returning the local record."""
        code = cli.main(["run", "astra@medium", str(self.brief), "--bg",
                         "--on", self.machine_name, *extra_argv])
        self.assertEqual(code, cli.EXIT_OK)
        return records.all_records()[-1]

    def live(self):
        return caps.live_records()

    # -- running the CLI -------------------------------------------------

    def main_out(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(argv)
        self.assertEqual(code, cli.EXIT_OK, buffer.getvalue())
        return buffer.getvalue()

    def main_fails(self, argv):
        """Run a command that must be refused; returns what the operator sees."""
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(argv)
        self.assertEqual(code, cli.EXIT_USAGE, buffer.getvalue())
        return buffer.getvalue()
