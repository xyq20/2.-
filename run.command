#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

typeset -a PYTHON_ARGS

# 双击本脚本时统一在当前终端选择平台和执行模式；带参数调用时保留
# 命令行入口，便于测试、定时任务和已有自动化调用。
if (( $# == 0 )); then
  print ""
  print "快麦一键铺货｜统一启动器"
  print "========================================"
  print "请选择需要运行的平台："
  print "  0) 全平台流程（基础资料 + 抖音 + 淘宝 + 天猫 + 拼多多 + 微信小店 + 小红书 + 有赞 + 京东）"
  print "  1) 基础资料"
  print "  2) 抖音"
  print "  3) 淘宝"
  print "  4) 天猫（正式保存/铺货）"
  print "  5) 拼多多（填写、保存、铺货）"
  print "  6) 微信小店（视频号，填写、保存、铺货）"
  print "  7) 小红书（填写、保存、铺货）"
  print "  8) 有赞（填写、保存、铺货）"
  print "  9) 京东（填写、保存、铺货）"
  print " 10) 一键新增链接（按视频固定填法，仅创建快麦商品）"
  while true; do
    read "platform_choice?平台编号 [0-10]: "
    case "$platform_choice" in
      0) selected_platform="all"; break ;;
      1) selected_platform="base"; break ;;
      2) selected_platform="douyin"; break ;;
      3) selected_platform="taobao"; break ;;
      4) selected_platform="tmall"; break ;;
      5) selected_platform="pdd"; break ;;
      6) selected_platform="wxsph"; break ;;
      7) selected_platform="xhs"; break ;;
      8) selected_platform="youzan"; break ;;
      9) selected_platform="jd"; break ;;
      10) selected_platform="create"; break ;;
      *) print "请输入 0-10 的编号。" ;;
    esac
  done

  print ""
  print "请选择执行模式："
  if [[ "$selected_platform" == "create" ]]; then
    print "  1) 填写新增页面，不保存"
  else
    print "  1) 平台仅填写/检查，不保存、不铺货（不会改动基础资料）"
  fi
  print "  2) 保存，但不铺货"
  if [[ "$selected_platform" != "create" ]]; then
    print "  3) 保存并铺货（需要再次输入 PUBLISH）"
  fi
  while true; do
    read "mode_choice?模式编号 [1-3]: "
    case "$mode_choice" in
      1) selected_mode="preview"; break ;;
      2)
        selected_mode="save_only"
        break
        ;;
      3)
        if [[ "$selected_platform" == "create" ]]; then
          print "新增链接仅创建快麦商品，请选择 1 或 2。"
          continue
        fi
        print ""
        print "警告：此选项会真实保存，并向所选平台的指定店铺提交铺货。"
        read "publish_confirmation?确认请输入大写 PUBLISH，其他输入取消： "
        if [[ "$publish_confirmation" != "PUBLISH" ]]; then
          print "未确认，已改为平台仅填写/检查，不保存、不铺货。"
          selected_mode="preview"
        else
          selected_mode="publish"
        fi
        break
        ;;
      *) print "请输入 1-3 的编号。" ;;
    esac
  done

  case "$selected_platform:$selected_mode" in
    create:preview) PYTHON_ARGS=(--platform base --create-product --no-save) ;;
    create:save_only) PYTHON_ARGS=(--platform base --create-product --save-only) ;;
    wxsph:preview)
      PYTHON_ARGS=(--platform wxsph --no-save)
      ;;
    wxsph:save_only)
      PYTHON_ARGS=(--platform wxsph --save-only)
      ;;
    wxsph:publish)
      PYTHON_ARGS=(--platform wxsph --save)
      ;;
    xhs:preview)
      PYTHON_ARGS=(--platform xhs --no-save)
      ;;
    xhs:save_only)
      PYTHON_ARGS=(--platform xhs --save-only)
      ;;
    xhs:publish)
      PYTHON_ARGS=(--platform xhs --save)
      ;;
    youzan:preview)
      PYTHON_ARGS=(--platform youzan --no-save)
      ;;
    youzan:save_only)
      PYTHON_ARGS=(--platform youzan --save-only)
      ;;
    youzan:publish)
      PYTHON_ARGS=(--platform youzan --save)
      ;;
    jd:preview)
      PYTHON_ARGS=(--platform jd --no-save)
      ;;
    jd:save_only)
      PYTHON_ARGS=(--platform jd --save-only)
      ;;
    jd:publish)
      PYTHON_ARGS=(--platform jd --save)
      ;;
    pdd:preview)
      PYTHON_ARGS=(--platform pdd --no-save)
      ;;
    pdd:save_only)
      PYTHON_ARGS=(--platform pdd --save-only)
      ;;
    pdd:publish)
      PYTHON_ARGS=(--platform pdd --save)
      ;;
    tmall:preview)
      PYTHON_ARGS=(--platform tmall --no-save)
      ;;
    tmall:save_only)
      PYTHON_ARGS=(--platform tmall --save-only)
      ;;
    tmall:publish)
      PYTHON_ARGS=(--platform tmall --save)
      ;;
    taobao:preview)
      PYTHON_ARGS=(--platform taobao --no-save)
      ;;
    taobao:save_only)
      PYTHON_ARGS=(--platform taobao --save-only --allow-taobao-save-once)
      ;;
    taobao:publish)
      PYTHON_ARGS=(--platform taobao --save --allow-taobao-publish-once)
      ;;
    all:preview|all:save_only|all:publish|base:preview|base:save_only|base:publish|douyin:preview|douyin:save_only|douyin:publish)
      PYTHON_ARGS=(--platform "$selected_platform")
      case "$selected_mode" in
        preview) PYTHON_ARGS+=(--no-save) ;;
        save_only) PYTHON_ARGS+=(--save-only) ;;
        publish) PYTHON_ARGS+=(--save) ;;
      esac
      ;;
  esac
else
  PYTHON_ARGS=("$@")
  has_platform_argument=0
  has_create_argument=0
  for argument in "${PYTHON_ARGS[@]}"; do
    if [[ "$argument" == "--platform" ]]; then
      has_platform_argument=1
    fi
    if [[ "$argument" == --platform=* ]]; then has_platform_argument=1; fi
    if [[ "$argument" == "--create-product" ]]; then has_create_argument=1; fi
  done
  if (( ! has_platform_argument )); then
    if (( has_create_argument )); then
      PYTHON_ARGS=(--platform base "${PYTHON_ARGS[@]}")
    else
      PYTHON_ARGS=(--platform all "${PYTHON_ARGS[@]}")
    fi
  fi
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "首次运行：正在创建 Python 环境……"
  python3 -m venv .venv
fi

echo "正在检查并安装运行依赖，请稍候……"
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
echo "运行环境已就绪，正在启动快麦铺货程序……"
export PYTHONUNBUFFERED=1

# 所有平台最终都从同一个 Python 主流程进入，避免多个 .command 参数漂移。
exec .venv/bin/python kuaimai_erp.py "${PYTHON_ARGS[@]}"
