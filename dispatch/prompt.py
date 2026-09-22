"""What dispatch types into a worker, and why it is never the brief itself.

The brief goes in by path. A 20k-character paste into a TUI is a rendering
hazard, the file is already durable in the run directory, and a prompt that is
not an argv element cannot smuggle a flag onto a command line.

The worker's result is a file, not a screen: a substrate scrapes a bounded
number of lines out of a terminal and a TUI redraws over its own history, so
asking the worker to write its answer down is the only lossless channel. That
instruction lives here rather than in the brief, so a brief stays the caller's
words only.

The preamble is the part a user owns. It is the standing context every worker
gets before the brief, and config replaces it wholesale: `set_preamble()` is
that entry point, and the default says only what is true of every dispatched
worker.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_PREAMBLE = (
    "You are a dispatched worker. The brief is complete: do not go looking for "
    "the instructions or documentation that sent you here, which exist for "
    "sending workers out rather than for being one. This exchange is "
    "agent-to-agent, so skip standing rules about text a person will read "
    "unless the brief names a human reader."
)

_PREAMBLE = DEFAULT_PREAMBLE


def preamble():
    return _PREAMBLE


def set_preamble(text):
    """Replace the standing preamble. Config's entry point; returns the old one."""
    global _PREAMBLE
    previous = _PREAMBLE
    _PREAMBLE = str(text or "")
    return previous


RESULT_PROMPT = (
    "Read and follow the brief at {brief}. {preamble}\n\n"
    "When the work is done, write your final answer to {out} and then exit the "
    "session. Only {out} is captured: anything left on screen is not a "
    "deliverable.\n"
)

SCHEMA_RESULT_PROMPT = (
    "Read and follow the brief at {brief}. {preamble}\n\n"
    "When the work is done, write JSON conforming to the schema below to "
    "{out_json} (JSON only, no prose, no code fence), then exit the session. "
    "Only {out_json} is captured: anything left on screen is not a "
    "deliverable.\n\nJSON Schema:\n{schema}\n"
)

# Validate-and-steer: the contract is checked after the fact and the worker is
# asked to fix its own output while it still has the context.
SCHEMA_REPAIR_PROMPT = (
    "The JSON at {out_json} does not satisfy the contract: {error}\n\n"
    "Rewrite {out_json} so it parses and conforms to the schema, then exit the "
    "session."
)

STEER_PROMPT = "Course correction from the operator: {message}"

# A turn ending is not a run ending, so a turn that ends with nothing on disk
# gets one reminder rather than an exit command. One, and never a drumbeat: the
# worker is usually waiting on something, and a second brief typed into that is
# noise.
# The nudge names the brief again because the first prompt can be lost at
# startup: the TUI reports a busy turn, nothing reaches the model, and a
# reminder that only names the deliverable gets "I have no task" back.
NUDGE_PROMPT = (
    "Your turn ended without writing {path}. If you have not yet read the brief "
    "at {brief}, read and follow it now. Then write {path}, or write a one-line "
    "reason there, and stop."
)

# What the fallback types when the substrate cannot deliver a prompt through its
# own agent channel. Deliberately a single line naming a file: the keystrokes
# may be going into a shell rather than a TUI, and a multi-line prompt typed at
# a shell prompt is a series of commands, not a brief.
FALLBACK_PROMPT_LINE = "Read and follow the instructions written in {path}"


def build_prompt(brief_path, paths, schema_path=""):
    """The text a worker is handed once its TUI is ready to read it."""
    if schema_path:
        return SCHEMA_RESULT_PROMPT.format(
            brief=brief_path, out_json=paths["out_json"], preamble=preamble(),
            schema=Path(schema_path).read_text(encoding="utf-8"))
    return RESULT_PROMPT.format(brief=brief_path, out=paths["out"],
                                preamble=preamble())


def repair_prompt(out_json, error):
    return SCHEMA_REPAIR_PROMPT.format(out_json=out_json, error=error)


def steer_prompt(message):
    return STEER_PROMPT.format(message=message)


def nudge_prompt(path, brief):
    return NUDGE_PROMPT.format(path=path, brief=brief)


def fallback_prompt_line(path):
    return FALLBACK_PROMPT_LINE.format(path=path)
