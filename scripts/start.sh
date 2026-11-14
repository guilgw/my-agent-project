#!/bin/bash
# macOS 一键启动：工作台 studio.py + DeepSeek Harness Web UI。
# 兼容 macOS 自带的 bash 3.2。

set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/.run"

if [ -f "$ROOT/config.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/config.env"
  set +a
fi

if [ -z "${RAIL_STUDIO_ROOT:-}" ]; then
  RAIL_STUDIO_ROOT="$HOME/Documents/loop/rail-content-studio"
fi
if [ -z "${DEEPSEEK_HARNESS_ROOT:-}" ]; then
  DEEPSEEK_HARNESS_ROOT="$HOME/Documents/loop/deepseek-harness"
fi
if [ -z "${MY_AGENT_PROJECT_ROOT:-}" ]; then
  MY_AGENT_PROJECT_ROOT="$ROOT"
fi
if [ -z "${RAIL_STUDIO_PORT:-}" ]; then
  RAIL_STUDIO_PORT=18770
fi
if [ -z "${RAIL_STUDIO_URL:-}" ]; then
  RAIL_STUDIO_URL="http://127.0.0.1:${RAIL_STUDIO_PORT}"
fi
if [ -z "${DSH_WEB_PORT:-}" ]; then
  DSH_WEB_PORT=3080
fi

case "$RAIL_STUDIO_PORT" in
  ''|*[!0-9]*) echo "RAIL_STUDIO_PORT 必须是数字：$RAIL_STUDIO_PORT" >&2; exit 1 ;;
esac
case "$DSH_WEB_PORT" in
  ''|*[!0-9]*) echo "DSH_WEB_PORT 必须是数字：$DSH_WEB_PORT" >&2; exit 1 ;;
esac

if [ -z "${RAIL_PYTHON:-}" ]; then
  if [ -x "$RAIL_STUDIO_ROOT/.venv/bin/python" ]; then
    RAIL_PYTHON="$RAIL_STUDIO_ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    RAIL_PYTHON="$(command -v python3)"
  else
    echo "找不到 python3。请先安装 Python 3.9 或更新版本。" >&2
    exit 1
  fi
fi

if [ ! -f "$RAIL_STUDIO_ROOT/studio.py" ]; then
  echo "找不到工作台：$RAIL_STUDIO_ROOT/studio.py" >&2
  echo "请克隆 https://github.com/xiaofanma666/rail-content-studio ，或设置 RAIL_STUDIO_ROOT。" >&2
  exit 1
fi

if [ -n "${DSH_BIN:-}" ]; then
  DSH_CMD="$DSH_BIN"
elif command -v dsh >/dev/null 2>&1; then
  DSH_CMD="$(command -v dsh)"
elif [ -x "$DEEPSEEK_HARNESS_ROOT/node_modules/.bin/dsh" ]; then
  DSH_CMD="$DEEPSEEK_HARNESS_ROOT/node_modules/.bin/dsh"
else
  echo "找不到 dsh。" >&2
  echo "可安装 Node.js 24 后执行：npx @deepseek-ai/dsh web" >&2
  echo "或在 $DEEPSEEK_HARNESS_ROOT 执行 pnpm install && pnpm run build && pnpm dsh web" >&2
  exit 1
fi

# macOS 上 python.org / Homebrew 的 Python 常常读不到系统证书，访问 DeepSeek、TikHub 会报 CERTIFICATE_VERIFY_FAILED。
# 启动时从系统钥匙串导出一份证书包给 Python 用；已手动设置 SSL_CERT_FILE 时不覆盖。
if [ -z "${SSL_CERT_FILE:-}" ] && command -v security >/dev/null 2>&1; then
  CA_BUNDLE="$ROOT/.run/ca-bundle.pem"
  {
    security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain
    security find-certificate -a -p /Library/Keychains/System.keychain
    security find-certificate -a -p "$HOME/Library/Keychains/login.keychain-db"
  } >"$CA_BUNDLE" 2>/dev/null || true
  if grep -q "BEGIN CERTIFICATE" "$CA_BUNDLE" 2>/dev/null; then
    SSL_CERT_FILE="$CA_BUNDLE"
    export SSL_CERT_FILE
  fi
fi

export RAIL_STUDIO_ROOT RAIL_STUDIO_PORT RAIL_STUDIO_URL RAIL_PYTHON MY_AGENT_PROJECT_ROOT
if [ -n "${RAIL_DRAFT_ROOT:-}" ]; then
  export RAIL_DRAFT_ROOT
fi

STUDIO_PID=""
ADAPTER_PID=""
cleanup() {
  if [ -n "$STUDIO_PID" ]; then
    kill "$STUDIO_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "$ADAPTER_PID" ]; then
    kill "$ADAPTER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "工作台：$RAIL_STUDIO_URL"
echo "Harness：http://127.0.0.1:${DSH_WEB_PORT}"
if [ "$RAIL_STUDIO_PORT" != "18770" ]; then
  echo "右侧浏览器插件默认打开 http://127.0.0.1:18770。" >&2
  echo "当前工作台是 $RAIL_STUDIO_URL 。请在地址栏手动打开，或修改 harness/plugins/rail-studio-sidebar/client.js 里的 DEFAULT_STUDIO_URL。" >&2
fi

PORT="$RAIL_STUDIO_PORT" "$RAIL_PYTHON" "$RAIL_STUDIO_ROOT/studio.py" >"$ROOT/.run/studio.log" 2>&1 &
STUDIO_PID=$!

if [ -n "${TIKHUB_API_KEY:-}" ]; then
  if [ -z "${TIKHUB_ADAPTER_PORT:-}" ]; then
    TIKHUB_ADAPTER_PORT=8781
  fi
  export TIKHUB_ADAPTER_PORT
  "$RAIL_PYTHON" -m tikhub_feed >"$ROOT/.run/tikhub.log" 2>&1 &
  ADAPTER_PID=$!
  echo "TikHub 适配层：http://127.0.0.1:${TIKHUB_ADAPTER_PORT}/videos"
  echo "工作台只接受 HTTPS 数据地址，不能把这个 http 地址写入 VIDEO_TRENDS_TIKTOK_FEED_URL。"
  echo "请在 Harness 里手动采集 TikTok；或把适配层放到可信 HTTPS 后面，再把那个地址写入工作台 .env。"
fi

# --patch 属于启动器，必须写在 profile 旁边。写在 web 自己的参数后面时，web 会把它当成未知选项。
set -- --profile web --patch "$ROOT/harness/rail-studio.cordis.yml" --host 127.0.0.1 --port "$DSH_WEB_PORT"
if [ "${DSH_NO_OPEN:-}" = "1" ]; then
  set -- "$@" --no-open
fi

"$DSH_CMD" "$@"
