#!/usr/bin/env bash
#
# 家庭盒子 - 开机自启管理
#
#   sudo ./setup_autostart.sh install     启用开机自启并立即启动
#   sudo ./setup_autostart.sh uninstall   停止并取消自启
#   ./setup_autostart.sh status           查看状态
#
# 前提：先执行过 sudo ./install.sh
# 只装了其中一个服务也可以，脚本会自动跳过未安装的。
set -euo pipefail

ALL_SERVICES=(21voc camera)
UNIT_DIR=/etc/systemd/system

info() { echo "==> $*"; }

usage() {
    echo "用法: sudo $0 [install|uninstall|status]"
    echo "  install    启用开机自启并立即启动（默认）"
    echo "  uninstall  停止并取消开机自启"
    echo "  status     查看状态（无需 root）"
}

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "请用 root 运行：sudo $0 $1" >&2
        exit 1
    fi
}

FOUND=()
installed_services() {
    local s
    FOUND=()
    for s in "${ALL_SERVICES[@]}"; do
        [ -f "$UNIT_DIR/$s.service" ] && FOUND+=("$s")
    done
    if [ "${#FOUND[@]}" -eq 0 ]; then
        echo "没有找到任何 service（${ALL_SERVICES[*]}）" >&2
        echo "请先执行 sudo ./install.sh" >&2
        return 1
    fi
    return 0
}

# 服务刚起来时 systemctl is-active 可能还是 activating，重试几次
wait_active() {
    local s="$1" i
    for i in $(seq 1 15); do
        systemctl is-active --quiet "$s" && return 0
        systemctl is-failed --quiet "$s" && return 1
        sleep 1
    done
    systemctl is-active --quiet "$s"
}

do_install() {
    installed_services || return 1

    local s
    for s in "${ALL_SERVICES[@]}"; do
        [ -f "$UNIT_DIR/$s.service" ] || echo "  跳过 $s（未安装）"
    done

    systemctl daemon-reload
    local failed=0
    for s in "${FOUND[@]}"; do
        info "$s"
        systemctl enable "$s" >/dev/null
        systemctl restart "$s" || true
        if wait_active "$s"; then
            echo "  已启用并运行"
        else
            echo "  启动失败，最近日志："
            journalctl -u "$s" -n 20 --no-pager || true
            failed=1
        fi
    done
    return "$failed"
}

do_uninstall() {
    installed_services || return 1
    local s
    for s in "${FOUND[@]}"; do
        info "$s"
        systemctl stop "$s" 2>/dev/null || true
        systemctl disable "$s" 2>/dev/null || true
        systemctl reset-failed "$s" 2>/dev/null || true
        echo "  已停止并取消自启"
    done
    echo ""
    echo "service 文件仍保留在 $UNIT_DIR，重新启用请执行 install"
}

do_status() {
    local s
    for s in "${ALL_SERVICES[@]}"; do
        info "$s"
        if [ ! -f "$UNIT_DIR/$s.service" ]; then
            echo "  未安装"
            echo ""
            continue
        fi
        if systemctl is-enabled --quiet "$s" 2>/dev/null; then
            echo "  开机自启: 是"
        else
            echo "  开机自启: 否"
        fi
        if systemctl is-active --quiet "$s" 2>/dev/null; then
            echo "  运行状态: 运行中"
        else
            echo "  运行状态: 已停止"
        fi
        echo ""
    done
    echo "日志: journalctl -u 21voc -f   /   journalctl -u camera -f"
    echo "推流: http://<板子IP>:8080/video"
}

# main
ACTION="${1:-install}"

case "$ACTION" in
    install|uninstall|status) ;;
    -h|--help|help) usage; exit 0 ;;
    *)
        echo "未知操作: $ACTION" >&2
        usage >&2
        exit 1
        ;;
esac

case "$ACTION" in
    install)
        need_root "$ACTION"
        do_install
        ;;
    uninstall)
        need_root "$ACTION"
        do_uninstall
        ;;
    status)
        do_status
        ;;
esac
