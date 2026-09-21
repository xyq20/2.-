#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"
REVIEW_PORT="${KUAIMAI_REVIEW_PORT:-8787}"
CLOUDFLARED="$SCRIPT_DIR/.local-state/bin/cloudflared"

if ! curl --fail --silent --max-time 3 "http://127.0.0.1:${REVIEW_PORT}/health" >/dev/null; then
  echo "审核中心尚未运行。请先双击 run-review-center.command，并保持那个窗口开启。"
  read "reply?按回车关闭窗口……"
  exit 1
fi

if [[ ! -x "$CLOUDFLARED" ]]; then
  echo "首次运行：正在从 Cloudflare 官方 GitHub 下载 cloudflared……"
  mkdir -p "$SCRIPT_DIR/.local-state/bin"
  case "$(uname -m)" in
    arm64) ASSET_NAME="cloudflared-darwin-arm64.tgz" ;;
    x86_64) ASSET_NAME="cloudflared-darwin-amd64.tgz" ;;
    *) echo "暂不支持当前 Mac 架构：$(uname -m)"; exit 1 ;;
  esac
  TEMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TEMP_DIR"' EXIT INT TERM
  curl --fail --location --silent --show-error \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/${ASSET_NAME}" \
    --output "$TEMP_DIR/cloudflared.tgz"
  tar -xzf "$TEMP_DIR/cloudflared.tgz" -C "$TEMP_DIR"
  install -m 755 "$TEMP_DIR/cloudflared" "$CLOUDFLARED"
  "$CLOUDFLARED" --version
  rm -rf "$TEMP_DIR"
  trap - EXIT INT TERM
fi

echo "正在创建临时 HTTPS 运营入口……"
echo "登录仍由审核中心控制；关闭本窗口后，临时网址立即失效。"
exec "$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:${REVIEW_PORT}"
