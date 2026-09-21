"""The board: five role slots, and the lane that fills each one.

A slot is a role an orchestrating agent routes by, and a lane is the model that
plays it. The indirection is the point: an agent asks for `high` and gets
whatever the operator decided `high` costs today, so changing a model is a
config edit rather than an edit to every brief that names one.

The slot set is fixed at five. A sixth would be a new role nobody has a rule
for, and an agent that cannot say when to pick a slot will not pick it.

`render_board` is the only rendering in the tree that both a human and an agent
read, which is why it is a plain aligned table: no colour, no box drawing, one
row per slot, and a `when` column that says what the slot is for.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DispatchError
from .lanes import Lane, resolve_lane

SLOTS = ("light", "medium", "high", "blindspot", "genius")

# What each slot is for, in the words an agent routes by. A per-slot note in
# config replaces the line for that slot; these are what an unnoted board says.
SLOT_PURPOSE = {
    "light": "bulk mechanical work, and all a depth-1 worker may spawn",
    "medium": "the default worker: well-scoped execution and investigation",
    "high": "work meant to be merged, or whose shape outlives the task",
    "blindspot": "adversarial review and second opinions",
    "genius": "the hardest single call, at the highest cost",
}

EMPTY = "-"

COLUMNS = ("slot", "lane", "driver", "model", "effort", "tier", "machine", "when")


@dataclass(frozen=True)
class BoardRow:
    """One slot, resolved. `lane` is None for a slot the operator left empty."""

    slot: str
    lane: Lane | None
    note: str


def board_rows(config):
    """Every slot in fixed order, resolved against this config's lane table."""
    rows = []
    for slot in SLOTS:
        text = (config.board.get(slot) or "").strip()
        lane = resolve_lane(text, config.lanes) if text else None
        note = (config.board_notes.get(slot) or "").strip()
        rows.append(BoardRow(slot=slot, lane=lane,
                             note=note or SLOT_PURPOSE.get(slot, "")))
    return rows


def slot_lane(config, slot):
    """The lane filling a slot. Refuses an unknown slot and an empty one."""
    if slot not in SLOTS:
        raise DispatchError(
            f"no board slot named {slot!r}; slots: {', '.join(SLOTS)}")
    text = (config.board.get(slot) or "").strip()
    if not text:
        raise DispatchError(
            f"board slot {slot!r} is empty; fill it in [board] in "
            f"{config.path or 'your config'}")
    return resolve_lane(text, config.lanes)


def render_board(config):
    """The board as one aligned table, for a human and an agent both."""
    cells = [list(COLUMNS)]
    for row in board_rows(config):
        lane = row.lane
        cells.append([
            row.slot,
            lane.name if lane else EMPTY,
            lane.driver if lane else EMPTY,
            lane.model if lane else EMPTY,
            lane.effort if lane else EMPTY,
            (lane.tier or EMPTY) if lane else EMPTY,
            (lane.machine or "local") if lane else EMPTY,
            row.note if lane else "unfilled",
        ])
    widths = [max(len(row[i]) for row in cells) for i in range(len(COLUMNS))]
    lines = [f"board: {config.path or 'built-in defaults'}"]
    if config.path is None:
        lines.append("no config yet: `dispatch init` fills these slots from a "
                     "preset")
    lines.append("")
    for row in cells:
        lines.append("  ".join(value.ljust(widths[i]) if i < len(COLUMNS) - 1
                               else value
                               for i, value in enumerate(row)).rstrip())
    return "\n".join(lines) + "\n"
