# usbip-host kernel patches

Local maintenance patches for the board kernel tree, not for mainline:

```
/home/alex/rk3588/linux-5.10-gen-rkr8/kernel
```

The relay in this repository runs in userspace and needs no kernel change. These
patches exist only because `usbip-host`, the in-kernel exporter the relay sits in
front of, cannot do what a 36 port server needs.

| patch | effect |
| --- | --- |
| `0001-usbip-host-raise-busid-limit.patch` | `MAX_BUSID` 16 -> 64 |
| `0002-usbip-host-reclaim-orphaned-busid-entries.patch` | give slots back, and make a bind after a half finished unbind work again |

Both apply to the 5.10.198 sources (tags `linux-5.10-gen-rkr7`,
`linux-5.10-gen-rkr7.1`) and to the 5.10.209 sources (`linux-5.10-gen-rkr8`,
current HEAD).

## Pick the matching tree before anything else

Measured in this SDK:

- `kernel/` HEAD is tag `linux-5.10-gen-rkr8`, and its `Makefile` says
  `SUBLEVEL = 209`.
- The shipped `drivers/usb/usbip/usbip-host.ko` has
  `vermagic: 5.10.198 SMP mod_unload aarch64`, i.e. it is a stale artifact from
  an earlier build of this same tree.
- The board runs 5.10.198, and `linux-5.10-gen-rkr7` / `rkr7.1` are the 5.10.198
  sources.

So a module built at HEAD is a 5.10.209 module and the board will reject it with
`invalid module format`. Build in a worktree of the 5.10.198 tag instead of
switching the SDK tree over; that is what the recipe below does. Moving the board
to rkr8 is the other option and a much bigger change.

## Host tools, without root

This host has no `bc`, `flex`, `bison`, `m4` and no `libssl-dev`, and no root
password was used. Everything below runs unprivileged: the packages are
downloaded and unpacked into `~/.bcroot/root`, and only the build process sees
them.

```sh
mkdir -p ~/.bcroot && cd ~/.bcroot
apt-get download bc flex bison m4 libssl-dev        # no root needed
for d in ./*.deb; do dpkg-deb -x "$d" root; done
rm -f root/usr/lib/x86_64-linux-gnu/libcrypto.a root/usr/lib/x86_64-linux-gnu/libssl.a
ln -sf /usr/lib/x86_64-linux-gnu/libcrypto.so.3 root/usr/lib/x86_64-linux-gnu/libcrypto.so
export PATH=~/.bcroot/root/usr/bin:$PATH
export BISON_PKGDATADIR=~/.bcroot/root/usr/share/bison
export M4=~/.bcroot/root/usr/bin/m4
```

`BISON_PKGDATADIR` and `M4` are needed because the binaries were unpacked under a
non-default prefix and both resolve their data files by absolute path.
`libssl-dev` ships the `libcrypto.so` symlink but not the library it points at,
so the symlink is re-pointed at the runtime `libcrypto.so.3`; otherwise the
linker silently falls back to the static `libcrypto.a` and then wants libzstd.

## 0001: the table is too small

`drivers/usb/usbip/stub_main.c` keeps the exportable busids in a fixed size
array:

```c
#define MAX_BUSID 16
static struct bus_id_priv busid_table[MAX_BUSID];
```

One instance per module. Every busid taken by `usbip bind` owns a slot until an
unbind hands it back, and when all 16 are taken `add_match_busid()` finds no free
entry, returns -1, and `match_busid_store()` turns that into `-ENOMEM` for the
sysfs write. The 17th concurrent export fails, which caps a 36 port server at 16
devices.

The number is bookkeeping only: nothing in USB/IP or in dwc3 imposes it,
`struct bus_id_priv` is a few dozen bytes, and every walk over the table is
short. 64 slots cost roughly 3 KiB of `.bss` and nothing at runtime.

64 is also the last comfortable value before `match_busid_show()` becomes a
problem: it `sprintf()`s into the page handed to sysfs, and 64 entries of at most
31 characters stay below 2 KiB, so anything much past ~120 entries would need
that function reworked first.

Upstream status, checked 2026-11-16: `torvalds/linux` master still carries
`#define MAX_BUSID 16` (only `strlcpy` -> `strscpy` changed since 5.10), and a
full `lore.kernel.org` search finds no patch that ever tried to raise it. The one
public report is a 2011 thread on the USB/IP project forum (`More than 16
device`), where the advice was to edit this same constant by hand. Do not expect
a kernel upgrade to fix it.

## 0002: slots are never given back

The status machine as 5.10 ships it (`stub.h`):

| status | value | meaning |
| --- | --- | --- |
| `STUB_BUSID_OTHER` | 0 | free, or left over and unowned |
| `STUB_BUSID_REMOV` | 1 | unbind requested while the device was claimed |
| `STUB_BUSID_ADDED` | 2 | authorized, device not claimed |
| `STUB_BUSID_ALLOC` | 3 | claimed by usbip-host (set by `stub_probe()`) |

- `bind`: `add_match_busid()` writes the name and sets ADDED; `stub_probe()`
  claims the device and sets ALLOC.
- `unbind`: `del_match_busid()` asked to drop an ALLOC entry sets REMOV and
  **keeps the name**, because `stub_disconnect()` looks that entry up later and
  `BUG()`s when it cannot find it.
- `disconnect`: `stub_disconnect()` turns ALLOC back into ADDED and clears
  nothing.

`unbind` in the stock userspace tool writes two things: `del` to `match_busid`,
then the busid to `rebind`. The second write is what drops the entry, because
`rebind_store()` calls `del_match_busid()` once `device_attach()` succeeded. So an
entry leaks when that second write never happens or fails - a bare `del`, which is
exactly what a udev helper does when a device goes away. Once leaked,
`add_match_busid()` returns success for the busid anyway, so the next
`usbip bind` of that device reports success while `stub_probe()` keeps rejecting
it: the device never comes back, the slot stays taken, and a board that plugs and
unplugs devices eventually turns every bind into either a silent no-op or
`-ENOMEM`. This is also why `archive/scripts/usbip-release-busid` exists; its
`del` could not free a REMOV entry either.

### Where the reclaim went (upstream history)

This is not a new complaint. The deletion used to live at the end of
`stub_disconnect()`, for every entry that was not ALLOC, and two 2018 commits by
Shuah Khan moved it and then dropped it:

| commit | what it did |
| --- | --- |
| `1e180f167d4e` *usbip: usbip_host: delete device from busid_table after rebind* | the original report and fix - "Device is left in the busid_table after unbind and rebind". Added `del_match_busid()` to `rebind_store()` after `device_attach()`. |
| `22d4a89efe86` *usbip: usbip_host: run rebind from exit when module is removed* | removed the older `else { status = OTHER; del_match_busid(); }` from `stub_disconnect()`, because the module exit rescan hands devices back to their drivers by looking their names up in that table. |

After those two, `rebind_store()` is the only reclaim point left in the kernel.
This patch extends the same idea to the two paths that are not covered - `del` and
`add` - instead of reintroducing the disconnect-side deletion that
`22d4a89efe86` removed on purpose.

Everything hangs on one invariant: `sdev`.

- `sdev == NULL` means no device is attached. `stub_disconnect()` clears `sdev`
  only at its very end, so with `sdev == NULL` no disconnect is running and none
  can start for that entry, which is what makes clearing the name safe.
- `sdev != NULL` means the device is claimed, so the name has to stay.

What changes:

- `del_match_busid()` clears the entry when status is OTHER (as before) **or
  `sdev == NULL`** - the new case covers a REMOV entry after its disconnect
  finished, and an ADDED entry that was never claimed. Otherwise it sets REMOV as
  before.
- `add_match_busid()` re-arms an already registered busid whose entry is
  orphaned (`sdev == NULL` and status OTHER or REMOV) to ADDED, so a bind after a
  half finished unbind exports the device instead of silently succeeding.

What it deliberately does not do: clear the name inside `stub_disconnect()`. That
is the tempting four line fix and it is wrong twice over - `stub_disconnect()`
begins with `BUG()` when the entry is missing, and module exit depends on names
surviving it (`stub_device_rebind()` rescans the table after
`usb_deregister_device_driver()` and hands devices back to their local drivers by
`name && shutdown_busid`).

Known limit: a `del` that lands on a **claimed** device (`sdev != NULL`) still
only marks it REMOV. Nothing on the kernel side can release a device that is
still held; that is what `usbip unbind` does by driving `rebind`, and what a
`rmmod`/`modprobe` of usbip-host guarantees. Treat `del` as "revoke when idle",
not as a substitute for unbind.

## Applying

From the kernel tree root, so the `a/drivers/...` prefixes line up:

```sh
cd /home/alex/rk3588/linux-5.10-gen-rkr8/kernel
for p in 0001-usbip-host-raise-busid-limit.patch \
         0002-usbip-host-reclaim-orphaned-busid-entries.patch; do
	git apply --check "/path/to/patches/kernel/$p" && git apply "/path/to/patches/kernel/$p"
done
git diff --stat drivers/usb/usbip
```

`git apply` is the intended tool; the files carry a plain commit message header
without mbox `From:`/`Date:` lines, so `git am` is not used here.

## Building the modules

`CONFIG_USBIP_CORE=m` and `CONFIG_USBIP_HOST=m`, so exactly two modules are
involved. `usbip-host` needs the symbols `usbip-core` exports, so build both in
one invocation, from a worktree of the 5.10.198 tag:

```sh
SDK=/home/alex/rk3588/linux-5.10-gen-rkr8
K=$SDK/kernel
T=/home/alex/k198
TC=$SDK/prebuilts/gcc/linux-x86/aarch64/gcc-arm-10.3-2021.07-x86_64-aarch64-none-linux-gnu/bin/aarch64-none-linux-gnu-
INC=$HOME/.bcroot/root/usr/include
LIB=$HOME/.bcroot/root/usr/lib/x86_64-linux-gnu

git -C $K worktree add $T linux-5.10-gen-rkr7.1
git -C $T apply /path/to/patches/kernel/0001-usbip-host-raise-busid-limit.patch
git -C $T apply /path/to/patches/kernel/0002-usbip-host-reclaim-orphaned-busid-entries.patch
cp $K/.config $T/.config
cd $T

make ARCH=arm64 CROSS_COMPILE=$TC CC=${TC}gcc olddefconfig
make -j8 ARCH=arm64 CROSS_COMPILE=$TC CC=${TC}gcc \
     "HOSTCFLAGS=-I$INC -I$INC/x86_64-linux-gnu" "KBUILD_HOSTLDFLAGS=-L$LIB" \
     modules_prepare
make -j8 ARCH=arm64 CROSS_COMPILE=$TC CC=${TC}gcc \
     "HOSTCFLAGS=-I$INC -I$INC/x86_64-linux-gnu" "KBUILD_HOSTLDFLAGS=-L$LIB" \
     KBUILD_MODPOST_WARN=1 \
     drivers/usb/usbip/usbip-core.ko drivers/usb/usbip/usbip-host.ko
```

What each of the odd flags is for:

- `CC=${TC}gcc` bypasses `scripts/gcc-wrapper.py`, which needs `python` (only
  `python3` exists here) and is wired in unconditionally by `Makefile:493`. The
  wrapper only filters warnings.
- `HOSTCFLAGS` / `KBUILD_HOSTLDFLAGS` point `scripts/extract-cert`
  (`CONFIG_SYSTEM_TRUSTED_KEYRING=y` builds it) at the unpacked OpenSSL headers
  and the runtime `libcrypto.so.3`.
- `KBUILD_MODPOST_WARN=1` is required because a `modules_prepare`-only tree has
  no `vmlinux.symvers`, which turns "unresolved symbol" into an error. It also
  means modpost no longer checks anything, so do the check below instead.
- `make drivers/usb/usbip/` silently builds nothing here. Name the two `.ko`
  targets explicitly.

## Checking the build before it goes anywhere

Two checks, both cheap, and the second one is what replaces modpost:

```sh
# 1. must say 5.10.198 SMP mod_unload aarch64
strings drivers/usb/usbip/usbip-host.ko | grep vermagic

# 2. the kernel symbols it needs must not change versus the previous module
nm -u old/usbip-host.ko | awk '{print $2}' | sort > /tmp/old.txt
nm -u drivers/usb/usbip/usbip-host.ko | awk '{print $2}' | sort > /tmp/new.txt
diff /tmp/old.txt /tmp/new.txt && echo SAME_KERNEL_DEPENDENCIES
```

Measured for the build in this repository: 36 undefined symbols for `usbip-core`
and 85 for `usbip-host`, zero differences in either direction against the
previous working pair. That is enough to say the new modules need exactly the
same kernel exports as the ones the board already loads.

Do not check this against `/proc/kallsyms`: without `CONFIG_KALLSYMS_ALL` it only
lists text symbols, so data exports such as `kmalloc_caches`, `cpu_hwcap_keys` or
`usbip_debug_flag` look missing when they are perfectly fine.

## Distributing to the board

The staging directory on the board is `/userdata/software/linux/external`, and it
holds the pair the running system uses, so keep the previous pair around:

```sh
ssh teamhd@192.168.2.111 'cd /userdata/software/linux/external &&
	for f in usbip-core.ko usbip-host.ko; do [ -f $f.prev ] || cp -p $f $f.prev; done'
scp drivers/usb/usbip/usbip-core.ko drivers/usb/usbip/usbip-host.ko \
	teamhd@192.168.2.111:/userdata/software/linux/external/
sha256sum drivers/usb/usbip/*.ko
ssh teamhd@192.168.2.111 'cd /userdata/software/linux/external && sha256sum *.ko'
```

Copying is not loading. `usbipd` holds the old modules, so the swap is a separate
step, done when no session is live:

```sh
ssh teamhd@192.168.2.111 'cp /userdata/software/linux/external/usbip-*.ko /lib/modules/$(uname -r)/kernel/drivers/usb/usbip/ &&
	depmod -a && rmmod usbip_host usbip_core && modprobe usbip_core usbip_host'
```

## Deploying and verifying

```sh
modinfo usbip-host.ko | grep vermagic    # must equal uname -r on the board
```

Verify 0001:

1. `cat /sys/bus/usb/drivers/usbip-host/match_busid` lists up to 64 names
   instead of at most 16.
2. Bind a 17th device. Before the patch that write is what returns `-ENOMEM`;
   after it, the device shows up as `usbip-host` in `usbip list -l`.
3. `usbip --tcp-port 3240 list -r <board-ip>` from the client sees all 36.

Verify 0002, in order of how little they disturb the board:

1. Idle revoke. `echo "add 9-9" > .../match_busid` on a busid that does not
   exist, then `echo "del 9-9" > .../match_busid`, then `cat .../match_busid`.
   Before the patch the name stays listed; after it, it is gone.
2. Bind after a stale entry. Repeat step 1 but skip the `del`, then
   `usbip bind -b <a real busid>` and confirm the client can attach. On the
   unpatched kernel a bind over a REMOV entry prints success and nothing appears.
3. Loop: bind a real device, attach from the client, detach, `usbip unbind`,
   repeat 20 times, and watch the number of names in `match_busid` stay flat
   instead of growing by one per cycle.
4. Unplug/replug the same port a few times with udev running
   `archive/scripts/usbip-release-busid`; the entry count must not drift upward.

## Rolling back

On the board, the pair saved before the last distribution is the fastest way
back, no rebuild involved:

```sh
ssh teamhd@192.168.2.111 'cd /userdata/software/linux/external &&
	cp -p usbip-core.ko.prev usbip-core.ko && cp -p usbip-host.ko.prev usbip-host.ko &&
	cp usbip-*.ko /lib/modules/$(uname -r)/kernel/drivers/usb/usbip/ &&
	depmod -a && rmmod usbip_host usbip_core && modprobe usbip_core usbip_host'
```

In the sources, drop the patches in reverse order:

```sh
cd /home/alex/rk3588/linux-5.10-gen-rkr8/kernel
git apply -R patches/kernel/0002-usbip-host-reclaim-orphaned-busid-entries.patch
git apply -R patches/kernel/0001-usbip-host-raise-busid-limit.patch
```

## Rebasing

Both patches are small, but both are the kind of change a SDK bump will touch.
Check every time:

```sh
git apply --check patches/kernel/0001-usbip-host-raise-busid-limit.patch
git apply --check patches/kernel/0002-usbip-host-reclaim-orphaned-busid-entries.patch
```

If a check fails, re-create rather than hand-edit: apply the change by hand, run
`git diff -- drivers/usb/usbip/stub_main.c`, restore with
`git checkout -- drivers/usb/usbip/stub_main.c`, and re-verify with
`git apply --check`.
