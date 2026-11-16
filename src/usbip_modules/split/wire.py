"""Wire vocabulary: how a URB header is laid out, and how its seqnum is tracked.

Both answer the same question -- which URB is this? The header offsets come from
Documentation/usb/usbip_protocol.rst; the seqnum table keeps every in-flight
number so a reply can be told apart from the next URB and routed to whoever is
waiting for it.

A reply is matched on the seqnum at URB header offset 4, and that number belongs
to the client: its own table is the one USBIP_RET_SUBMIT is resolved against.
Two kinds of number therefore pass through the table:

  * client numbers, tracked only so the reply's direction is known before its
    payload length is read, then forwarded to the client;
  * minted numbers, one per chunk, whose replies are consumed here and never
    reach the client -- it only sees the single aggregated RET_SUBMIT.

Minted numbers start far above anything a client mints for itself, so the two
spaces cannot collide within one connection.
"""

from __future__ import annotations

import struct
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from usbip_core.stream import CLOSED

HDR_LEN = 48
OP_LEN = 8
BUSID_LEN = 32
DEV_DESC_LEN = 312

OP_REQ_IMPORT = 0x8003
OP_REQ_DEVLIST = 0x8005

CMD_SUBMIT = 0x00000001
CMD_UNLINK = 0x00000002
RET_SUBMIT = 0x00000003

DIR_IN = 1
DIR_OUT = 0

OFF_CMD = 0
OFF_SEQ = 4
OFF_DIR = 12
OFF_EP = 16
OFF_LEN = 24
OFF_NPKT = 32

RET_OFF_STATUS = 20
RET_OFF_ACTUAL = 24
RET_OFF_START_FRAME = 28
RET_OFF_NPKT = 32
RET_OFF_ERRCNT = 36

SYNTH_SEQ_BASE = 0x40000000
SYNTH_SEQ_SPAN = 0x10000000

Send = Callable[[bytes], Awaitable[None]]
Reply = tuple[bytes, bytes]


def unpack_u32(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def unpack_i32(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from(">i", buf, off)[0]


class Seqnums:
    """The in-flight table: direction per seqnum, plus a mailbox per chunk."""

    def __init__(self, timeout: float, base: int = SYNTH_SEQ_BASE,
                 span: int = SYNTH_SEQ_SPAN):
        self._timeout = timeout
        self._base = base
        self._span = span
        self._seq = base
        self._directions: dict[int, int] = {}
        self._waiters: dict[int, Any] = {}

    def next(self) -> int:
        self._seq += 1
        if self._seq >= self._base + self._span:
            self._seq = self._base
        return self._seq

    def track(self, seq: int, direction: int) -> None:
        self._directions[seq] = direction

    def direction(self, seq: int) -> int:
        """Direction of the URB a reply belongs to; OUT when it was never seen."""
        return self._directions.pop(seq, DIR_OUT)

    async def wait(self, seq: int) -> Reply | None:
        """The (header, payload) reply for a minted seqnum, or None on timeout."""
        send, recv = anyio.create_memory_object_stream[Reply](1)
        self._waiters[seq] = send
        try:
            with anyio.fail_after(self._timeout):
                return await recv.receive()
        except CLOSED + (TimeoutError,):
            return None
        finally:
            self._waiters.pop(seq, None)
            self._directions.pop(seq, None)
            await send.aclose()
            await recv.aclose()

    async def deliver(self, seq: int, reply: Reply) -> bool:
        """Hand a reply to the chunk waiting for it; False when nobody waits."""
        send = self._waiters.pop(seq, None)
        if send is None:
            return False
        try:
            await send.send(reply)
        except CLOSED:
            pass
        finally:
            await send.aclose()
        return True

    async def reset(self) -> None:
        """Release every waiter, so a torn-down connection leaves nothing open."""
        for send in list(self._waiters.values()):
            await send.aclose()
        self._waiters.clear()
        self._directions.clear()
