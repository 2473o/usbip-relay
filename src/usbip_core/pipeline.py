"""Compose relay stages into one client -> usbipd chain.

Stages are stream transforms, not processes: every stage sees one connection,
runs in the same event loop and hands the (possibly rewritten) stream to the
next stage.  Only the last stage opens a TCP connection, to usbipd; stages in
front of it are wired to each other through in-memory duplex pairs, so the relay
needs exactly one listening port and one upstream port no matter how many
components are registered.

Order is meaningful: ``stages[0]`` is the stage closest to the client, so it
owns request/response correlation, and ``stages[-1]`` is the stage closest to
usbipd, so it owns whatever normalisation the kernel must finally see.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio

from usbip_core.stream import Connect, Duplex, duplex_pair

Handler = Callable[[Duplex, Connect, Any], Awaitable[None]]


@dataclass(frozen=True)
class Stage:
    """One relay component, ready to run."""

    name: str
    handler: Handler
    ctx: Any = field(default=None, compare=False)


def single_use(stream: Duplex) -> Connect:
    """A connect callback that hands out one prepared stream exactly once."""
    state = {"used": False}

    async def guarded() -> Duplex:
        if state["used"]:
            raise RuntimeError("relay stage connected to its upstream twice")
        state["used"] = True
        return stream

    return guarded


async def run_chain(client: Duplex, stages: Sequence[Stage], connect: Connect) -> None:
    """Run every stage for one connection."""
    if not stages:
        raise ValueError("run_chain() needs at least one stage")

    inbounds: list[Duplex] = [client]
    connects: list[Connect | None] = [None] * len(stages)
    connects[-1] = connect
    for index in range(len(stages) - 1):
        downstream, upstream = duplex_pair()
        inbounds.append(downstream)
        connects[index] = single_use(upstream)

    async def run_stage(stage: Stage, inbound: Duplex, upstream: Connect) -> None:
        await stage.handler(inbound, upstream, stage.ctx)

    async with anyio.create_task_group() as task_group:
        for index in range(1, len(stages)):
            task_group.start_soon(run_stage, stages[index], inbounds[index], connects[index])
        # The client-facing stage decides when the connection is over; the rest
        # of the chain must not outlive it.
        await run_stage(stages[0], inbounds[0], connects[0])
        task_group.cancel_scope.cancel()
