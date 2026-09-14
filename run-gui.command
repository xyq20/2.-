#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  print "找不到 python3。请先在这台 Mac 安装 Python 3（需要包含 Tkinter），然后重新双击本文件。"
  exit 1
fi

if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
  print "当前 Python 没有 Tkinter 支持。请安装带 Tk 支持的 Python 3，然后重新双击本文件。"
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  print "首次运行：正在创建 Python 环境……"
  python3 -m venv .venv
fi

print "正在检查并安装运行依赖，请稍候……"
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
print "环境已就绪，正在打开快麦一键铺货窗口……"
export PYTHONUNBUFFERED=1
exec .venv/bin/python kuaimai_gui.py
