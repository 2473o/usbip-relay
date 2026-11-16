"""Run the USB/IP relay chain: client -> declared stages -> usbipd.

Usage:
    uv run usbip-relay          # run the chain declared in config.toml
    uv run usbip-relay --help   # list the declared chain and its notes
"""

from __future__ import annotations

import argparse
import traceback

import anyio

from usbip import __VERSION__, build_stages, chain_order
from usbip_core import config
from usbip_core.log import Logger
from usbip_core.pipeline import Stage, run_chain
from usbip_core.stream import CLOSED, Duplex, connect_tcp, tune

LOG_PREFIX = "usbip_relay_"


def format_chain(cfg: config.Config, order: list[str]) -> str:
    """One line per declared stage, in the order the anchors put them."""
    rows = ["relay chain: client -> %s -> usbipd" % " -> ".join(order), ""]
    for name in order:
        rows.append("  %-9s  %s" % (name, cfg.sections[name].note))
    return "\n".join(rows)


def build_parser(cfg: config.Config | None, order: list[str],
                 load_error: str | None) -> argparse.ArgumentParser:
    epilog = format_chain(cfg, order) if cfg is not None else "relay chain: unavailable"
    if cfg is None:
        epilog += "\n\nconfig.toml not loaded: %s" % load_error
    return argparse.ArgumentParser(
        prog="usbip-relay",
        description="USB/IP relay: one listening port, chained stages, usbipd behind them.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


async def serve(cfg: config.Config, stages: list[Stage]) -> None:
    log = Logger(cfg.logs, LOG_PREFIX, "usbip-relay", "INFO")
    chain = " -> ".join(stage.name for stage in stages)

    async def handle_client(client: Duplex) -> None:
        tune(client)
        try:
            await run_chain(client, stages, lambda: connect_tcp(
                config.UPSTREAM_HOST, cfg.upstream_port))
        except CLOSED + (OSError, TimeoutError) as e:
            log.warn("connection ended: %s" % e)
        except Exception as e:
            log.error("connection failed: %s | %s" % (
                e, traceback.format_exc().replace("\n", " ")))
        finally:
            try:
                await client.aclose()
            except CLOSED + (OSError,):
                pass

    listener = await anyio.create_tcp_listener(
        local_host=cfg.listen_host, local_port=cfg.listen_port)
    print("usbip-relay %s: %s:%d -> %s -> %s:%d" % (
        __VERSION__, cfg.listen_host, cfg.listen_port, chain,
        config.UPSTREAM_HOST, cfg.upstream_port), flush=True)
    log.info("listening on %s:%d -> %s -> %s:%d" % (
        cfg.listen_host, cfg.listen_port, chain,
        config.UPSTREAM_HOST, cfg.upstream_port))
    await listener.serve(handle_client)


def main() -> int:
    try:
        cfg: config.Config | None = config.load()
        order = [name for name, _ in chain_order(cfg)] if cfg is not None else []
        load_error = None
    except config.ConfigError as e:
        cfg, order, load_error = None, [], str(e)

    parser = build_parser(cfg, order, load_error)
    parser.parse_args()

    if cfg is None:
        parser.error(load_error)

    try:
        stages = build_stages(cfg)
    except config.ConfigError as e:
        parser.error(str(e))

    anyio.run(serve, cfg, stages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
