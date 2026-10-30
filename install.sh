#!/usr/bin/env bash
# Tencent CSS traffic monitoring installer. Config and state are kept outside Git.
set -Eeuo pipefail
umask 027

REPO_URL="${REPO_URL:-https://github.com/userreksai/TencentLiveStreamingTrafficMonitoring.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="/usr/local/tencenttraffic"
CONFIG_DIR="/etc/tencent-traffic-monitor"
CONFIG_FILE="$CONFIG_DIR/config.json"
STATE_DIR="/var/lib/tencent-traffic-monitor"
SERVICE_NAME="tencent-traffic-monitor"
SERVICE_USER="tencent-traffic-monitor"
UNIT_FILE="/etc/systemd/system/$SERVICE_NAME.service"
PYTHON_BIN=""
ACTION="${1:---install}"

info() { printf '[INFO] %s\n' "$*"; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
trap 'printf "[ERROR] 操作失败（行 %s）。配置和状态文件不会被覆盖。\n" "$LINENO" >&2' ERR

usage() {
    cat <<'HELP'
用法：sudo bash install.sh [操作]

  --install   安装依赖、获取最新代码并创建服务（默认）；模板未配置时不启动
  --update    拉取 main 最新代码，检查通过后重启已经运行/启用的服务
  --start     校验配置，启动服务并设置开机自启
  --restart   校验配置后重启服务
  --stop      停止服务（保留配置、状态及开机自启设置）
  --status    查看服务状态
  --logs      持续查看服务日志，Ctrl+C 退出
  --check     仅校验配置，不联网、不发送通知
  --help      查看帮助

路径：
  代码 /usr/local/tencenttraffic
  配置 /etc/tencent-traffic-monitor/config.json
  状态 /var/lib/tencent-traffic-monitor

可通过 REPO_URL、REPO_BRANCH 环境变量选择仓库和分支。
更新不会覆盖配置、密钥、通知地址或历史状态；本地代码有改动时拒绝覆盖。
HELP
}

require_root() {
    [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "请使用 sudo bash install.sh $ACTION。"
}

require_systemd() {
    command -v systemctl >/dev/null 2>&1 || die "需要使用 systemd 的 Linux 系统。"
    [[ -d /run/systemd/system ]] || die "当前系统没有运行 systemd，请在实际服务器运行。"
}

choose_python() {
    local candidate
    for candidate in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PYTHON_BIN="$(command -v "$candidate")"
            return 0
        fi
    done
    return 1
}

install_dependencies() {
    if ! command -v git >/dev/null 2>&1 || ! choose_python; then
        info "安装 Git、Python 和 CA 证书。"
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update
            DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 python3-venv ca-certificates
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y git python3 ca-certificates
            if ! choose_python; then
                dnf install -y python3.11
            fi
        elif command -v yum >/dev/null 2>&1; then
            yum install -y git python3 ca-certificates
            if ! choose_python; then
                yum install -y python3.11
            fi
        else
            die "无法识别包管理器。请先安装 Git、Python 3.9+（含 venv）与 CA 证书。"
        fi
    fi
    choose_python || die "需要 Python 3.9 或更新版本，建议 Ubuntu 22.04+/Debian 11+/Rocky Linux 9+。"
    "$PYTHON_BIN" -c 'import ssl, venv' || die "Python 缺少 ssl 或 venv 标准库。"
    info "Python：$($PYTHON_BIN --version 2>&1)；Git：$(git --version)"
}

create_service_user() {
    if ! getent group "$SERVICE_USER" >/dev/null 2>&1; then
        groupadd --system "$SERVICE_USER"
    fi
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        local nologin
        nologin="$(command -v nologin || printf '/sbin/nologin')"
        useradd --system --gid "$SERVICE_USER" --home-dir "$STATE_DIR" --no-create-home --shell "$nologin" "$SERVICE_USER"
    fi
    install -d -m 0755 -o root -g root "$INSTALL_DIR"
    install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
}

normalize_remote() {
    local remote="$1"
    remote="${remote%/}"
    printf '%s' "${remote%.git}"
}

sync_checkout() {
    export GIT_TERMINAL_PROMPT=0
    if [[ -e "$INSTALL_DIR/.git" ]]; then
        local actual_root actual_remote current_branch ahead
        actual_root="$(git -C "$INSTALL_DIR" rev-parse --show-toplevel)"
        [[ "$(realpath "$actual_root")" == "$(realpath "$INSTALL_DIR")" ]] || die "代码目录不是预期的独立 Git 仓库。"
        actual_remote="$(git -C "$INSTALL_DIR" remote get-url origin)"
        [[ "$(normalize_remote "$actual_remote")" == "$(normalize_remote "$REPO_URL")" ]] || die "代码目录 origin 与指定仓库不一致，拒绝更新。"
        [[ -z "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=normal)" ]] || die "代码目录存在本地修改或未跟踪文件。请先备份并处理，再更新；配置应放在 /etc 中。"
        current_branch="$(git -C "$INSTALL_DIR" symbolic-ref --short HEAD)"
        [[ "$current_branch" == "$REPO_BRANCH" ]] || die "当前分支不是 $REPO_BRANCH，拒绝切换或覆盖。"
        info "获取最新 $REPO_BRANCH 代码。"
        git -C "$INSTALL_DIR" fetch --prune origin "$REPO_BRANCH"
        ahead="$(git -C "$INSTALL_DIR" rev-list --count "origin/$REPO_BRANCH..HEAD")"
        [[ "$ahead" -eq 0 ]] || die "代码目录包含尚未推送的提交，拒绝覆盖。"
        git -C "$INSTALL_DIR" merge --ff-only "origin/$REPO_BRANCH"
    else
        [[ -z "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || die "$INSTALL_DIR 已有非仓库文件，拒绝覆盖。"
        info "下载项目代码。"
        git clone --branch "$REPO_BRANCH" --single-branch -- "$REPO_URL" "$INSTALL_DIR"
    fi
    info "当前代码：$(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
}

create_environment() {
    if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
        info "创建独立 Python 环境（项目无第三方运行依赖）。"
        if ! "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"; then
            if command -v apt-get >/dev/null 2>&1; then
                apt-get update
                DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
                "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
            else
                die "无法创建 venv。请安装所选 Python 对应的 venv/ensurepip 包后重试。"
            fi
        fi
    fi
    "$INSTALL_DIR/.venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "现有虚拟环境 Python 版本太旧。"
    (
        cd "$INSTALL_DIR"
        export PYTHONDONTWRITEBYTECODE=1
        "$INSTALL_DIR/.venv/bin/python" -m compileall -q traffic_monitor
        if [[ -d tests ]]; then
            info "运行离线测试。"
            "$INSTALL_DIR/.venv/bin/python" -m unittest discover -s tests -p 'test*.py'
        fi
    )
    # umask=027 创建的 venv/代码不能让服务用户误失读权限。
    # root 保留所有权，服务组只获得读取/遍历权限；状态目录单独授权写入。
    chgrp -hR "$SERVICE_USER" "$INSTALL_DIR"
    chmod -R g+rX,g-w "$INSTALL_DIR"
    chmod 0755 "$INSTALL_DIR"
}

install_config() {
    if [[ ! -e "$CONFIG_FILE" ]]; then
        install -m 0640 -o root -g "$SERVICE_USER" "$INSTALL_DIR/config.example.json" "$CONFIG_FILE"
        info "已创建配置模板：$CONFIG_FILE"
    else
        info "保留已有配置：$CONFIG_FILE"
        chown root:"$SERVICE_USER" "$CONFIG_FILE"
        chmod 0640 "$CONFIG_FILE"
    fi
}

write_unit() {
    install -m 0644 -o root -g root "$INSTALL_DIR/deploy/$SERVICE_NAME.service" "$UNIT_FILE"
    systemctl daemon-reload
}

check_config() {
    [[ -x "$INSTALL_DIR/.venv/bin/python" ]] || die "尚未安装，请先执行 --install。"
    [[ -f "$CONFIG_FILE" ]] || die "缺少配置文件：$CONFIG_FILE"
    # 使用与 systemd 相同的账户、工作目录和环境，避免 root 可读但服务不可读。
    (
        cd "$INSTALL_DIR"
        runuser -u "$SERVICE_USER" -- env PYTHONDONTWRITEBYTECODE=1 \
            "$INSTALL_DIR/.venv/bin/python" -m traffic_monitor --config "$CONFIG_FILE" --check-config
    )
}

show_next_steps() {
    cat <<'NEXT'

请填写配置中的账号 SecretId/SecretKey、正常 Lark 地址和异常 Lark 地址，
并设置已核对的套餐余额基准。未设置基准时，只能展示“未校准”，不会伪报正常余量。

  sudo nano /etc/tencent-traffic-monitor/config.json
  sudo bash /usr/local/tencenttraffic/install.sh --check
  sudo bash /usr/local/tencenttraffic/install.sh --start

查看日志：
  sudo journalctl -u tencent-traffic-monitor -f

更新最新代码（保留配置与状态）：
  sudo bash /usr/local/tencenttraffic/install.sh --update
NEXT
}

case "$ACTION" in
    --help|-h) usage; exit 0 ;;
    --install|--update|--start|--restart|--stop|--status|--logs|--check) ;;
    *) usage; die "未知操作：$ACTION" ;;
esac
[[ "$#" -le 1 ]] || die "一次只能指定一个操作。"
require_root

case "$ACTION" in
    --logs) require_systemd; exec journalctl -u "$SERVICE_NAME" -f ;;
    --status) require_systemd; exec systemctl status "$SERVICE_NAME" --no-pager ;;
    --stop) require_systemd; systemctl stop "$SERVICE_NAME"; info "服务已停止。"; exit 0 ;;
    --check) check_config; exit 0 ;;
    --start|--restart)
        require_systemd
        check_config
        if [[ "$ACTION" == --start ]]; then
            systemctl enable --now "$SERVICE_NAME"
        else
            systemctl restart "$SERVICE_NAME"
        fi
        systemctl is-active --quiet "$SERVICE_NAME" || die "服务未成功启动，请查看 journalctl 日志。"
        info "服务已启动。使用 --logs 查看运行情况。"
        exit 0
        ;;
esac

require_systemd
install_dependencies
create_service_user
was_enabled=false
was_active=false
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then was_enabled=true; fi
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then was_active=true; fi
sync_checkout
create_environment
install_config
write_unit

if check_config; then
    if [[ "$was_enabled" == true || "$was_active" == true ]]; then
        systemctl restart "$SERVICE_NAME"
        systemctl is-active --quiet "$SERVICE_NAME" || die "重启失败，请查看 journalctl 日志。"
        info "更新完成，服务已重启。"
    else
        info "代码和环境已就绪。执行 --start 后启用服务。"
    fi
else
    printf '[NOTICE] 配置尚未通过检查，本次没有启动或重启服务。请按下列步骤填写。\n'
    show_next_steps
    if [[ "$ACTION" == --update ]]; then
        exit 1
    fi
    exit 0
fi
show_next_steps
