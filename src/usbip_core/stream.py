"""Byte streams shared by the relay stages.

The relay runs its stages chained inside one process, so a stage must not assume
that its upstream is a socket: only the last stage in the chain talks TCP, every
earlier stage writes into an in-memory pair.  ``Duplex`` is the whole I/O
surface a stage is allowed to use, and ``duplex_pair`` builds the in-memory
version that connects two stages.
"""

from __future__ import annotations

import socket
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

import anyio
from anyio.abc import SocketAttribute

BUFFER_BYTES = 65536

CLOSED = (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError)
CONNECT_ERRORS = CLOSED + (OSError, TimeoutError)


@runtime_checkable
class Duplex(Protocol):
    """The only I/O surface a relay stage sees."""

    async def receive(self, max_bytes: int = BUFFER_BYTES) -> bytes: ...

    async def send(self, data: bytes) -> None: ...

    async def send_eof(self) -> None: ...

    async def aclose(self) -> None: ...


Connect = Callable[[], Awaitable[Duplex]]


class MemoryDuplex:
    """One end of an in-process duplex pair.

    ``receive`` honours ``max_bytes`` the way a socket does.  Memory objects are
    message framed, so the tail of an over-long message is carried over to the
    next call instead of overflowing the caller's buffer.
    """

    def __init__(self, tx: Any, rx: Any):
        self._tx = tx
        self._rx = rx
        self._carry = bytearray()
        self._rx_closed = False
        self._tx_closed = False

    async def receive(self, max_bytes: int = BUFFER_BYTES) -> bytes:
        while not self._carry:
            if self._rx_closed:
                return b""
            try:
                data = await self._rx.receive()
            except CLOSED:
                self._rx_closed = True
                return b""
            if not data:
                self._rx_closed = True
                return b""
            self._carry += data
        chunk = bytes(self._carry[:max_bytes])
        del self._carry[:max_bytes]
        return chunk

    async def send(self, data: bytes) -> None:
        if not data:
            return
        await self._tx.send(data)

    async def send_eof(self) -> None:
        if self._tx_closed:
            return
        self._tx_closed = True
        try:
            await self._tx.aclose()
        except CLOSED:
            pass

    async def aclose(self) -> None:
        await self.send_eof()
        self._rx_closed = True
        try:
            await self._rx.aclose()
        except CLOSED:
            pass


def duplex_pair(buffer_size: int = 8) -> tuple[Duplex, Duplex]:
    """Two connected in-memory ends, left and right."""
    left_to_right = anyio.create_memory_object_stream[bytes](buffer_size)
    right_to_left = anyio.create_memory_object_stream[bytes](buffer_size)
    left = MemoryDuplex(left_to_right[0], right_to_left[1])
    right = MemoryDuplex(right_to_left[0], left_to_right[1])
    return left, right


async def connect_tcp(host: str, port: int) -> Duplex:
    stream = await anyio.connect_tcp(host, port)
    tune(stream)
    return stream


def peer(stream: Duplex) -> str:
    """Remote address for logging; in-memory ends have none."""
    try:
        return stream.extra(SocketAttribute.remote_address)[0]  # type: ignore[attr-defined]
    except Exception:
        return "-"


def tune(stream: Duplex) -> None:
    """Best-effort TCP options; a non-socket stream is not an error."""
    try:
        sock: socket.socket = stream.extra(SocketAttribute.raw_socket)  # type: ignore[attr-defined]
    except Exception:
        return
    try:
        # Without this each 48-byte header waits for the peer's delayed ACK,
        # which adds ~40ms per sub-request and dominates the transfer time.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # A client that vanishes without FIN used to leave the upstream socket
        # open, which kept the device in SDEV_ST_USED and made every later
        # import fail with EBUSY.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, val in (("TCP_KEEPIDLE", 5), ("TCP_KEEPINTVL", 3), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
    except OSError:
        pass


async def recv_exact(stream: Duplex, n: int) -> bytes | None:
    """Read exactly n bytes; None means the peer closed before that."""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        try:
            chunk = await stream.receive(n - got)
        except CLOSED:
            return None
        if not chunk:
            return None
        view[got : got + len(chunk)] = chunk
        got += len(chunk)
    return bytes(buf)


async def pump(src: Duplex, dst: Duplex) -> None:
    """Copy src to dst until src ends, then half-close dst."""
    try:
        while True:
            try:
                data = await src.receive(BUFFER_BYTES)
            except CLOSED:
                break
            if not data:
                break
            await dst.send(data)
    except CLOSED + (OSError,):
        pass
    finally:
        try:
            await dst.send_eof()
        except CLOSED + (OSError,):
            pass
