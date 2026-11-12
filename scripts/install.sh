#!/usr/bin/env bash
# gh-accel 一键安装
#
#   curl -fsSL <repo>/scripts/install.sh | bash
#   或  ./scripts/install.sh [--prefix DIR] [--no-client] [--no-service]
#
# 做四件事：
#   1. 安装命令行工具到 ~/.local/bin（不依赖 pip，直接调用源码）
#   2. 安装并启用 systemd user 服务
#   3. 生成 CA 并配置客户端信任（curl / wget / git）
#   4. 打印验证方式
#
# 幂等：重复执行安全。

set -euo pipefail

PREFIX="${HOME}/.local"
BINDIR="${PREFIX}/bin"
ROOT="${GH_ACCEL_HOME:-${HOME}/Code/personal/gh-accel}"
UNIT_DIR="${HOME}/.config/systemd/user"
CLIENT=1
SERVICE=1
PORT="${GH_ACCEL_PORT:-19770}"

usage() {
  cat <<EOF
用法: install.sh [选项]

选项:
  --prefix DIR     命令安装目录 (默认: ${PREFIX})
  --root DIR       源码目录 (默认: ${ROOT})
  --no-client      不配置客户端 (curl/wget/git) 的 CA 与代理
  --no-service     不安装 systemd 服务
  -h, --help       显示本帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) BINDIR="$2/"; shift 2 ;;
    --root) ROOT="$2"; shift 2 ;;
    --no-client) CLIENT=0; shift ;;
    --no-service) SERVICE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知选项: $1" >&2; usage; exit 1 ;;
  esac
done

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m警告:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "${ROOT}/src/gh_accel/__init__.py" ] || die "找不到源码: ${ROOT}/src/gh_accel/
（用 --root 指定源码目录，或先 clone 仓库）"

command -v openssl >/dev/null || die "需要 openssl（用于签发本地 CA）"
command -v python3 >/dev/null || die "需要 python3 >= 3.10"

PYTHON="$(command -v python3)"
version="$("${PYTHON}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$version" in
  3.1[0-9]|3.[2-9][0-9]|4.*) ;;
  *) die "需要 python3 >= 3.10（当前 ${version}）" ;;
esac

# ---- 1. 命令行工具 ------------------------------------------------------
say "安装命令行工具到 ${BINDIR}"
mkdir -p "${BINDIR}"
for cmd in gh-accel-get gh-accel-env; do
  install -m755 "${ROOT}/scripts/${cmd}" "${BINDIR}/${cmd}"
  echo "  ${BINDIR}/${cmd}"
done
cat > "${BINDIR}/gh-accel" <<EOF
#!/usr/bin/env bash
# gh-accel —— GitHub 下载加速代理的管理工具
set -euo pipefail
ROOT="\${GH_ACCEL_HOME:-${ROOT}}"
exec env PYTHONPATH="\${ROOT}/src" "\${GH_ACCEL_PYTHON:-python3}" -m gh_accel.cli "\$@"
EOF
chmod +x "${BINDIR}/gh-accel"
echo "  ${BINDIR}/gh-accel"

# ---- 2. systemd 服务 ----------------------------------------------------
if [ "$SERVICE" -eq 1 ]; then
  if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    say "安装 systemd user 服务"
    mkdir -p "${UNIT_DIR}"
    # unit 模板里的源码路径用 @GH_ACCEL_ROOT@ 占位，替换成实际路径，
    # 否则用 --root 指定其它位置时服务会找不到模块
    sed "s|@GH_ACCEL_ROOT@|${ROOT}|g" \
      "${ROOT}/systemd/gh-accel.service" > "${UNIT_DIR}/gh-accel.service"
    systemctl --user daemon-reload
    systemctl --user enable --now gh-accel.service
    sleep 1
    systemctl --user is-active gh-accel.service >/dev/null \
      && echo "  gh-accel.service 已启动" \
      || warn "服务启动失败，用 'journalctl --user -u gh-accel' 查看日志"
  else
    warn "当前环境不支持 systemd user 服务，跳过。"
    warn "可手动运行： PYTHONPATH=${ROOT}/src python3 -m gh_accel"
  fi
fi

# ---- 3. 客户端配置 ------------------------------------------------------
CA="${HOME}/.config/gh-accel/certs/ca.pem"
if [ "$CLIENT" -eq 1 ]; then
  say "配置客户端"
  # CA 由服务在启动时生成；若服务没跑，这里主动生成一份
  if [ ! -f "${CA}" ]; then
    mkdir -p "$(dirname "${CA}")"
    openssl req -x509 -newkey rsa:2048 -keyout "${CA%.pem}.key" -out "${CA}" \
      -days 3650 -nodes -subj "/CN=gh-accel local CA" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
      -addext "keyUsage=critical,keyCertSign,cRLSign,digitalSignature" \
      -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1 \
      && chmod 600 "${CA%.pem}.key"
  fi

  # git: 只让 github.com 走代理，其它站点不受影响
  if command -v git >/dev/null; then
    git config --global http.https://github.com/.proxy "http://127.0.0.1:${PORT}"
    git config --global http.sslCAInfo "${CA}"
    echo "  git   -> 仅 github.com 走代理"
  fi

  # curl
  if command -v curl >/dev/null; then
    printf '# 通过 gh-accel 代理访问 GitHub\ncacert = %s\nproxy = "http://127.0.0.1:%s"\n' \
      "${CA}" "${PORT}" > "${HOME}/.curlrc"
    echo "  curl  -> ${HOME}/.curlrc"
  fi

  # wget（1.25 起读 XDG 路径，不是 ~/.wgetrc）
  if command -v wget >/dev/null; then
    mkdir -p "${HOME}/.config/wget"
    printf '# 通过 gh-accel 代理访问 GitHub\nca_certificate = %s\nhttps_proxy = http://127.0.0.1:%s\n' \
      "${CA}" "${PORT}" > "${HOME}/.config/wget/wgetrc"
    echo "  wget  -> ${HOME}/.config/wget/wgetrc"
  fi

  # mise
  MISE_CFG="${HOME}/.config/mise/config.toml"
  if [ -f "${MISE_CFG}" ] && ! grep -q "url_replacements" "${MISE_CFG}"; then
    say "提示：把下面两行加进 ${MISE_CFG} 的 [settings.url_replacements]"
    cat <<EOF

[settings.url_replacements]
'regex:^https://github\\.com/' = "http://127.0.0.1:${PORT}/gh/"
'regex:^https://raw\\.githubusercontent\\.com/' = "http://127.0.0.1:${PORT}/raw/"

EOF
  fi
fi

# ---- 4. 验证 ------------------------------------------------------------
say "验证"
"${BINDIR}/gh-accel" status || true

cat <<EOF

安装完成。

  代理服务    systemctl --user status gh-accel.service
  管理工具    gh-accel status | test | sources | clean | logs
  下载文件    gh-accel-get https://github.com/owner/repo/releases/download/v1.0/x.tar.gz
  任意程序    eval "\$(gh-accel-env)" && <命令>

  CA 位置     ${CA}
EOF
