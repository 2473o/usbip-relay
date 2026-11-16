"""The URB path: one oversize SUBMIT in, N sub-requests out, one RET_SUBMIT back.

Chunking is the whole reason this stage exists. A bulk SUBMIT longer than
max_transfer is cut into sequential sub-requests of at most max_transfer bytes,
each with a minted seqnum, and their replies are folded back into a single
RET_SUBMIT built from the client's own header -- status is the first failure (or
-ETIMEDOUT when a chunk never answers), actual_length the sum -- so nothing the
client reads back reveals the URB was ever chunked.

The chunks are sequential: each is sent and its reply awaited before the next is
built, so usbipd sees the order the client would have produced and the payload
stays in transfer order.

``client_loop`` decides per URB whether it is chunked; ``server_loop`` either
hands a reply to the chunk waiting for it or forwards it to the client. Only the
client-facing loop terminates the connection: when it stops, the task group is
cancelled and ``close`` drops both sockets.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import anyio

from usbip_core import protocol
from usbip_core.config import StageConfig
from usbip_core.log import Logger
from usbip_core.stream import CLOSED, Connect, Duplex, peer, pump, recv_exact

from .handshake import negotiate
from .wire import (CMD_SUBMIT, DIR_IN, DIR_OUT, HDR_LEN, OFF_CMD, OFF_DIR, OFF_EP,
                   OFF_LEN, OFF_NPKT, OFF_SEQ, RET_OFF_ACTUAL, RET_OFF_ERRCNT,
                   RET_OFF_NPKT, RET_OFF_START_FRAME, RET_OFF_STATUS, RET_SUBMIT,
                   Send, Seqnums, unpack_i32, unpack_u32)

CONNECT_TIMEOUT = 10.0
STATUS_ETIMEDOUT = -110


@dataclass(frozen=True)
class Options:
    """Resolved once at startup and shared by every connection."""

    max_transfer: int
    chunk_timeout: float
    hex_dump: int
    log: Logger


def create(section: StageConfig) -> Options:
    return Options(
        max_transfer=section.need_int("max_transfer"),
        chunk_timeout=section.need_float("timeout"),
        hex_dump=section.need_int("hex_dump"),
        log=Logger(section.log_dir, section.log_prefix, "usbip-split",
                   section.need_str("log_level")),
    )


async def submit_split(server: Duplex, send_client: Send, hdr: bytes,
                       payload: bytes, opts: Options, seqs: Seqnums) -> None:
    log = opts.log
    direction = unpack_u32(hdr, OFF_DIR)
    length = unpack_i32(hdr, OFF_LEN)
    collected = []
    total = 0
    status = 0
    sent = 0
    chunks = (length + opts.max_transfer - 1) // opts.max_transfer

    for off in range(0, length, opts.max_transfer):
        n = min(opts.max_transfer, length - off)
        sub = bytearray(hdr)
        seq = seqs.next()
        struct.pack_into(">I", sub, OFF_SEQ, seq)
        struct.pack_into(">i", sub, OFF_LEN, n)
        seqs.track(seq, direction)

        await server.send(bytes(sub))
        if direction != DIR_IN:
            await server.send(payload[off:off + n])
        sent += 1

        reply = await seqs.wait(seq)
        if reply is None:
            log.error("chunk %d/%d seq=%d timeout" % (sent, chunks, seq))
            status = STATUS_ETIMEDOUT
            break

        rhdr, rdata = reply
        st = unpack_i32(rhdr, RET_OFF_STATUS)
        act = unpack_i32(rhdr, RET_OFF_ACTUAL)
        if st != 0:
            log.warn("chunk %d status=%d" % (sent, st))
            status = st
            break

        total += act
        if direction == DIR_IN:
            if rdata:
                collected.append(rdata)
            if act < n:
                log.debug("short chunk %d: %d < %d" % (sent, act, n))
                break

    out = bytearray(hdr)
    struct.pack_into(">I", out, OFF_CMD, RET_SUBMIT)
    struct.pack_into(">i", out, RET_OFF_STATUS, status)
    struct.pack_into(">i", out, RET_OFF_ACTUAL, total)
    struct.pack_into(">i", out, RET_OFF_START_FRAME, 0)
    struct.pack_into(">i", out, RET_OFF_NPKT, 0)
    struct.pack_into(">i", out, RET_OFF_ERRCNT, 0)
    await send_client(bytes(out))
    if direction == DIR_IN and collected:
        await send_client(b"".join(collected))

    log.info("split ep=%d len=%d -> %d chunks, total=%d status=%d" % (
        unpack_u32(hdr, OFF_EP), length, sent, total, status))


class Relay:
    def __init__(self, client: Duplex, connect: Connect, opts: Options, peer_name: str):
        self.client = client
        self.connect = connect
        self.opts = opts
        self.log = opts.log
        self.peer = peer_name
        self.server: Duplex | None = None
        self.seqs = Seqnums(opts.chunk_timeout)
        self.hex_left = opts.hex_dump
        self._client_lock = anyio.Lock()

    async def send_client(self, data: bytes) -> None:
        """Both loops write to the client, so a write may not interleave."""
        async with self._client_lock:
            await self.client.send(data)

    async def server_loop(self, server: Duplex) -> None:
        try:
            while True:
                hdr = await recv_exact(server, HDR_LEN)
                if hdr is None:
                    break
                cmd = unpack_u32(hdr, OFF_CMD)
                seq = unpack_u32(hdr, OFF_SEQ)
                direction = DIR_OUT
                payload = b""

                if cmd == RET_SUBMIT:
                    # A failed URB reports a negative actual_length, so only a
                    # positive one can be used as an allocation size.
                    actual = unpack_i32(hdr, RET_OFF_ACTUAL)
                    direction = self.seqs.direction(seq)
                    if direction == DIR_IN and actual > 0:
                        protocol.check_transfer_length(actual, "reply")
                        payload = await recv_exact(server, actual)
                        if payload is None:
                            break

                self.log.debug("ret cmd=%d seq=%d dir=%d status=%d actual=%d data=%d" % (
                    cmd, seq, direction,
                    unpack_i32(hdr, RET_OFF_STATUS),
                    unpack_i32(hdr, RET_OFF_ACTUAL), len(payload)))
                if self.hex_left > 0:
                    self.hex_left -= 1
                    self.log.debug("ret hex %s" % hdr.hex())

                if await self.seqs.deliver(seq, (hdr, payload)):
                    continue
                await self.send_client(hdr)
                if payload:
                    await self.send_client(payload)
        except CLOSED + (OSError,) as e:
            self.log.debug("server loop end: %s" % e)
        finally:
            await self.seqs.reset()

    async def client_loop(self, server: Duplex) -> None:
        try:
            while True:
                hdr = await recv_exact(self.client, HDR_LEN)
                if hdr is None:
                    break
                cmd = unpack_u32(hdr, OFF_CMD)
                payload = b""

                if cmd == CMD_SUBMIT:
                    direction = unpack_u32(hdr, OFF_DIR)
                    length = unpack_i32(hdr, OFF_LEN)
                    ep = unpack_u32(hdr, OFF_EP)
                    npkt = unpack_i32(hdr, OFF_NPKT)
                    seq = unpack_u32(hdr, OFF_SEQ)
                    self.log.debug("submit seq=%d dir=%d ep=%d len=%d npkt=%d" % (
                        seq, direction, ep, length, npkt))
                    if self.hex_left > 0:
                        self.log.debug("submit hex %s" % hdr.hex())

                    if direction != DIR_IN and length > 0:
                        protocol.check_transfer_length(length)
                        payload = await recv_exact(self.client, length)
                        if payload is None:
                            break

                    # usbip-win2 sends -1, not 0, for everything that is not isochronous
                    splittable = ep != 0 and npkt <= 0 and length > self.opts.max_transfer
                    if splittable:
                        await submit_split(server, self.send_client, hdr, payload,
                                           self.opts, self.seqs)
                        continue
                    if length > self.opts.max_transfer:
                        self.log.warn("oversize submit not splittable: ep=%d dir=%d len=%d npkt=%d" % (
                            ep, direction, length, npkt))

                    self.seqs.track(seq, direction)

                await server.send(hdr)
                if payload:
                    await server.send(payload)
        except CLOSED + (OSError,) as e:
            self.log.debug("client loop end: %s" % e)
        except protocol.ProtocolError as e:
            self.log.error("rejected submit from %s: %s" % (self.peer, e))

    async def run(self) -> None:
        try:
            with anyio.fail_after(CONNECT_TIMEOUT):
                self.server = await self.connect()
        except (OSError, TimeoutError) as e:
            self.log.error("connect upstream failed: %s" % e)
            return

        server = self.server
        try:
            state = await negotiate(self.client, server, self.send_client,
                                    self.log, self.peer)
            if state is False:
                return
            if state == "raw":
                async with anyio.create_task_group() as tg:
                    tg.start_soon(pump, server, self.client)
                    await pump(self.client, server)
                    tg.cancel_scope.cancel()
                return

            async with anyio.create_task_group() as tg:
                tg.start_soon(self.server_loop, server)
                await self.client_loop(server)
                tg.cancel_scope.cancel()
        except CLOSED + (OSError, TimeoutError, protocol.ProtocolError) as e:
            self.log.error("connection %s error: %s" % (self.peer, e))
        finally:
            await self.close()

    async def close(self) -> None:
        for stream in (self.server, self.client):
            if stream is not None:
                try:
                    await stream.aclose()
                except CLOSED + (OSError,):
                    pass


async def handle(inbound: Duplex, upstream: Connect, opts: Options) -> None:
    peer_name = peer(inbound)
    opts.log.info("connection from %s" % peer_name)
    try:
        await Relay(inbound, upstream, opts, peer_name).run()
    except Exception as e:
        opts.log.error("connection %s error: %s" % (peer_name, e))
