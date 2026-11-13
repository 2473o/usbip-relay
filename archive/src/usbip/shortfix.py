#!/usr/bin/env python3
"""TEMPORARY shim for usbip-win2 issue #203.

https://github.com/vadimgrn/usbip-win2/issues/203

The client maps "no USBD_SHORT_TRANSFER_OK" to URB_SHORT_NOT_OK even for
control pipes, so a legal short data stage becomes -EREMOTEIO. This proxy
clears that bit on control IN submits and forwards everything else untouched.

Proper fix: skip URB_SHORT_NOT_OK when the endpoint type is control
(drivers/libdrv/usbd_helper.cpp, drivers/ude/proto.cpp). Delete this shim and
point usbipd back at port 3240 once the client is fixed.
"""

import os
import socket
import struct
import sys
import threading
import time

LISTEN_HOST = os.environ.get('SHORTFIX_LISTEN_HOST', '0.0.0.0')
LISTEN_PORT = int(os.environ.get('SHORTFIX_LISTEN_PORT', '3240'))
UPSTREAM_HOST = os.environ.get('SHORTFIX_UPSTREAM_HOST', '127.0.0.1')
UPSTREAM_PORT = int(os.environ.get('SHORTFIX_UPSTREAM_PORT', '3241'))
LOG_PATH = os.environ.get('SHORTFIX_LOG', '/var/log/usbip-shortfix.log')
VERBOSE = os.environ.get('SHORTFIX_VERBOSE', '1') == '1'

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

_log_lock = threading.Lock()
_stats = {'conns': 0, 'rewritten': 0}


def log(msg):
    line = '%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    with _log_lock:
        try:
            with open(LOG_PATH, 'a') as f:
                f.write(line)
        except OSError:
            pass


def recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def raw_pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def forward_urbs(client, server, peer):
    while True:
        hdr = recv_exact(client, HDR_LEN)
        if hdr is None:
            return
        h = bytearray(hdr)
        cmd = struct.unpack_from('>I', h, 0)[0]
        payload = b''

        if cmd == CMD_SUBMIT:
            direction = struct.unpack_from('>I', h, 12)[0]
            ep = struct.unpack_from('>I', h, 16)[0]
            length = struct.unpack_from('>i', h, 24)[0]
            seq = struct.unpack_from('>I', h, 4)[0]
            if VERBOSE:
                bm, br, wv, wi, wl = struct.unpack_from('<BBHHH', h, 40)
                log('submit seq=%d dir=%d ep=%d len=%d %02x.%02x v=%04x i=%04x l=%d' % (
                    seq, direction, ep, length, bm, br, wv, wi, wl))
            if direction == DIR_OUT and length > 0:
                payload = recv_exact(client, length)
                if payload is None:
                    return
            elif direction == DIR_IN and ep == 0:
                flags = struct.unpack_from('>I', h, 20)[0]
                if flags & URB_SHORT_NOT_OK:
                    struct.pack_into('>I', h, 20, flags & ~URB_SHORT_NOT_OK)
                    _stats['rewritten'] += 1
                    log('cleared URB_SHORT_NOT_OK seq=%d len=%d from %s' % (seq, length, peer))
        elif cmd != CMD_UNLINK:
            log('unexpected command 0x%08x from %s, switching to raw relay' % (cmd, peer))
            server.sendall(bytes(h))
            raw_pump(client, server)
            return

        server.sendall(bytes(h))
        if payload:
            server.sendall(payload)


def handle(client, peer):
    _stats['conns'] += 1
    server = None
    try:
        server = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10)
        server.settimeout(None)
        client.settimeout(None)

        op = recv_exact(client, OP_COMMON_LEN)
        if op is None:
            return
        code = struct.unpack_from('>H', op, 2)[0]

        if code == OP_REQ_IMPORT:
            busid = recv_exact(client, OP_IMPORT_BUSID_LEN)
            if busid is None:
                return
            server.sendall(op + busid)
            log('import %s from %s' % (busid.rstrip(b'\x00').decode('utf-8', 'replace'), peer))
        elif code == OP_REQ_DEVLIST:
            server.sendall(op)
            back = threading.Thread(target=raw_pump, args=(server, client), daemon=True)
            back.start()
            raw_pump(client, server)
            back.join(timeout=5)
            return
        else:
            log('unknown op 0x%04x from %s, raw relay' % (code, peer))
            server.sendall(op)
            back = threading.Thread(target=raw_pump, args=(server, client), daemon=True)
            back.start()
            raw_pump(client, server)
            back.join(timeout=5)
            return

        back = threading.Thread(target=raw_pump, args=(server, client), daemon=True)
        back.start()
        forward_urbs(client, server, peer)
        back.join(timeout=5)
    except OSError as e:
        log('connection %s error: %s' % (peer, e))
    finally:
        for s in (server, client):
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
    log('listening on %s:%d -> %s:%d' % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_HOST, UPSTREAM_PORT))
    while True:
        client, addr = srv.accept()
        threading.Thread(target=handle, args=(client, addr[0]), daemon=True).start()


if __name__ == '__main__':
    main()
