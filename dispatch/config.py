"""Configuration: a user's fleet, as one TOML file.

The code ships a lane table, a policy, and a preamble; this module is how an
operator replaces them. One file does it: the user file,
`~/.config/dispatch/config.toml`, or whatever `DISPATCH_CONFIG` names. It owns
everything: lanes, board, machines, caps, policy, preamble.

Two values are read off the board rather than kept twice: the default lane is
the medium slot unless a file names one, and the lanes a depth-1 worker may
spawn are the light slot unless `[policy] depth1_lanes` names them. A config
that replaces the lane table therefore does not have to restate either.

Every validation error names the key and the file, because the operator is
looking at that file when they read the message.

`load_config()` reads and validates; `apply_config()` installs the result into
the modules that hold it (`lanes`, `policy`, `prompt`) and remembers it for the
verbs that need to read a board or a machine list. Loading and applying are
separate so that a test, or a second config, can be validated without changing
what the process is running on.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from .board import SLOTS
from .drivers import driver_names
from .errors import DispatchError
from .lanes import (DEFAULT_LANE, DEFAULT_LANE_TABLE, LaneSpec, resolve_lane,
                    set_default_lane, set_lane_table)
from .policy import FOREVER, Policy, parse_deadline, set_policy
from .prompt import DEFAULT_PREAMBLE, set_preamble
from .remote import (DEFAULT_REMOTE_DISPATCH, DEFAULT_REMOTE_HOME,
                     DISPATCH_MODE, LOCAL, MACHINE_MODES, REMOTE_SHELLS, Machine,
                     set_machines)

CONFIG_ENV = "DISPATCH_CONFIG"
DEFAULT_PRESET = "balanced"

# Lane keys, efforts, and tiers are the tokens of `key@effort[:tier]`, so the
# grammar constrains what a config may call them.
TOKEN_RE = re.compile(r"^[a-z0-9]+$")

GENERAL_KEYS = ("default_lane", "substrate", "prompt_preamble")
CAPS_KEYS = ("machine", "session")
POLICY_KEYS = ("default_deadline", "human_hand_timeout", "blank_metered_keys",
               "depth1_lanes")
LANE_KEYS = ("driver", "model", "efforts", "tier", "alt_tiers", "tier_aliases",
             "machine")
MACHINE_KEYS = ("ssh", "dispatch", "mode", "home", "shell")
# `when` is a sibling table of the slots, so that a slot stays a lane string.
BOARD_KEYS = SLOTS + ("when",)
SECTIONS = ("general", "caps", "policy", "lanes", "machines", "board")

# Where `dispatch init` installs the skill, keyed by the parent directory whose
# presence means the tool is installed for this operator.
SKILL_HOMES = (".claude", ".codex", ".agents")
SKILL_SUBPATH = ("skills", "dispatch")
SKILL_FILE = "SKILL.md"
AGENTS_SNIPPET_FILE = "agents-snippet.md"


@dataclass(frozen=True)
class Config:
    """Everything the file decided, resolved and validated."""

    path: Path | None = None
    default_lane: str = DEFAULT_LANE
    substrate: str = ""
    preamble: str = DEFAULT_PREAMBLE
    lanes: dict = field(default_factory=lambda: dict(DEFAULT_LANE_TABLE))
    board: dict = field(default_factory=dict)
    board_notes: dict = field(default_factory=dict)
    machines: dict = field(default_factory=dict)
    policy: Policy = field(default_factory=Policy)


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def user_config_path(env=None):
    """The user file this invocation reads, `DISPATCH_CONFIG` first.

    The only config file there is. Nothing is read from the task directory,
    because a task repository is untrusted input: a checkout that could name a
    lane would be choosing its own sandbox.
    """
    env = os.environ if env is None else env
    override = (env.get(CONFIG_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "dispatch" / "config.toml"


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def load_config(env=None):
    """Read and validate the file. Never touches process state."""
    env = os.environ if env is None else env
    path = user_config_path(env)
    named = bool((env.get(CONFIG_ENV) or "").strip())
    if path.is_file():
        data = _read_toml(path)
        config = _user_config(data, path)
    elif named:
        # An explicit path that is not there is a typo, not an absent config.
        raise DispatchError(f"{CONFIG_ENV}={path}: no such file")
    else:
        data, config = {}, Config()
    # Both are settled last because both are read off the board, which the
    # section readers have to build first.
    return replace(config,
                   default_lane=config.default_lane or _derived_default_lane(config),
                   policy=_policy(data, config, path))


def _read_toml(path):
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise DispatchError(f"{path}: cannot be read: {exc}") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise DispatchError(f"{path}: not valid TOML: {exc}") from exc


def _user_config(data, path):
    _known_keys(data, SECTIONS, "", path)
    general = _table(data, "general", "", path)
    _known_keys(general, GENERAL_KEYS, "general", path)

    machines = _machines(_table(data, "machines", "", path), path)
    lane_data = _table(data, "lanes", "", path)
    if "lanes" in data and not lane_data:
        # An empty table is not an absent one. Read as absent it brought the
        # built-in lanes back, for an operator who had just removed their last.
        raise DispatchError(f"{path}: [lanes]: define at least one lane, or "
                            "remove the table to keep the built-in ones")
    lanes = _lanes(lane_data, machines, path)
    board, notes = _board(_table(data, "board", "", path), path)

    substrate = _string(general, "substrate", "general", path)
    if substrate:
        from .substrates import SUBSTRATES
        if substrate not in SUBSTRATES:
            raise DispatchError(
                f"{path}: [general] substrate: {substrate!r} is not one of "
                + ", ".join(sorted(SUBSTRATES)) + "; leave it empty to detect")
    preamble = _string(general, "prompt_preamble", "general", path) or DEFAULT_PREAMBLE

    _validate_board(board, lanes, path)
    return Config(
        path=path,
        # Empty when this file names none, and derived from the board by the
        # caller.
        default_lane=_named_default_lane(general, lanes, path),
        substrate=substrate,
        preamble=preamble,
        lanes=lanes,
        board=board,
        board_notes=notes,
        machines=machines,
    )


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------


def _machines(data, path):
    machines = {}
    for name, table in data.items():
        where = f"machines.{name}"
        _table_value(table, where, path)
        _known_keys(table, MACHINE_KEYS, where, path)
        if name == LOCAL:
            raise DispatchError(
                f"{path}: [{where}] {LOCAL!r} is the name this machine already "
                "has, and is what `--on` takes to mean here")
        ssh = _string(table, "ssh", where, path)
        if not ssh:
            raise DispatchError(f"{path}: [{where}] ssh: required, "
                                "as `user@host` or an ssh config alias")
        # ssh takes its destination positionally, so a leading `-` is read as an
        # option instead of a host. No destination holds whitespace or control
        # characters either, and both hide what a command line really says.
        if ssh.startswith("-") or any(c.isspace() or not c.isprintable()
                                      for c in ssh):
            raise DispatchError(
                f"{path}: [{where}] ssh: {ssh!r} is not a destination; give "
                "`user@host` or an ssh config alias")
        mode = _string(table, "mode", where, path) or DISPATCH_MODE
        if mode not in MACHINE_MODES:
            raise DispatchError(
                f"{path}: [{where}] mode: {mode!r} is not one of "
                + ", ".join(MACHINE_MODES))
        shell = _string(table, "shell", where, path) or "posix"
        if shell not in REMOTE_SHELLS:
            raise DispatchError(
                f"{path}: [{where}] shell: {shell!r} is not one of "
                + ", ".join(REMOTE_SHELLS))
        if mode != DISPATCH_MODE and shell != "posix":
            raise DispatchError(f"{path}: [{where}] shell: powershell needs "
                                'mode = "dispatch"; shell mode requires tmux')
        machines[name] = Machine(
            name=name, ssh=ssh, mode=mode, shell=shell,
            home=_string(table, "home", where, path) or DEFAULT_REMOTE_HOME,
            dispatch=_string(table, "dispatch", where, path)
            or DEFAULT_REMOTE_DISPATCH)
    return machines


def _lanes(data, machines, path):
    """Config lanes replace the built-in set; an absent table keeps it."""
    if not data:
        return dict(DEFAULT_LANE_TABLE)
    lanes = {}
    for key, table in data.items():
        where = f"lanes.{key}"
        _table_value(table, where, path)
        _known_keys(table, LANE_KEYS, where, path)
        _token(key, where, path, "lane key")
        driver = _string(table, "driver", where, path)
        if driver not in driver_names():
            raise DispatchError(
                f"{path}: [{where}] driver: {driver!r} is not a driver; "
                f"drivers: {', '.join(sorted(driver_names()))}")
        model = _string(table, "model", where, path)
        if not model:
            raise DispatchError(f"{path}: [{where}] model: required")
        efforts = tuple(_string_list(table, "efforts", where, path))
        if not efforts:
            raise DispatchError(f"{path}: [{where}] efforts: required, "
                                'as a list such as ["medium", "high"]')
        for effort in efforts:
            _token(effort, where, path, "effort")
        tier = _string(table, "tier", where, path)
        alt_tiers = tuple(_string_list(table, "alt_tiers", where, path))
        for name in (tier, *alt_tiers):
            if name:
                _token(name, where, path, "tier")
        aliases = _string_map(table, "tier_aliases", where, path)
        for alias, target in aliases.items():
            if target not in alt_tiers:
                raise DispatchError(
                    f"{path}: [{where}] tier_aliases: {alias!r} points at "
                    f"{target!r}, which is not in alt_tiers")
        machine = _string(table, "machine", where, path)
        if machine and machine not in machines:
            raise DispatchError(
                f"{path}: [{where}] machine: no machine named {machine!r}; "
                f"machines: {', '.join(sorted(machines)) or 'none configured'}")
        lanes[key] = LaneSpec(driver=driver, model=model, efforts=efforts,
                              tier=tier, alt_tiers=alt_tiers,
                              tier_aliases=aliases or None, machine=machine)
    return lanes


def _board(data, path):
    _known_keys(data, BOARD_KEYS, "board", path, extra=(
        f"slots: {', '.join(SLOTS)}"))
    board = {}
    for slot in SLOTS:
        text = _string(data, slot, "board", path)
        if text:
            board[slot] = text
    notes = _string_map(data, "when", "board", path)
    for slot in notes:
        if slot not in SLOTS:
            raise DispatchError(
                f"{path}: [board.when] {slot}: not a slot; "
                f"slots: {', '.join(SLOTS)}")
    return board, notes


def _policy(data, config, path):
    """Caps and rules from the user file, with the ladder read off the board."""
    lanes, board = config.lanes, config.board
    caps = _table(data, "caps", "", path)
    _known_keys(caps, CAPS_KEYS, "caps", path)
    rules = _table(data, "policy", "", path)
    _known_keys(rules, POLICY_KEYS, "policy", path)

    changes = {}
    for key, field_name in (("machine", "machine_cap"), ("session", "session_cap")):
        if key in caps:
            value = _integer(caps, key, "caps", path)
            if value < 1:
                raise DispatchError(f"{path}: [caps] {key}: must be at least 1")
            changes[field_name] = value

    if "default_deadline" in rules:
        changes["default_deadline"] = _deadline(rules, "default_deadline", path,
                                                allow_forever=False)
    if "human_hand_timeout" in rules:
        changes["hand_timeout"] = _deadline(rules, "human_hand_timeout", path)
    if "blank_metered_keys" in rules:
        changes["blank_metered_keys"] = _boolean(rules, "blank_metered_keys",
                                                 "policy", path)

    changes["depth1_lane_keys"] = _depth1_keys(rules, lanes, board, path)
    return replace(Policy(), **changes)


def _depth1_keys(rules, lanes, board, path):
    """Which lanes a depth-1 worker may spawn.

    The board decides it: rung 1 gets the light slot and nothing above it. An
    explicit `depth1_lanes` outranks the board, and a default that names a lane
    this config does not define means the rung spawns nothing, which fails the
    ladder closed rather than on a lane that cannot resolve.
    """
    if "depth1_lanes" in rules:
        keys = tuple(_string_list(rules, "depth1_lanes", "policy", path))
        for key in keys:
            if key not in lanes:
                raise DispatchError(
                    f"{path}: [policy] depth1_lanes: no lane named {key!r}; "
                    f"lanes: {', '.join(sorted(lanes))}")
        return keys
    if board.get("light"):
        return (resolve_lane(board["light"], lanes).key,)
    return tuple(key for key in Policy().depth1_lane_keys if key in lanes)


def _validate_board(board, lanes, path):
    for slot, text in board.items():
        try:
            resolve_lane(text, lanes)
        except DispatchError as exc:
            raise DispatchError(f"{path}: [board] {slot}: {exc}") from exc


def _named_default_lane(general, lanes, path):
    """The default lane a file names, checked against that file's lane table."""
    text = _string(general, "default_lane", "general", path)
    if not text:
        return ""
    try:
        resolve_lane(text, lanes)
    except DispatchError as exc:
        raise DispatchError(f"{path}: [general] default_lane: {exc}") from exc
    return text


def _derived_default_lane(config):
    """What `dispatch run` takes when no file named a lane.

    The medium slot, which is the role a default worker plays. A config with no
    board falls back to the shipped default, and then to its own first lane,
    because a lane table that replaced the built-in one has no astra in it.
    """
    if config.board.get("medium"):
        return config.board["medium"]
    try:
        resolve_lane(DEFAULT_LANE, config.lanes)
        return DEFAULT_LANE
    except DispatchError:
        pass
    key, spec = next(iter(config.lanes.items()))
    return f"{key}@{spec.efforts[0]}"


# --------------------------------------------------------------------------
# Typed readers. Each names the key and the file, because that is what the
# operator is looking at when the message arrives.
# --------------------------------------------------------------------------


def _label(where, key):
    return f"[{where}] {key}" if where else f"[{key}]"


def _known_keys(table, allowed, where, path, extra=""):
    for key in table:
        if key not in allowed:
            hint = extra or ("known keys: " + ", ".join(allowed))
            raise DispatchError(f"{path}: {_label(where, key)}: unknown; {hint}")


def _table(data, key, where, path):
    value = data.get(key, {})
    _table_value(value, key if not where else f"{where}.{key}", path)
    return value


def _table_value(value, where, path):
    if not isinstance(value, dict):
        raise DispatchError(f"{path}: [{where}]: expected a table, got "
                            f"{type(value).__name__}")


def _string(table, key, where, path, default=""):
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, str):
        raise DispatchError(f"{path}: {_label(where, key)}: expected a string, "
                            f"got {type(value).__name__}")
    return value.strip()


def _integer(table, key, where, path):
    value = table[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise DispatchError(f"{path}: {_label(where, key)}: expected an integer, "
                            f"got {type(value).__name__}")
    return value


def _boolean(table, key, where, path):
    value = table[key]
    if not isinstance(value, bool):
        raise DispatchError(f"{path}: {_label(where, key)}: expected true or "
                            f"false, got {type(value).__name__}")
    return value


def _string_list(table, key, where, path):
    value = table.get(key, [])
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise DispatchError(f"{path}: {_label(where, key)}: expected a list of "
                            "strings")
    return [v.strip() for v in value]


def _string_map(table, key, where, path):
    value = table.get(key, {})
    if not isinstance(value, dict) or any(not isinstance(v, str)
                                          for v in value.values()):
        raise DispatchError(f"{path}: {_label(where, key)}: expected a table of "
                            "strings")
    return {k: v.strip() for k, v in value.items()}


def _token(text, where, path, what):
    if not TOKEN_RE.match(text or ""):
        raise DispatchError(
            f"{path}: [{where}]: {text!r} is not a usable {what}; a lane reads "
            "as key@effort[:tier], so each part is lower-case letters and digits")


def _deadline(table, key, path, allow_forever=True):
    text = _string(table, key, "policy", path)
    if allow_forever and text.lower() in (FOREVER, "never"):
        return text.lower()
    try:
        parse_deadline(text)
    except DispatchError as exc:
        raise DispatchError(f"{path}: [policy] {key}: {exc}") from exc
    return text


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


_ACTIVE = Config()


def active_config():
    """The config this process is running on. A default one until `apply`."""
    return _ACTIVE


def apply_config(config):
    """Install a config into the modules that hold it. Returns it."""
    global _ACTIVE
    set_lane_table(config.lanes)
    set_default_lane(config.default_lane)
    set_policy(config.policy)
    set_preamble(config.preamble)
    set_machines(config.machines)
    _ACTIVE = config
    return config


def load_and_apply(env=None):
    return apply_config(load_config(env=env))


# --------------------------------------------------------------------------
# Packaged files: presets, and the skill
# --------------------------------------------------------------------------


def _package_file(*parts):
    return resources.files("dispatch").joinpath(*parts)


def preset_names():
    """Every preset shipped with this install, alphabetically."""
    names = [item.name[: -len(".toml")]
             for item in _package_file("presets").iterdir()
             if item.name.endswith(".toml")]
    return sorted(names)


def preset_text(name):
    """The TOML of one preset, as `dispatch init` writes it."""
    try:
        return _package_file("presets", f"{name}.toml").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise DispatchError(
            f"no preset named {name!r}; presets: {', '.join(preset_names())}"
        ) from exc


def skill_text():
    return _package_file("skill", SKILL_FILE).read_text(encoding="utf-8")


def agents_snippet_text():
    return _package_file("skill", AGENTS_SNIPPET_FILE).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def run_init(preset=DEFAULT_PRESET, force=False, env=None):
    """Write the user config from a preset and install the skill.

    Returns the report lines rather than printing them, so the same work is
    testable without capturing stdout.
    """
    text = preset_text(preset)
    path = user_config_path(env)
    lines = []
    if path.exists() and not force:
        raise DispatchError(f"{path} already exists; --force overwrites it")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_if_present(path)
    path.write_text(text, encoding="utf-8")
    lines.append(f"{'unchanged' if existing == text else 'wrote'} {path} "
                 f"(preset {preset})")
    lines.extend(install_skill(force=force))
    return lines


def install_skill(force=False, home=None):
    """Install the skill under every agent home that exists. Idempotent.

    The snippet lands beside the skill as well as being printed, so an agent
    asked to fill in a repository's AGENTS.md reads it from a path rather than
    from whatever scrollback the operator pasted it out of.
    """
    root = Path(home) if home else Path.home()
    files = ((SKILL_FILE, skill_text()),
             (AGENTS_SNIPPET_FILE, agents_snippet_text()))
    lines = []
    for parent in SKILL_HOMES:
        base = root / parent
        if not base.is_dir():
            lines.append(f"skipped {base} (not installed)")
            continue
        for name, text in files:
            lines.append(_install_file(base.joinpath(*SKILL_SUBPATH, name),
                                       text, force))
    return lines


def _install_file(target, text, force):
    """Write one packaged file, keeping an edited one until `--force`."""
    existing = _read_if_present(target)
    if existing == text:
        return f"unchanged {target}"
    if existing is not None and not force:
        return f"kept {target} (differs; --force overwrites)"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return f"{'overwrote' if existing is not None else 'installed'} {target}"


def _read_if_present(path):
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
