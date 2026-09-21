"""The Substrate interface: where a worker process lives, and how to reach it.

A substrate owns a worker's home. It creates one, starts a CLI in it with a
given environment and working directory, types into it, reads what it has drawn,
reports what its processes are doing, and closes it. It owns no policy: which
CLI runs, when a turn is over, and when a run ends are the driver's and the
runner's business.

Three substrates ship: `herdr` (a terminal multiplexer whose daemon owns real
PTYs), `tmux`, and `headless` (a plain subprocess). They differ in what they can
honestly offer, which is what `capabilities` is for. The runner asks before it
promises: a headless run cannot be steered and cannot have a dialog answered for
it, so `dispatch steer` refuses with a one-line reason there instead of typing
into a void.

To add a substrate, subclass `Substrate`, set `capabilities`, and implement the
methods marked required below. The optional ones have honest defaults: they
answer "cannot tell", and the capability record is what stops the runner from
believing that answer means "no".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import DispatchError


class SubstrateError(DispatchError):
    """The substrate could not do what was asked. Operator-facing."""


class WorkerSetupError(SubstrateError):
    """A fresh worker home died or never reached a shell before the CLI started.

    Its own class so that a retryable birth failure (a race between concurrent
    creates) can be told apart from a refusal that must stand, like an
    environment the shell keeps re-poisoning.
    """


@dataclass(frozen=True)
class SubstrateCapabilities:
    """What this substrate can honestly do, asked before anything is promised."""

    # A live worker can be interrupted and typed at mid-run.
    can_steer: bool = False
    # A finished session can be reopened in a new home and attached to.
    can_inspect: bool = False
    # `read_screen` returns what the worker has drawn, not an empty string.
    can_read_screen: bool = False
    # Dialogs are detectable by rule and answerable by keystroke.
    can_answer_dialogs: bool = False


@dataclass(frozen=True)
class Worker:
    """A handle for one worker's home, as the run record stores it.

    `id` addresses the home; `group` is the container closed when the run ends
    (a workspace, a tmux session); `agent` is the name the substrate knows this
    worker's agent by, where it tracks agents at all.
    """

    id: str
    group: str = ""
    agent: str = ""


@dataclass(frozen=True)
class WorkerProcess:
    """What is running in a worker's home right now.

    `at_prompt` means the shell owns the foreground again, which is what a
    worker that has exited looks like. `child_pids` is the foreground group
    minus the shell, and is empty at the prompt by definition: the processes
    listed there belong to the shell's own housekeeping, and killing them
    because they happened to be alive during a poll would shoot the prompt
    instead of a worker.
    """

    shell_pid: int | None = None
    at_prompt: bool = False
    child_pids: tuple = ()
    group_id: int | None = None


@dataclass(frozen=True)
class WorkerStatus:
    """The substrate's own reading of what the worker is doing.

    `tracked` is False when the substrate has no record of this worker at all,
    which is different from a worker it is tracking and calls idle: an
    unmeasurable signal must never become a veto, so the runner treats untracked
    as "no opinion" rather than as "not ready".

    `seq` is a counter the substrate bumps on every state change, or None where
    it has none. It is what tells this turn's `done` apart from the `done` the
    turn began at.
    """

    tracked: bool = False
    status: str = ""       # idle | working | blocked | done | unknown
    ready: bool = False    # the TUI is interactive and will take input
    seq: int | None = None


@dataclass
class SpawnResult:
    """How the CLI actually got started, and whether that was the good path."""

    method: str = ""       # the substrate's own spawn, or the typed fallback
    argv: list = field(default_factory=list)
    error: str = ""
    flag: str = ""         # non-empty exactly when the spawn was not clean
    alive: bool = True


class Substrate:
    """Where a worker process lives. See the module docstring for the contract."""

    name = ""
    capabilities = SubstrateCapabilities()

    # False where a worker is a process with no terminal in front of it. Two
    # consequences, both the runner's: the brief arrives as an argv element of
    # the driver's one-shot form rather than being typed in afterwards, and the
    # process ends itself rather than being sent the driver's exit command.
    has_tui = True

    def available(self):
        """Is this substrate usable on this machine right now?

        Asked once per detection, and never of a substrate the caller pinned, so
        a pinned one still fails loudly with its own diagnosis. A daemon that is
        not listening is not an error here, it is the next substrate's turn.
        """
        return True

    # -- lifecycle (required) --------------------------------------------

    def open(self, label, cwd="", env=None, focus=False):
        """Create a home for one worker and return its handle.

        `env` reaches the worker's shell before the CLI starts, not after: the
        depth ladder marker and the session key have to be in place at spawn.
        `cwd` is set at creation rather than by cd-ing a live shell.

        Raises WorkerSetupError when the home dies before it can take a command,
        which the runner retries with a fresh one.
        """
        raise NotImplementedError

    def start_worker(self, worker, driver, argv):
        """Start the CLI in an open home. Returns a SpawnResult.

        The substrate's own spawn is tried first where it has one, because that
        is what lets it detect the agent afterwards. A fallback that types the
        command instead must set `flag`, loudly: a run whose agent was never
        detected has degraded prompting and detection for the rest of its life,
        and nobody should discover that a week later in a log.

        Returns only once the CLI has actually forked. Typing a command is not
        starting one: until the fork, a home looks exactly like an idle shell,
        which is also what a finished worker looks like.
        """
        raise NotImplementedError

    def close(self, worker, release=True):
        """Close the worker's home and its container. Already gone is success."""
        raise NotImplementedError

    def exists(self, worker):
        """Is this worker's home still there?"""
        raise NotImplementedError

    def version(self):
        """What this substrate is, precisely enough to debug a run against it.

        Recorded on every run: a protocol number, a release, or None where the
        substrate has nothing version-shaped to report.
        """
        return None

    def agent_name(self, run_id):
        """The name this substrate will know a run's worker by.

        Run ids are not always legal names: a substrate with its own charset
        rule narrows them here, and the runner stores what it gets back so that
        every later call names the same agent.
        """
        return str(run_id)

    def worker_ids(self):
        """Every live worker id in one call, or None when the substrate is unreachable.

        One call, not one per run: the caps ask about liveness for every record
        on every spawn. None is not "nothing is alive", it is "cannot tell", and
        the caps stay conservative on it.
        """
        return None

    # -- typing (required) -----------------------------------------------

    def send_line(self, worker, text):
        """Type a shell command and run it."""
        raise NotImplementedError

    def send_keys(self, worker, keys):
        """Send named keys (`enter`, `escape`) rather than text."""
        raise NotImplementedError

    def send_tui_line(self, worker, text, enters=1, settle=None, confirm=None,
                      log_path=None):
        """Type a line into a TUI input box and confirm it was submitted.

        Three things a shell does not need: wait for the text to render before
        pressing enter, check afterwards that the screen moved, and press enter
        again if it did not. An unsubmitted brief looks exactly like a working
        worker.
        """
        raise NotImplementedError

    # -- reading (required) ----------------------------------------------

    def read_screen(self, worker):
        """What the worker has drawn, or empty where that cannot be read."""
        raise NotImplementedError

    def screen_since_spawn(self, worker, run_id=""):
        """The screen since this CLI was last started, rather than all scrollback.

        A relaunch in the same home leaves the previous launch's banner sitting
        above it, and reading that as this launch's output is how one self-update
        is counted twice. A substrate that leaves a per-run marker at spawn
        anchors on it; one that starts each worker with a clean buffer returns
        the whole screen.
        """
        return self.read_screen(worker)

    def process_info(self, worker):
        """A WorkerProcess, or None once the home is gone."""
        raise NotImplementedError

    def cpu_percent(self, pids):
        """Recent CPU across these pids, or None where it cannot be read.

        A free signal in the liveness judge's sense: no tokens. None is "no
        evidence either way" and never "idle".
        """
        return None

    # -- detection (optional; gated by capabilities) ----------------------

    def status(self, worker):
        """The substrate's reading of the worker. Untracked by default."""
        return WorkerStatus()

    def screen_state(self, worker):
        """What the substrate's screen rules make of the worker right now.

        Worth asking separately from `status`: the two are different clocks, and
        the screen one runs ahead. A dialog is drawn and matched seconds before
        the agent's status stops saying idle, and readiness is a question about
        the screen.
        """
        return ""

    def blocked_rule(self, worker):
        """The name of the detection rule blocking this worker, or empty.

        Only a rule the substrate reports as the one that fired. Empty is the
        honest answer for a substrate that cannot name it, and it is what keeps
        dispatch from typing at a dialog it cannot read.
        """
        return ""

    # -- the agent channel (optional) ------------------------------------

    def deliver_prompt(self, worker, text):
        """Hand the worker its brief through the substrate's own write channel.

        A liveness-gated write is preferred where the substrate has one: it
        re-verifies the worker's identity and refuses to write into a blocked
        one. Returns the name of the channel it went through.

        Raises SubstrateError when the write could not be made. It does not fall
        back to typing: whether a refusal means the brief never landed is a
        question about the run, and the runner answers it.
        """
        raise NotImplementedError

    def report_state(self, worker, state, message="", seq=None):
        """Tell the substrate how this run ended, for its own wall. Never fatal."""

    def report_session(self, worker, session_id, session_path="", seq=None):
        """Hand over the CLI's native session id, so the run is reopenable."""

    # -- environment (optional) ------------------------------------------

    def verify_environment(self, worker, assignments, depth_var, depth,
                           blank_keys=(), log_path=None, run_id=""):
        """Put the ladder marker in place and prove it took.

        Substrates that hand a process its environment directly need nothing
        here. One that starts a login shell does: the shell sources the user's
        rc files, and an `export` or an `unset` in one wins on the way past.
        Neither the depth ladder nor metered key blanking is a per-run
        preference, so both are re-asserted and then read back, and a home whose
        marker is wrong never gets a CLI started in it.

        `assignments` is the whole environment to re-assert, `blank_keys` the
        subset that must read back empty, and the return is the depth actually
        observed.
        """
        return str(depth)

    def read_exit_code(self, worker, run_id="", log_path=None):
        """The worker's real exit code, or None where it cannot be read.

        A substrate that owns the process has one already. A substrate whose
        panes discard exit codes has to ask the shell for it, which is why this
        is a method rather than a field on the process record.
        """
        return None

    def wait_for_shell(self, worker, seconds=None):
        """Block until the home has a shell that owns its own foreground."""
        return None

    # -- killing (required) ----------------------------------------------

    def kill_worker_tree(self, worker):
        """Stop what the home is running, leaving its shell alive.

        The foreground process group is the unit, and it is not the whole story:
        a CLI's background child that reparents itself to init has a process
        group of its own and survives the group signal, so descendants are
        walked and signalled by pid as well. Returns the pids it signalled.
        """
        raise NotImplementedError

    # -- presence (optional) ---------------------------------------------

    def focus(self, worker):
        """Bring this worker's home to the front, so an attach lands on it."""

    def attach_hint(self, worker):
        """The line an operator types to reach this home themselves, or empty.

        A substrate whose homes outlive dispatch has one and should say it:
        `dispatch inspect` prints it wherever there is no terminal to hand over.
        """
        return ""

    def attach(self, worker):
        """Hand this terminal to the worker's home until the operator detaches.

        Foreground and stdio-inheriting: this is the attach, so it owns the
        terminal for as long as the human wants it.
        """
        raise SubstrateError(
            f"the {self.name} substrate has no terminal to attach to")


def refuse(verb, substrate):
    """The one-line refusal a verb this substrate cannot do gives the operator."""
    return DispatchError(
        f"`dispatch {verb}` needs a substrate that can do it; this run lives in "
        f"{substrate.name}, which cannot")
