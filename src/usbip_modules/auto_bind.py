"""Auto-export sidecar for the 2-1.{1-6}.{1-6} ports, entirely in-process.

Not a relay stage: no client connection ever reaches this module. It follows
the kernel's kobject uevents (NETLINK_KOBJECT_UEVENT), binds the devices on
those 36 ports to usbip-host, and releases exports a vanished client left
behind in SDEV_ST_USED.

No udev rule, no shell helper, no subprocess. The kernel is the event source:
hot plug arrives as a netlink uevent, cold plug is one startup sweep, and a slow
fallback sweep covers a dropped datagram. Every action is a kernel interface --
a sysfs write for the bind, another for the release, /proc/net/tcp for live
client detection, and a libc umount2 call for auto-mounted partitions. Loading
usbip-host is the one thing a device daemon cannot do through sysfs;
usbipd.service already loads it, and this process waits for the driver
directory to appear.

The allowlist is expanded once into 36 exact busids, not pattern-matched per
scan: a device outside 2-1.{1-6}.{1-6} is never bound, never released, and
never logged. The hub itself (2-1.1) and interface nodes (2-1.1.1:1.0) are
outside the set by construction.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import anyio

from usbip_core.config import AutobindConfig, ConfigError, load_autobind
from usbip_core.log import Logger

DEVDIR = Path("/sys/bus/usb/devices")
BLKDIR = Path("/sys/class/block")
STUB = Path("/sys/bus/usb/drivers/usbip-host")
MATCH = STUB / "match_busid"
BIND = STUB / "bind"
DRIVERS = Path("/sys/bus/usb/drivers")
SETTLE = 2.0
STORAGE = 5.0
RELEASE = 10.0
STEP = 0.1
MNT_DETACH = 2
NETLINK_KOBJECT_UEVENT = 15
UEVENT_GROUP = 1
UEVENT_BUF = 8192
UEVENT_MAX = 64
RANGE = re.compile(r"\{(\d+)-(\d+)\}")

_libc = ctypes.CDLL(None, use_errno=True)
_stub_warned = False

def expand(pattern: str) -> frozenset[str]:
    """Expand every {low-high} group: 2-1.{1-6}.{1-6} -> 36 busids."""
    hit = RANGE.search(pattern)
    if hit is None:
        return frozenset({pattern})
    low, high = int(hit.group(1)), int(hit.group(2))
    if low > high:
        raise ConfigError(f"busid range {{{low}-{high}}} in {pattern!r} is empty")
    head, tail = pattern[:hit.start()], pattern[hit.end():]
    names: set[str] = set()
    for n in range(low, high + 1):
        names |= expand(f"{head}{n}{tail}")
    return frozenset(names)

@dataclass(frozen=True)
class Options:
    log: Logger
    busids: frozenset[str]
    poll_interval: float
    host_port: int
    recover: bool

def create(cfg: AutobindConfig) -> Options:
    if not cfg.busids.strip():
        raise ConfigError("empty 'busids' in [autobind]")
    if cfg.poll_interval <= 0:
        raise ConfigError("'poll_interval' in [autobind] must be positive")
    return Options(
        log=Logger(cfg.log_dir, cfg.log_prefix, "usbip-autobind", cfg.log_level),
        busids=expand(cfg.busids),
        poll_interval=cfg.poll_interval,
        host_port=cfg.host_port,
        recover=cfg.recover,
    )

def attr(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None

def driver_of(dev: Path) -> str | None:
    # strict: a device with no driver has no "driver" link at all, and a
    # non-strict resolve would hand back the literal path, naming it "driver".
    try:
        return (dev / "driver").resolve(strict=True).name
    except OSError:
        return None

def needs_bind(dev: Path) -> bool:
    if attr(dev / "idVendor") is None:
        return False
    if attr(dev / "bDeviceClass") == "09":
        return False
    return driver_of(dev) != "usbip-host"

def has_storage(dev: Path) -> bool:
    for intf in dev.glob("*:*"):
        if attr(intf / "bInterfaceClass") == "08":
            return True
    return False

def block_ready(dev: Path) -> bool:
    for blk in BLKDIR.glob("sd*"):
        try:
            parts = blk.resolve().parts
        except OSError:
            continue
        if dev.name in parts:
            return True
    return False

def umount2(target: str) -> str | None:
    """Detach one mount through libc; None on success, an error text otherwise."""
    try:
        fn = _libc.umount2
    except AttributeError:
        return "libc has no umount2"
    if fn(target.encode(), MNT_DETACH) == 0:
        return None
    return os.strerror(ctypes.get_errno())

def unmount(dev: Path, log: Logger) -> None:
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return
    for line in mounts:
        fields = line.split()
        if len(fields) < 2 or not fields[0].startswith("/dev/sd"):
            continue
        try:
            parts = (BLKDIR / Path(fields[0]).name).resolve().parts
        except OSError:
            continue
        if dev.name not in parts:
            continue
        error = umount2(fields[1])
        if error is None:
            log.info("%s: unmounted %s" % (dev.name, fields[1]))
        else:
            log.warn("%s: umount %s failed: %s" % (dev.name, fields[1], error))

def stub_ready(log: Logger) -> bool:
    """usbip-host is loaded by usbipd.service; here we only wait for it."""
    global _stub_warned
    if STUB.is_dir():
        _stub_warned = False
        return True
    if not _stub_warned:
        log.error("usbip-host driver missing; waiting for it before binding")
        _stub_warned = True
    return False

async def wait_for(check: Callable[[], bool], timeout: float,
                   step: float = STEP) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        await anyio.sleep(step)

async def bind(opts: Options, busid: str, dev: Path) -> None:
    """Bind one device the way usbip bind does: unbind, match, attach."""
    log = opts.log
    if not stub_ready(log):
        return
    if not await wait_for(lambda: (dev / "idVendor").is_file(), SETTLE):
        log.warn("%s: sysfs incomplete after %.1fs, binding anyway" % (busid, SETTLE))
    if has_storage(dev):
        ready = await wait_for(lambda: block_ready(dev), STORAGE)
        await anyio.sleep(0.5)
        log.debug("%s: storage settled block_node=%s" % (busid, ready))
    unmount(dev, log)

    current = driver_of(dev)
    if current == "usbip-host":
        return
    if current:
        try:
            (DRIVERS / current / "unbind").write_text(busid)
            log.debug("%s: unbound from %s" % (busid, current))
        except OSError as e:
            log.error("%s: cannot unbind %s: %s" % (busid, current, e))
            return
    try:
        MATCH.write_text("add %s" % busid)
        BIND.write_text(busid)
    except OSError as e:
        log.error("%s: bind failed: %s" % (busid, e))
        return
    if await wait_for(lambda: driver_of(dev) == "usbip-host", SETTLE):
        log.info("%s: bound to usbip-host" % busid)
    else:
        log.error("%s: bind did not take effect (driver=%s)"
                  % (busid, driver_of(dev)))

def peers(port: int) -> int:
    count = 0
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "01":
                continue
            try:
                local = int(fields[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if local == port:
                count += 1
    return count

async def release(opts: Options, found: dict[str, Path]) -> None:
    log = opts.log
    if not opts.recover or peers(opts.host_port) > 0:
        return
    stale = [b for b, dev in found.items() if attr(dev / "usbip_status") == "2"]
    if not stale:
        return
    for busid in stale:
        try:
            (found[busid] / "usbip_sockfd").write_text("-1")
            log.info("%s: released stale export" % busid)
        except OSError as e:
            log.warn("%s: release failed: %s" % (busid, e))
    await wait_for(
        lambda: all(attr(found[b] / "usbip_status") != "2" for b in stale),
        RELEASE, 0.2)

def present(opts: Options) -> dict[str, Path]:
    found: dict[str, Path] = {}
    try:
        entries = list(DEVDIR.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.name in opts.busids:
            found[entry.name] = entry
    return found

async def worker(opts: Options, busid: str, dev: Path, busy: set[str]) -> None:
    try:
        await bind(opts, busid, dev)
    except Exception as e:
        opts.log.error("%s: bind aborted: %s" % (busid, e))
    finally:
        busy.discard(busid)

def open_uevent() -> socket.socket:
    """Subscribe to the kernel's kobject uevents; no udev, no daemon."""
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM,
                         NETLINK_KOBJECT_UEVENT)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind((0, UEVENT_GROUP))
    sock.setblocking(False)
    return sock

def uevent_events(sock: socket.socket) -> list[tuple[str, str]]:
    """Drain at most UEVENT_MAX uevents as (action, busid) for usb devices."""
    events: list[tuple[str, str]] = []
    for _ in range(UEVENT_MAX):
        try:
            data = sock.recv(UEVENT_BUF)
        except (BlockingIOError, InterruptedError):
            break
        except OSError:
            break
        msg: dict[str, str] = {}
        for field in data.split(b"\0"):
            key, sep, value = field.partition(b"=")
            if sep:
                msg[key.decode(errors="replace")] = value.decode(errors="replace")
        if msg.get("SUBSYSTEM") != "usb" or msg.get("DEVTYPE") != "usb_device":
            continue
        busid = Path(msg.get("DEVPATH", "")).name
        action = msg.get("ACTION")
        if action in ("add", "remove") and busid:
            events.append((action, busid))
    return events

def free_slot(busid: str, log: Logger) -> None:
    """Drop the match_busid entry so the next plug is bound by our sequence."""
    try:
        MATCH.write_text("del %s" % busid)
        log.info("%s: match slot freed" % busid)
    except OSError as e:
        log.warn("%s: free slot failed: %s" % (busid, e))

async def sweep(opts: Options, tg: anyio.abc.TaskGroup, busy: set[str]) -> None:
    found = present(opts)
    await release(opts, found)
    for busid, dev in found.items():
        if busid in busy or not needs_bind(dev):
            continue
        busy.add(busid)
        tg.start_soon(worker, opts, busid, dev, busy)

async def dispatch(opts: Options, tg: anyio.abc.TaskGroup, busy: set[str],
                   action: str, busid: str) -> None:
    if busid not in opts.busids:
        return
    if action == "remove":
        free_slot(busid, opts.log)
        return
    dev = DEVDIR / busid
    if busid in busy or not needs_bind(dev):
        return
    busy.add(busid)
    tg.start_soon(worker, opts, busid, dev, busy)

async def reconcile(opts: Options, tg: anyio.abc.TaskGroup,
                    busy: set[str]) -> None:
    while True:
        await anyio.sleep(opts.poll_interval)
        await sweep(opts, tg, busy)

async def follow(sock: socket.socket, opts: Options, tg: anyio.abc.TaskGroup,
                 busy: set[str]) -> None:
    while True:
        await anyio.wait_readable(sock)
        for action, busid in uevent_events(sock):
            await dispatch(opts, tg, busy, action, busid)

async def monitor(opts: Options) -> None:
    log = opts.log
    log.info("watching %d busid(s) on kernel uevents, reconcile every %.1fs"
             % (len(opts.busids), opts.poll_interval))
    try:
        sock: socket.socket | None = open_uevent()
    except OSError as e:
        log.error("kernel uevent socket unavailable: %s; sweeping only" % e)
        sock = None
    busy: set[str] = set()
    async with anyio.create_task_group() as tg:
        try:
            tg.start_soon(reconcile, opts, tg, busy)
            await sweep(opts, tg, busy)
            if sock is None:
                await anyio.sleep_forever()
            await follow(sock, opts, tg, busy)
        finally:
            if sock is not None:
                sock.close()

def exported() -> list[tuple[str, Path]]:
    """Every device bound to usbip-host, allowlist or not."""
    found: list[tuple[str, Path]] = []
    try:
        links = list(STUB.glob("*-*"))
    except OSError:
        return found
    for link in links:
        found.append((link.name, DEVDIR / link.name))
    return found

def recover_once(opts: Options, force: bool) -> int:
    """One forced release pass, for usbipd stop/start; then exit."""
    log = opts.log
    if not force and peers(opts.host_port) > 0:
        log.info("recover: clients connected, nothing released")
        return 0
    released: list[str] = []
    for busid, dev in exported():
        if attr(dev / "usbip_status") != "2":
            continue
        try:
            (dev / "usbip_sockfd").write_text("-1")
            released.append(busid)
            log.info("%s: released by recover" % busid)
        except OSError as e:
            log.warn("%s: recover failed: %s" % (busid, e))
    if not released:
        log.info("recover: nothing to release")
        return 0
    deadline = time.monotonic() + RELEASE
    while time.monotonic() < deadline:
        if all(attr(DEVDIR / b / "usbip_status") != "2" for b in released):
            break
        time.sleep(0.2)
    busy = [b for b in released if attr(DEVDIR / b / "usbip_status") == "2"]
    if busy:
        log.warn("recover: %d export(s) still busy" % len(busy))
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="usbip-autobind")
    parser.add_argument("--recover", action="store_true",
                        help="release stale exports once and exit")
    parser.add_argument("--force", action="store_true",
                        help="with --recover: release even while clients are connected")
    args = parser.parse_args(argv)
    if args.force and not args.recover:
        parser.error("--force needs --recover")
    try:
        opts = create(load_autobind())
    except ConfigError as e:
        print("usbip-autobind: %s" % e, file=sys.stderr)
        return 2
    if not DEVDIR.is_dir():
        print("usbip-autobind: %s is not available" % DEVDIR, file=sys.stderr)
        return 2
    if args.recover:
        return recover_once(opts, args.force)
    if not opts.busids:
        print("usbip-autobind: empty busid allowlist", file=sys.stderr)
        return 2
    try:
        anyio.run(monitor, opts)
    except KeyboardInterrupt:
        pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
