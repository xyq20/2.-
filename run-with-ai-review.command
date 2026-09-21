#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"
KEYCHAIN_SERVICE="kuaimai-review-device-token"
KEYCHAIN_ACCOUNT="$(id -un)"
DEVICE_TOKEN="$(security find-generic-password -a "$KEYCHAIN_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
if [[ -z "$DEVICE_TOKEN" ]]; then
  echo "尚未初始化审核中心。请先双击 run-review-center.command。"
  read "reply?按回车关闭窗口……"
  exit 1
fi

REVIEW_PORT="${KUAIMAI_REVIEW_PORT:-8787}"
if ! curl --fail --silent --max-time 3 "http://127.0.0.1:${REVIEW_PORT}/health" >/dev/null; then
  echo "审核中心尚未运行。请先双击 run-review-center.command，并保持那个窗口开启。"
  read "reply?按回车关闭窗口……"
  exit 1
fi

export KUAIMAI_LEARNING_DEVICE_TOKEN="$DEVICE_TOKEN"
unset DEVICE_TOKEN
export KUAIMAI_LEARNING_API_URL="http://127.0.0.1:${REVIEW_PORT}"
export KUAIMAI_LEARNING_AUTO_ENABLE=1
exec "$SCRIPT_DIR/run.command" "$@"
