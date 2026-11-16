"""USB/IP relay stage discovery, ordered by the protocol rules the stages serve.

The relay is a single process with a single listening port: the client-facing
stage is first, usbipd sits behind the last stage. Every stage is a stream
transform, so enabling one never needs another port.

Which stages run is declared in config.toml ([server] stages plus one
[server.<name>] table per stage). Which order they run in is not declared
anywhere: it follows from where each stage's edits are consumed, one anchor per
end of the relay, and that comes straight out of the two protocols:

  * USB/IP protocol (Documentation/usb/usbip_protocol.rst): USBIP_CMD_SUBMIT
    carries the client's seqnum (URB header, offset 4) and USBIP_RET_SUBMIT
    echoes it back, so one URB is one seqnum and the client owns that
    correlation table. A stage that turns one SUBMIT into N has to mint
    seqnums, so it can only sit where the client's seqnum space is still the
    key: first, next to the client. Nothing behind it may change the number of
    requests, or correlation would end up split across two owners.

  * USB 2.0 8.5.3.2 (Variable-length Data Stage): "a control pipe may have a
    variable-length data phase ... the function should indicate that the Data
    stage is ended by returning a packet that is shorter than the
    MaxPacketSize for the pipe. If the data structure is an exact multiple of
    wMaxPacketSize for the pipe, the function will return a zero-length packet
    to indicate the end of the Data stage." A short packet on the control pipe
    is therefore the spec's end-of-data-stage marker, not an error, and the
    flag that turns it into one (URB_SHORT_NOT_OK) is consumed by the host
    controller when it completes the URB. A stage that fixes that flag must sit
    where the kernel reads the URB: last, next to usbipd.

A stage module states which end it serves with ANCHOR; the chain is built in
ANCHOR_ORDER, so neither config.toml nor this file can hand-pick a sequence.
Adding a stage means choosing an anchor the same way: find the entity that
consumes what the stage rewrites, per the spec, and place the stage next to it.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

from usbip_core.config import Config, ConfigError, StageConfig
from usbip_core.pipeline import Stage

__VERSION__ = "0.1.0"

STAGE_PACKAGE = "usbip_modules"

# Client -> usbipd. An anchor names the entity that consumes the rewrite.
ANCHOR_ORDER = ("client", "usbipd")


def load_module(name: str) -> ModuleType:
    """Import usbip_modules.<name>, reporting a bad entry as a config error."""
    module_name = f"{STAGE_PACKAGE}.{name}"
    try:
        return import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name != module_name:
            raise
        raise ConfigError(f"stage '{name}' has no module {module_name}") from None


def anchor_rank(module: ModuleType, name: str) -> int:
    """Position of a stage in the chain, from the end it is anchored to."""
    anchor = getattr(module, "ANCHOR", None)
    if anchor not in ANCHOR_ORDER:
        raise ConfigError(
            f"stage module {STAGE_PACKAGE}.{name} must declare ANCHOR as one of "
            f"{', '.join(ANCHOR_ORDER)}, got {anchor!r}")
    return ANCHOR_ORDER.index(anchor)


def build_stage(section: StageConfig, module: ModuleType) -> Stage:
    """Turn one [server.<name>] table and its module into a runnable stage."""
    create = getattr(module, "create", None)
    handle = getattr(module, "handle", None)
    if not callable(create) or not callable(handle):
        raise ConfigError(
            f"stage module {STAGE_PACKAGE}.{section.name} must define "
            "create(section) and handle(inbound, upstream, ctx)")
    return Stage(name=section.name, handler=handle, ctx=create(section))


def chain_order(cfg: Config) -> list[tuple[str, ModuleType]]:
    """The declared stages with their modules, in protocol anchor order."""
    staged = []
    for name in cfg.stages:
        module = load_module(name)
        staged.append((anchor_rank(module, name), name, module))

    # Stable sort, so stages sharing an anchor keep the order config lists them.
    staged.sort(key=lambda entry: entry[0])
    return [(name, module) for _, name, module in staged]


def build_stages(cfg: Config) -> list[Stage]:
    """Instantiate the declared stages, in protocol anchor order.

    A [server.<name>] table that [server] stages does not list is parked: its
    knobs are kept, the stage never runs.
    """
    return [build_stage(cfg.sections[name], module) for name, module in chain_order(cfg)]
