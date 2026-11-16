"""Configuration for the USB/IP toolset.

Every tunable is read from <repo>/config.toml, including which relay stages run
([server] stages): the relay chain is a deployment decision, so membership lives
in the config document next to the knobs it drives, not as a literal in the
package. The order those stages run in is not a deployment decision -- it falls
out of the USB/IP and USB 2.0 rules each stage implements, so it is derived in
usbip/__init__.py and cannot be set here. Modules must not read environment
variables; add new keys to config.toml (and config.toml.example) first, then
expose them here as typed fields.

Loading is explicit: nothing here runs at import time, so an import of the
package never touches the disk and a bad config surfaces from ``main()``
instead of from an import site.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# usbipd always runs beside the relays on the board; only its port is configurable.
UPSTREAM_HOST = "127.0.0.1"

# Keys every [server.<stage>] table carries; a stage module owns everything else.
# Membership and order live in [server] chain, not here: one switch, one truth.
COMMON_KEYS = ("note", "log_prefix")


class ConfigError(RuntimeError):
    """Raised when config.toml is missing or incomplete."""


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from None


@dataclass(frozen=True)
class StageConfig:
    """One [server.<stage>] table.

    The keys every stage shares are parsed here; the remaining ones stay in
    ``values`` for the stage module to read through the need_* helpers. A stage
    therefore owns its own knobs and this module never grows a per-stage branch.
    """

    name: str
    note: str
    log_dir: Path
    log_prefix: str
    values: dict[str, Any]
    source: Path

    def _need(self, key: str, kinds: tuple[type, ...], label: str) -> Any:
        if key not in self.values:
            raise ConfigError(
                f"missing '{key}' in [server.{self.name}] of {self.source}")
        value = self.values[key]
        # bool is a subclass of int, so an explicit true/false never passes for
        # a number and a number never passes for a flag.
        if isinstance(value, bool) and bool not in kinds:
            raise ConfigError(f"[server.{self.name}] {key} must be {label}, "
                              f"got {value!r} in {self.source}")
        if not isinstance(value, kinds):
            raise ConfigError(f"[server.{self.name}] {key} must be {label}, "
                              f"got {value!r} in {self.source}")
        return value

    def need_int(self, key: str) -> int:
        return self._need(key, (int,), "an integer")

    def need_float(self, key: str) -> float:
        return float(self._need(key, (int, float), "a number"))

    def need_str(self, key: str) -> str:
        return self._need(key, (str,), "a string")

    def need_bool(self, key: str) -> bool:
        return self._need(key, (bool,), "true or false")


@dataclass(frozen=True)
class Config:
    """Relay-wide settings; stage knobs hang off their own section."""

    logs: Path
    # listen_port is where clients attach, upstream_port is where usbipd listens.
    listen_host: str
    listen_port: int
    upstream_port: int
    # Members of the relay chain: the one switch that decides which stages run.
    # Not their order -- each stage pins itself to one end of the relay from the
    # protocol rule it implements, see usbip/__init__.py. A [server.<name>] table
    # absent from here is parked, not run.
    stages: tuple[str, ...]
    sections: dict[str, StageConfig]


def _require(section: dict[str, Any], key: str, where: str, path: Path) -> Any:
    try:
        return section[key]
    except KeyError:
        raise ConfigError(f"missing '{key}' in [{where}] of {path}") from None


def _section(raw: dict[str, Any], name: str, path: Path) -> dict[str, Any]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"missing [{name}] section in {path}")
    return section


def _stages(server: dict[str, Any], path: Path) -> tuple[str, ...]:
    """The declared stage members, validated enough to be importable."""
    raw = _require(server, "stages", "server", path)
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            f"'stages' in [server] of {path} must be a non-empty array of stage names")
    names: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.isidentifier():
            raise ConfigError(
                f"stage name {entry!r} in [server] stages of {path} is not a module name")
        if entry in names:
            raise ConfigError(f"stage '{entry}' is listed twice in [server] stages of {path}")
        names.append(entry)
    return tuple(names)


def _stage(server: dict[str, Any], name: str, logs: Path, path: Path) -> StageConfig:
    table = server.get(name)
    where = f"server.{name}"
    if not isinstance(table, dict):
        raise ConfigError(f"missing [{where}] section in {path}")
    return StageConfig(
        name=name,
        note=str(_require(table, "note", where, path)),
        log_dir=logs,
        log_prefix=str(_require(table, "log_prefix", where, path)),
        values={k: v for k, v in table.items() if k not in COMMON_KEYS},
        source=path,
    )


def default_path() -> Path:
    """config.toml at any repository level above this package."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config.toml"
        if candidate.is_file():
            return candidate
    return Path.cwd() / "config.toml"


def load(path: Path | None = None) -> Config:
    path = path or default_path()
    raw = _read(path)

    server = _section(raw, "server", path)

    logs = Path(_require(server, "logs", "server", path))
    listen_host = str(_require(server, "host", "server", path))
    listen_port = int(_require(server, "host_port", "server", path))
    # The key name is historical: [server] listen_port is where usbipd listens,
    # not where this relay listens. Only the key is kept, the field is not.
    upstream_port = int(_require(server, "listen_port", "server", path))
    stages = _stages(server, path)

    return Config(
        logs=logs,
        listen_host=listen_host,
        listen_port=listen_port,
        upstream_port=upstream_port,
        stages=stages,
        sections={name: _stage(server, name, logs, path) for name in stages},
    )


@dataclass(frozen=True)
class AutobindConfig:
    """The [autobind] table.

    The auto-export sidecar never sees a client connection, so it cannot be a
    member of [server] stages and needs its own top-level table. Its knobs are
    validated through the same need_* helpers, so a missing or mistyped value
    is a config error rather than a silent default.
    """

    log_dir: Path
    log_prefix: str
    log_level: str
    busids: str
    poll_interval: float
    host_port: int
    recover: bool
    source: Path


def load_autobind(path: Path | None = None) -> AutobindConfig:
    """Load [autobind]; [server] logs is the only thing borrowed from it."""
    path = path or default_path()
    raw = _read(path)

    server = _section(raw, "server", path)
    logs = Path(_require(server, "logs", "server", path))
    table = _section(raw, "autobind", path)
    section = StageConfig(
        name="autobind",
        note=str(_require(table, "note", "autobind", path)),
        log_dir=logs,
        log_prefix=str(_require(table, "log_prefix", "autobind", path)),
        values={k: v for k, v in table.items() if k not in COMMON_KEYS},
        source=path,
    )
    return AutobindConfig(
        log_dir=logs,
        log_prefix=section.log_prefix,
        log_level=section.need_str("log_level"),
        busids=section.need_str("busids"),
        poll_interval=section.need_float("poll_interval"),
        host_port=section.need_int("host_port"),
        recover=section.need_bool("recover"),
        source=path,
    )
