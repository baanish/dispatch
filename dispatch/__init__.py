"""dispatch: one worker spawn, one deterministic command.

The package is layered so that each vendor CLI and each place a worker can live
is a plug-in rather than a branch:

- `lanes`, `records`, `caps`, `policy`, `prompt`: what a run is and what it is
  allowed to do, with no knowledge of any vendor or terminal.
- `drivers`: one adapter per vendor CLI (codex, claude, grok, pi).
- `substrates`: one adapter per place a worker process can live (herdr, tmux,
  headless).
- `runner`: the run lifecycle, driven against a Driver and a Substrate.
- `cli`: the verbs.

docs/architecture.md is the map.
"""

__version__ = "0.2.0"
