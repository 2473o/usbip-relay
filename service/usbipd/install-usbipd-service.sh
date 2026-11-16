#!/bin/bash
# Install and enable the usbipd systemd unit on the target board.
#
# Usage (on the board):
#   sudo bash install-usbipd-service.sh
#   sudo USBIPD_BIN=/xxx/usbipd bash install-usbipd-service.sh
#
# Uninstall:
#   sudo systemctl disable --now usbipd
#   sudo rm -f /etc/systemd/system/usbipd.service
#   sudo systemctl daemon-reload
#   sudo userdel usbip && sudo groupdel usbip
#
# Creates the system user/group "usbip" (no home directory, no login shell)
# and runs the daemon under that identity instead of root.
set -euo pipefail

fail() {
    echo "错误：$*" >&2
    exit 1
}

escape_sed_replacement() {
    printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

# --- Configuration ---
DEFAULT_BIN=/userdata/software/linux/tools/usb/usbipd
BIN="${USBIPD_BIN:-$DEFAULT_BIN}"
UNIT_NAME=usbipd.service
UNIT_SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)/$UNIT_NAME"
UNIT_DST="/etc/systemd/system/$UNIT_NAME"
SVC_USER=usbip
SVC_GROUP=usbip

# --- Preconditions ---
if [ "$(id -u)" -ne 0 ]; then
    fail "需要 root 权限，请用 sudo 执行"
fi
if [ ! -f "$UNIT_SRC" ]; then
    fail "找不到单元文件 $UNIT_SRC"
fi
if [ ! -x "$BIN" ]; then
    fail "$BIN 不存在或不可执行（chmod +x $BIN）"
fi
if ! command -v systemctl >/dev/null; then
    fail "本机没有 systemd/systemctl"
fi

# Read the port from ExecStart so the hints below always match the unit.
PORT="$(sed -n 's/^ExecStart=.*--tcp-port *\([0-9]*\).*/\1/p' "$UNIT_SRC")"
PORT="${PORT:-3240}"

# --- Service account ---
# Prefer nologin; fall back to /bin/false when it is unavailable.
LOGIN_SHELL=/bin/false
for candidate in /usr/sbin/nologin /sbin/nologin; do
    if [ -x "$candidate" ]; then
        LOGIN_SHELL=$candidate
        break
    fi
done

# 1) Idempotent creation of the dedicated system group and user.
if ! getent group "$SVC_GROUP" >/dev/null; then
    groupadd --system "$SVC_GROUP"
fi
if ! getent passwd "$SVC_USER" >/dev/null; then
    useradd --system \
            --gid "$SVC_GROUP" \
            --no-create-home \
            --shell "$LOGIN_SHELL" \
            "$SVC_USER"
fi

# 2) Verify user attributes (system UID, primary group, no home, no login shell).
PASSWD_LINE="$(getent passwd "$SVC_USER")"
IFS=: read -r _ _ USER_UID USER_GID _ USER_HOME USER_SHELL _ <<< "$PASSWD_LINE"
GROUP_GID="$(getent group "$SVC_GROUP" | cut -d: -f3)"
echo "校验 $SVC_USER: UID=$USER_UID GID=$USER_GID 组GID=$GROUP_GID"
echo "         HOME=$USER_HOME SHELL=$USER_SHELL"

if [ "$USER_UID" -ge 1000 ]; then
    fail "UID=$USER_UID 不是系统用户 UID（应小于 1000）"
fi
if [ "$USER_GID" != "$GROUP_GID" ]; then
    fail "主组 GID=$USER_GID 与 $SVC_GROUP($GROUP_GID) 不一致"
fi
case "$USER_SHELL" in
    */nologin|*/false) ;;
    *) fail "登录 shell 为 $USER_SHELL，未禁止交互登录" ;;
esac
if [ -d "$USER_HOME" ]; then
    fail "家目录 $USER_HOME 存在，专用用户不应有可写家目录"
fi

# --- Unit file ---
install -m 0644 "$UNIT_SRC" "$UNIT_DST"

# 3) Rewrite only the binary path; keep the rest of ExecStart (e.g. --tcp-port) intact.
BIN_ESC="$(escape_sed_replacement "$BIN")"
sed -i \
    -e "s|^ConditionPathExists=.*|ConditionPathExists=$BIN_ESC|" \
    -e "s|^ExecStart=[^ ]*|ExecStart=$BIN_ESC|" \
    "$UNIT_DST"

# 4) The shipped unit already pins User=/Group=; refuse to start if it was altered.
if ! grep -q "^User=$SVC_USER$" "$UNIT_DST"; then
    fail "单元文件未以 $SVC_USER 运行，请检查 $UNIT_SRC"
fi
if ! grep -q "^Group=$SVC_GROUP$" "$UNIT_DST"; then
    fail "单元文件未设置 Group=$SVC_GROUP，请检查 $UNIT_SRC"
fi

# --- Start ---
systemctl daemon-reload
systemctl enable "$UNIT_NAME"
systemctl restart "$UNIT_NAME"
systemctl --no-pager --full status "$UNIT_NAME" || true

# 5) Confirm the main process does not run as root.
MAIN_PID="$(systemctl show -p MainPID --value "$UNIT_NAME" 2>/dev/null || echo 0)"
if [ -n "$MAIN_PID" ] && [ "$MAIN_PID" != "0" ]; then
    RUN_USER="$(ps -o user= -p "$MAIN_PID" 2>/dev/null | tr -d ' ' || true)"
    echo "主进程 PID=$MAIN_PID 运行用户=${RUN_USER:-未知}"

    if [ "$RUN_USER" = "root" ]; then
        fail "服务仍以 root 运行，权限最小化失败"
    fi
    if [ "$RUN_USER" != "$SVC_USER" ]; then
        echo "警告：运行用户为 ${RUN_USER:-未知}，预期 $SVC_USER" >&2
    fi
else
    echo "警告：未取到主进程 PID，服务可能未成功启动，请查看日志" >&2
fi

echo
echo "已安装并启动 $UNIT_NAME（运行用户: $SVC_USER；端口: $PORT）"
echo "  查看日志:   journalctl -u $UNIT_NAME -f"
echo "  监听端口:   ss -lntp | grep $PORT"
echo "  导出设备:   sudo usbip list -l && sudo usbip bind -b <busid>"
echo "  客户端接入: usbip --tcp-port $PORT attach -r <板子IP> -b <busid>"
