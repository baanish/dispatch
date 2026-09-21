## Workers

Before a substantial task, name the piece another worker can finish while you
carry on, and hand it to `dispatch`: one worker per command, permission-limited, capped,
and recorded under `~/.dispatch/runs/`. Keep small edits and tightly coupled
decisions in-process. Read the full instructions with `dispatch skill` before
the first launch.

- Route by board slot, never by a model name you remember. `dispatch board`
  prints the five slots (`light`, `medium`, `high`, `blindspot`, `genius`) and
  the lane filling each; read it once per session and pass that lane string.
- The brief is a file: `dispatch run <lane> brief.md --dir <repo> [--write]
  [--bg]`. Name the paths, the file ownership, the authorized side effects, the
  deliverable, and what done means, or the worker decides those for you.
- The worker's answer is `out.md` in its run directory, and nothing it leaves on
  screen is read as one. A foreground run prints it; `--bg` prints the run id
  and then the run directory, and `dispatch wait <id>` blocks for it. Pair every
  background launch with a waiter, and read the answer before integrating it.
- `dispatch status` lists runs. Correct a live worker with `dispatch steer <id>
  <message>`, ask a finished one for more with `dispatch continue <id>
  <message>`, and stop one with `dispatch kill <id>`.
- Caps are 4 live workers per session and 16 per machine, and a worker you spawn
  runs one rung down the depth ladder: at depth 1 only the `light` slot's lane
  key, at any effort, and at depth 2 nothing. Never work around either.
