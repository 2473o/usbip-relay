# usbipd systemd 服务

在 RK3588 板端把 `usbipd` 做成开机自启的守护进程，二进制放在
`/userdata/software/linux/tools/usb/usbipd`（设备重启后仍保留）。

## 文件

- `usbipd.service`：systemd 单元（前台运行，仅监听 IPv4 的 TCP 3241，上游默认端口 3240；日志进 journal）
- `install-usbipd-service.sh`：板端一键安装脚本

## 前置条件（板端）

1. 内核已按 `rk3588_d3588.config` 打开 `CONFIG_USBIP_CORE=m` / `CONFIG_USBIP_HOST=m`，
   且模块已装到 `/lib/modules/$(uname -r)/kernel/drivers/usb/usbip/`：

   ```bash
   ls /lib/modules/$(uname -r)/kernel/drivers/usb/usbip/usbip-{core,host}.ko
   depmod -a        # 首次拷入 .ko 后执行
   ```

2. 二进制可执行：

   ```bash
   ls -l /userdata/software/linux/tools/usb/usbipd
   chmod +x /userdata/software/linux/tools/usb/usbipd
   ```

3. 板端运行的是 systemd 发行版。

## 安装

把本目录两个文件拷到板子（示例）：

```bash
scp usbipd.service install-usbipd-service.sh root@<板子IP>:/tmp/
```

然后在板子上执行：

```bash
sudo bash install-usbipd-service.sh
# 二进制不在默认路径时：
sudo USBIPD_BIN=/your/path/usbipd bash install-usbipd-service.sh
```

脚本会：创建系统组/用户 `usbip`（无家目录、禁用登录 shell，已存在则跳过）、复制单元到
`/etc/systemd/system/`（保留 `ExecStart` 参数，校验 `User=usbip` / `Group=usbip`）、
`daemon-reload`、`enable`、`restart`，最后打印状态并校验主进程运行用户不是 `root`。
自定义 `USBIPD_BIN` 时只替换二进制路径，`--tcp-port 3241 -4` 等参数不会被丢掉。

## 验证

```bash
systemctl status usbipd            # active (running)
ps -o user= -p $(systemctl show -p MainPID --value usbipd)   # 应为 usbip，不是 root
journalctl -u usbipd -b            # 启动日志
ss -lntp | grep 3241               # 端口已监听
lsmod | grep usbip                 # usbip_core / usbip_host 已加载
usbip list -l                      # 列出可导出设备
sudo usbip bind -b 1-1.2           # 绑定要导出的设备（busid 以 list 为准）
```

客户端必须显式指定端口；`usbip` 的 `--tcp-port` 是全局选项，要放在子命令之前：

```bash
usbip --tcp-port 3241 list -r <板子IP>
usbip --tcp-port 3241 attach -r <板子IP> -b 1-1.2
```

## 常见问题

- **端口被占用 / 启动后立即退出**
  上游 `usbipd` 使用 `SO_REUSEADDR`，也不会检查"另一个实例"；只有传了 `-P` / `--pid`
  才会写 `/var/run/usbipd.pid`。先 `ps -ef | grep usbipd` 找到残留的手工进程
  （`usbipd -D`）并 `kill`，再 `sudo systemctl restart usbipd`；冲突信息看 `journalctl -u usbipd -b`。
- **`usbip bind` 报找不到 stub 驱动**
  `usbip-host` 没加载：`modprobe usbip-host`，并确认 `disable_stub` 之类参数未被关闭。
- **切成 usbip 用户后导出设备失败 / permission denied**
  上游 `usbipd` 收到 import 请求时会把 socket fd 写进 sysfs 属性
  `/sys/bus/usb/devices/<busid>/usbip_sockfd`（内核 `DEVICE_ATTR_WO`，`root:root 0200`），
  非 root 进程写不了；`usbipd` 启动时也会提示 `not running as root?`。
  udev 规则（如 `SUBSYSTEM=="usb", GROUP="usbip", MODE="0664"`）只能放开 `/dev/bus/usb/*/*`，
  改不了这个 sysfs 属性。可选方案：给单元加 `AmbientCapabilities=CAP_DAC_OVERRIDE` 并收紧
  `CapabilityBoundingSet`（需 systemd ≥ 229），或把单元改回 `User=root`。
  另外 `usbip bind -b <busid>` 本身也需要 root 执行。
- **IPv4 / IPv6**：单元已带 `-4`（`--ipv4`），只监听 IPv4；上游默认同时监听 IPv4/IPv6，
  去掉 `-4` 即恢复双栈。改端口用 `--tcp-port PORT`（`-tPORT`）。
- **不需要开机自启**：`systemctl disable usbipd`。

## 卸载

```bash
sudo systemctl disable --now usbipd
sudo rm -f /etc/systemd/system/usbipd.service
sudo systemctl daemon-reload
sudo userdel usbip && sudo groupdel usbip   # 专用系统用户/组，按需清理
```

## 随镜像一起打包（可选）

若希望刷机即带该服务，可新建 overlay 目录
`device/rockchip/common/overlays/rootfs/usbipd/`，按 rootfs 路径摆放文件
（`post-overlay.sh` 无 `install.sh` 时会原样 rsync 到根文件系统）：

```
usbipd/etc/systemd/system/usbipd.service
usbipd/etc/systemd/system/multi-user.target.wants/usbipd.service -> ../usbipd.service
```

注意 `/userdata` 不随 rootfs 打包，二进制仍需单独放到板子的
`/userdata/software/linux/tools/usb/usbipd`。
