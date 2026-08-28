#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "首次运行：正在创建 Python 环境……"
  python3 -m venv .venv
fi

echo "正在检查并安装运行依赖，请稍候……"
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
echo "运行环境已就绪，正在启动快麦铺货程序……"
export PYTHONUNBUFFERED=1

# 总入口默认运行全部已实现平台；平台独立脚本通过后置参数覆盖此值。
exec .venv/bin/python kuaimai_erp.py --platform all "$@"
