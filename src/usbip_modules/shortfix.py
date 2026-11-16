#!/usr/bin/env python3
"""TEMPORARY shim for usbip-win2 issue #203.

https://github.com/vadimgrn/usbip-win2/issues/203

The client maps "no USBD_SHORT_TRANSFER_OK" to URB_SHORT_NOT_OK even for
control pipes, so a legal short data stage becomes -EREMOTEIO. This proxy
clears that bit on control IN submits and forwards everything else untouched.

USB 2.0 8.5.3.2 (Variable-length Data Stage) is what makes that legal: a control
pipe ends its data stage "by returning a packet that is shorter than the
MaxPacketSize for the pipe", or a zero-length packet when the length is an exact
multiple of wMaxPacketSize. The short packet is the end-of-stage marker the spec
asks for, so URB_SHORT_NOT_OK must not apply on ep0.

Proper fix: skip URB_SHORT_NOT_OK when the endpoint type is control
(drivers/libdrv/usbd_helper.cpp, drivers/ude/proto.cpp). Delete this shim and
point usbipd back at port 3240 once the client is fixed.

Stage position: last in the chain, closest to usbipd. The bit has to be clear
on the URB the kernel finally sees, and this stage never changes the number of
requests, so it is invisible to the correlation done in front of it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import anyio

from usbip_core import protocol
from usbip_core.config import StageConfig
from usbip_core.log import Logger
from usbip_core.stream import CLOSED, Connect, Duplex, peer, pump, recv_exact

HDR_LEN = 48
OP_COMMON_LEN = 8
OP_IMPORT_BUSID_LEN = 32
OP_REQ_DEVLIST = 0x8005
OP_REQ_IMPORT = 0x8003
CMD_SUBMIT = 0x00000001
CMD_UNLINK = 0x00000002
URB_SHORT_NOT_OK = 0x0001
DIR_OUT = 0
DIR_IN = 1

OFF_CMD = 0
OFF_SEQ = 4
OFF_DIR = 12
OFF_EP = 16
OFF_FLAGS = 20
OFF_LEN = 24
OFF_SETUP = 40
SETUP_LEN = 8

# Where this stage sits in the relay, from who consumes what it rewrites: the
# host controller reads transfer_flags when it completes the URB (USB 2.0
# 8.5.3.2). See usbip/__init__.py.
ANCHOR = "usbipd"

CONNECT_TIMEOUT = 10.0


@dataclass
class Options:
    """Resolved once at startup and shared by every connection."""

    log: Logger
    verbose: bool
    stats: dict[str, int] = field(
        default_factory=lambda: {"conns": 0, "rewritten": 0})


def create(section: StageConfig) -> Options:
    verbose = section.need_bool("verbose")
    return Options(
        log=Logger(section.log_dir, section.log_prefix, "usbip-shortfix",
                   "DEBUG" if verbose else "INFO"),
        verbose=verbose,
    )


async def relay_raw(client: Duplex, server: Duplex) -> None:
    async with anyio.create_task_group() as tg:
        tg.start_soon(pump, server, client)
        await pump(client, server)
        tg.cancel_scope.cancel()


async def forward_urbs(
    client: Duplex, server: Duplex, opts: Options, peer_name: str
) -> None:
    log = opts.log
    while True:
        hdr = await recv_exact(client, HDR_LEN)
        if hdr is None:
            return
        h = bytearray(hdr)
        cmd = struct.unpack_from(">I", h, OFF_CMD)[0]
        payload = b""

        if cmd == CMD_SUBMIT:
            direction = struct.unpack_from(">I", h, OFF_DIR)[0]
            ep = struct.unpack_from(">I", h, OFF_EP)[0]
            length = struct.unpack_from(">i", h, OFF_LEN)[0]
            seq = struct.unpack_from(">I", h, OFF_SEQ)[0]
            if opts.verbose:
                bm, br, wv, wi, wl = struct.unpack_from("<BBHHH", h, OFF_SETUP)
                log.debug("submit seq=%d dir=%d ep=%d len=%d %02x.%02x v=%04x i=%04x l=%d" % (
                    seq, direction, ep, length, bm, br, wv, wi, wl))
            if direction == DIR_OUT and length > 0:
                protocol.check_transfer_length(length)
                payload = await recv_exact(client, length)
                if payload is None:
                    return
            elif direction == DIR_IN and ep == 0:
                flags = struct.unpack_from(">I", h, OFF_FLAGS)[0]
                if flags & URB_SHORT_NOT_OK:
                    struct.pack_into(">I", h, OFF_FLAGS, flags & ~URB_SHORT_NOT_OK)
                    opts.stats["rewritten"] += 1
                    log.info("cleared URB_SHORT_NOT_OK seq=%d len=%d from %s" % (
                        seq, length, peer_name))
        elif cmd != CMD_UNLINK:
            log.warn("unexpected command 0x%08x from %s, switching to raw relay" % (
                cmd, peer_name))
            await server.send(bytes(h))
            await pump(client, server)
            return

        await server.send(bytes(h))
        if payload:
            await server.send(payload)


async def handle(inbound: Duplex, upstream: Connect, opts: Options) -> None:
    peer_name = peer(inbound)
    opts.stats["conns"] += 1
    server: Duplex | None = None
    try:
        with anyio.fail_after(CONNECT_TIMEOUT):
            server = await upstream()

        op = await recv_exact(inbound, OP_COMMON_LEN)
        if op is None:
            return
        code = struct.unpack_from(">H", op, 2)[0]

        if code == OP_REQ_IMPORT:
            busid = await recv_exact(inbound, OP_IMPORT_BUSID_LEN)
            if busid is None:
                return
            await server.send(op + busid)
            opts.log.info("import %s from %s" % (
                busid.rstrip(b"\x00").decode("utf-8", "replace"), peer_name))
        elif code == OP_REQ_DEVLIST:
            await server.send(op)
            await relay_raw(inbound, server)
            return
        else:
            opts.log.warn("unknown op 0x%04x from %s, raw relay" % (code, peer_name))
            await server.send(op)
            await relay_raw(inbound, server)
            return

        async with anyio.create_task_group() as tg:
            tg.start_soon(pump, server, inbound)
            await forward_urbs(inbound, server, opts, peer_name)
            tg.cancel_scope.cancel()
    except CLOSED + (OSError, TimeoutError) as e:
        opts.log.error("connection %s error: %s" % (peer_name, e))
    except protocol.ProtocolError as e:
        opts.log.error("rejected urb from %s: %s" % (peer_name, e))
    except Exception as e:
        opts.log.error("connection error: %s" % e)
    finally:
        for stream in (server, inbound):
            if stream is not None:
                try:
                    await stream.aclose()
                except CLOSED + (OSError,):
                    pass
