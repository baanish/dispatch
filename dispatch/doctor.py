"""`dispatch doctor`: whether this machine can actually run the configured board.

Three questions, and no more: is each lane's CLI installed and logged in, is
each machine reachable, and which substrate would a run land in. Everything it
runs is non-interactive, bounded by a timeout, and started with stdin closed. A
vendor CLI opened on a terminal here would sit at its TUI forever, and a probe
that costs a model call is not a health check.

A driver that declares no login probe reports "installed" and says so. Reporting
less than was checked is the point: a green line that only means the binary
exists is worse than a line that says so.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, replace

from .drivers import get_driver
from .remote import remote_command

PROBE_TIMEOUT = 15
SSH_TIMEOUT = 20
SSH_CONNECT_SECONDS = 5
# ssh must never ask for a password or a host key: an unattended doctor that
# blocks on a prompt looks exactly like an unreachable machine, minutes later.
SSH_BATCH = ("-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_SECONDS}")


@dataclass(frozen=True)
class Check:
    """One line of the report: what was checked, and what came back."""

    name: str
    subject: str
    ok: bool
    detail: str


def run_probe(argv, timeout=PROBE_TIMEOUT):
    """Run a probe with no stdin and a deadline. Returns (ok, first line)."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as exc:
        return False, str(exc)
    output = (done.stdout or "") + (done.stderr or "")
    first = next((line.strip() for line in output.splitlines() if line.strip()), "")
    return done.returncode == 0, first


def driver_check(driver):
    """Is this CLI installed, and does its own probe say it is logged in."""
    binary = driver.cli_binary or driver.name
    found = shutil.which(binary)
    if not found:
        return Check(driver.name, binary, False, f"{binary} not on PATH")
    if not driver.login_probe:
        return Check(driver.name, binary, True,
                     f"installed at {found} (no login probe that is free)")
    argv = list(driver.login_probe)
    if argv[0] == binary:
        argv[0] = found
    ok, first = run_probe(argv)
    probe = " ".join(driver.login_probe)
    if ok:
        return Check(driver.name, binary, True, f"installed at {found}, logged in")
    return Check(driver.name, binary, False,
                 f"installed at {found}, but `{probe}` failed"
                 + (f": {first}" if first else ""))


def lane_checks(config):
    """One check per lane key, the driver probe run once per driver."""
    probed = {}
    checks = []
    for key, spec in config.lanes.items():
        if spec.driver not in probed:
            probed[spec.driver] = driver_check(get_driver(spec.driver))
        checks.append(replace(probed[spec.driver], name=key))
    return checks


def machine_check(machine):
    """ssh reaches it, and in dispatch mode it has a dispatch to run."""
    ok, first = run_probe(["ssh", *SSH_BATCH, machine.ssh, "exit 0"],
                          timeout=SSH_TIMEOUT)
    if not ok:
        return Check(machine.name, machine.ssh, False,
                     "ssh failed" + (f": {first}" if first else ""))
    if machine.mode != "dispatch":
        return Check(machine.name, machine.ssh, True,
                     f"ssh ok, mode {machine.mode} (driven from here)")
    ok, first = run_probe(["ssh", *SSH_BATCH, machine.ssh,
                           remote_command([machine.dispatch, "--version"],
                                          shell=machine.shell)], timeout=SSH_TIMEOUT)
    if not ok:
        return Check(machine.name, machine.ssh, False,
                     f"ssh ok, but `{machine.dispatch} --version` failed"
                     + (f": {first}" if first else ""))
    return Check(machine.name, machine.ssh, True, f"ssh ok, remote {first}")


def machine_checks(config):
    return [machine_check(machine) for machine in config.machines.values()]


def render_report(config, substrate="", version=""):
    """The whole report as text, plus whether everything checked passed."""
    lanes = lane_checks(config)
    machines = machine_checks(config)
    lines = [f"dispatch {version}".strip(),
             f"config: {config.path or 'built-in defaults'}",
             f"substrate: {substrate or 'none reachable'}"]

    lines.append("")
    lines.append("lanes")
    lines.extend(_rows(lanes, "no lanes configured"))
    lines.append("")
    lines.append("machines")
    lines.extend(_rows(machines, "none configured, every run is local"))

    ok = all(check.ok for check in lanes + machines) and bool(substrate)
    return "\n".join(lines) + "\n", ok


def _rows(checks, empty):
    if not checks:
        return [f"  {empty}"]
    width = max(len(check.name) for check in checks)
    return [f"  {'ok ' if check.ok else 'BAD'}  {check.name.ljust(width)}  "
            f"{check.detail}" for check in checks]
