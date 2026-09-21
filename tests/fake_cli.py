#!/usr/bin/env python3
"""A stand-in for a vendor CLI's one-shot mode, copied onto PATH as codex,
claude, and grok.

It models what a real headless worker does and nothing else: it finds the
deliverable path in the prompt it was handed, writes its answer there, prints
that answer on stdout, and exits. That is exactly the instruction dispatch
gives a real worker, which is what makes a run against this a run against the
contract rather than against a mock.

Each driver's headless form carries the prompt in its own place, so where to
look for it is the one thing this branches on.

Environment knobs, for the cases that need a worker to behave badly:

- `FAKE_CLI_RC`: exit with this status instead of 0.
- `FAKE_CLI_SECONDS`: stay alive this long before answering.
- `FAKE_CLI_MESSAGE`: say this instead of the default.
- `FAKE_CLI_NO_DELIVERABLE`: print the answer and write no file.
"""

import os
import re
import sys
import time
from pathlib import Path

MESSAGE = os.environ.get("FAKE_CLI_MESSAGE") or "the fake worker is finished"
# On stderr, so a test can prove both streams reach pane.log.
NOTE = "fake-cli: starting"


def prompt_of(name, argv):
    """The brief this launch was handed.

    `claude -p <prompt>` and `grok --single <prompt>` put it behind their own
    flag; `codex exec` takes it as the last argument.
    """
    if name == "claude" and "-p" in argv:
        return argv[argv.index("-p") + 1]
    if name == "grok" and "--single" in argv:
        return argv[argv.index("--single") + 1]
    return argv[-1] if argv else ""


def deliverable_of(prompt):
    """The file the brief told this worker to write, or None."""
    match = re.search(r"final answer to (\S+)", prompt or "")
    return Path(match.group(1)) if match else None


def main():
    name = Path(sys.argv[0]).name
    argv = sys.argv[1:]
    print(NOTE, file=sys.stderr, flush=True)
    seconds = float(os.environ.get("FAKE_CLI_SECONDS") or 0)
    if seconds:
        time.sleep(seconds)
    targets = []
    if not os.environ.get("FAKE_CLI_NO_DELIVERABLE"):
        written = deliverable_of(prompt_of(name, argv))
        if written is not None:
            targets.append(written)
    if "-o" in argv:
        # codex `exec -o` writes the final message itself.
        targets.append(Path(argv[argv.index("-o") + 1]))
    for target in targets:
        target.write_text(MESSAGE + "\n", encoding="utf-8")
    print(MESSAGE, flush=True)
    return int(os.environ.get("FAKE_CLI_RC") or 0)


if __name__ == "__main__":
    sys.exit(main())
