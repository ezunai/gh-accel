#!/usr/bin/env bash
# gh-accel 一键安装
#
#   curl -fsSL <repo>/scripts/install.sh | bash
#   或  ./scripts/install.sh [--prefix DIR] [--no-client] [--no-service]
#
# 这是 `gh-accel install` 的薄包装 —— 实际逻辑在 src/gh_accel/installer.py，
# 两边共用同一份实现，不会漂移。
#
# 脚本会：
#   1. 安装命令行工具到 ~/.local/bin
#   2. 安装并启用 systemd user 服务
#   3. 生成 CA 并配置客户端信任（curl / wget / git）
#   4. 打印验证方式
#
# 幂等：重复执行安全。

set -euo pipefail

# 定位源码目录（本脚本所在仓库的根）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${GH_ACCEL_HOME:-$(dirname "$SCRIPT_DIR")}"

PYTHON="${GH_ACCEL_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null || {
  echo "错误: 找不到 python3" >&2
  exit 1
}

if [ ! -f "${ROOT}/src/gh_accel/installer.py" ]; then
  echo "错误: 找不到源码 ${ROOT}/src/gh_accel/installer.py" >&2
  echo "      用 GH_ACCEL_HOME 指定源码目录，或先 clone 仓库" >&2
  exit 1
fi

exec env PYTHONPATH="${ROOT}/src" "$PYTHON" -m gh_accel.cli install \
  --root "$ROOT" "$@"
