#!/usr/bin/env python3
"""USB/IP relay that caps bulk transfer size and fixes short control IN transfers.

Two transforms, both applied on the wire between the Windows client and usbipd:

1. bulk SUBMIT with transfer_buffer_length above MAX_TRANSFER is split into
   sequential sub-requests and reassembled into one RET_SUBMIT. The board's
   dwc3 controller has no dma-coherent property, so every DMA mapping goes
   through swiotlb, which cannot map more than IO_TLB_SEGSIZE(128) * 2KB in one
   go. A single 512KB URB therefore fails with -EAGAIN and takes the whole port
   down. 128KB chunks stay well below that ceiling.

2. control IN SUBMIT with URB_SHORT_NOT_OK set has the flag cleared, working
   around a usbip-win2 defect where a legal short data stage becomes an error.

Usage:
    uv run python usbserver/usbip-split.py
    USBIP_BOARD_HOST=192.168.2.111 uv run python usbserver/usbip-split.py

Then attach against 127.0.0.1 instead of the board address:
    usbip attach -r 127.0.0.1 -b 6-1
"""

import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

LISTEN_HOST = os.environ.get("SPLIT_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SPLIT_LISTEN_PORT", "3240"))
UPSTREAM_HOST = os.environ.get("SPLIT_UPSTREAM_HOST",
                               os.environ.get("USBIP_BOARD_HOST", "192.168.2.111"))
UPSTREAM_PORT = int(os.environ.get("SPLIT_UPSTREAM_PORT", "3240"))
MAX_TRANSFER = int(os.environ.get("SPLIT_MAX_TRANSFER", str(128 * 1024)))
CHUNK_TIMEOUT = float(os.environ.get("SPLIT_TIMEOUT", "10"))
LOG_PATH = os.environ.get("SPLIT_LOG", str(HERE.parent / "logs" / "usbip-split.log"))
LOG_LEVEL = os.environ.get("SPLIT_LOG_LEVEL", "DEBUG").upper()

HDR_LEN = 48
OP_LEN = 8
BUSID_LEN = 32
DEV_DESC_LEN = 312

OP_REQ_IMPORT = 0x8003
OP_REQ_DEVLIST = 0x8005
CMD_SUBMIT = 0x00000001
CMD_UNLINK = 0x00000002
RET_SUBMIT = 0x00000003

URB_SHORT_NOT_OK = 0x0001
DIR_IN = 1
DIR_OUT = 0

OFF_CMD = 0
OFF_SEQ = 4
OFF_DIR = 12
OFF_EP = 16
OFF_FLAGS = 20
OFF_LEN = 24
OFF_NPKT = 32
RET_OFF_STATUS = 20
RET_OFF_ACTUAL = 24
RET_OFF_START_FRAME = 28
RET_OFF_NPKT = 32
RET_OFF_ERRCNT = 36

SYNTH_SEQ_BASE = 0x40000000
STATUS_ETIMEDOUT = -110

LEVELS = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4, "FATAL": 5}
_log_lock = threading.Lock()


def log(level, msg):
    if LEVELS.get(level, 2) < LEVELS.get(LOG_LEVEL, 0):
        return
    line = "%s | %-5s | usbip-split | pid=%d | %s\n" % (
        time.strftime("%Y-%m-%dT%H:%M:%S%z"), level, os.getpid(), msg)
    with _log_lock:
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line)
        except OSError:
            pass


def recv_exact(sock, n):
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        try:
            k = sock.recv_into(view[got:], n - got)
        except OSError:
            return None
        if not k:
            return None
        got += k
    return bytes(buf)


def unpack_u32(buf, off):
    return struct.unpack_from(">I", buf, off)[0]


def unpack_i32(buf, off):
    return struct.unpack_from(">i", buf, off)[0]


class Relay:
    def __init__(self, client, peer):
        self.client = client
        self.server = None
        self.peer = peer
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.waiters = {}
        self.pending = {}
        self.seq = SYNTH_SEQ_BASE
        self.splits = 0
        self.chunks = 0
        self.hex_left = int(os.environ.get("SPLIT_HEX_DUMP", "4"))

    def next_seq(self):
        with self.lock:
            self.seq += 1
            if self.seq >= SYNTH_SEQ_BASE + 0x10000000:
                self.seq = SYNTH_SEQ_BASE
            return self.seq

    def handshake(self):
        op = recv_exact(self.client, OP_LEN)
        if op is None:
            return False
        code = struct.unpack_from(">H", op, 2)[0]

        if code == OP_REQ_DEVLIST:
            log("DEBUG", "devlist from %s, raw relay" % self.peer)
            self.server.sendall(op)
            return "raw"

        if code != OP_REQ_IMPORT:
            log("WARN", "unknown op 0x%04x from %s, raw relay" % (code, self.peer))
            self.server.sendall(op)
            return "raw"

        busid = recv_exact(self.client, BUSID_LEN)
        if busid is None:
            return False
        self.server.sendall(op + busid)
        log("INFO", "import %s from %s" % (busid.rstrip(b"\x00").decode("utf-8", "replace"), self.peer))

        rep = recv_exact(self.server, OP_LEN)
        if rep is None:
            log("ERROR", "no import reply")
            return False
        self.client.sendall(rep)
        status = struct.unpack_from(">i", rep, 4)[0]
        log("DEBUG", "import reply %s status=%d" % (rep.hex(), status))
        if status != 0:
            # usbipd may append an error string, but does not always do so.
            # Never block on it: a stuck read here holds the upstream socket
            # open and keeps the device in SDEV_ST_USED forever.
            self.server.settimeout(1.0)
            try:
                err = self.server.recv(256)
                if err:
                    self.client.sendall(err)
            except OSError:
                pass
            finally:
                self.server.settimeout(None)
            log("ERROR", "import rejected, status=%d" % status)
            return False

        self.server.settimeout(5.0)
        try:
            dev = recv_exact(self.server, DEV_DESC_LEN)
        except OSError as e:
            log("ERROR", "device descriptor read failed: %s" % e)
            return False
        finally:
            self.server.settimeout(None)
        if dev is None:
            log("ERROR", "device descriptor truncated")
            return False
        self.client.sendall(dev)
        nintf = dev[DEV_DESC_LEN - 1]
        log("DEBUG", "device descriptor head %s nintf=%d" % (dev[:8].hex(), nintf))
        if 0 < nintf <= 32:
            self.server.settimeout(2.0)
            try:
                extra = recv_exact(self.server, 4 * nintf)
            finally:
                self.server.settimeout(None)
            if extra:
                self.client.sendall(extra)
        log("INFO", "import ok, interfaces=%d" % nintf)
        return True

    def server_loop(self):
        try:
            while True:
                hdr = recv_exact(self.server, HDR_LEN)
                if hdr is None:
                    break
                cmd = unpack_u32(hdr, OFF_CMD)
                seq = unpack_u32(hdr, OFF_SEQ)
                payload = b""

                if cmd == RET_SUBMIT:
                    actual = unpack_i32(hdr, RET_OFF_ACTUAL)
                    with self.lock:
                        direction = self.pending.pop(seq, DIR_OUT)
                    if direction == DIR_IN and actual > 0:
                        payload = recv_exact(self.server, actual)
                        if payload is None:
                            break

                log("DEBUG", "ret cmd=%d seq=%d dir=%d status=%d actual=%d data=%d" % (
                    cmd, seq, direction if cmd == RET_SUBMIT else -1,
                    unpack_i32(hdr, RET_OFF_STATUS),
                    unpack_i32(hdr, RET_OFF_ACTUAL), len(payload)))
                if self.hex_left > 0:
                    self.hex_left -= 1
                    log("DEBUG", "ret hex %s" % hdr.hex())

                box = None
                with self.lock:
                    box = self.waiters.get(seq)
                    if box is not None:
                        box.append((hdr, payload))
                        self.cond.notify_all()

                if box is None:
                    self.client.sendall(hdr)
                    if payload:
                        self.client.sendall(payload)
        except OSError as e:
            log("DEBUG", "server loop end: %s" % e)
        finally:
            with self.lock:
                self.cond.notify_all()

    def wait_reply(self, seq):
        box = []
        with self.lock:
            self.waiters[seq] = box
        deadline = time.time() + CHUNK_TIMEOUT
        with self.lock:
            while not box:
                left = deadline - time.time()
                if left <= 0:
                    break
                self.cond.wait(left)
            self.waiters.pop(seq, None)
        return box[0] if box else None

    def submit_split(self, hdr, payload):
        direction = unpack_u32(hdr, OFF_DIR)
        length = unpack_i32(hdr, OFF_LEN)
        collected = []
        total = 0
        status = 0
        sent = 0

        for off in range(0, length, MAX_TRANSFER):
            n = min(MAX_TRANSFER, length - off)
            sub = bytearray(hdr)
            seq = self.next_seq()
            struct.pack_into(">I", sub, OFF_SEQ, seq)
            struct.pack_into(">i", sub, OFF_LEN, n)
            with self.lock:
                self.pending[seq] = direction

            self.server.sendall(bytes(sub))
            if direction != DIR_IN:
                self.server.sendall(payload[off:off + n])
            sent += 1

            reply = self.wait_reply(seq)
            if reply is None:
                log("ERROR", "chunk %d/%d seq=%d timeout" % (sent, (length + MAX_TRANSFER - 1)//MAX_TRANSFER, seq))
                status = STATUS_ETIMEDOUT
                break

            rhdr, rdata = reply
            st = unpack_i32(rhdr, RET_OFF_STATUS)
            act = unpack_i32(rhdr, RET_OFF_ACTUAL)
            if st != 0:
                log("WARN", "chunk %d status=%d" % (sent, st))
                status = st
                break

            total += act
            if direction == DIR_IN:
                if rdata:
                    collected.append(rdata)
                if act < n:
                    log("DEBUG", "short chunk %d: %d < %d" % (sent, act, n))
                    break

        out = bytearray(hdr)
        struct.pack_into(">I", out, OFF_CMD, RET_SUBMIT)
        struct.pack_into(">i", out, RET_OFF_STATUS, status)
        struct.pack_into(">i", out, RET_OFF_ACTUAL, total)
        struct.pack_into(">i", out, RET_OFF_START_FRAME, 0)
        struct.pack_into(">i", out, RET_OFF_NPKT, 0)
        struct.pack_into(">i", out, RET_OFF_ERRCNT, 0)
        self.client.sendall(bytes(out))
        if direction == DIR_IN and collected:
            self.client.sendall(b"".join(collected))

        self.splits += 1
        self.chunks += sent
        log("INFO", "split ep=%d len=%d -> %d chunks, total=%d status=%d" % (
            unpack_u32(hdr, OFF_EP), length, sent, total, status))

    def client_loop(self):
        try:
            while True:
                hdr = recv_exact(self.client, HDR_LEN)
                if hdr is None:
                    break
                cmd = unpack_u32(hdr, OFF_CMD)
                payload = b""

                if cmd == CMD_SUBMIT:
                    direction = unpack_u32(hdr, OFF_DIR)
                    length = unpack_i32(hdr, OFF_LEN)
                    ep = unpack_u32(hdr, OFF_EP)
                    npkt = unpack_i32(hdr, OFF_NPKT)
                    log("DEBUG", "submit seq=%d dir=%d ep=%d len=%d npkt=%d" % (
                        unpack_u32(hdr, OFF_SEQ), direction, ep, length, npkt))
                    if self.hex_left > 0:
                        log("DEBUG", "submit hex %s" % hdr.hex())

                    if direction != DIR_IN and length > 0:
                        payload = recv_exact(self.client, length)
                        if payload is None:
                            break

                    # usbip-win2 sends -1, not 0, for everything that is not isochronous
                    splittable = ep != 0 and npkt <= 0 and length > MAX_TRANSFER
                    if splittable:
                        self.submit_split(hdr, payload)
                        continue
                    if length > MAX_TRANSFER:
                        log("WARN", "oversize submit not splittable: ep=%d dir=%d len=%d npkt=%d" % (
                            ep, direction, length, npkt))

                    if direction == DIR_IN and ep == 0 and length > 0:
                        flags = unpack_u32(hdr, OFF_FLAGS)
                        if flags & URB_SHORT_NOT_OK:
                            h = bytearray(hdr)
                            struct.pack_into(">I", h, OFF_FLAGS, flags & ~URB_SHORT_NOT_OK)
                            log("DEBUG", "cleared URB_SHORT_NOT_OK seq=%d len=%d" % (unpack_u32(hdr, OFF_SEQ), length))
                            hdr = bytes(h)

                if cmd == CMD_SUBMIT:
                    with self.lock:
                        self.pending[unpack_u32(hdr, OFF_SEQ)] = unpack_u32(hdr, OFF_DIR)

                self.server.sendall(hdr)
                if payload:
                    self.server.sendall(payload)
        except OSError as e:
            log("DEBUG", "client loop end: %s" % e)

    def run(self):
        try:
            self.server = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10)
            self.server.settimeout(None)
            self.client.settimeout(None)
            # Without this each 48-byte header waits for the peer's delayed ACK,
            # which adds ~40ms per sub-request and dominates the transfer time.
            for s in (self.server, self.client):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # A client that vanishes without FIN used to leave the upstream
                # socket open, which kept the device in SDEV_ST_USED and made
                # every later import fail with EBUSY.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                for opt, val in (("TCP_KEEPIDLE", 5), ("TCP_KEEPINTVL", 3), ("TCP_KEEPCNT", 3)):
                    if hasattr(socket, opt):
                        try:
                            s.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
                        except OSError:
                            pass
        except OSError as e:
            log("ERROR", "connect %s:%d failed: %s" % (UPSTREAM_HOST, UPSTREAM_PORT, e))
            return

        state = self.handshake()
        if state is False:
            self.close()
            return
        if state == "raw":
            t = threading.Thread(target=self.raw_back, daemon=True)
            t.start()
            self.raw_forward()
            t.join(timeout=5)
            self.close()
            return

        t = threading.Thread(target=self.server_loop, daemon=True)
        t.start()
        self.client_loop()
        self.close()

    def raw_forward(self):
        try:
            while True:
                data = self.client.recv(65536)
                if not data:
                    break
                self.server.sendall(data)
        except OSError:
            pass

    def raw_back(self):
        try:
            while True:
                data = self.server.recv(65536)
                if not data:
                    break
                self.client.sendall(data)
        except OSError:
            pass

    def close(self):
        for s in (self.server, self.client):
            try:
                if s:
                    s.close()
            except OSError:
                pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(16)
    log("INFO", "listening on %s:%d -> %s:%d, max_transfer=%d" % (
        LISTEN_HOST, LISTEN_PORT, UPSTREAM_HOST, UPSTREAM_PORT, MAX_TRANSFER))
    print("listening on %s:%d -> %s:%d, max_transfer=%d" % (
        LISTEN_HOST, LISTEN_PORT, UPSTREAM_HOST, UPSTREAM_PORT, MAX_TRANSFER))
    sys.stdout.flush()

    while True:
        client, addr = srv.accept()
        log("INFO", "connection from %s" % (addr[0],))
        threading.Thread(target=Relay(client, addr[0]).run, daemon=True).start()


if __name__ == "__main__":
    main()
