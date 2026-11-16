# USB/IP relay

> auto-export, stale-export recovery, transfer splitting, short-packet fix

The board (RK3588, kernel 5.10.198, Debian 11 Linaro) acts as a USB/IP server,
exporting its USB devices to Windows clients over TCP. The relay here sits in
front of usbipd and, without touching the client or the kernel, fixes two
problems that take the whole link down:

- large bulk transfers fail to map on swiotlb — one 512 KB URB is enough to take
  the entire port down;
- usbip-win2 treats a legal short packet on a control pipe as an error
  ([#203](https://github.com/vadimgrn/usbip-win2/issues/203)): the device
  enumerates, but every control IN read can come back `-EREMOTEIO`.

## Architecture

One process, one listening port. Every component is a stream transform, so the
chain needs no per-component port:

```
usbip client --TCP--> host_port 3240
                        |
                        +-- split      (bulk URBs above max_transfer -> N sub-requests)
                        +-- shortfix   (clear URB_SHORT_NOT_OK on control IN)
                        |
                        +--TCP--> usbipd port 3241
```

Which stages run is declared in `config.toml`: `[server] stages` is the only
switch deciding membership, and every name has its own `[server.<name>]` table
(`note` plus its knobs). A stage is any module under `usbip_modules/` exposing
`create(section)` and `handle(inbound, upstream, ctx)`, so adding or dropping one
is a config edit, not a code edit. A table left out of `stages` is parked: knobs
kept, stage never runs.

The running order is **not** declared anywhere — it follows from the protocol
rule each stage implements, from which end of the relay consumes its rewrite:

- `split` mints USB/IP seqnums (it turns one `USBIP_CMD_SUBMIT` into N), and the
  seqnum at URB header offset 4 is the client's correlation token, echoed in
  `USBIP_RET_SUBMIT`. Only the client owns that table, so this stage anchors to
  the client: it runs first.
- `shortfix` clears `URB_SHORT_NOT_OK` on control submits. USB 2.0 **8.5.3.2
  (Variable-length Data Stage)** says a control pipe ends its data stage "by
  returning a packet that is shorter than the MaxPacketSize for the pipe", or a
  ZLP when the length is an exact multiple of `wMaxPacketSize` — a short packet
  there is the spec's end-of-stage marker, not an error. The flag is consumed by
  the host controller when it completes the URB, so this stage anchors to
  usbipd: it runs last.

Each module states `ANCHOR` (`client` / `usbipd`) and the chain is built in
anchor order, so a config cannot reorder the pipeline.

## Run

```bash
uv run usbip-relay --help   # declared chain and notes (from config.toml)
uv run usbip-relay          # run the declared chain
uv run pytest               # chain tests (in-memory, no sockets)
```

On start it prints the chain actually in effect, for example:

```
usbip-relay 0.1.0: 0.0.0.0:3240 -> split -> shortfix -> 127.0.0.1:3241
```

The upstream address is fixed at `127.0.0.1` (usbipd always runs on the same
board as the relay); only the port is configurable.

## The two stages

### split: chunking large bulk transfers

The board's dwc3 controller has no `dma-coherent` property, so every DMA mapping
goes through swiotlb, which cannot map more than `IO_TLB_SEGSIZE`(128) × 2 KB in
one go. A 512 KB URB returns `-EAGAIN` and takes the whole port down. Therefore:

- a bulk submit whose transfer buffer exceeds `max_transfer` is cut into
  sequential sub-requests, each with a freshly minted seqnum;
- the chunks are serial: send one, wait for its reply, then build the next, so
  usbipd sees the order the client would have produced and the payload stays in
  transfer order;
- all chunk replies are folded into a single `RET_SUBMIT` for the client — status
  is the first failure (or `-ETIMEDOUT`, i.e. -110, when a chunk never answers),
  `actual_length` is their sum, the remaining fields are zeroed. Nothing the
  client reads back reveals the URB was ever chunked;
- an IN chunk that comes back short (`actual` below that chunk's length) ends the
  sequence early: no further chunks are sent.

**What is never chunked**: `ep == 0` (a control transfer cannot be split),
`npkt > 0` (isochronous; usbip-win2 sends `-1`, not `0`, for everything that is
not isochronous), and anything at or below `max_transfer`. An oversize submit
outside those conditions logs a WARN and is forwarded as-is.

**Seqnum space**: minted numbers start at `0x40000000` with a span of
`0x10000000`, far above anything a client mints for itself, so the two spaces
cannot collide within one connection. Both kinds are tracked — client numbers
only for their direction (needed before the payload length is read), minted ones
with a mailbox as well, so their replies are consumed here and never reach the
client.

### shortfix: control-pipe short packet fix (temporary shim for usbip-win2 #203)

[usbip-win2 #203](https://github.com/vadimgrn/usbip-win2/issues/203): the client
treats a legal short data stage on a control pipe as an error. `to_linux_flags()`
(`drivers/libdrv/usbd_helper.cpp`) tests `dir_in` without looking at the endpoint
type, and `hidclass` does not set `USBD_SHORT_TRANSFER_OK` for `GET_REPORT`, so
every control IN read goes on the wire carrying `URB_SHORT_NOT_OK`; Linux
completes it with `-EREMOTEIO` and the HID layer sees error 31 — the device
enumerates, but the vendor application believes it is not there.

Per USB 2.0 §8.5.3.2, a control pipe marks the end of its data stage with a short
packet (a ZLP when the length is an exact multiple of `wMaxPacketSize`); "short
packet means error" is bulk / interrupt semantics, and applying it to a control
pipe turns a normal reply into a failure.

This stage clears bit `0x0001` of `transfer_flags` (offset 20) only on submits
with `direction == IN && ep == 0`, and forwards every other byte untouched. It
does not change the number of requests, so it is invisible to the correlation
`split` does in front of it.

This is a temporary shim for usbip-win2 #203. The proper fix is in the client:
skip `URB_SHORT_NOT_OK` by endpoint type (`drivers/libdrv/usbd_helper.cpp`,
`drivers/ude/proto.cpp`). Once the client is fixed, revert this stage.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `[server] host` | `0.0.0.0` | relay listen address |
| `[server] host_port` | `3240` | relay listen port, clients attach here |
| `[server] listen_port` | `3241` | usbipd port, the relay dials it |
| `[server] logs` | — | log directory |
| `[server] stages` | — | chain members: non-empty array, no duplicates |

`split` knobs:

| Key | Meaning |
| --- | --- |
| `max_transfer` | per-chunk cap, default 128 KB; must stay below swiotlb's `IO_TLB_SEGSIZE × 2 KB` ceiling |
| `timeout` | seconds to wait for one chunk reply, default `10.0` |
| `hex_dump` | how many submit/reply headers to dump per connection |
| `log_level` | log level |
| `log_prefix` | log file name prefix |

`shortfix` knobs: `verbose` (level becomes DEBUG and setup packet fields are
logged when `true`), `log_prefix`.

Config is read from a `config.toml` in any directory above the package. Loading
is explicit — importing the package never touches the disk, so a bad config
surfaces from `main()`. Modules do not read environment variables: add a new key
to `config.toml` / `config.toml.example` first, then expose it as a typed field
in `usbip_core/config.py`.

## Logs

Each module writes to `<logs>/<log_prefix><YYYY-MM-DD>.log`, one line per event:

```
2026-11-16T12:48:43+0800 | INFO  | usbip-relay-split | pid=1234 | split ep=1 len=524288 -> 4 chunks, total=524288 status=0
```

The file name is resolved on every write, so a long-running relay rolls over at
midnight without a restart. Writes are serialised and never raise — a full or
read-only disk must not take the relay down.

## Deployment (on the board)

```
client --(3240)--> this relay --(3241)--> usbipd --usbip-host--> device
```

`service/usbipd/` holds the systemd unit for usbipd plus a one-shot installer:
the unit runs in the foreground, listens on IPv4 TCP 3241 only, runs as the
dedicated system user `usbip`, and relies on `CAP_DAC_OVERRIDE` to write
`/sys/bus/usb/devices/<busid>/usbip_sockfd`.

Clients must name the port explicitly, and `--tcp-port` is a global option that
goes before the subcommand:

```bash
usbip --tcp-port 3240 list   -r <board-ip>
usbip --tcp-port 3240 attach -r <board-ip> -b 1-1.2
```

Details and troubleshooting: `service/usbipd/README.md`.

## Directory structure

```
src/
├── usbip/
│   ├── __init__.py      # stage lookup for the chain in config.toml, anchor order
│   └── __main__.py      # CLI + listener
├── usbip_core/
│   ├── pipeline.py      # stage chain over in-memory duplex pairs
│   ├── stream.py        # Duplex contract, pump, recv_exact, tune
│   ├── protocol.py      # transfer length guards
│   ├── config.py
│   └── log.py
└── usbip_modules/
    ├── split/           # stage: bulk chunking
    │   ├── __init__.py  # stage contract: ANCHOR, create, handle
    │   ├── wire.py      # header layout and the seqnum table
    │   ├── handshake.py # OP_REQ_IMPORT and its descriptors
    │   └── relay.py     # chunking and the two forwarding loops
    ├── shortfix.py      # stage: URB_SHORT_NOT_OK shim
    └── auto_bind.py     # not a stream stage
tests/
└── test_chain.py
```

## Adding a stage

1. Create a module under `usbip_modules/` exposing three things:
   - `ANCHOR`: `"client"` or `"usbipd"`, stating **who consumes your rewrite**;
   - `create(section)`: read the knobs from the `[server.<name>]` table and return
     the context shared between connections; use `section.need_int / need_float /
     need_str / need_bool` — a missing key is a config error;
   - `handle(inbound, upstream, ctx)`: the whole handling of one connection.
     `inbound` is a `Duplex` (only `receive / send / send_eof / aclose`);
     `upstream` is a callback that may be called **exactly once**, and only the
     last stage in the chain actually opens a TCP connection.
2. Add the name to `stages` in `config.toml` and a matching `[server.<name>]`
   table (at least `note` and `log_prefix`).

Pick the anchor from the spec: find the entity that consumes the rewrite, and put
the stage next to it. A stage that changes the number of requests (like `split`)
must sit on the seqnum owner's side, and nothing behind it may do the same, or
the correlation ends up split across two owners.

## Current state and leftovers

- `src/usbip_modules/auto_bind.py` is an empty file right now; udev auto-export
  and stale-export recovery (`usbip-autobind`, `usbip-recover`, etc.) live under
  `archive/` and are not part of this chain yet.
- `shortfix` is a temporary shim for usbip-win2 #203, not a long-term design;
  revert it once the client is fixed.
- The relay bounds `transfer_buffer_length` per URB (32 MB): negative or oversize
  submits are rejected and logged as ERROR, so a single `0x7fffffff` announcement
  cannot reserve 2 GB per connection.
