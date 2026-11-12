#!/usr/bin/env bash
# 让 dockerd 的 registry 流量走 gh-accel 本地代理。
#
#   docker-proxy.sh status     查看当前状态
#   docker-proxy.sh enable     启用（需 root，会重启 dockerd）
#   docker-proxy.sh disable    停用（需 root，会重启 dockerd）
#
# 为什么默认不启用:
#   * Docker Hub 已有 daemon.json 的 registry-mirrors，更直接
#   * ghcr.io 等 registry 直连通常可用，国内镜像更快
#   * 启用后代理成为 dockerd 的单点故障 —— 代理挂了 Docker 全挂
#   * 重启 dockerd 会中断正在运行的容器
#
# 权衡详见 README 的「Docker 加速」章节。

set -euo pipefail

DROPIN_DIR="/etc/systemd/system/docker.service.d"
DROPIN="${DROPIN_DIR}/gh-accel.conf"
ROOT="${GH_ACCEL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PROXY="${GH_ACCEL_URL:-http://127.0.0.1:19770}"
CA_BUNDLE="${GH_ACCEL_CA:-$HOME/.config/gh-accel/certs/ca-bundle.pem}"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m警告:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "需要 root（会修改 /etc/systemd/system 并重启 docker）"
}

check_gh_accel() {
  command -v systemctl >/dev/null || die "找不到 systemctl"
  if ! systemctl is-active --quiet gh-accel.service; then
    die "gh-accel 服务未运行，先启动它: systemctl --user start gh-accel.service"
  fi
  if [ ! -f "$CA_BUNDLE" ]; then
    die "找不到 CA bundle: ${CA_BUNDLE}
      先执行 'gh-accel install' 生成，或手动创建：
      cat /etc/ssl/certs/ca-certificates.crt ${HOME}/.config/gh-accel/certs/ca.pem > ${CA_BUNDLE}"
  fi
}

show_status() {
  if [ -f "$DROPIN" ]; then
    echo "dockerd 代理: 已启用"
    echo "  drop-in: $DROPIN"
    grep -E "^Environment" "$DROPIN" | sed 's/^/    /'
    echo "  生效需重启 dockerd: systemctl restart docker"
  else
    echo "dockerd 代理: 未启用"
    echo "  启用: docker-proxy.sh enable"
  fi
  echo ""
  echo "dockerd 自身环境:"
  systemctl show docker --property=Environment 2>/dev/null | sed 's/^/  /'
}

enable() {
  need_root
  check_gh_accel
  say "安装 systemd drop-in"
  mkdir -p "$DROPIN_DIR"
  sed -e "s|%h|${HOME}|g" -e "s|${PROXY}|${PROXY}|g" \
    -e "s|${CA_BUNDLE}|${CA_BUNDLE}|g" \
    "${ROOT}/systemd/docker-proxy.conf" > "$DROPIN"
  chmod 644 "$DROPIN"
  echo "  $DROPIN"

  warn "接下来会重启 dockerd —— 正在运行的容器会被中断！"
  if [ "${GH_ACCEL_ASSUME_YES:-0}" != "1" ]; then
    read -r -p "确认重启 dockerd? [y/N] " reply
    case "$reply" in
      [yY]*) ;;
      *) warn "已写入 drop-in，但未重启 dockerd。稍后手动执行: systemctl restart docker"; return 0 ;;
    esac
  fi
  systemctl daemon-reload
  systemctl restart docker
  say "完成。验证: docker pull alpine:latest"
}

disable() {
  need_root
  if [ ! -f "$DROPIN" ]; then
    echo "dockerd 代理: 本就未启用"
    return 0
  fi
  say "移除 systemd drop-in"
  rm -f "$DROPIN"
  systemctl daemon-reload
  warn "接下来会重启 dockerd —— 正在运行的容器会被中断！"
  if [ "${GH_ACCEL_ASSUME_YES:-0}" != "1" ]; then
    read -r -p "确认重启 dockerd? [y/N] " reply
    case "$reply" in
      [yY]*) ;;
      *) warn "已移除 drop-in，但未重启 dockerd。稍后手动执行: systemctl restart docker"; return 0 ;;
    esac
  fi
  systemctl restart docker
  say "完成"
}

case "${1:-status}" in
  status) show_status ;;
  enable) enable ;;
  disable) disable ;;
  *) echo "用法: $(basename "$0") {status|enable|disable}" >&2; exit 1 ;;
esac
