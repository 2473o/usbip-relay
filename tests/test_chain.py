"""Chain-level tests: client -> split -> shortfix -> fake usbipd, all in memory."""

from __future__ import annotations

import struct
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest

from usbip import build_stages as relay_stages
from usbip_core.config import ConfigError, load
from usbip_core.pipeline import Stage, run_chain
from usbip_core.stream import Duplex, duplex_pair

HDR_LEN = 48
MAX_TRANSFER = 1000
CHUNK_TIMEOUT = 0.3

OP_REQ_IMPORT = 0x8003
CMD_SUBMIT = 0x00000001
RET_SUBMIT = 0x00000003
DIR_OUT = 0
DIR_IN = 1
URB_SHORT_NOT_OK = 0x0001
STATUS_ETIMEDOUT = -110

CONFIG = """\
[server]
host = "127.0.0.1"
host_port = 3240
listen_port = 3241
logs = "{logs}"
stages = [{stages}]

[server.split]
note = "chunk bulk URBs"
log_prefix = "test_split_"
log_level = "DEBUG"
max_transfer = {max_transfer}
timeout = {timeout}
hex_dump = 0

[server.shortfix]
note = "clear URB_SHORT_NOT_OK"
log_prefix = "test_shortfix_"
verbose = true
"""


def build_stages(tmp_path: Path, *, max_transfer: int = MAX_TRANSFER,
                 timeout: float = CHUNK_TIMEOUT,
                 stages: str = '"split", "shortfix"') -> list[Stage]:
    """Stages as the relay builds them: straight from a config.toml."""
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(
        logs=(tmp_path / "logs").as_posix(), stages=stages,
        max_transfer=max_transfer, timeout=timeout),
        encoding="utf-8")
    return relay_stages(load(path))


class Wire:
    def __init__(self, stream: Duplex):
        self.stream = stream

    async def read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = await self.stream.receive(n - len(buf))
            if not chunk:
                raise EOFError("closed with %d/%d bytes" % (len(buf), n))
            buf += chunk
        return bytes(buf)

    async def send(self, data: bytes) -> None:
        await self.stream.send(data)


def submit(seq: int, direction: int, ep: int, length: int,
           flags: int = 0, npkt: int = -1) -> bytes:
    h = bytearray(HDR_LEN)
    struct.pack_into(">I", h, 0, CMD_SUBMIT)
    struct.pack_into(">I", h, 4, seq)
    struct.pack_into(">I", h, 12, direction)
    struct.pack_into(">I", h, 16, ep)
    struct.pack_into(">I", h, 20, flags)
    struct.pack_into(">i", h, 24, length)
    struct.pack_into(">i", h, 32, npkt)
    return bytes(h)


def ret_submit(seq: int, status: int, actual: int) -> bytes:
    h = bytearray(HDR_LEN)
    struct.pack_into(">I", h, 0, RET_SUBMIT)
    struct.pack_into(">I", h, 4, seq)
    struct.pack_into(">i", h, 20, status)
    struct.pack_into(">i", h, 24, actual)
    return bytes(h)


def header_fields(hdr: bytes) -> tuple[int, int, int, int, int, int, int]:
    cmd, seq = struct.unpack_from(">II", hdr, 0)
    direction, ep, flags, length, npkt = struct.unpack_from(">IIiii", hdr, 12)
    return cmd, seq, direction, ep, flags, length, npkt


@asynccontextmanager
async def open_chain(stages: list[Stage]) -> AsyncGenerator[tuple[Wire, Wire], None]:
    """Yield (client side, usbipd side) with the chain running in the middle."""
    client, client_chain = duplex_pair()
    usbipd_link, usbipd = duplex_pair()
    used = {"connected": False}

    async def connect() -> Duplex:
        if used["connected"]:
            raise RuntimeError("upstream connected twice")
        used["connected"] = True
        return usbipd_link

    async def run() -> None:
        await run_chain(client_chain, stages, connect)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run)
        try:
            yield Wire(client), Wire(usbipd)
        finally:
            await client.aclose()
            task_group.cancel_scope.cancel()


async def handshake(client: Wire, usbipd: Wire) -> None:
    request = struct.pack(">HHI", 0x0111, OP_REQ_IMPORT, 0) + b"1-1.2".ljust(32, b"\x00")
    await client.send(request)
    assert await usbipd.read(len(request)) == request

    reply = struct.pack(">HHI", 0x0111, OP_REQ_IMPORT, 0)
    descriptor = bytearray(312)
    descriptor[0] = 12
    await usbipd.send(reply + bytes(descriptor))
    assert await client.read(len(reply) + len(descriptor)) == reply + bytes(descriptor)


@pytest.mark.anyio
async def test_import_request_reaches_usbipd(tmp_path: Path) -> None:
    async with open_chain(build_stages(tmp_path)) as (client, usbipd):
        await handshake(client, usbipd)


@pytest.mark.anyio
async def test_shortfix_clears_short_not_ok_on_control_in(tmp_path: Path) -> None:
    async with open_chain(build_stages(tmp_path)) as (client, usbipd):
        await handshake(client, usbipd)

        await client.send(submit(seq=7, direction=DIR_IN, ep=0, length=64,
                                 flags=URB_SHORT_NOT_OK))
        forwarded = await usbipd.read(HDR_LEN)
        assert header_fields(forwarded) == header_fields(
            submit(seq=7, direction=DIR_IN, ep=0, length=64, flags=0))

        await usbipd.send(ret_submit(7, 0, 8) + b"\xaa" * 8)
        assert await client.read(HDR_LEN + 8) == ret_submit(7, 0, 8) + b"\xaa" * 8


@pytest.mark.anyio
async def test_bulk_submit_is_chunked_and_reassembled(tmp_path: Path) -> None:
    async with open_chain(build_stages(tmp_path)) as (client, usbipd):
        await handshake(client, usbipd)

        payload = bytes(range(250)) * 10
        await client.send(submit(seq=9, direction=DIR_OUT, ep=1, length=len(payload)))
        await client.send(payload)

        lengths = []
        seqs = []
        received = b""
        for _ in range(3):
            hdr = await usbipd.read(HDR_LEN)
            cmd, seq, direction, ep, _flags, length, _npkt = header_fields(hdr)
            assert (cmd, direction, ep) == (CMD_SUBMIT, DIR_OUT, 1)
            seqs.append(seq)
            lengths.append(length)
            received += await usbipd.read(length)
            await usbipd.send(ret_submit(seq, 0, 0))

        assert lengths == [MAX_TRANSFER, MAX_TRANSFER, len(payload) - 2 * MAX_TRANSFER]
        assert received == payload
        assert len(set(seqs)) == 3
        assert all(seq != 9 for seq in seqs)

        out = await client.read(HDR_LEN)
        cmd, seq = struct.unpack_from(">II", out, 0)
        assert (cmd, seq) == (RET_SUBMIT, 9)


@pytest.mark.anyio
async def test_chunk_timeout_reports_etimedout(tmp_path: Path) -> None:
    async with open_chain(build_stages(tmp_path)) as (client, usbipd):
        await handshake(client, usbipd)

        await client.send(submit(seq=11, direction=DIR_OUT, ep=1, length=2 * MAX_TRANSFER))
        await client.send(b"\x01" * (2 * MAX_TRANSFER))
        await usbipd.read(HDR_LEN + MAX_TRANSFER)

        out = await client.read(HDR_LEN)
        cmd, seq = struct.unpack_from(">II", out, 0)
        status = struct.unpack_from(">i", out, 20)[0]
        assert (cmd, seq, status) == (RET_SUBMIT, 11, STATUS_ETIMEDOUT)


@pytest.mark.anyio
async def test_control_urb_is_never_chunked(tmp_path: Path) -> None:
    async with open_chain(build_stages(tmp_path, max_transfer=64)) as (client, usbipd):
        await handshake(client, usbipd)

        await client.send(submit(seq=13, direction=DIR_IN, ep=0, length=200))
        hdr = await usbipd.read(HDR_LEN)
        assert header_fields(hdr) == header_fields(
            submit(seq=13, direction=DIR_IN, ep=0, length=200, flags=0))


def test_example_config_loads_and_names_the_ports(pytestconfig: pytest.Config) -> None:
    example_config = pytestconfig.rootpath / "config.toml.example"
    example = example_config.read_text(encoding="utf-8")
    # The deployed configs use these key names; they are a contract.
    assert "host_port = 3240" in example
    assert "listen_port = 3241" in example

    cfg = load(example_config)
    assert cfg.listen_port == 3240
    assert cfg.upstream_port == 3241
    assert cfg.stages == ("split", "shortfix")
    assert "enable" not in cfg.sections["split"].values


def test_example_config_builds_the_documented_chain(pytestconfig: pytest.Config) -> None:
    cfg = load(pytestconfig.rootpath / "config.toml.example")
    stages = relay_stages(cfg)
    assert [stage.name for stage in stages] == ["split", "shortfix"]
    assert all(cfg.sections[stage.name].note for stage in stages)
    assert all(stage.ctx is not None for stage in stages)


def test_stage_absent_from_the_list_is_parked(tmp_path: Path) -> None:
    stages = build_stages(tmp_path, stages='"shortfix"')
    assert [stage.name for stage in stages] == ["shortfix"]


def test_config_order_cannot_reorder_the_chain(tmp_path: Path) -> None:
    # Order comes from the protocol anchors each module declares (client first,
    # usbipd last), so listing them backwards must not change the pipeline.
    stages = build_stages(tmp_path, stages='"shortfix", "split"')
    assert [stage.name for stage in stages] == ["split", "shortfix"]


def test_unknown_stage_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(
        logs=tmp_path.as_posix(), stages='"split", "ghost"',
        max_transfer=MAX_TRANSFER, timeout=CHUNK_TIMEOUT)
        + '\n[server.ghost]\nnote = "no such module"\n'
          'log_prefix = "ghost_"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="has no module"):
        relay_stages(load(path))
