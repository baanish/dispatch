# dispatch

dispatch hands one task to one AI-agent worker running on a vendor CLI you
already have (`codex`, `claude`, `grok`), on this machine or on one you reach
over ssh. It launches the worker under the tightest permissions its CLI offers
(an OS sandbox on codex lanes only, see
[What `--write` means on each driver](#what---write-means-on-each-driver)), caps
how many can be live at once, checks in on it while it works, and records the
brief, the answer, and the run's log under `~/.dispatch/runs/<run id>/`.

Requires Python 3.11 or newer, and no third-party packages.

## Install

```
uv tool install git+https://github.com/baanish/dispatch-oss
```

`pipx install git+https://github.com/baanish/dispatch-oss` works the same way. The
install brings a `dispatch` command and nothing else: the vendor CLIs are yours
to install and log in to.

## Quickstart

Start from the preset that matches the CLIs you have:

| Preset | What it assumes | Default lane |
| --- | --- | --- |
| `anthropic-only` | `claude` | `opus@medium` |
| `openai-only` | `codex` | `astra@medium` |
| `balanced` | `codex`, `claude`, and `grok` | `astra@medium` |
| `all-genius` | `claude`, and one top-rate lane in every slot | `fable@high` |

Every example below uses `anthropic-only`, so every lane name and run id in
them says `opus@medium`. Under another preset, read that preset's default lane
in its place.

```
dispatch init --preset anthropic-only   # writes ~/.config/dispatch/config.toml
dispatch doctor                         # which CLIs and machines answer here
dispatch board                          # the five slots, and the lane filling each
```

On a machine where everything answers, the report reads like this, one line per
lane and one per machine:

```
dispatch 0.1.0
config: ~/.config/dispatch/config.toml
substrate: herdr

lanes
  ok   opus   installed at ~/.local/bin/claude, logged in
  ok   fable  installed at ~/.local/bin/claude, logged in

machines
  none configured, every run is local
```

Every preset also picks a model for each of its lanes, and your account has to
be entitled to those models. `dispatch doctor` answers a narrower question:
whether each lane's CLI is on `PATH`, and for codex and claude whether its
login probe passes (grok has no free probe, so an installed binary reads as
ok). It does not
ask a vendor which models the account behind that login may run. A lane naming a
model you do not have is an edit to its `model` in the config, below.

`dispatch doctor --smoke [LANE]` asks the wider question, and pays one small
model call for the answer:

```
dispatch doctor --smoke              # the default lane
dispatch doctor --smoke fable@high   # or whichever lane you name
```

It runs the checks above and then one tiny real task on that lane: read-only,
in an empty temporary directory, in the foreground. It reports `ok` with how
long the task took and the run id, or `FAIL` with the run's state, its error,
and the `dispatch wait <id>` that shows the CLI's own output. A failure exits
1. What it proves that `doctor` alone cannot: that the account behind the login
can actually run the lane's model, and that the whole path from brief to
`out.md` works on this machine. It costs that call, which is why you have to
ask for it.

`init` also installs the agent skill under `~/.claude/skills/dispatch/`,
`~/.codex/skills/dispatch/`, and `~/.agents/skills/dispatch/`, for each of those
homes that exists, and prints a short block to paste into `AGENTS.md` or
`CLAUDE.md`. It refuses to overwrite an existing config without `--force`.
`dispatch skill --install` does the skill half on its own, for an agent
installed after that first `init` or a skill left behind by an older dispatch.
It leaves the config alone, and keeps a skill you edited until `--force`.

Then write a brief and run it. There are no inline prompts: a brief is a file,
and its path is what `dispatch run` takes. In bash:

```bash
cat > brief.md <<'EOF'
List the top-level files in this directory and say what this project appears
to be, in a short paragraph. Read only: change no file and run no command that
writes.
EOF

dispatch run brief.md --dir .                           # block, print the answer

run_id=$(dispatch run brief.md --dir . --bg | head -1)  # return, keep the id
dispatch status                                         # one line per run
dispatch wait "$run_id"                                 # block, then print the answer
```

The same sequence in PowerShell:

```powershell
@'
List the top-level files in this directory and say what this project appears
to be, in a short paragraph. Read only: change no file and run no command that
writes.
'@ | Set-Content brief.md

dispatch run brief.md --dir .

$lines = dispatch run brief.md --dir . --bg
$runId = $lines | Select-Object -First 1
dispatch status
dispatch wait $runId
```

That brief only reads, so the run needs no `--write`; a task that has to change
files takes it.

`dispatch run` with no lane takes the default lane, which is the `medium` slot
unless your config names another. A config that leaves `medium` empty falls back
to the shipped default lane, and to its own first lane if it does not define
that one.

A foreground run blocks until the worker finishes, then prints the answer it
wrote, and nothing else:

```
This directory is a Python package called `dispatch`, laid out as a CLI:
`pyproject.toml` declares the `dispatch` entry point, `dispatch/` holds
the package, `tests/` holds the suite, and `docs/architecture.md` is the
map of the code. `README.md` is the operator-facing documentation.
```

`--bg` returns instead, printing the run id on its first line and the run
directory on its second, which is why both sequences keep the first line and
drop the rest:

```
opus@medium-183640-1ed5
~/.dispatch/runs/opus@medium-183640-1ed5
```

Every verb that takes an id takes the whole id, the first line `--bg` printed.
There is no short form.

`dispatch wait` blocks until that run ends and then prints three things: the
run's state and the tail of its status log, the whole answer, and the end of
what the worker's CLI printed on its own.

```
opus@medium-183640-1ed5  opus@medium  done

START 2026-01-01T18:36:40Z claude --permission-mode auto --model claude-opus-5
PROMPT 2026-01-01T18:36:44Z via paste
ALIVE 2026-01-01T18:36:59Z worker running
EXIT 2026-01-01T18:41:01Z rc=0
state: done
COMPLETE 2026-01-01T18:41:02Z state=done out=~/.dispatch/runs/opus@medium-183640-1ed5/out.md

-- out.md (4 lines) --
This directory is a Python package called `dispatch`, laid out as a CLI:
...

-- worker CLI output: last 30 lines of ~/.dispatch/runs/opus@medium-183640-1ed5/screen.log --
> Read and follow the brief at ~/src/yourproject/brief.md. You are a
  dispatched worker. ...
...
  Write(~/.dispatch/runs/opus@medium-183640-1ed5/out.md)
Wrote the summary to out.md.
```

The run outlives the command that printed it. Its directory holds `brief.md` as
it was passed, `prompt.txt` as dispatch typed it, the answer in `out.md`, the
timeline in `status.log`, the screen in `screen.log`, and the record itself in
`run.json`. [The run record](#the-run-record) is the whole list.

### The verbs

| Verb | What it does |
| --- | --- |
| `dispatch run [lane] <brief>` | One worker, one brief. Prints the answer, or the run id under `--bg`. |
| `dispatch status` | One line per run, and the sweep that settles finished background runs. |
| `dispatch wait <id> [--give-up 30m]` | Block until the run ends, then print its status tail, the whole answer, and the last 30 lines of the worker CLI's own output. |
| `dispatch logs <id> [-f]` | The run's own status log. |
| `dispatch watch [id] [-f] [--deep] [--attach]` | The multi-run wall, one run followed, one deep snapshot, or its live terminal. |
| `dispatch steer <id> <message>` | Type a correction into a live worker. |
| `dispatch continue <id> <message>` | A new turn on a finished run's session. |
| `dispatch inspect <id>` | Reopen a finished session and attach to it. |
| `dispatch kill <id>` | Stop the worker's process tree and close its home. |
| `dispatch board`, `dispatch lanes`, `dispatch doctor` | The board, the lane table, the health check. |
| `dispatch init`, `dispatch skill [--install]`, `dispatch agents-snippet` | The config, and the two agent-facing documents. |

`run` takes `--dir PATH` (the working directory, and codex's sandbox root;
default the current directory), `--write` (writing allowed under the selected
driver's own permissions, read-only otherwise), `--net` (network
inside a codex write sandbox), `--add-dir PATH` (extra writable tree for codex
lanes with `--write`, repeatable), `--schema PATH` (JSON output shaped by this
schema; dispatch checks that it parses, and only a headless codex run has the
schema enforced, by codex itself),
`--out PATH` (copy the answer here too), `--bg`, `--deadline 45m`,
`--image PATH` (codex only), and `--on MACHINE`.

On claude lanes, `--add-dir` adds a tool directory under Claude Code's permission
rules. Grok ignores it. `continue` does not forward extra directories.

### What `--write` means on each driver

| Driver | Without `--write` | With `--write` |
| --- | --- | --- |
| codex | Codex's OS sandbox in `read-only` mode: commands run, and a write happens only when codex's reviewing agent approves the escalation, which is how the answer file under `~/.dispatch/runs/` gets written. | The `workspace-write` sandbox: writes are confined to `--dir`, the `--add-dir` trees, and temp; no network without `--net`; escalation requests go to codex's reviewing agent. |
| claude | Not a sandbox. `--permission-mode auto` with `Read`, `Grep`, and `Glob` pre-approved; every other tool call, shell commands and edits included, is decided by Claude Code's auto mode and your own Claude settings. | `--permission-mode auto` with nothing pre-approved. Nothing confines writes to `--dir`. |
| grok | Not a sandbox. `--permission-mode auto`, subagents off, 12 turns. Nothing blocks a write. | Refused. |

Only a codex lane is confined by the operating system, and what that confines
is writes and network, not reads: a codex worker can read anything your account
can, your ssh keys and other runs' answers included, and what it reads can come
back in its answer. On claude and grok lanes, leaving `--write` off states
intent and sets permissions, and a worker that misbehaves can still cross it. A
brief you do not trust belongs in a container or VM of your own; a codex lane
only keeps it from writing outside `--dir`.

The vendor CLI is the vendor's. dispatch reads no config from the task
directory, but the CLI it starts there does: Claude Code loads that checkout's
`.claude/settings.json`, hooks, and MCP servers, and codex its project config,
with whatever they allow. dispatch also answers the CLI's first-visit "do you
trust this folder" dialog for you, because you chose the directory with `--dir`,
and the CLI remembers that answer for the directory afterwards. Look at a
checkout you did not write before you dispatch onto it.

### What dispatch is not

dispatch is a launcher that runs as you, not a boundary between you and a
worker. Caps, the depth limit, and `DISPATCH_HOME` are bookkeeping that a
cooperative worker respects and a hostile one can step around. Every worker runs
under your account and can read what you can read, on every lane. A worker's run
directory under `~/.dispatch/runs/` holds its answer next to dispatch's own
records. dispatch replaces the files it writes there rather than writing through
them, so a name a worker has turned into a symlink is replaced and not followed;
on POSIX an append refuses a link outright, and the `--out` copy is anchored by
inode to the directory recorded at launch, where Windows, on which making a
symlink takes a privilege, checks that path instead. dispatch reads nothing from the
task directory, but a worker that can write in its run directory can still
corrupt that run's records. Work you consider hostile
belongs under a separate account, container, or VM that holds only the checkout
and the vendor login it needs.

`steer` and `continue` also take `--message-file PATH`, which is how a long
correction gets in without becoming a command-line argument.

## The board, and the five slots

A **lane** is the whole routing decision in one token, `key@effort[:tier]`:
which CLI runs, which model it runs, how hard it thinks, and which service tier
it bills against. `luna@high`, `astra@medium`, `sol@high:priority`.

A **slot** is a role, and the board has five. Each names the lane that plays it:

| Slot | What it is for |
| --- | --- |
| `light` | Bulk mechanical work. A depth-1 worker may spawn this slot's lane key, at any effort. |
| `medium` | The default worker: well-scoped execution and investigation. |
| `high` | Work meant to be merged, or whose shape outlives the task. |
| `blindspot` | Adversarial review and second opinions. |
| `genius` | The hardest single call, at the highest cost. |

The indirection is the point. An orchestrating agent asks for `high` and gets
whatever you decided `high` costs today, so changing models is an edit to one
TOML file rather than to every brief that named one. A slot may be empty, and
one lane may fill several. `[board.when]` replaces the standing line for a slot
with your own routing rule.

`dispatch board` prints the table, and the shipped skill tells an agent to read
it once per session and route by slot rather than by a model name it remembers.

## Substrates

A **substrate** is where the worker process lives. dispatch detects one per
fresh run, in the order herdr, tmux, headless, and `substrate = "..."` in config
pins one. None of them is a dependency: headless needs nothing beyond the vendor
CLI.

| Substrate | The worker lives in | `steer` | `inspect` | Screen | Dialogs |
| --- | --- | --- | --- | --- | --- |
| `herdr` | A pane in a workspace, with agent detection | yes | yes | yes | answered by rule |
| `tmux` | A detached tmux session, named from the run id | yes | yes | yes | only those the driver names |
| `headless` | A subprocess running the CLI's one-shot form | no | no | log only | none is raised |

herdr is preferred where its daemon is listening, because it is the only one of
the three that detects and tracks the agent inside the pane, which is what makes
"the turn is over" a reading rather than a guess. It lives at
[herdr.dev](https://herdr.dev), where `brew install herdr` is one of the ways
in; dispatch talks to its control socket at protocol 20 or 22, and refuses any
other version rather than guessing at it. tmux gives the same panes without that
detection. headless is always available and degrades honestly rather than
pretending: `steer` and `inspect` refuse with a one-line reason,
`continue` still works through the vendor's own resume flag, and check-ins judge
on CPU, log growth, and whether the deliverable exists.

Windows has headless and herdr. There is no native tmux there.

`dispatch doctor` prints the substrate this machine detected.

## Machines, and `--on`

A machine is an ssh target with a name:

```toml
[machines.workshop]
ssh = "operator@workshop"     # user@host, or an ssh config alias
mode = "dispatch"             # the box runs its own dispatch (the default)

[machines.spare]
ssh = "operator@spare"
mode = "shell"                # no dispatch there; drive its tmux over ssh
```

Placement is one rule and no scheduler: `--on <name>` wins, then a lane's own
`machine = "..."`, then local. `--on local` runs a machine-pinned lane here
instead.

```
dispatch run brief.md --on workshop --dir /srv/checkout --write
```

Hosts, users, keys, ports, and jump hosts come from your own ssh config. Files
cross with `scp -s`, so this machine needs OpenSSH 8.7 or later.
dispatch adds `BatchMode=yes`, because a password prompt in a background run
hangs forever.

**`dispatch` mode** is for a machine with a full install. The brief is copied
over, with a `--schema` or `--image` file if the run has one, and handed to that
machine's own `dispatch run --bg`. Until the run ends, `status`, `wait`, `logs`,
`watch`, `kill`, `steer`, and `continue` are questions asked over ssh; at the
end the answer and that machine's record are copied down, the status log and the
screen come with them when they can, and every later read is local. The far side owns the worker, so a
dropped connection costs nothing and the next poll finds the run.

For a Windows host whose SSH shell is PowerShell, set `shell = "powershell"`
in its `[machines.<name>]` table. If SSH cannot find `dispatch` on PATH, set
`dispatch` in that table to the installed command's absolute path.

**`shell` mode** is for a VM or a headless box carrying only tmux and a vendor
CLI. The local process drives the remote tmux over one ssh connection and is the
run's supervisor, so an ssh drop it cannot re-establish loses the run. `--dir`
is required, because the sandbox root has to be a path on that machine, and
`--image` is refused.

### Setting a machine up

In `dispatch` mode what crosses is the lane's name and the run's flags, not the
lane's definition: the machine resolves the lane name against its own config and
its own vendor login, so both ends have to agree on what that name means. Say
the machine is reached as `operator@workshop` and you call it `workshop`:

1. Install a vendor CLI on the machine and log in to it there. A login here is
   not a login there.
2. In `dispatch` mode, install dispatch on the machine too, by the command you
   installed it with here. In `shell` mode install tmux there instead, and skip
   step 3: a shell-mode lane is resolved by this machine.
3. Give the machine a config defining every lane name you will send it, with
   `dispatch init --preset <name>` on the machine or by copying your `[lanes]`
   table over. Caps, `[worker_env]`, and metered-key blanking for the run are that machine's
   config; the deadline crosses with the run.
4. Name the machine here, and check the path to it:

   ```toml
   [machines.workshop]
   ssh = "operator@workshop"
   mode = "dispatch"
   dispatch = "dispatch"   # a full path, when a non-interactive ssh has no PATH to it
   ```

   ```
   dispatch doctor
   ```

5. Send it a run. `--dir` is a path on `workshop`:

   ```
   dispatch run brief.md --on workshop --dir /srv/checkout --write
   ```

`dispatch doctor` asks two questions per machine and no more: whether a
`BatchMode=yes` ssh reaches it and runs a trivial command, and in `dispatch`
mode whether `<dispatch> --version` answers over that same ssh. A machine's
green line says nothing about whether it has your lanes, a logged-in vendor
CLI, or a tmux.

## Config

One user file, `~/.config/dispatch/config.toml`, or whatever `DISPATCH_CONFIG`
names. Nothing is read from the task directory: a task repository is untrusted
input, and a checkout that could name a lane would be choosing its own sandbox.
Per-repository routing is a second user file that `DISPATCH_CONFIG` selects.

```toml
[general]
default_lane = "astra@medium"   # default: the lane in the `medium` slot
substrate = ""                  # default: "", meaning detect herdr, tmux, headless
prompt_preamble = ""            # default: the standing preamble, which this replaces

[caps]
machine = 16                    # live workers on this machine
session = 4                     # live workers from one orchestrating session

[policy]
default_deadline = "30m"        # check-in interval when --deadline is absent
human_hand_timeout = "30m"      # wait at a dialog only a human can answer; "forever" allowed
blank_metered_keys = false      # blank the driver's listed credential variables in the worker's environment
depth1_lanes = ["astra"]        # lane keys, each at any effort; omit to take the `light` slot's key

[worker_env]                    # extra variables every worker starts with; default: the one below
CLAUDE_CODE_PROMPT_CACHE_TTL = "5m"   # "" takes a shipped variable out
TMPDIR = "/scratch/agents"      # your own are laid over the shipped ones

[lanes.astra]                   # a [lanes] table replaces the built-in set wholesale
driver = "codex"                # codex, claude, or grok; required
model = "gpt-6-astra"           # required
efforts = ["medium", "high"]    # required, at least one
tier = "default"                # the standing service tier; default: none
alt_tiers = ["default", "priority"]   # tiers a `:suffix` may select; default: none
tier_aliases = { fast = "priority" }  # other spellings of an offered tier; default: none
machine = ""                    # a machine this lane always runs on; default: local

[machines.workshop]
ssh = "operator@workshop"       # required
mode = "dispatch"               # or "shell"; default: "dispatch"
home = "~/.dispatch"            # the remote DISPATCH_HOME
dispatch = "dispatch"           # the remote dispatch command
shell = "posix"                 # "powershell" for Windows SSH; dispatch mode only

[board]                         # every slot is optional
light = "astra@medium"
medium = "astra@medium"
high = "astra@high"
genius = "astra@high:priority"
# blindspot stays empty: one lane is not its own second opinion

[board.when]
high = "work meant to be merged"   # replaces the standing line for that slot
```

`dispatch lanes` prints exactly what your `[lanes]` table says, because config
lanes replace the built-in set rather than merging into it. Every validation
error names the key and the file.

`dispatch init --preset <name>` writes this file from a preset: `balanced` (the
default, one vendor per role), `openai-only`, `anthropic-only`, and `all-genius`
(one lane in every slot).

Environment overrides: `DISPATCH_CONFIG` points at another user file,
`DISPATCH_HOME` relocates `~/.dispatch`, `DISPATCH_SUBSTRATE` pins the substrate
ahead of `[general] substrate`, `DISPATCH_SESSION` names the session the
per-session cap counts a run against, in place of the OS session it would be
derived from, and `AGENT_DEPTH` is the ladder marker a spawned worker inherits.

### Policy defaults

| Default | Value | Change it with |
| --- | --- | --- |
| Live workers per machine | 16 | `[caps] machine` |
| Live workers per session | 4 | `[caps] session` |
| Check-in interval | `30m` | `[policy] default_deadline`, or `--deadline` per run |
| Wait at a dialog only a human can answer | `30m` | `[policy] human_hand_timeout`, `forever` allowed |
| Metered key blanking | off | `[policy] blank_metered_keys = true` |
| Lanes a depth-1 worker may spawn | the `light` slot's lane key, at any effort | `[policy] depth1_lanes` |
| Ladder ceiling | depth 2, which spawns nothing | fixed |

A deadline is when a run gets checked on, not when it dies. At each interval the
run is judged on signals that cost nothing (screen movement, CPU, whether the
deliverable exists), and a working worker is bought another interval. Only a
demonstrably dead, stuck, or blocked one is ended, so `--deadline` caps no
runtime.

`[worker_env]` is the environment every worker starts with on top of its
shell's. It ships with `CLAUDE_CODE_PROMPT_CACHE_TTL = "5m"`, for any `claude` a
worker runs: Claude Code treats that as a main conversation, which on a
subscription defaults to the one-hour prompt cache, and a worker that runs its
brief start to finish and is never resumed pays that cache's dearer writes on
every turn and never uses the hour. Your own variables are laid over the shipped
ones, an empty value takes one out, and `AGENT_DEPTH`, `DISPATCH_SESSION`, and
`DISPATCH_RUN` are dispatch's own and cannot be set here.

Metered key blanking empties the driver's listed credential variables in the
environment the worker is launched with. That is all it does: which account a
run is billed to is still decided by every other credential source, a CLI's own
stored login and any credential file included.

The depth ladder is enforced against the environment rather than a config file.
A worker inherits `AGENT_DEPTH` from whatever spawned it, a depth-1 worker may
spawn any effort or tier of the `light` slot's lane key unless `[policy]
depth1_lanes` names other keys, and a depth-2 worker may spawn nothing. An
unreadable marker is refused rather than guessed at.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The command did what was asked. For a foreground `run` or `continue`, and for a `wait` that saw the run end, that is a run that finished and wrote its deliverable. `run --bg` returns it for a worker that started, and `status`, `board`, and the other reads for a question answered. |
| 1 | The run failed, or `doctor` found something broken. |
| 2 | dispatch refused or could not proceed: a usage error, a bad config file, an unknown lane, a cap refusal, a missing run, or a worker that would not start. |
| 3 | The worker refused the brief: its answer starts with `ABORT:`. |
| 4 | A `wait` reached its `--give-up`. The run carries on. |

## The run record

One run is one directory under `~/.dispatch/runs/<run id>/`:

| File | What it holds |
| --- | --- |
| `run.json` | The record: lane, options, state, exit code, session id, check-in history. Replaced atomically, never edited in place. |
| `brief.md` | The brief, as it was passed. |
| `cmd.txt` | The argv the vendor CLI was launched with, quoted for a shell. |
| `prompt.txt` | What dispatch typed into the worker: the preamble, the brief's path, and where to write the answer. |
| `out.md` | The worker's answer, the one file read back as the result. After a run that ended without one, dispatch leaves its copy of the last screen here instead. |
| `out.json` | The deliverable of a `--schema` run, mirrored into `out.md`. |
| `status.log` | The run's timeline: state changes, check-in verdicts, and what dispatch did when. |
| `screen.log` | What was on the worker's screen, as far back as the substrate keeps it. |
| `pane.log` | A headless worker's captured stdout and stderr. |
| `worker.rc` | A headless worker's exit status, written by the relay that ran it. |
| `watcher.log` | Anything a background run's detached watcher printed, which is where it says why it could not start. |
| `owner.lock`, `watcher.lock` | Advisory locks. A run's liveness is a held lock, never a bare pid. |
| `remote-run.json` | For a run placed on a machine: that machine's own record, copied down at the end. |

## Contributing

Issues and pull requests are welcome. The package is stdlib-only Python 3.11+,
and a change that adds a dependency has to argue for it first. The test runner
is not part of it, so install it once with `pip install -e '.[test]'`, then run
the suite with `python3 -m pytest tests -q -n auto`; no case launches a real
vendor CLI, a real multiplexer, or a real ssh, and a new one should not either.
`docs/architecture.md` is the map of the code: what a driver is, what a
substrate is, and what a new one has to implement. MIT licensed.
