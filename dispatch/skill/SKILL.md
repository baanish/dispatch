---
name: dispatch
description: Hand a bounded task to an AI-agent worker on another vendor CLI with the `dispatch` command. Read before spawning a worker, before asking for an independent review, before running work on another machine, and when a live run has to be watched, corrected, continued, or killed.
---

# dispatch

`dispatch` runs one worker per command on the vendor CLIs installed here
(`codex`, `claude`, `grok`), limits its permissions, caps how many can be live at once,
and records what happened under `~/.dispatch/runs/<run id>/`. You keep
decomposition, authorization, integration, and verification. A worker's result
is evidence to check, never permission to widen the task.

## Decide whether to dispatch

Before a substantial task, name the piece another worker can finish while you
carry on, and launch it once its inputs and ownership are settled. Dispatch a
bounded task when a second opinion from a vendor that does not share your blind
spots is worth having, or when the work belongs on another machine. Routine
delegation inside the task you were given needs no approval.

Work in-process when the change is small, when the context costs more to
transfer than to use, or when each next step turns on your own judgement. Never
dispatch a duplicate of work you are already doing. For the same mechanical
change in many places, write one rerunnable script instead of one worker per
instance.

Give parallel workers separate files or separate checkouts, and tell each one
that others are editing the repository and must not have their work undone.
Integration stays with you.

## Route by slot

Run `dispatch board` once per session, then pass the lane string that board
prints for the slot you want. A slot name is not valid `run` syntax.

| Slot | Route to it when |
| --- | --- |
| `light` | The task is a bounded lookup, extraction, or mechanical edit with a clear check. |
| `medium` | The task is ordinary implementation or investigation with a defined outcome. |
| `high` | The task is hard implementation or analysis, or its result is meant to be merged. |
| `blindspot` | You want assumptions challenged, or a review independent of your own reasoning. |
| `genius` | One hard call remains after you have isolated the problem. |

Slots come from the operator's config. A slot may be empty, and one lane may
fill several, so the board promises neither availability nor an independent
model. Take the least demanding filled slot that can do the task. When the slot
you want is empty, use another filled slot or work in-process; never invent a
lane or edit the board.

A lane reads `key@effort[:tier]`. Do not assemble one out of a vendor's model
names.

## Write the brief to a file

`dispatch run` takes a path, not inline instructions. Write for a worker that
has not seen this conversation and cannot ask you anything:

- The problem, the outcome you expect, and the paths that carry the context.
- The working directory, which files are this worker's to change, and whose
  concurrent edits it must preserve.
- Hard constraints, and which side effects are authorized: edits, commands,
  network, commits, writes outside the tree.
- The deliverables, their exact paths and format, and the condition that means
  done.
- The narrowest validation bar that gives real confidence, written as the
  command to run. For a long tool loop, a stop condition and a retry cap.
- What must stop the worker: missing authority, or a material scope choice.
  Everything else it resolves itself, mechanical problems included.

Leave out conversation dumps, text it can read at a path, and setup that belongs
in the invocation. Split build, verification, and release when they need
different permissions.

dispatch itself tells the worker where to write the answer. Only `out.md` in the
run directory is captured (`out.json` under `--schema`); a message left on
screen is lost. A worker that hits its stop condition writes `ABORT:` and the
question there instead, which is terminal and is never retried.

## Launch

```sh
dispatch run --dir /path/to/project --write --bg --deadline 20m "$lane" /path/to/brief.md
```

Omit `--write` for read-only work. Omitting the lane takes the configured
default, which may not be the slot you meant.

| Flag | What it does |
| --- | --- |
| `--dir PATH` | The working directory and sandbox root. Defaults to the current directory. |
| `--write` | A workspace-write sandbox. Read-only otherwise. |
| `--net` | Network inside a codex write sandbox. Needs `--write`, and claude and grok lanes refuse it. |
| `--add-dir PATH` | Extra writable tree on codex lanes with `--write`, repeatable. Claude adds a tool directory under its permission rules. Grok ignores it. `continue` does not forward extra directories. |
| `--on NAME` | Place the run on a configured machine, ahead of any machine the lane names. |
| `--bg` | Return once the worker is under way, printing the run id. |
| `--deadline 20m` | The check-in interval. Defaults to the configured one, `30m` out of the box. |
| `--schema PATH` | Ask for JSON output shaped by this schema file. dispatch checks that the answer parses as JSON, not that it conforms; only a headless codex run has the schema enforced, by codex itself. |
| `--out PATH` | Copy the answer here as well. The parent directory must exist. |
| `--image PATH` | Attach an image. codex lanes only. |

Drivers do not offer the same permissions. Only a codex lane runs in an OS
sandbox (`read-only`, or `workspace-write` under `--write`). A claude lane is
Claude Code's auto permission mode, with only `Read`, `Grep`, and `Glob`
pre-approved when `--write` is off, and a grok lane is auto permission mode
with nothing blocking a write: on both, read-only is an instruction the brief
must also state, not a boundary. A grok lane refuses `--write`, so route
editing work to a codex or claude lane. An access flag authorizes nothing
the brief did not ask for. Fix a mismatch by choosing the right lane, never by
loosening a sandbox.

A machine in the default `dispatch` mode runs the work through its own dispatch,
so dispatch, the vendor CLI, and the task's files all have to be there already:
`--on` copies the brief and nothing else. `status`, `wait`, and `logs` query
that machine until the run ends, then read the mirror copied down here. A
machine configured in `shell` mode is driven from here over ssh instead, needs
`--dir` to name a path on that machine, and refuses `--image`.

## Follow every run to a result

Use `--bg` when you have work of your own to do. Keep the run id, and start
`dispatch wait <id>` through your harness's background facility in the same
turn, then resume that waiter to collect the result. Use the waiter rather than
a polling loop of your own. A clean launch says nothing about whether the worker
finished.

| Command | What it does |
| --- | --- |
| `dispatch wait <id> [--give-up 45m]` | Block until the run ends, then print the status tail and the answer. Giving up ends the waiter only. |
| `dispatch status` | One line per run: state and check-ins. |
| `dispatch watch [<id>] [-f] [--deep]` | The wall, one run followed, or one snapshot of state, logs, and the answer so far. |
| `dispatch logs <id> [-f]` | Print or follow the status log. |
| `dispatch steer <id> "correction"` | Correct a live worker mid-turn. |
| `dispatch continue <id> "answer" [--bg]` | A new turn on a finished run's session, including answering an `ABORT:`. It gets its own run id. |
| `dispatch inspect <id>` | Reopen a finished session and attach, without starting a turn. |
| `dispatch kill <id>` | Stop the worker's process tree and close its home. |
| `dispatch board`, `dispatch lanes`, `dispatch doctor` | The board, the lane table, and what answers on this machine. |

Steer a live worker that is going the wrong way; continue a finished one that
needs more. Both keep the session and its context, which a fresh `run` pays for
again. A long message goes in with `--message-file PATH` on either verb.

Read the whole answer file and open the artifacts it names before accepting a
result. Every run you launch gets a terminal state observed, killed ones
included.

Exit codes: `0` done, `1` failed, `2` usage error, `3` the worker wrote an
`ABORT:`, `4` a `wait` gave up while the run carried on. An `ABORT:` is a
question: answer it with `continue` rather than rerunning the brief. When a
finished run captured no vendor session id, `continue` cannot resume it, so
carry the result into a fresh brief instead.

A run directory holds `brief.md`, `prompt.txt`, `run.json`, `out.md`
(`out.json` under `--schema`), `status.log`, and `screen.log`. Read them before
judging a failure: between them they say whether the worker refused the brief,
spent its deadline, or never started.

## Limits that refuse rather than bend

- **Caps**: 4 live workers per session and 16 per machine by default. A refusal
  names the runs holding the slots. Finish one or kill one; never work around
  the accounting.
- **Depth ladder**: `AGENT_DEPTH` puts a worker one rung below whoever spawned
  it. At depth 1 only the `light` slot may be spawned, and at depth 2 nothing
  may. Do the work yourself instead of editing the marker.
- **One worker, one deliverable.** Run workers yourself and carry each result
  into the next brief rather than having one worker spawn the next.
- **A deadline is a check-in, not a runtime cap.** A worker still showing
  progress is extended again and again, and only evidence that it is dead,
  stuck, or blocked ends it. A firm stop belongs in the brief, under your
  supervision.
- **Headless refuses live intervention.** Where the substrate is headless,
  `steer` and `inspect` refuse with one line, `continue` works through the
  vendor's resume flag, and check-ins read CPU and the deliverable only. There
  is no pane to read progress from. `dispatch doctor` names the substrate in
  force.

`dispatch skill` prints this document, and `dispatch agents-snippet` prints the
short block for a repository's `AGENTS.md`. When a launch fails on the setup,
run `dispatch doctor` and fix what it names rather than relaunching.
