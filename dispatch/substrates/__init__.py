"""Places a worker process can live.

`get_substrate(name)` returns the one asked for; `detect_substrate()` picks the
first that is usable, in the order herdr, tmux, headless, which is best to worst
by what each can honestly offer.
"""

from .base import (Substrate, SubstrateCapabilities, SubstrateError, SpawnResult,
                   Worker, WorkerProcess, WorkerSetupError, WorkerStatus)
from .headless import HeadlessSubstrate
from .herdr import HerdrSubstrate
from .tmux import TmuxSubstrate
from ..errors import DispatchError

SUBSTRATES = {
    "herdr": HerdrSubstrate,
    "tmux": TmuxSubstrate,
    "headless": HeadlessSubstrate,
}

DETECT_ORDER = ("herdr", "tmux", "headless")


def get_substrate(name, **kwargs):
    factory = SUBSTRATES.get(name)
    if factory is None:
        raise DispatchError(
            f"no substrate named {name!r}; known substrates: "
            + ", ".join(sorted(SUBSTRATES)))
    return factory(**kwargs)


def detect_substrate(**kwargs):
    """The first usable substrate, best first.

    Two ways a candidate is skipped, and they mean the same thing here: one that
    is not built raises NotImplementedError from its constructor, and one whose
    daemon is not listening answers False to `available()`. Headless is last and
    always available, so detection lands somewhere.

    Detection is for a fresh run only. A recorded run reopens the substrate it
    named, which is what keeps a herdr run from being silently reconciled by a
    substrate that never held it.
    """
    for name in DETECT_ORDER:
        try:
            candidate = get_substrate(name, **kwargs)
        except (NotImplementedError, DispatchError):
            continue
        if candidate.available():
            return candidate
    raise DispatchError(
        "no usable substrate: none of herdr, tmux, or headless can run here. "
        "Pin one with `substrate = \"...\"` in your config to see why it refuses.")


__all__ = ["Substrate", "SubstrateCapabilities", "SubstrateError", "SpawnResult",
           "Worker", "WorkerProcess", "WorkerSetupError", "WorkerStatus",
           "HerdrSubstrate", "TmuxSubstrate", "HeadlessSubstrate",
           "SUBSTRATES", "DETECT_ORDER", "get_substrate", "detect_substrate"]
