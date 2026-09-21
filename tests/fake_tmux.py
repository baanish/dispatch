#!/usr/bin/env python3
"""A fake `tmux` binary: records what dispatch asked it for, and models one pane.

The test copies this file onto PATH as `tmux` and points `FAKE_TMUX_STATE` at a
JSON file holding the server's whole state. Every invocation is appended to
`<state>.log` as one JSON line, so a test can assert on the command lines
themselves: the literal flag on a brief, the join flag on a capture, the session
name a run was given.

The pane is modelled rather than mocked. Typed text accumulates, an Enter
commits the line to the screen, and a line that parses as one of dispatch's own
shell probes is answered the way a real shell would answer it. The dialect that
answers is the one dispatch types with, so the simulation cannot drift from it.

The tmux behaviours that matter here are the surprising ones, and they are
reproduced exactly: `display-message` against a missing pane prints an empty
line and exits 0, `capture-pane` against one fails, a duplicate session name is
refused, and once the last session is gone every command answers "no server
running".
"""

import json
import os
import sys

STATE_PATH = os.environ["FAKE_TMUX_STATE"]

with open(STATE_PATH, encoding="utf-8") as handle:
    STATE = json.load(handle)

sys.path.insert(0, STATE["repo_root"])

from dispatch.substrates.paneshell import (ENV_MARKER, PANE_SHELL,  # noqa: E402
                                           RC_MARKER)


def save():
    with open(STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(STATE, handle)


def record(argv):
    with open(STATE_PATH + ".log", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(argv) + "\n")


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def pane_for(target):
    """The pane a `-t` target names: a pane id, or a session's only pane."""
    if target in STATE["panes"]:
        return STATE["panes"][target]
    for pane in STATE["panes"].values():
        if pane["session"] == target:
            return pane
    return None


def expand(fmt, pane):
    for key in ("pane_id", "pane_pid", "pane_tty", "session_name"):
        source = pane["session"] if key == "session_name" else pane[key]
        fmt = fmt.replace("#{%s}" % key, str(source))
    return fmt


def option(args, flag):
    """The value of `-x value`, or empty."""
    return args[args.index(flag) + 1] if flag in args else ""


def commit_line(pane):
    """What the pane's shell does with the line that was just submitted."""
    text, pane["pending"] = pane["pending"], ""
    pane["screen"] += text + "\n"
    for reply in pane["replies"]:
        if reply["match"] and reply["match"] in text:
            pane["screen"] += reply.get("screen", "")
            if "running" in reply:
                pane["running"] = reply["running"]
    kind, payload = PANE_SHELL.parse_line(text)
    if kind == "rc-probe":
        pane["screen"] += f"{RC_MARKER}-{payload}={pane['rc']}\n"
    elif kind == "env-probe":
        # The shell expands the rung, then a marker for each metered key that is
        # not blank.
        rung = pane["env"].get("AGENT_DEPTH", "")
        live = "".join("set" for var in STATE["metered_vars"] if pane["env"].get(var))
        pane["screen"] += f"{ENV_MARKER}-{payload}={rung}:{live}\n"
    elif kind == "export":
        for name, value in payload:
            pane["env"][name] = value
        # A shell whose rc files re-export a key after dispatch blanked it.
        pane["env"].update(pane["sticky_env"])


def new_session(args):
    name = option(args, "-s")
    if any(pane["session"] == name for pane in STATE["panes"].values()):
        fail(f"duplicate session: {name}")
    pane_id = "%" + str(STATE["next_pane"])
    STATE["next_pane"] += 1
    env = {}
    for index, arg in enumerate(args):
        if arg == "-e":
            key, _, value = args[index + 1].partition("=")
            env[key] = value
    pane = {"pane_id": pane_id, "session": name, "pane_pid": 4200 + STATE["next_pane"],
            "pane_tty": f"/dev/ttyfake{STATE['next_pane']}", "screen": "",
            "pending": "", "env": env, "rc": STATE["default_rc"], "running": False,
            "swallow": False, "height": int(option(args, "-y") or 24),
            "sticky_env": dict(STATE["sticky_env"]),
            "cwd": option(args, "-c"), "replies": list(STATE["replies"])}
    # A tmux too old for `-e` is given the exports as a shell command instead,
    # and `sh` runs them before the login shell it execs.
    command = args[-1] if not args[-1].startswith("-") and args[-2] != "-F" else ""
    if command:
        kind, payload = PANE_SHELL.parse_line(command.split(";")[0])
        if kind == "export":
            pane["env"].update(dict(payload))
    STATE["panes"][pane_id] = pane
    if "-P" in args:
        print(expand(option(args, "-F"), pane))


def send_keys(args):
    pane = pane_for(option(args, "-t"))
    if pane is None:
        fail(f"can't find pane: {option(args, '-t')}")
    if "-l" in args:
        pane["pending"] += args[-1]
        return
    for key in args[args.index("-t") + 2:]:
        if key != "Enter":
            continue
        if pane["swallow"]:
            # A TUI that is not listening: the line sits in the box.
            continue
        commit_line(pane)


def capture_pane(args):
    pane = pane_for(option(args, "-t"))
    if pane is None:
        fail(f"can't find pane: {option(args, '-t')}")
    lines = pane["screen"].split("\n")
    if "-S" not in args:
        lines = lines[-pane["height"]:]
    # capture-pane pads its answer out to the height of the pane.
    lines += [""] * max(0, pane["height"] - len(lines))
    print("\n".join(lines))


def display_message(args):
    pane = pane_for(option(args, "-t"))
    # tmux prints an empty line and exits 0 for a target that no longer exists.
    print(expand(args[-1], pane) if pane is not None else "")


def list_panes(args):
    for pane in STATE["panes"].values():
        print(expand(option(args, "-F"), pane))


def kill_session(args):
    name = option(args, "-t")
    found = [pane_id for pane_id, pane in STATE["panes"].items()
             if pane["session"] == name]
    if not found:
        fail(f"can't find session: {name}")
    for pane_id in found:
        del STATE["panes"][pane_id]


def main(argv):
    record(argv)
    if not argv:
        fail("usage: tmux [command]")
    command = argv[0]
    if command == "-V":
        print(STATE["version"])
        return
    if command == "start-server":
        if not STATE["can_start_server"]:
            fail("error connecting to /fake/tmux (No such file or directory)")
        return
    if command == "new-session":
        return new_session(argv)
    # Every other command needs a server, and tmux keeps one only while a
    # session is alive.
    if not STATE["panes"]:
        fail("no server running on /fake/tmux")
    if command == "send-keys":
        return send_keys(argv)
    if command == "capture-pane":
        return capture_pane(argv)
    if command == "display-message":
        return display_message(argv)
    if command == "list-panes":
        return list_panes(argv)
    if command == "kill-session":
        return kill_session(argv)
    if command in ("kill-pane", "attach"):
        return None
    fail(f"unknown command: {command}")


try:
    main(sys.argv[1:])
finally:
    save()
