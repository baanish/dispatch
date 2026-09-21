"""Lanes: `key@effort[:tier]`, and the default table config replaces.

A lane is the whole routing decision in one token: which vendor CLI runs, which
model it runs, how hard it thinks, and which service tier it bills against.
Nothing else in the tree parses that string.

DEFAULT_LANE_TABLE is data, in one place, on purpose. It is the set a fresh
install starts with, not a fixed list of what dispatch can run: a user's
`[lanes]` config table replaces it entry by entry, so adding a model is an edit
to a TOML file rather than a patch to this module. `lane_table()` is what every
caller reads, and `set_lane_table()` is what config calls once at startup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import DispatchError

# A lane is `word@word[:word]` and nothing else. Matching on a bare `@` swallows
# real brief paths (`reports@v2/brief.md`).
LANE_TEXT_RE = re.compile(r"^[a-z0-9]+@[a-z0-9]+(:[a-z0-9]+)?$")


@dataclass(frozen=True)
class LaneSpec:
    """One row of the lane table: a model, and the shapes it is offered in."""

    driver: str               # which vendor CLI runs it: codex, claude, grok
    model: str
    efforts: tuple            # the reasoning efforts this model accepts
    tier: str = ""            # the standing service tier; empty where the driver has none
    alt_tiers: tuple = ()     # tiers a `:suffix` may select, e.g. `luna@high:priority`
    tier_aliases: dict = None  # accepted spellings of an offered tier, not advertised
    machine: str = ""         # a machine name this lane always runs on, or local


@dataclass(frozen=True)
class Lane:
    """A resolved lane: the table row plus the effort and tier that were asked for."""

    name: str        # canonical lane string, e.g. "sol@medium" or "luna@high:priority"
    key: str         # table key, e.g. "sol"
    driver: str
    model: str
    effort: str
    tier: str
    machine: str = ""


# The lane set a fresh install starts with. Speed is bought only when the caller
# is waiting on it, so a faster tier is a `:suffix` rather than the standing one.
DEFAULT_LANE_TABLE = {
    "luna": LaneSpec("codex", "gpt-5.6-luna", ("high", "xhigh", "max"),
                     tier="priority", alt_tiers=("default", "priority"),
                     tier_aliases={"fast": "priority"}),
    "sol": LaneSpec("codex", "gpt-5.6-sol", ("medium", "high", "xhigh", "max"),
                    tier="default", alt_tiers=("default", "priority"),
                    tier_aliases={"fast": "priority"}),
    "astra": LaneSpec("codex", "gpt-6-astra", ("medium", "high", "xhigh"),
                      tier="default", alt_tiers=("default", "priority"),
                      tier_aliases={"fast": "priority"}),
    "opus": LaneSpec("claude", "claude-opus-5", ("medium", "high")),
    "grok": LaneSpec("grok", "grok-4.6", ("high",)),
}

DEFAULT_LANE = "astra@medium"

_LANE_TABLE = dict(DEFAULT_LANE_TABLE)
_DEFAULT_LANE = DEFAULT_LANE


def lane_table():
    """The lane set in force: the default one until config replaces it."""
    return _LANE_TABLE


def set_lane_table(table):
    """Replace the lane set wholesale. Config's entry point; returns the old one."""
    global _LANE_TABLE
    previous = _LANE_TABLE
    _LANE_TABLE = dict(table)
    return previous


def default_lane():
    """The lane `dispatch run` takes when the caller names none."""
    return _DEFAULT_LANE


def set_default_lane(text):
    """Replace the default lane. Config's entry point; returns the old one."""
    global _DEFAULT_LANE
    previous = _DEFAULT_LANE
    _DEFAULT_LANE = str(text or "") or DEFAULT_LANE
    return previous


def lane_names(table=None):
    """Every valid lane string, in table order, tier variants after their base."""
    names = []
    for key, spec in (lane_table() if table is None else table).items():
        for effort in spec.efforts:
            names.append(f"{key}@{effort}")
            for alt in spec.alt_tiers:
                names.append(f"{key}@{effort}:{alt}")
    return names


def resolve_lane(text, table=None):
    """Parse `key@effort[:tier]`. An unknown lane is a hard error, no fuzzy match.

    `table` names a lane set other than the one in force, which is what lets
    config validate a board against the lanes it is about to install.
    """
    raw = (text or "").strip()
    tier_suffix = ""
    if ":" in raw:
        raw, tier_suffix = raw.rsplit(":", 1)
    if "@" not in raw:
        raise DispatchError(f"not a lane: {text!r}. {valid_lanes_hint(table)}")
    key, effort = raw.split("@", 1)
    spec = (lane_table() if table is None else table).get(key)
    if spec is None or effort not in spec.efforts:
        raise DispatchError(f"unknown lane: {text!r}. {valid_lanes_hint(table)}")
    tier = spec.tier
    if tier_suffix:
        tier_suffix = (spec.tier_aliases or {}).get(tier_suffix, tier_suffix)
        if tier_suffix not in spec.alt_tiers:
            raise DispatchError(f"unknown lane: {text!r}. {valid_lanes_hint(table)}")
        tier = tier_suffix
    name = f"{key}@{effort}" + (f":{tier_suffix}" if tier_suffix else "")
    return Lane(name=name, key=key, driver=spec.driver, model=spec.model,
                effort=effort, tier=tier, machine=spec.machine)


def valid_lanes_hint(table=None):
    return "valid lanes: " + ", ".join(lane_names(table))


def looks_like_lane(text):
    """Does this argument name a lane, or a file that happens to look like one?

    An existing file always wins the tie, so `dispatch run reports@v2/brief.md`
    reads the brief instead of refusing an unknown lane.
    """
    if not LANE_TEXT_RE.match(text or ""):
        return False
    try:
        return not Path(text).exists()
    except (OSError, ValueError):
        return True
