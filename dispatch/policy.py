"""Policy: the values that decide what a run is allowed to do.

Every number and switch a user can reasonably want to change lives on one
`Policy` record, so config has a single object to fill and the rest of the tree
has a single place to read. `policy()` is what callers read; `set_policy()` is
what config calls once at startup.

The depth ladder lives here too, because it is the one policy that is enforced
against the environment rather than against a config file: a worker inherits
`AGENT_DEPTH` from whatever spawned it, and refusing to guess when that marker
is unreadable is the whole safeguard.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace

from .errors import DispatchError

# The ladder marker a spawned worker inherits. Absent means top seat.
DEPTH_ENV = "AGENT_DEPTH"

# Nothing at this rung may spawn, so no run ever reaches one below it.
MAX_DEPTH = 2

FOREVER = "forever"


@dataclass(frozen=True)
class Policy:
    """What a run may cost, how long it may wait, and who may spawn it."""

    # Two limits, not one. Only workers consume slots; orchestrator processes
    # never do.
    machine_cap: int = 16
    session_cap: int = 4

    # Every run has a check-in deadline, foreground ones included: a worker that
    # ends its turn with no deliverable waits rather than failing, so without
    # check-ins a foreground run could hold the terminal forever. Check-ins
    # extend a working worker unboundedly, so this caps nothing.
    default_deadline: str = "30m"

    # How long a run may stand at a dialog only a human can answer before it is
    # given up on. `forever` is allowed and is what an operator sitting at the
    # keyboard wants; the default exists so an unattended fleet does not hold
    # cap slots overnight on a question nobody saw.
    hand_timeout: str = "30m"

    # Blank the driver's metered key variables in the worker's environment and
    # read them back before starting the CLI. Opt-in: it is the right default
    # for subscription-billed lanes, where an ambient key silently moves the
    # lane onto per-token billing, and the wrong one for anybody deliberately
    # running on an API key.
    blank_metered_keys: bool = False

    # Extra variables every worker starts with, as (name, value) pairs. The
    # shipped one is for any `claude` a worker runs, whether the worker is one
    # or spawns one with `claude -p`: to Claude Code that is a main
    # conversation, which on a subscription defaults to the one-hour prompt
    # cache, whose writes cost 2x base input against 1.25x for the five-minute
    # one. A worker's turns land minutes apart at most, whether that is the brief
    # run to the end, a `steer` inside the same run, or a `continue` opening a
    # new run on the session, so each read refreshes the entry for free and the
    # rest of the hour is paid for on every turn's write and never used.
    worker_env: tuple = (("CLAUDE_CODE_PROMPT_CACHE_TTL", "5m"),)

    # Which lanes a depth-1 worker may spawn, by lane key. The board replaces
    # this with whatever fills its `light` slot; until then it is the set a
    # bulk mechanical subtask is cheap enough to run on.
    depth1_lane_keys: tuple = ("luna",)

    # Terminal states, in one place because several modules ask.
    terminal_states: tuple = field(
        default=("done", "failed", "aborted", "killed", "timeout", "orphaned"))


_POLICY = Policy()


def policy():
    return _POLICY


def set_policy(new_policy):
    """Replace the policy in force. Returns the previous one."""
    global _POLICY
    previous = _POLICY
    _POLICY = new_policy
    return previous


def with_policy(**changes):
    """Set a policy that differs from the current one in these fields only."""
    return set_policy(replace(_POLICY, **changes))


def parse_deadline(text):
    """`30m`, `90s`, `2h`, or bare seconds to an int number of seconds."""
    raw = str(text).strip().lower()
    match = re.fullmatch(r"(\d+)([smh]?)", raw)
    if not match:
        raise DispatchError(f"bad deadline: {text!r} (use 45s, 30m, 2h)")
    value = int(match.group(1))
    return value * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def hand_timeout_seconds():
    """How long to wait on a human's hand, or None for `forever`."""
    raw = str(policy().hand_timeout or "").strip().lower()
    if raw in ("", FOREVER, "never", "0"):
        return None
    return parse_deadline(raw)


def current_depth():
    """This process's rung on the ladder. Fail closed on an unreadable marker.

    Absent means top seat (0). Anything else must be a non-negative integer:
    `-1` and `xyz` would otherwise sail past every depth check.
    """
    raw = os.environ.get(DEPTH_ENV)
    if raw is None or raw.strip() == "":
        return 0
    text = raw.strip()
    if not re.fullmatch(r"\d+", text):
        raise DispatchError(
            f"{DEPTH_ENV}={raw!r} is not a non-negative integer; refusing to guess "
            "where this run sits on the depth ladder")
    return int(text)


def child_depth():
    """The rung a worker spawned from here sits on."""
    return current_depth() + 1


def enforce_depth(lane, command="run"):
    """Refuse a spawn the ladder does not allow."""
    depth = current_depth()
    if depth >= MAX_DEPTH:
        raise DispatchError(
            f"{DEPTH_ENV}={depth}: depth {MAX_DEPTH} spawns nothing, so no run "
            f"reaches depth {MAX_DEPTH + 1}")
    if depth == 1 and lane is not None:
        allowed = policy().depth1_lane_keys
        if lane.key not in allowed:
            raise DispatchError(
                f"{DEPTH_ENV}=1: lane {lane.name} refused; a depth-1 worker may "
                f"spawn only {', '.join(allowed) or 'nothing'}")
