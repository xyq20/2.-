#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt

# 总入口默认运行全部已实现平台；平台独立脚本通过后置参数覆盖此值。
exec .venv/bin/python kuaimai_erp.py --platform all "$@"
