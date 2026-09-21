"""What dispatch types at a pane's own shell, and how it reads the echo back.

Two substrates put a worker in a terminal pane, and both need the same three
lines of shell: the environment exports, the environment read-back, and the
exit-code probe. Those are the only places the pane's language matters, and it
is not the same language everywhere, which is what the dialect classes are for.
Everything else dispatch types (briefs, exit commands, dialog answers) goes to a
TUI, which has no shell in it at all and is untouched here.

The parsers are the other half: a pane is scrollback rather than a fresh buffer,
so an echoed marker is only an answer once it is anchored to the line that
produced it.
"""

from __future__ import annotations

import re
import shlex

from ..processes import IS_WINDOWS


# The markers the pane's own shell echoes back. The `\d` in the rc pattern is
# what separates the echoed output from the command line that produced it: the
# typed line still reads `=$?`, so only the result line can ever match.
RC_MARKER = "dispatch-rc"
RC_RE = re.compile(RC_MARKER + r"-([A-Za-z0-9]+)=(-?\d+)")
ENV_MARKER = "dispatch-env"
ENV_RE = re.compile(ENV_MARKER + r"-([A-Za-z0-9]+)=(\S*)")


# --------------------------------------------------------------------------
# Pane shell dialect
# --------------------------------------------------------------------------
#
# A pane runs the operator's login shell on POSIX and Windows PowerShell on
# Windows, so the same three lines are typed in two languages. `PANE_SHELL` is
# this platform's; a substrate that reaches a pane on another host asks that
# host's dialect for its lines instead.


class PosixPaneShell:
    """What dispatch types at a pane whose shell is bash or zsh."""

    name = "posix"

    def export_line(self, assignments):
        parts = [f"{name}={shlex.quote(value)}" if value else f"{name}="
                 for name, value in assignments]
        return "export " + " ".join(parts)

    def env_probe_line(self, marker, token, depth_var, keys):
        """`${VAR:+set}` prints nothing for a blank key, so blank reads as empty."""
        flags = "".join(f"${{{var}:+set}}" for var in keys)
        return f"echo {marker}-{token}=${depth_var}:{flags}"

    def rc_probe_line(self, marker, token):
        return f"echo {marker}-{token}=$?"

    def command_line(self, prefix, argv):
        parts = list(prefix) + list(argv)
        return " ".join(shlex.quote(part) for part in parts)

    def parse_line(self, text):
        """The inverse of the builders above: what this shell would do with a line.

        Only a stub daemon needs it, and it lives here so that a simulated shell
        cannot drift from the lines dispatch actually types. Returns (kind,
        payload), kind empty for anything else.
        """
        if text.startswith(f"echo {ENV_MARKER}-"):
            return ("env-probe", text.split(f"echo {ENV_MARKER}-", 1)[1]
                    .split("=", 1)[0])
        if text.startswith(f"echo {RC_MARKER}-"):
            return ("rc-probe", text.split(f"echo {RC_MARKER}-", 1)[1]
                    .split("=", 1)[0])
        if text.startswith("export "):
            return ("export", [item.partition("=")[::2]
                               for item in shlex.split(text[len("export "):])])
        return ("", None)


class PowerShellPaneShell:
    """The same lines for a pane running Windows PowerShell.

    Every one of these was typed into a live pane on Windows and read back off
    the screen before it was written down here.
    """

    name = "powershell"

    def quote(self, text):
        """A single-quoted PowerShell string: the only escape in one is `''`."""
        return "'" + str(text).replace("'", "''") + "'"

    def export_line(self, assignments):
        return "; ".join(f"$env:{name}={self.quote(value)}"
                         for name, value in assignments)

    def env_probe_line(self, marker, token, depth_var, keys):
        """A bare string is PowerShell's `echo`, and `$(...)` is its expansion."""
        flags = "".join("$(if($env:%s){'set'})" % var for var in keys)
        return f'"{marker}-{token}=$($env:{depth_var}):{flags}"'

    def rc_probe_line(self, marker, token):
        """`$?` here is a boolean, not a status: the exit code is $LASTEXITCODE.

        `+0` is what makes the marker parseable on the first probe of a session,
        where $LASTEXITCODE is still $null and would otherwise print nothing at
        all rather than a number.
        """
        return f'"{marker}-{token}=$($LASTEXITCODE+0)"'

    def command_line(self, prefix, argv):
        """`&` because a quoted first token is a string expression, not a command.

        The POSIX `command` prefix has no analog worth typing here: PowerShell
        resolves functions ahead of applications the same way, but the escape is
        a mouthful for a path that only runs when the substrate's own spawn has
        already failed and said so loudly.
        """
        return "& " + " ".join(self.quote(part) for part in argv)

    def parse_line(self, text):
        if text.startswith(f'"{ENV_MARKER}-'):
            return ("env-probe", text.split(f'"{ENV_MARKER}-', 1)[1]
                    .split("=", 1)[0])
        if text.startswith(f'"{RC_MARKER}-'):
            return ("rc-probe", text.split(f'"{RC_MARKER}-', 1)[1]
                    .split("=", 1)[0])
        if text.startswith("$env:"):
            pairs = []
            for item in text.split("; "):
                name, _, value = item[len("$env:"):].partition("=")
                pairs.append((name, value.strip("'").replace("''", "'")))
            return ("export", pairs)
        return ("", None)


PANE_SHELL = PowerShellPaneShell() if IS_WINDOWS else PosixPaneShell()


def anchor_after(text, after):
    """The screen printed after the last occurrence of `after`, or None.

    A pane is scrollback, not a fresh buffer, so anything parsed off it has to
    be anchored to the command that produced it.
    """
    body = text or ""
    if not after:
        return body
    index = body.rfind(after)
    return None if index < 0 else body[index + len(after):]


def parse_env_echo(text, token, after=""):
    """The value the shell echoed for an env probe, or None."""
    body = anchor_after(text, after)
    if body is None:
        return None
    value = None
    for found_token, found in ENV_RE.findall(body):
        if found_token == token:
            value = found
    return value


def parse_rc_echo(text, token, after=""):
    """The exit code the shell echoed back, or None if it has not yet.

    Without the anchor the first matching line wins, and a run whose own output
    happened to contain a marker earlier in the scrollback would be journaled
    with that number instead of its real exit code.
    """
    body = anchor_after(text, after)
    if body is None:
        return None
    rc = None
    for found_token, value in RC_RE.findall(body):
        if found_token == token:
            rc = int(value)
    return rc


def rc_token(run_id):
    """A per-run marker so one pane's echo can never be read as another's."""
    return re.sub(r"[^A-Za-z0-9]", "", str(run_id))[-16:] or "run"
