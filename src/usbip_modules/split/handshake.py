"""The OP_REQ_IMPORT exchange that precedes every URB.

The reply is not one message: usbipd answers with an op header, then the 312 byte
device descriptor, then 4 bytes per interface. Nothing in it is rewritten here --
the stage only has to keep the byte counts straight, so the descriptors are never
mistaken for a URB header. OP_REQ_DEVLIST and unknown ops carry no URBs at all,
so the caller drops into a raw relay for those.
"""

from __future__ import annotations

import struct

import anyio

from usbip_core.log import Logger
from usbip_core.stream import CLOSED, Duplex, recv_exact

from .wire import BUSID_LEN, DEV_DESC_LEN, OP_LEN, OP_REQ_DEVLIST, OP_REQ_IMPORT, Send

IMPORT_ERROR_TIMEOUT = 1.0
IMPORT_ERROR_MAX = 256
DEV_DESC_TIMEOUT = 5.0
IFACE_DESC_TIMEOUT = 2.0
MAX_INTERFACES = 32


async def negotiate(client: Duplex, server: Duplex, send_client: Send,
                    log: Logger, peer_name: str) -> bool | str:
    """True for a device to relay, "raw" to copy bytes, False to give up."""
    op = await recv_exact(client, OP_LEN)
    if op is None:
        return False
    code = struct.unpack_from(">H", op, 2)[0]

    if code == OP_REQ_DEVLIST:
        log.debug("devlist from %s, raw relay" % peer_name)
        await server.send(op)
        return "raw"

    if code != OP_REQ_IMPORT:
        log.warn("unknown op 0x%04x from %s, raw relay" % (code, peer_name))
        await server.send(op)
        return "raw"

    busid = await recv_exact(client, BUSID_LEN)
    if busid is None:
        return False
    await server.send(op + busid)
    log.info("import %s from %s" % (
        busid.rstrip(b"\x00").decode("utf-8", "replace"), peer_name))

    rep = await recv_exact(server, OP_LEN)
    if rep is None:
        log.error("no import reply")
        return False
    await send_client(rep)
    status = struct.unpack_from(">i", rep, 4)[0]
    log.debug("import reply %s status=%d" % (rep.hex(), status))
    if status != 0:
        # usbipd may append an error string, but does not always do so.
        # Never block on it: a stuck read here holds the upstream socket
        # open and keeps the device in SDEV_ST_USED forever.
        try:
            with anyio.fail_after(IMPORT_ERROR_TIMEOUT):
                err = await server.receive(IMPORT_ERROR_MAX)
            if err:
                await send_client(err)
        except CLOSED + (TimeoutError,):
            pass
        log.error("import rejected, status=%d" % status)
        return False

    try:
        with anyio.fail_after(DEV_DESC_TIMEOUT):
            dev = await recv_exact(server, DEV_DESC_LEN)
    except TimeoutError:
        log.error("device descriptor read timed out")
        return False
    if dev is None:
        log.error("device descriptor truncated")
        return False
    await send_client(dev)
    nintf = dev[DEV_DESC_LEN - 1]
    log.debug("device descriptor head %s nintf=%d" % (dev[:8].hex(), nintf))
    if 0 < nintf <= MAX_INTERFACES:
        try:
            with anyio.fail_after(IFACE_DESC_TIMEOUT):
                extra = await recv_exact(server, 4 * nintf)
        except TimeoutError:
            log.error("interface descriptor read timed out")
            return False
        if extra:
            await send_client(extra)
    log.info("import ok, interfaces=%d" % nintf)
    return True
