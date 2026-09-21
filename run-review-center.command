#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "首次运行：正在创建 Python 环境……"
  python3 -m venv .venv
fi

echo "正在检查审核中心依赖……"
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt

KEYCHAIN_SERVICE="kuaimai-review-device-token"
KEYCHAIN_ACCOUNT="$(id -un)"
DEVICE_TOKEN="$(security find-generic-password -a "$KEYCHAIN_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
if [[ -z "$DEVICE_TOKEN" ]]; then
  DEVICE_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  security add-generic-password -U -a "$KEYCHAIN_ACCOUNT" -s "$KEYCHAIN_SERVICE" -w "$DEVICE_TOKEN" >/dev/null
  echo "已在 macOS 钥匙串创建设备令牌（不会写入项目文件或日志）。"
fi

export KUAIMAI_REVIEW_DEVICE_TOKEN="$DEVICE_TOKEN"
export KUAIMAI_LEARNING_DEVICE_TOKEN="$DEVICE_TOKEN"
unset DEVICE_TOKEN

if ! .venv/bin/python -m local_review.cli has-users; then
  echo "首次启动需要创建审核网页登录账号。"
  .venv/bin/python -m local_review.cli init-admin --username admin
fi

.venv/bin/python -m local_review.cli backup
REVIEW_PORT="${KUAIMAI_REVIEW_PORT:-8787}"
echo "审核中心：http://127.0.0.1:${REVIEW_PORT}"
echo "保持本窗口运行；关闭窗口后运营网页会暂时离线。"
(sleep 2; open "http://127.0.0.1:${REVIEW_PORT}/") &
exec .venv/bin/python -m uvicorn local_review.app:app \
  --host 127.0.0.1 --port "$REVIEW_PORT" --proxy-headers --forwarded-allow-ips 127.0.0.1
