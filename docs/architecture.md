# Architecture

dispatch launches one AI-agent worker per command, caps how many can be live,
and records what happened. Three things vary and everything else is shared:
which vendor CLI runs (a **driver**), where its process lives (a **substrate**),
and which model and effort it runs at (a **lane**). The rest of the tree knows
about those three abstractions and nothing about codex, claude, grok, or any
terminal multiplexer.

## Module map

| Module | Owns |
| --- | --- |
| `dispatch/lanes.py` | The lane grammar `key@effort[:tier]`, and the default lane table config replaces. |
| `dispatch/config.py` | The user TOML file, its validation, the presets, and `init`. |
| `dispatch/board.py` | The five role slots, and the table `dispatch board` prints. |
| `dispatch/doctor.py` | Whether this machine can run the configured board. |
| `dispatch/records.py` | The run directory and `run.json`: ids, atomic writes, advisory locks, `status.log`, `RunOptions`. |
| `dispatch/policy.py` | Values a user can change: caps, deadlines, the hand-wait timeout, metered key blanking, the depth ladder. |
| `dispatch/caps.py` | Liveness of a record, and the check-and-reserve that enforces both caps under one lock. |
| `dispatch/prompt.py` | What dispatch types into a worker, and the preamble config replaces. |
| `dispatch/processes.py` | Cross-platform process facts: alive, descendants, stop, CPU. |
| `dispatch/drivers/` | One adapter per vendor CLI, behind `Driver`. |
| `dispatch/substrates/` | One adapter per place a worker can live, behind `Substrate`, plus the pane-shell dialect the terminal ones share. |
| `dispatch/runner.py` | The run lifecycle, written against a Driver and a Substrate. |
| `dispatch/remote.py` | Machines: placement, ssh and scp, and a run that happens somewhere else. |
| `dispatch/cli.py` | The verbs, and the exit codes. |

Dependencies run one way: `cli` sees everything; `runner` sees drivers,
substrates, the record layer, and `remote`; `config` sees `lanes`, `policy`,
`prompt`, `board`, `remote`, and the driver registry, and nothing sees `config`
except `cli`; drivers, substrates, and `remote` see the record layer; `lanes`,
`records`, `policy`, `prompt`, and `processes` see nothing above them. `caps`
needs two facts it cannot produce, "does this run's worker still exist" and
"what does the machine it is on say about it", and takes both as a `Sweep`
rather than importing a substrate or ssh.

## The Driver interface

A driver is `dispatch/drivers/base.py`. It answers everything that is true of
one vendor CLI and no other, and owns no lifecycle: the runner decides when a
worker starts, gets its brief, and ends, and asks the driver only what that CLI
needs.

**Command lines**

- `launch_argv(lane, opts, session_id="")`: the interactive command typed into a
  fresh worker home. No prompt is ever an argv element.
- `resume_argv(lane, opts, session_id)`: reopen a native session, for
  `dispatch continue` and `dispatch inspect`.
- `headless_argv(lane, opts, prompt_text, out_path="", session_id="", resume_session="")`:
  the one-shot form, for the headless substrate. The prompt is an argv element
  here because there is no TUI to type into. `resume_session` is `continue` on a
  substrate with no session to reopen: the same form, carrying the CLI's own
  resume flag instead of a new session id.

Every form is checked against the CLI's own `--help` before it is written down.
Flags that exist only on a headless subcommand are rejected by the interactive
binary, so the two lists never merge.

**Ending a turn and a session**

- `exit_command`: `(command, enters)`. Interactive CLIs do not take themselves
  down; they finish a turn and sit at the prompt. Some eat the first enter in a
  slash-command popup, which is what the count is for.
- `turn_signal`: `agent-done` where the substrate's own done signal is
  trustworthy for this CLI, `deliverable` where it is not and the file the
  worker was asked to write is the only unambiguous evidence.
- `update_markers`: what this CLI leaves on screen when it updates itself and
  quits without reading its brief. Matched only for a worker that exited early
  with nothing to show, which is what keeps an ordinary crash from being
  restarted instead of reported.

**Dialogs**

`dialog_rules` is a `DialogRules` record with two classes. `answered_rules` are
the detection rules dispatch may answer itself, because it already made that
decision: a directory-trust question is dispatch's own, since the caller chose
the directory and the worker chose nothing. `handback_rules` are the rules
dispatch refuses to answer, because a blind enter at a numbered selector picks
an option, and a run standing at a dialog can still be answered by hand while a
run killed for one cannot be un-killed. `trust_markers` is the exemption inside
the handback class: a CLI whose trust dialog matches the same generic rule as
every other form is recognised by its own words on screen, and all of the
markers must appear.

**Everything else**

- `agent_kind`, `shell_prefix`: what a substrate needs to spawn this CLI.
- `cli_binary`, `login_probe`: what must be on PATH, and a probe `dispatch
  doctor` may run to tell "installed" from "logged in". A probe must be
  non-interactive and cost no model call; a CLI that offers none leaves it
  empty, and doctor reports "installed" and says that is all it checked.
- `metered_key_vars`: variables that outrank subscription credentials in this
  CLI's credential order.
- `names_own_session`, `capture_session`, `resolve_transcript`,
  `transcript_root`: where this CLI keeps its sessions, and how a run finds the
  one it produced.
- `validate_options(lane, opts)`: the option combinations this CLI cannot
  honour. The runner runs the shared checks first.

### Writing a new driver

Subclass `Driver`, fill the class attributes, implement the three argv builders
and `validate_options`, and call `register_driver(MyDriver())`. Then add a lane
whose `driver` field names it.

The test for a complete driver: nothing in `runner.py`, `cli.py`, or any
substrate has to branch on which CLI is running. If a branch would be needed, it
belongs on the driver instead.

## The Substrate interface

A substrate is `dispatch/substrates/base.py`. It owns a worker's home: it
creates one, starts a CLI in it with a given environment and working directory,
types into it, reads what it has drawn, reports what its processes are doing,
and closes it. It owns no policy.

**Capabilities**

`capabilities` is a `SubstrateCapabilities` record: `can_steer`, `can_inspect`,
`can_read_screen`, `can_answer_dialogs`. The runner asks before it promises. A
headless run cannot be steered and cannot have a dialog answered for it, so
`dispatch steer` refuses there with a one-line reason instead of typing into a
void. A substrate that claims `can_answer_dialogs` without a rule engine to name
a dialog is how a blind enter picks "No, quit".

`has_tui` is the separate question of whether there is a terminal in front of
the worker at all, and the runner reads it twice: to decide which of the
driver's command lines to build, and to decide whether the turn is dispatch's to
end. A substrate with no TUI starts the driver's one-shot form, whose prompt is
already an argv element, and its worker exits on its own rather than being sent
the driver's exit command.

`available()` is what auto-detection asks. It returns True by default; a
substrate with a daemon answers whether that daemon is listening, so detection
can move on to the next one instead of failing a run. It is never asked of a
substrate the caller pinned, which is what keeps `substrate = "herdr"` a loud
failure on a machine with no herdr.

**Required**

- `open(label, cwd, env, focus=False) -> Worker`: create a home. `env` reaches
  the worker's shell before the CLI starts. Raises `WorkerSetupError` when the
  home dies before it can take a command, which the runner retries with a fresh
  one.
- `start_worker(worker, driver, argv) -> SpawnResult`: start the CLI, returning
  only once it has actually forked. A substrate with its own spawn tries that
  first, because that is what lets it detect the agent afterwards; a fallback
  that types the command instead sets `flag`, loudly.
- `send_line`, `send_keys`, `send_tui_line`: typing. The TUI form waits for the
  text to render, presses enter, checks that the screen moved, and presses again
  if it did not, because an unsubmitted brief looks exactly like a working
  worker.
- `read_screen(worker)`, `process_info(worker) -> WorkerProcess | None`.
- `close(worker, release=True)`, `exists(worker)`, `kill_worker_tree(worker)`.

**Optional, with honest defaults**

- `worker_ids()`: every live worker id in one call, or `None` for "cannot tell".
  `None` is never "nothing is alive": the caps stay conservative on it.
- `status(worker) -> WorkerStatus`: the substrate's own reading. `tracked=False`
  means it has no record of this worker at all, which is different from one it
  is tracking and calls idle: an unmeasurable signal must never become a veto.
- `screen_state(worker)`, `blocked_rule(worker, known_rules)`: detection. The
  screen clock runs ahead of the status clock, which is why readiness asks the
  screen.
- `deliver_prompt(worker, text)`: the substrate's own write channel. Raises when
  the write could not be made; whether that means the brief never landed is a
  question about the run, and the runner answers it.
- `report_state`, `report_session`: what the substrate's UI shows.
- `verify_environment(...)`: re-assert the run's environment inside the home and
  read it back. A substrate that hands a process its environment directly needs
  nothing here. One that starts a login shell does, because the shell sources
  the user's rc files afterwards.
- `read_exit_code(worker, run_id)`: a substrate that owns the process has one
  already; one whose panes discard exit codes asks the shell for it.
- `screen_since_spawn`, `agent_name`, `version`, `focus`, `attach`,
  `attach_hint`, `wait_for_shell`, `cpu_percent`.

**Running somewhere else**

A substrate that drives a command-line multiplexer takes `command_builder`, a
callable from the argv it wanted to run to the argv actually run. The default is
the identity. `remote.remote_tmux_command(machine)` returns one that wraps each
call in ssh, which is the whole of shell mode: the same substrate, one
connection in front of it. The pane's own process probe and the signals that
end a worker go through it too, because the tty and the pids belong to whichever
machine holds the pane, and this one's would name something else entirely.

### Writing a new substrate

Subclass `Substrate`, set `capabilities` and `has_tui` honestly, implement the
required methods, and register it in `dispatch/substrates/__init__.py`. Leave the
optional methods alone where the substrate genuinely cannot answer: their
defaults say "no opinion", and the capability record is what stops the runner
from reading that as "no".

### Detection order, and what each substrate is for

`DETECT_ORDER` is `herdr`, `tmux`, `headless`, best first by what each can
honestly offer, and a fresh run lands in the first one usable here. Only a fresh
run: a recorded run reopens the substrate it named, which is what keeps a herdr
run from being reconciled by a substrate that never held it. `substrate = "..."`
in config pins one, and a pinned substrate is never asked `available()`, so it
fails loudly instead of falling through.

Nothing in dispatch requires herdr. It is preferred where its daemon is already
listening, because it is the only one of the three that detects and tracks the
agent inside a pane; where it is not, tmux gives the same panes without that
detection, and headless needs nothing at all. `dispatch lanes` and
`dispatch doctor` both print which one this machine detected.

### The herdr substrate

herdr is a terminal multiplexer whose daemon owns real PTYs and answers a JSON
control socket (a named pipe on Windows). A worker is an interactive CLI in a
pane inside a workspace labelled `dispatch-<run id>`, so the home outlives every
dispatch process and a run can be reconciled later by a command that did not
launch it. `herdr.py` is the largest of the three substrates because it is the
only one speaking a protocol rather than shelling out to a binary.

What it offers that the others do not is the agent channel. `agent.start` runs
the lane's command line and detects the TUI behind it, after which `agent.status`
and `agent.explain` answer what the worker is doing and which detection rule is
holding it. That is what the drivers' `turn_signal` and `dialog_rules` are
written against: a tracked status is what lets `done` mean the end of a turn, and
a named rule is what makes a dialog answerable rather than guessed at.
`deliver_prompt` is a liveness-gated write into that agent, which re-verifies the
worker before writing and refuses a blocked one.

Four facts shape the file:

- **The protocol is pinned.** `PROTOCOLS` lists the releases dispatch has been
  verified against, call by call; anything else is refused, naming the version it
  found. Drift is a loud refusal, never a guess.
- **The socket is connect-per-call.** The daemon closes the connection after
  every response, so nothing may hold one open for a second request.
- **The transport self-heals once.** Nothing listening is a daemon dispatch
  starts itself, once per invocation, and then retries the call that found it
  down. A daemon that is listening and refuses us belongs to another security
  context, so that class is refused with the bounce command in the message
  rather than retried, because a loop of detached spawns is how a box ends up
  with six daemons and no working one.
- **Reporting evicts detection.** A state report from a source outside herdr's
  own allowlist replaces detection instead of sitting beside it, and every later
  probe then answers "agent not found". So dispatch reports exactly once, at the
  terminal transition, and lets herdr's own detection be the status of record
  while the run is live.

Panes carry no exit code, so `read_exit_code` types the shell's own status probe
and parses a per-run marker off the screen; a login shell sources rc files after
the pane exists, so `verify_environment` re-asserts the run's environment inside
the pane and reads it back. Both lines come from `substrates/paneshell.py`, which
is shared with tmux.

### The tmux substrate

A run is one detached tmux session, `dispatch-<run id>`, holding one window with
one pane. dispatch never attaches the operator to it: `dispatch watch` reads the
pane, and `tmux attach -t dispatch-<run id>` is the line for looking at one by
hand, which is what `attach_hint` returns and what `dispatch inspect` prints
where it has no terminal to hand over.

It is detected by asking two questions rather than one, both in the constructor:
is there a tmux on PATH, and will it give us a server. `tmux start-server` is the
second, because it answers without leaving anything behind, tmux exiting an empty
server as soon as it has started one.

What tmux gives it is the whole screen (`capture-pane -p -J`, with scrollback
only where a caller needs it) and keys (`send-keys -l` for text, so a brief
containing the word `Enter` is typed rather than pressed). What it does not give
it is detection: nothing in tmux tells a TUI from a shell, so `status` stays
untracked, `blocked_rule` stays empty, and the only dialog dispatch answers is
the one the driver names in its own words on the screen this substrate hands it.

The two problems the herdr substrate already solved come back unchanged, and are
solved the same way. A pane carries no exit code, so `read_exit_code` types the
shell's own status probe and parses a per-run marker off the screen. A login
shell sources rc files after `new-session -e` has done its work, so
`verify_environment` re-asserts the run's environment inside the pane and reads
it back; on a tmux older than 3.2, which has no `-e`, that read-back is also
where the environment first arrives, ahead of any CLI. Both lines come from
`substrates/paneshell.py`, which is the pane shell's dialect and the parsers for
what it echoes back, shared by the two substrates that type at one.

Process facts are the pane's pid from tmux and the foreground process group from
`ps` on the pane's tty. The group, not the process list: a shell at its prompt
forks subshells for prompt hooks continuously, and counting those as a worker
would mean no run ever looked finished.

### The headless substrate

`headless.py` is the fallback that is always there: no daemon, no terminal, one
subprocess per worker. It runs the driver's one-shot form (`codex exec`,
`claude -p`, grok's non-interactive mode) with the run's environment, captures
both output streams to `pane.log` in the run directory, and calls the run over
when that process exits.

Every capability is False, honestly: `steer` and `inspect` refuse with one line
naming the substrate, `continue` still works because it relaunches through the
driver's resume flag, and the one-shot forms are run with flags that mean the
CLI never raises a dialog. `read_screen` returns the captured log rather than a
screen, which is enough for the runner to measure stillness and to salvage
`out.md`, and not enough to show an operator, which is what `can_read_screen`
is saying.

Two details are worth knowing before reading it:

- **The exit code is a file, not a pid.** The CLI runs under a two-line relay
  that writes its status to `worker.rc`. A background run's launcher exits long
  before its worker does, and only the process that forked a child can read that
  child's status; the file is what any later process can read instead. It is
  also the completion signal, ahead of pid liveness, because an unreaped pid
  lingers as a zombie that still answers `kill -0`.
- **Check-ins run on what exists.** There is no TUI state, so `status` stays
  untracked and `checkin_verdict` judges on CPU in the worker's tree, on whether
  the captured log has grown, and on whether the deliverable is there. Nothing
  fabricates a screen that has been still.

Where `out.md` comes from differs per driver, and the driver's `headless_argv`
is where that is decided: codex is told to write the deliverable itself with
`exec -o`; claude (`-p`) and grok (`--output-format text`) print their final
message on stdout, so it lands in `pane.log` and the runner's own salvage copies
it into `out.md` when the worker wrote no file.

## Config, and the board

Nothing in the tree reads a config file except `config.py`, and it loads one:
the user file, `~/.config/dispatch/config.toml`, or whatever `DISPATCH_CONFIG`
names. It owns lanes, board, machines, caps, policy, substrate, and the prompt
preamble. Nothing is read from the task directory, because a task repository is
untrusted input, and a checkout that could name a lane would be choosing its own
sandbox; per-repository routing is a second user file that `DISPATCH_CONFIG`
selects.

`load_config()` reads and validates without touching process state.
`apply_config()` installs the result through the setters those modules already
had: `set_lane_table`, `set_default_lane`, `set_policy`, `set_preamble`. That
split is why a test can validate a config without running on it, and why
`cli.main` is the only place a config is loaded: it applies one, then calls
`run_verb`, which is what the suite calls directly.

Config lanes replace the built-in table wholesale rather than merging into it,
so what `dispatch lanes` prints is exactly what the file says. Two values are
read off the board instead of being kept twice: the default lane is the `medium`
slot unless a file names one, and the lanes a depth-1 worker may spawn are the
`light` slot unless `[policy] depth1_lanes` names them.

Every validation error names the key and the file, in that order, because the
operator is looking at that file when the message arrives.

The board is five slots, `light`, `medium`, `high`, `blindspot`, `genius`. A
slot names a lane, or is empty; one lane may fill several. `render_board` is one
aligned table read by a human and by an orchestrating agent both, and its `when`
column is the routing rule for that slot: `[board.when]` in config replaces the
standing line.

Presets are TOML files shipped inside the package (`dispatch/presets/`), read
through `importlib.resources` so they work from a wheel. `dispatch init` copies
one to the user path, installs `dispatch/skill/SKILL.md` and
`dispatch/skill/agents-snippet.md` under every agent home that exists
(`~/.claude`, `~/.codex`, `~/.agents`), and prints the snippet for pasting into
an AGENTS.md. The config is written once and kept until `--force`. The skill
install is idempotent: an unchanged file is reported unchanged, a file that
differs is kept until `--force`.

The skill and the snippet live inside the package rather than at the top of the
tree, which is what lets `importlib.resources` reach them from a wheel;
`dispatch skill` and `dispatch agents-snippet` print exactly those files.

`dispatch doctor` answers three questions and no more: is each lane's CLI
installed and logged in, is each machine reachable over ssh (and, in dispatch
mode, does a dispatch answer there), and which substrate a run would land in.
Every probe is non-interactive, bounded by a timeout, and started with stdin
closed, because a vendor CLI opened on a terminal here would sit at its TUI
forever.

## How a run flows

```
dispatch run sol@medium brief.md
  |
  lanes.resolve_lane   -> Lane(driver="codex", model, effort, tier)
  drivers.validate_options
  remote.place_run     -> a machine, and the run happens there instead
  substrates.detect_substrate  -> Substrate
  |
  caps.reserve_slot(prepare)         # both caps, under one lock
    runner.prepare_run               # run dir, argv, prompt, record
                                     # argv is the driver's interactive form,
                                     # or its one-shot form where has_tui is False
  |
  runner.execute_run(rec, substrate)
    substrate.open                   # a home, with the ladder env
    substrate.wait_for_shell
    substrate.verify_environment     # ladder marker read back
    substrate.start_worker(driver)   # the CLI, or a flagged fallback
    wrapper.deliver_brief            # wait for settled idle, then the brief by
                                     # path; already delivered where the argv
                                     # carried it
    wrapper.watch                    # poll, judge, check in, end the turn
      substrate.read_exit_code
      substrate.close
  |
  finalize_schema -> exit code
```

A background run stops after `deliver_brief` and hands the home to a detached
watcher (`dispatch _watch <id>`), which runs exactly the loop a foreground run
runs. Nothing else supervises: every command that reads runs also reconciles the
ones nobody is watching, which is how a finished background worker frees its cap
slot and lands its exit code.

### Two rules worth knowing before reading `runner.py`

**Completion is produced, not waited for.** Interactive CLIs finish a turn and
sit at their prompt forever. The runner decides the turn is over, types the
driver's exit command, confirms the shell came back, and reads the real exit
code from the substrate. When the exit command is ignored twice, the process
tree is killed and the run is journaled as done if its deliverable was already
settled.

**A turn ending is not a run ending.** A worker that backgrounds a long command
and ends its turn to wait is still working, and a human's queued message starts
a turn dispatch never typed. So the run ends on its deliverable: the turn is
over, it has stayed over across two consecutive ready looks, and the file the
brief asked for exists and has stopped changing.

Which evidence says the turn is over is the driver's `turn_signal`, and on a
substrate that tracks no agent there is none to read: `status` is untracked,
so an `agent-done` driver has neither a `done` to wait for nor a working look to
corroborate one with. Every driver is then judged the way a `deliverable` one
is, plus one condition that signal does not need, that the file was written
during this turn. Without that, the previous turn's deliverable would end the
next one the moment it started.

### Liveness and check-ins

A deadline is when a run gets checked on, not when it dies. Slow inference, a
loaded machine, or a long tool loop all make a healthy run look late, so only a
worker that is demonstrably not working is killed.

`free_signal_verdict(signals)` is a pure function over signals that cost
nothing: at the prompt with no children, screen movement, CPU, elapsed time. It
answers `gone`, `working`, `settled`, or `starting`. `settled` is the
interesting one, and `snapshot_judge_seam` is where a paid judge can be wired to
answer it; it returns `None` today, meaning no opinion. `set_liveness_judge`
swaps the free verdict itself. Every verdict is written to the record before
anything acts on it.

`checkin_verdict(...)` is the second pure function: given the same signals plus
the substrate's status, whether a deliverable exists, and how many consecutive
check-ins have already come back ambiguous, it answers `working`, `blocked`,
`dead`, `stuck`, or `review`. Only the middle three kill. `review` says the
signals disagree, and its answer is always to keep the home and look again.

## Machines

A machine is an ssh target with a name: `[machines.<name>]` with `ssh =
"user@host"`, and optionally `mode`, `home` (the remote `DISPATCH_HOME`), and
`dispatch` (the remote command). `config.py` parses that table like every other
section and installs it with `remote.set_machines`, so `remote.py` reads
machines and never a file. Placement is one line of policy and no scheduler: `--on <machine>` wins, then the lane's own `machine`, then local.
`--on local` is how a lane that always runs on a machine is run here instead.

ssh configuration is the operator's. Hosts, users, keys, ports, and jump hosts
come from their ssh config; dispatch adds `BatchMode=yes`, because a password
prompt in a background run hangs forever, and the `ControlMaster` settings that
keep one connection open across the many small calls a live run makes (the
socket lives under `DISPATCH_HOME/ssh/`). Nothing sensitive is ever an argv
element: briefs, schemas, images, and steer and continue messages travel as
files and are named by path, which is why `steer` and `continue` also take
`--message-file`.

### `dispatch` mode

The default, for a machine with a full install. The brief and any schema or
image are copied into `<remote home>/remote/<local run id>/`, and the machine's
own `dispatch run ... --bg` starts the worker with the same lane and options.
Its run id and directory come back on stdout and into the local record.

The local record is a pointer while the run is live: `status`, `wait`, `logs`,
`watch`, `kill`, `steer`, and `continue` are questions asked over ssh. One poll
is one round trip, and it runs `dispatch status` on the machine before reading
the record, because the machine has no supervising daemon either, only each
background run's own watcher: when that watcher is gone, a run's exit code
lands there when something reads its status.

At terminal state, `out.md`, `out.json`, `status.log`, and `screen.log` are
copied down once, and the machine's own `run.json` lands beside them as
`remote-run.json`. It is not copied over the local record: this side's record is
the run's identity here, and the fields worth having (state, rc, session id,
the check-in ladder) are merged into it by the poll that saw it finish. From
then on every verb reads the mirror and the connection is not touched again.

The far side owns the worker, so a dropped connection costs nothing: the run
carries on and the next poll finds it. That is also why a failed poll never ends
a run. A machine that is asleep or off the network is recorded as unreachable
and reported by `status`; only the operator ends the run, and `kill` on an
unreachable machine says plainly that the worker may still be live there.

`continue` on a remote run starts a run of its own on that machine, so it gets
its own local record with the first as its parent. One local record per remote
run is what keeps `kill` and the mirror pointing at exactly one thing.

The ladder and the session key cross the connection unchanged. The remote
dispatch is this seat reaching further rather than a worker, so the worker it
starts lands one rung below the process that asked for it, and a depth-1 worker
cannot buy a spawn by going remote. Both caps count a remote run here, and the
machine's own dispatch counts it there.

### `shell` mode

For a machine with only tmux and a vendor CLI: a VM, a headless box, anything
where installing dispatch is not worth it. There is no remote dispatch. The
local process drives a remote tmux through the same tmux substrate a local run
uses, with `command_builder` wrapping every tmux call in ssh over one control
connection. A record-addressed verb rebuilds that substrate from the record
(`cli.substrate_for`), because a tmux substrate without the ssh in front of it
drives this machine's panes and the run's pane id names nothing here.

The machine shares no filesystem with this one, so the run has a directory on
both. `--dir` is required, since the sandbox root has to be a path over there
and defaulting it to the current directory would sandbox a worker to a directory
nobody meant. The brief and the prompt are staged into
`<remote home>/remote/<run id>/` before the CLI starts; every path the worker is
given, in its prompt, its nudge, and its typed fallback, is a path in that
directory; and the deliverable it writes there is mirrored down on every poll
and once more at the end. The mirror is rewritten only when the bytes change,
because its mtime is what the deliverable's quiet window is measured with.
`--image` is refused: it is an argv element of the CLI's own command line, built
before the run has a directory on the machine to stage a file into.

The record, the status log, and the screen stay on this side, which is what
keeps `status`, `wait`, and `logs` local questions. The local process is the
run's supervisor, so an ssh drop it cannot re-establish loses the run, the way
closing a laptop on a local foreground run does.

## Concurrency

Two invariants:

- A run's liveness is the existence of its worker, or an advisory file lock held
  by whichever process owns it, never a bare pid. Pids get reused; a lock dies
  with its holder.
- Both caps are checked and the reservation written under one cross-process lock
  before the worker is spawned. Anything that counts runs only after spawning
  has a window in which the caps do not exist.

Adopting an unwatched run is exclusive: a reconciling process takes
`watcher.lock` first, so two `dispatch status` calls cannot both send a run's
exit command. A run with a live watcher is live whatever else the sweep can see,
because that lock is held for exactly as long as somebody is driving the worker.

A sweep answers for one substrate on one machine, and `Sweep.covers(rec)` is
where it says so. A record from another substrate, or one placed on a machine,
is left exactly as it is: worker ids mean something only inside the substrate
that issued them, so reading one against another's list is how a live run reads
as orphaned and a stranger's worker gets closed.

## Tests

`tests/herdr_stub.py` is a stub daemon on a temp endpoint, speaking shapes
recorded off a live release (`tests/herdr-protocol-19.json`), and the test case
every suite builds on. It models a pane well enough to be worth trusting: a
shell, a worker that holds the foreground for a few polls, a screen that
accumulates what was typed, and an exit code the shell reports when asked. It
also pins `DISPATCH_CONFIG` at an empty file, so no suite ever runs on the
config the developer happens to have.

Each substrate and the machines get a fake of their own, on the same principle:
the thing dispatch talks to is simulated, and everything on this side of it is
real.

- `tests/fake_cli.py` is copied onto PATH as `codex`, `claude`, and `grok`, and
  does what a brief tells a worker to do, which is write its final answer to the
  file the prompt names. A headless run is driven end to end against it, with a
  real process, real captured output, and a real exit code.
- `tests/fake_tmux.py` is a fake `tmux` binary that records every invocation and
  models one pane against a JSON state file. Its pane answers dispatch's own
  shell probes through the same dialect dispatch types them with, and it
  reproduces the tmux behaviours the substrate is written around, including a
  `display-message` that prints an empty line and exits 0 for a pane that is
  gone.
- `tests/fake_ssh.py` is fake `ssh` and `scp` on PATH, logging their argv and
  modelling one remote filesystem in a temp directory, so `mkdir -p`, `cat`, and
  both directions of `scp` really move files and a case can assert on what was
  staged and what came back. Everything else the remote shell is asked to run
  answers from a scripted reply table. No machine is reached, and a run placed
  on one never opens a substrate here.

`tests/test_config.py` needs none of them: it runs against a temporary HOME, a
temporary working directory, and a PATH holding nothing but fake CLIs, so the
config, board, preset, `init`, and `doctor` cases never read the machine's own
config and never run a real vendor CLI or a real ssh.

No vendor CLI and no real multiplexer is ever launched. The exception that
proves the caps rule is that a few cases spawn real dispatch subprocesses, which
reach the same stub over the same endpoint, because a cap that only holds inside
one process is not a cap.

```
python3 -m pytest tests -q -n auto
```
