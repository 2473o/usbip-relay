# USB/IP 工具链

> 自动导出、僵死导出回收、传输分片、短包修复

板端(RK3588, kernel 5.10.198, Debian 11 Linaro)作为 USB/IP 服务器，把本地 USB 设备经 TCP 导出给 Windows 客户端使用。
本仓库的中继把 usbipd 挡在身后，在不改客户端、不改内核的前提下，修掉了两个会让链路直接不可用的问题：

- 大块 bulk 传输在 swiotlb 上映射失败，一次 512 KB 的 URB 就能把整个端口打挂；
- usbip-win2 把控制管道上合法的短包当成错误([#203](https://github.com/vadimgrn/usbip-win2/issues/203))，设备能枚举，但每次控制 IN 读都可能 `-EREMOTEIO`。

## 架构

一个进程、一个监听端口。每个组件都是一次流变换，所以整条链不需要为每个组件单独开端口：

```
usbip 客户端 --TCP--> 监听端口 3240
                        |
                        +-- split      (超过 max_transfer 的 bulk URB 拆成 N 个子请求)
                        +-- shortfix   (清掉控制 IN 上的 URB_SHORT_NOT_OK)
                        |
                        +--TCP--> usbipd 端口 3241
```

跑哪些阶段在 `config.toml` 里声明：`[server] stages` 是唯一的开关，只负责决定成员；每个名字都要有对应的 `[server.<name>]` 表(`note` 加该阶段自己的旋钮)。
一个阶段就是 `usbip_modules/` 下任意一个暴露了 `create(section)` 和 `handle(inbound, upstream, ctx)` 的模块——增减阶段是改配置，不是改代码。
表留在配置里但没写进 `stages`，该阶段处于停用状态：旋钮保留，永不运行。

运行顺序**不在任何地方声明**，它由每个阶段实现的协议规则推出，取决于中继的哪一端会消费它的改写：

- `split` 自己生成 USB/IP seqnum(把一个 `USBIP_CMD_SUBMIT` 变成 N 个)，而 URB 头偏移 4 处的 seqnum 是客户端的关联令牌，在 `USBIP_RET_SUBMIT` 里被回带。这张表只有客户端持有，因此该阶段锚定客户端：它排在最前。
- `shortfix` 清除控制提交上的 `URB_SHORT_NOT_OK`。USB 2.0 **8.5.3.2(变长数据阶段)** 规定控制管道的数据阶段"以一个短于该管道 MaxPacketSize 的包返回"结束；当数据长度正好是 `wMaxPacketSize` 的整数倍时，则用一个零长度包结束。也就是说，这里的短包是规范规定的阶段结束标记，不是错误。这个标志位是在 URB 完成时由主机控制器消费的，因此该阶段锚定 usbipd：它排在最后。

每个模块自己声明 `ANCHOR`(`client` / `usbipd`)，链按锚点顺序组装，所以配置无法重排流水线。

## 运行

```bash
uv run usbip --help   # 打印声明出的链和每个阶段的 note(来自 config.toml)
uv run usbip          # 运行声明出的链
uv run pytest         # 链级测试(纯内存，不开 socket)
```

启动后会打印一行实际生效的链，例如：

```
usbip 0.1.0: 0.0.0.0:3240 -> split -> shortfix -> 127.0.0.1:3241
```

上游地址固定为 `127.0.0.1`(usbipd 总是和本中继跑在同一块板子上)，只有端口可配置。

## 两个阶段

### split：大块 bulk 传输分片

板子的 dwc3 控制器没有 `dma-coherent` 属性，每次 DMA 映射都要走 swiotlb，而 swiotlb 一次最多只能映射 `IO_TLB_SEGSIZE`(128) × 2 KB。一次 512 KB 的 URB 会返回 `-EAGAIN`，并把整个端口拖死。因此：

- 传输缓冲区长度超过 `max_transfer` 的 bulk 提交被切成若干顺序子请求，每片新分配一个 seqnum；
- 分片是串行的：发一片、等它的回包，再构造下一片，所以 usbipd 看到的顺序与客户端本会产生的顺序一致，载荷也保持传输序；
- 所有分片的回包被折叠成一个 `RET_SUBMIT` 返回给客户端——状态取第一个失败值(某片超时则为 `-ETIMEDOUT`，即 -110)，`actual_length` 取各片之和，其余字段归零。客户端读回的内容看不出 URB 曾被拆分；
- IN 方向收到短包(`actual < 本片长度`)即提前结束，不再发后续分片。

**不分片的情况**：`ep == 0`(控制端点，控制传输不能拆)、`npkt > 0`(等时传输；usbip-win2 对一切非等时 URB 发的是 `-1` 而不是 `0`)、长度未超过 `max_transfer`。长度超限但又不满足分片条件时记一条 WARN 并原样转发。

**seqnum 空间**：合成的编号从 `0x40000000` 起，跨度 `0x10000000`，远高于客户端自用的编号段，单条连接内不会冲突。两类编号都进表——客户端编号只记录方向(在读载荷长度前就要知道方向)，合成编号还额外挂一个信箱，其回包在此消费、不会到达客户端。

### shortfix：控制管道短包修复(usbip-win2 #203 的临时规避)

[usbip-win2 #203](https://github.com/vadimgrn/usbip-win2/issues/203)：客户端把控制管道上合法的短数据阶段当成了错误。`to_linux_flags()`(`drivers/libdrv/usbd_helper.cpp`)只判 `dir_in`、不判端点类型，而 `hidclass` 对 `GET_REPORT` 不设 `USBD_SHORT_TRANSFER_OK`，于是每次控制 IN 读都带着 `URB_SHORT_NOT_OK` 上线，Linux 以 `-EREMOTEIO` 完成，HID 层拿到错误 31——设备能枚举，厂商应用却认为它不存在。

按 USB 2.0 §8.5.3.2，控制管道用短包(长度为 `wMaxPacketSize` 整数倍时用 ZLP)标记数据阶段结束；"短包即错误"是 bulk / interrupt 的语义，套到控制管道上等于把正常回包判成失败。

本阶段只在 `direction == IN && ep == 0` 的提交上清掉 `transfer_flags`(偏移 20)的 `0x0001` 位，其余字节原样转发。它不改变请求数量，所以对前面 `split` 的关联完全透明。

这是对 usbip-win2 #203 的临时规避，正确修法是在客户端按端点类型跳过 `URB_SHORT_NOT_OK`(`drivers/libdrv/usbd_helper.cpp`、`drivers/ude/proto.cpp`)。等客户端修好后，撤销这个阶段。

## 配置

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `[server] host` | `0.0.0.0` | 中继监听地址 |
| `[server] host_port` | `3240` | 中继监听端口，客户端连这里 |
| `[server] listen_port` | `3241` | usbipd 的端口，中继往这里拨 |
| `[server] logs` | — | 日志目录 |
| `[server] stages` | — | 链成员列表，非空数组，不可重名 |

`split` 的旋钮：

| 键 | 说明 |
| --- | --- |
| `max_transfer` | 单片上限，默认 128 KB，必须留在 swiotlb 的 `IO_TLB_SEGSIZE × 2 KB` 天花板之下 |
| `timeout` | 等待单个分片回包的秒数，默认 `10.0` |
| `hex_dump` | 每条连接转储多少个提交/回包头 |
| `log_level` | 日志级别 |
| `log_prefix` | 日志文件名前缀 |

`shortfix` 的旋钮：`verbose`(`true` 时级别为 DEBUG，并打印 setup 包字段)、`log_prefix`。

配置从包所在的任意上层目录里的 `config.toml` 读取，加载是显式的——导入包不会碰磁盘，配置错误从 `main()` 里报出。模块不读环境变量：新增键先写进 `config.toml` / `config.toml.example`，再在 `usbip_core/config.py` 里以类型化字段暴露。

## 日志

每个模块写到 `<logs>/<log_prefix><YYYY-MM-DD>.log`，行格式：

```
2026-11-16T12:48:43+0800 | INFO  | usbip-split | pid=1234 | split ep=1 len=524288 -> 4 chunks, total=524288 status=0
```

文件名在每次写入时解析，所以长时间运行的中继跨零点会自动换文件，不需要重启。写入串行化且从不抛异常——磁盘满或只读不能把中继带下来。

## 部署(板端)

```
客户端 --(3240)--> 本中继 --(3241)--> usbipd --usbip-host--> 设备
```

`service/usbipd/` 里有 usbipd 的 systemd 单元和一键安装脚本：单元以前台方式只监听 IPv4 的 TCP 3241，以专用系统用户 `usbip` 运行，靠 `CAP_DAC_OVERRIDE` 写 `/sys/bus/usb/devices/<busid>/usbip_sockfd`。

客户端必须显式指定端口，且 `--tcp-port` 是全局选项，要放在子命令之前：

```bash
usbip --tcp-port 3240 list   -r <板子IP>
usbip --tcp-port 3240 attach -r <板子IP> -b 1-1.2
```

细节与排错见 `service/usbipd/README.md`。

## 目录结构

```
src/
├── usbip/
│   ├── __init__.py      # 按 config.toml 声明的链做阶段查找、按锚点排序
│   └── __main__.py      # CLI + 监听
├── usbip_core/
│   ├── pipeline.py      # 基于内存双工对的阶段链
│   ├── stream.py        # Duplex 契约、pump、recv_exact、TCP 调优
│   ├── protocol.py      # 传输长度防护
│   ├── config.py
│   └── log.py
└── usbip_modules/
    ├── split/           # 阶段：bulk 分片
    │   ├── __init__.py  # 阶段契约：ANCHOR、create、handle
    │   ├── wire.py      # 头部布局与 seqnum 表
    │   ├── handshake.py # OP_REQ_IMPORT 及其描述符
    │   └── relay.py     # 分片逻辑与两个转发循环
    ├── shortfix.py      # 阶段：URB_SHORT_NOT_OK 垫片
    └── auto_bind.py     # 不是流阶段
tests/
└── test_chain.py
```

## 新增一个阶段

1. 在 `usbip_modules/` 下建模块，暴露三样东西：
   - `ANCHOR`：`"client"` 或 `"usbipd"`，写清楚**谁消费你的改写**；
   - `create(section)`：从 `[server.<name>]` 表读出旋钮，返回连接间共享的上下文；用 `section.need_int / need_float / need_str / need_bool` 取值，缺键即报配置错误；
   - `handle(inbound, upstream, ctx)`：一次连接的完整处理。`inbound` 是 `Duplex`(只有 `receive / send / send_eof / aclose`)，`upstream` 是个回调，**只能调用一次**，只有链尾才会真的建 TCP 连接。
2. 在 `config.toml` 的 `stages` 里加上名字，并补一张 `[server.<name>]` 表(至少 `note` 和 `log_prefix`)。

锚点的选法是查规范：找到消费这次改写的实体，阶段就贴着它放。改动请求数量的阶段(像 `split`)必须在 seqnum 归属方那一端，且它后面不能再有第二个做同样事的阶段，否则关联关系会分裂到两个所有者手上。

## 现状与遗留

- `src/usbip_modules/auto_bind.py` 目前是空文件；udev 自动导出、僵死导出回收(`usbip-autobind`、`usbip-recover` 等)在 `archive/` 下，尚未并入当前这条链。
- `shortfix` 是对 usbip-win2 #203 的临时规避，不是长期设计；客户端修复后撤销这个阶段。
- 中继对每个 URB 的 `transfer_buffer_length` 有上限保护(32 MB)，负数或超限的提交被拒绝并记 ERROR，避免单个 `0x7fffffff` 宣告为每条连接预留 2 GB。
