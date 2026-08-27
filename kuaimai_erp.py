#!/usr/bin/env python3
"""
快麦 ERP 基础资料自动编辑程序。

读取产品文件夹中的产品信息.xlsx 和图片子目录，使用 Playwright 操作快麦通。
商品查询优先调用页面同源 API，页面导航、文件上传和表单校验使用 DOM。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import unquote, urlsplit


ERP_ENTRY_URL = "https://erp.superboss.cc/index.html#/index/"
CENTER_URL = "https://scm.superboss.cc/supplier/prod/center"
ERP_HOSTS = {"erp.superboss.cc", "erpa.superboss.cc"}
SCM_HOSTS = {"scm.superboss.cc", "scma.superboss.cc"}
DEFAULT_EXCEL_URL = "smb://gongxiang/共享文件/谭/products/绿巨人+NGBL-10588/产品信息.xlsx"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class AutomationError(RuntimeError):
    """可向用户直接展示的自动化异常。"""


def is_scm_url(value: str) -> bool:
    return urlsplit(value).hostname in SCM_HOSTS


def center_url_for(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.hostname in SCM_HOSTS:
        return f"{parsed.scheme}://{parsed.netloc}/supplier/prod/center"
    return CENTER_URL


def is_scm_cookie(cookie: Dict[str, Any]) -> bool:
    domain_label = (cookie.get("domain") or "").lstrip(".").split(".", 1)[0].casefold()
    return domain_label.startswith("scm") or "scm" in (cookie.get("name") or "").casefold()


@dataclass(frozen=True)
class ProductData:
    excel_path: Path
    product_dir: Path
    title: str
    style_code: str
    base_price: str
    main_images: List[Path]
    main_images_34: List[Path]
    detail_images: List[Path]
    sku_images: List[Path]


def natural_key(path: Path) -> List[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_price(value: Any) -> str:
    text = normalize_cell(value).replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise AutomationError(f"基本售价不是有效数字：{text!r}") from exc
    if number < 0:
        raise AutomationError(f"基本售价不能小于 0：{text!r}")
    return format(number.normalize(), "f")


def resolve_excel_path(value: str) -> Path:
    """将 smb://host/share/path 映射到 macOS 已挂载的 /Volumes/share/path。"""
    if value.lower().startswith("smb://"):
        parsed = urlsplit(value)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise AutomationError(f"SMB 路径缺少共享名或文件路径：{value}")
        path = Path("/Volumes").joinpath(*parts)
    else:
        path = Path(value).expanduser()

    path = path.resolve(strict=False)
    if not path.is_file():
        if value.lower().startswith("smb://"):
            raise AutomationError(
                f"找不到 Excel：{path}\n"
                f"请先在 Finder 中连接服务器 {urlsplit(value).hostname}，并挂载共享文件夹。"
            )
        raise AutomationError(f"找不到 Excel：{path}")
    return path


def find_image_dir(product_dir: Path, candidates: Sequence[str], label: str) -> Path:
    for relative in candidates:
        candidate = product_dir / relative
        if candidate.is_dir():
            return candidate
    names = "、".join(candidates)
    raise AutomationError(f"找不到{label}文件夹，已尝试：{names}")


def list_images(directory: Path, label: str) -> List[Path]:
    files = sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() in IMAGE_SUFFIXES),
        key=natural_key,
    )
    if not files:
        raise AutomationError(f"{label}文件夹中没有可上传图片：{directory}")
    return files


def read_product_data(excel_path: Path) -> ProductData:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise AutomationError("缺少 openpyxl，请先运行：python3 -m pip install -r requirements.txt") from exc

    workbook = load_workbook(excel_path, data_only=True, read_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    rows = list(sheet.iter_rows(values_only=True))
    if len(rows) < 2:
        raise AutomationError(f"Excel 内容不完整：{excel_path}")

    fields: Dict[str, Any] = {}
    for row in rows:
        if not row:
            continue
        key = normalize_cell(row[0])
        if not key:
            continue
        value = next((cell for cell in row[1:] if cell is not None and normalize_cell(cell)), None)
        fields[key] = value

    def field_containing(*aliases: str) -> Any:
        for key, value in fields.items():
            key_parts = [part.strip() for part in key.split("/")]
            if any(alias == key or alias in key_parts for alias in aliases):
                return value
        return None

    # 用户明确指定商品名称使用 Excel 第二行。
    title = normalize_cell(rows[1][1] if len(rows[1]) > 1 else None)
    if not title:
        title = normalize_cell(field_containing("商品名称"))
    style_code = normalize_cell(field_containing("货号", "商家外部编码", "款式编码"))
    price_value = field_containing("基本售价")

    if not title:
        raise AutomationError("Excel 第二行没有商品名称")
    if not style_code:
        raise AutomationError("Excel 中找不到货号/款式编码")
    if price_value is None or normalize_cell(price_value) == "":
        raise AutomationError("Excel 中找不到“吊牌价/价格/基本售价”")

    product_dir = excel_path.parent
    main_dir = find_image_dir(
        product_dir,
        ("1：1主图", "1:1主图", "主图/1：1", "主图/1:1"),
        "1:1 主图",
    )
    main_34_dir = find_image_dir(
        product_dir,
        ("3：4主图", "3:4主图", "主图/3：4", "主图/3:4"),
        "3:4 主图",
    )
    detail_dir = find_image_dir(product_dir, ("详情页图", "商品详情图"), "商品详情图")
    sku_dir = find_image_dir(product_dir, ("SKU图", "sku图", "Sku图"), "SKU 图")

    return ProductData(
        excel_path=excel_path,
        product_dir=product_dir,
        title=title,
        style_code=style_code,
        base_price=normalize_price(price_value),
        main_images=list_images(main_dir, "1:1 主图"),
        main_images_34=list_images(main_34_dir, "3:4 主图"),
        detail_images=list_images(detail_dir, "商品详情图"),
        sku_images=list_images(sku_dir, "SKU 图"),
    )


def product_summary(product: ProductData) -> Dict[str, Any]:
    data = asdict(product)
    for key in ("excel_path", "product_dir"):
        data[key] = str(data[key])
    for key in ("main_images", "main_images_34", "detail_images", "sku_images"):
        data[key] = [str(path) for path in data[key]]
    return data


def setup_logging(artifact_dir: Path) -> logging.Logger:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("kuaimai_erp")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(artifact_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    return logger


async def save_auth_state(
    context: Any,
    state_path: Path,
    logger: logging.Logger,
    scm_verified: bool = True,
) -> None:
    """显式保存包括会话 Cookie 在内的认证状态，避免 Chrome 关闭时清理。"""
    state = await context.storage_state()
    state["cookies"] = [
        cookie
        for cookie in state.get("cookies") or []
        if (cookie.get("domain") or "").lstrip(".").endswith("superboss.cc")
    ]
    state["origins"] = [
        origin_state
        for origin_state in state.get("origins") or []
        if (urlsplit(origin_state.get("origin") or "").hostname or "").endswith("superboss.cc")
    ]
    if not scm_verified:
        state["cookies"] = [
            cookie
            for cookie in state["cookies"]
            if not is_scm_cookie(cookie)
        ]
        state["origins"] = [
            origin_state
            for origin_state in state["origins"]
            if urlsplit(origin_state.get("origin") or "").hostname in ERP_HOSTS
        ]
    session_storage: Dict[str, List[Dict[str, str]]] = {}
    for page in getattr(context, "pages", []):
        try:
            snapshot = await page.evaluate(
                """
                () => ({
                  origin: location.origin,
                  entries: Object.entries(sessionStorage).map(([name, value]) => ({name, value}))
                })
                """
            )
            origin = snapshot.get("origin")
            hostname = urlsplit(origin or "").hostname or ""
            if hostname.endswith("superboss.cc") and (scm_verified or hostname in ERP_HOSTS):
                session_storage[origin] = snapshot.get("entries") or []
        except Exception:
            continue
    state["sessionStorage"] = session_storage
    state["scmVerified"] = scm_verified

    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary_path.chmod(0o600)
    temporary_path.replace(state_path)
    state_path.chmod(0o600)
    logger.info(
        "已保存登录状态：%s 个 Cookie，%s 个站点，SCM 已验证=%s",
        len(state.get("cookies") or []),
        len(state.get("origins") or []),
        "是" if scm_verified else "否",
    )


async def restore_auth_state(context: Any, state_path: Path, logger: logging.Logger) -> bool:
    """恢复 storage_state 中不会由持久化 Chrome 自动保留的会话认证数据。"""
    if not state_path.is_file():
        logger.info("未找到已保存的登录状态，本次需要手工登录")
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        cookies = state.get("cookies") or []
        scm_verified = state.get("scmVerified") is True
        if not scm_verified:
            cookies = [
                cookie
                for cookie in cookies
                if not is_scm_cookie(cookie)
            ]
        if cookies:
            await context.add_cookies(cookies)

        storage_by_origin: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
        for origin_state in state.get("origins") or []:
            origin = origin_state.get("origin")
            hostname = urlsplit(origin or "").hostname or ""
            if origin and (scm_verified or hostname in ERP_HOSTS):
                storage_by_origin.setdefault(origin, {})["localStorage"] = (
                    origin_state.get("localStorage") or []
                )
        for origin, entries in (state.get("sessionStorage") or {}).items():
            hostname = urlsplit(origin or "").hostname or ""
            if scm_verified or hostname in ERP_HOSTS:
                storage_by_origin.setdefault(origin, {})["sessionStorage"] = entries or []

        if storage_by_origin:
            serialized = json.dumps(storage_by_origin, ensure_ascii=False)
            await context.add_init_script(
                """
                (() => {
                  const states = %s;
                  const state = states[location.origin];
                  if (!state) return;
                  for (const item of state.localStorage || []) localStorage.setItem(item.name, item.value);
                  for (const item of state.sessionStorage || []) sessionStorage.setItem(item.name, item.value);
                })();
                """
                % serialized
            )
        logger.info(
            "已恢复登录状态：%s 个 Cookie，SCM 已验证=%s",
            len(cookies),
            "是" if scm_verified else "否",
        )
        return True
    except Exception as exc:
        logger.warning("登录状态文件无法恢复，将改为手工登录：%s", exc)
        return False


def saved_scm_state_is_verified(state_path: Optional[Path]) -> bool:
    """只有之前经商品 API 验证过的 SCM 状态才允许走快速路径。"""
    if state_path is None or not state_path.is_file():
        return False
    try:
        return json.loads(state_path.read_text(encoding="utf-8")).get("scmVerified") is True
    except Exception:
        return False


async def visible(locator: Any) -> bool:
    try:
        return await locator.is_visible()
    except Exception:
        return False


async def safe_screenshot(page: Any, path: Path) -> None:
    try:
        await page.screenshot(path=str(path), full_page=False)
    except Exception:
        pass


async def visible_text_across_frames(page: Any, text: str) -> Optional[Any]:
    """在主页面及所有同/跨域 iframe 中查找可见文本定位器。"""
    # 登录跳转会替换首页 iframe。frame 列表是一个瞬时快照，查询期间旧 frame
    # 可能已经脱离；忽略该快照并让外层状态轮询读取新 frame。
    for frame in list(page.frames):
        try:
            candidate = await first_visible(frame.get_by_text(text, exact=True))
            if candidate is not None:
                return candidate
        except Exception:
            continue
    return None


async def dismiss_erp_blocking_dialogs(page: Any, logger: logging.Logger) -> int:
    """关闭只用于提醒、但会遮挡快麦通入口的 ERP 首页弹窗。"""
    dismissed = 0
    for frame in list(page.frames):
        try:
            dialogs = frame.locator(".el-dialog:visible").filter(has_text="店铺状态异常确认")
            for index in range(await dialogs.count()):
                dialog = dialogs.nth(index)
                close_button = dialog.locator(
                    "button.el-dialog__headerbtn, .el-dialog__headerbtn, .el-dialog__close"
                ).first
                if await visible(close_button):
                    await close_button.click(force=True)
                    dismissed += 1
        except Exception:
            continue
    if dismissed:
        logger.info("已关闭 %s 个 ERP 首页遮挡弹窗（未进入店铺管理）", dismissed)
    return dismissed


async def dismiss_scm_first_entry_dialog(page: Any, logger: logging.Logger) -> int:
    """首次进入快麦通时，只关闭温馨提示，不进入“立即查看”流程。"""
    dismissed = 0
    for frame in list(page.frames):
        try:
            dialogs = frame.locator(".el-dialog:visible").filter(has_text="温馨提示")
            for index in range(await dialogs.count()):
                dialog = dialogs.nth(index)
                acknowledge = await first_visible(dialog.get_by_text("知道了", exact=True))
                if acknowledge is not None:
                    await acknowledge.click(force=True)
                    dismissed += 1
        except Exception:
            continue
    if dismissed:
        logger.info("已关闭 %s 个快麦通首次进入温馨提示", dismissed)
    return dismissed


async def wait_for_erp_entry_stable(page: Any, timeout_seconds: int) -> Any:
    """等待快麦通入口连续可见，避免 ERP 刚跳首页就过早触发 SSO。"""
    deadline = time.monotonic() + timeout_seconds
    stable_since: Optional[float] = None
    while time.monotonic() < deadline:
        entry = await visible_text_across_frames(page, "快麦通")
        if entry is None:
            stable_since = None
        else:
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= 0.5:
                return entry
        await asyncio.sleep(0.1)
    raise AutomationError(f"快麦 ERP 首页的“快麦通”入口在 {timeout_seconds} 秒内未稳定加载")


async def wait_for_scm_api_session(
    page: Any,
    style_code: str,
    timeout_seconds: int,
    logger: logging.Logger,
) -> None:
    """以受保护的商品查询 API 为准，确认 SCM 会话确实已经建立。"""
    body = {
        "pageNo": 1,
        "pageSize": 1,
        "outerIds": [style_code] if style_code else [],
        "companyNames": [],
        "shopNames": [],
        "api_name": "item_base_page",
    }
    deadline = time.monotonic() + timeout_seconds
    last_status = "尚未请求"
    next_log_at = 0.0
    while time.monotonic() < deadline:
        await dismiss_scm_first_entry_dialog(page, logger)
        current_path = urlsplit(page.url).path
        if current_path.startswith("/login"):
            raise AutomationError("ERP 单点登录未建立快麦通会话，页面被跳转到快麦通登录页")
        try:
            result = await page.evaluate(
                """
                async (body) => {
                  try {
                    const response = await fetch('/item/base/page.json', {
                      method: 'POST',
                      credentials: 'include',
                      headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify(body)
                    });
                    const contentType = response.headers.get('content-type') || '';
                    let data = null;
                    if (contentType.includes('json')) {
                      try { data = await response.json(); } catch (_) {}
                    }
                    return {
                      status: response.status,
                      finalPath: new URL(response.url).pathname,
                      result: data && data.result
                    };
                  } catch (error) {
                    return {error: String(error)};
                  }
                }
                """,
                body,
            )
            last_status = (
                f"HTTP {result.get('status')}，result={result.get('result')}，"
                f"响应路径={result.get('finalPath') or '-'}"
            )
            if int(result.get("result", 0) or 0) == 1:
                logger.info("快麦通会话已由商品查询 API 确认")
                return
        except Exception as exc:
            last_status = str(exc)

        now = time.monotonic()
        if now >= next_log_at:
            logger.info("等待快麦通会话就绪：%s", last_status)
            next_log_at = now + 10
        await asyncio.sleep(0.5)
    raise AutomationError(
        f"快麦通页面已打开，但会话在 {timeout_seconds} 秒内未通过 API 校验；最后状态：{last_status}"
    )


async def try_reuse_verified_scm_session(
    page: Any,
    style_code: str,
    timeout_seconds: int,
    logger: logging.Logger,
    auth_state_path: Optional[Path] = None,
) -> Any:
    """
    复用已验证的快麦通会话；任何异常由调用方回退到 ERP SSO。
    快速路径仍必须通过受保护商品 API，不以 Cookie 存在与否作为成功依据。
    """
    fast_timeout = max(3, min(timeout_seconds, 12))
    fast_timeout_ms = fast_timeout * 1000
    center_url = center_url_for(page.url)
    await page.goto(center_url, wait_until="domcontentloaded", timeout=fast_timeout_ms)
    await wait_for_scm_api_session(page, style_code, fast_timeout, logger)

    deadline = time.monotonic() + fast_timeout
    while time.monotonic() < deadline:
        if urlsplit(page.url).path.startswith("/login"):
            raise AutomationError("已保存的快麦通会话已失效")
        if await visible(page.get_by_text("商品中心", exact=True).first):
            if auth_state_path is not None:
                await save_auth_state(page.context, auth_state_path, logger)
            logger.info("已复用通过 API 校验的快麦通会话，跳过 ERP 中转")
            return page
        await asyncio.sleep(0.1)
    raise AutomationError("快速打开商品中心超时")


async def enter_kuaimai_from_erp(
    page: Any,
    timeout_seconds: int,
    headless: bool,
    logger: logging.Logger,
    operation_timeout_seconds: Optional[int] = None,
    auth_state_path: Optional[Path] = None,
    style_code: str = "",
) -> Any:
    """从快麦 ERP 首页的“快麦通”入口进入 SCM，避免直接访问 SCM 登录页。"""
    operation_timeout = operation_timeout_seconds or min(timeout_seconds, 300)
    operation_timeout_ms = operation_timeout * 1000
    await page.goto(ERP_ENTRY_URL, wait_until="domcontentloaded", timeout=operation_timeout_ms)

    async def erp_home_ready() -> bool:
        if urlsplit(page.url).hostname not in ERP_HOSTS:
            return False
        return await visible_text_across_frames(page, "快麦通") is not None

    if not await erp_home_ready():
        if headless:
            raise AutomationError("当前自动化专用 Chrome 未登录快麦 ERP，请去掉 --headless 后先登录一次")

        logger.info(
            "等待登录：请在打开的 Chrome 中完成快麦 ERP 登录（最多等待 %s 秒）",
            timeout_seconds,
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if await erp_home_ready():
                break
            await asyncio.sleep(1)
        else:
            raise AutomationError("等待快麦 ERP 登录超时")

    await dismiss_erp_blocking_dialogs(page, logger)
    context = page.context
    if auth_state_path is not None:
        await save_auth_state(context, auth_state_path, logger, scm_verified=False)

    logger.info("已进入快麦 ERP 首页，等待账号与“快麦通”入口稳定")
    entry = await wait_for_erp_entry_stable(page, operation_timeout)
    logger.info("快麦 ERP 首页已稳定，正在从“快麦通”入口进入商品中心")

    existing_scm_pages = {id(candidate) for candidate in context.pages if is_scm_url(candidate.url)}
    # ERP 弹窗关闭后偶尔残留透明 v-modal。原生 click 通常立即返回；若站点
    # 事件处理仍阻塞，最多等待 5 秒后也直接扫描/接管已经打开的 SCM 标签。
    try:
        await asyncio.wait_for(entry.evaluate("element => element.click()"), timeout=5)
        logger.info("已触发 ERP“快麦通”入口，开始接管快麦通标签")
    except asyncio.TimeoutError:
        logger.warning("ERP 入口触发 5 秒未返回，继续接管已打开的快麦通标签")
    except Exception as exc:
        logger.warning("ERP 入口触发返回异常，继续检查已打开标签：%s", exc)

    scm_page = None
    open_deadline = time.monotonic() + operation_timeout
    while time.monotonic() < open_deadline:
        if is_scm_url(page.url):
            scm_page = page
            break
        candidates = [candidate for candidate in context.pages if is_scm_url(candidate.url)]
        new_candidates = [candidate for candidate in candidates if id(candidate) not in existing_scm_pages]
        if new_candidates:
            scm_page = new_candidates[-1]
            break
        reusable_candidates = [
            candidate
            for candidate in candidates
            if urlsplit(candidate.url).path.startswith("/supplier/")
        ]
        if reusable_candidates:
            scm_page = reusable_candidates[-1]
            break
        await asyncio.sleep(0.5)
    if scm_page is None:
        raise AutomationError("点击“快麦通”后未打开快麦通页面")

    # ERP 会先打开 /account/erpVisit.json，再由该页建立 SCM 会话并跳到
    # /supplier/index。不能在中间页出现时就 goto 商品中心，否则会打断 SSO。
    last_path = ""
    sso_deadline = time.monotonic() + operation_timeout
    while time.monotonic() < sso_deadline:
        current_url = scm_page.url
        parsed = urlsplit(current_url)
        current_path = parsed.path
        if current_path != last_path:
            logger.info("快麦通 SSO 跳转：%s", current_path or "/")
            last_path = current_path
        if parsed.hostname in SCM_HOSTS and current_path.startswith("/supplier/"):
            break
        if parsed.hostname in SCM_HOSTS and current_path.startswith("/login"):
            raise AutomationError("ERP 单点登录未建立快麦通会话，页面被跳转到快麦通登录页")
        await asyncio.sleep(0.25)
    else:
        raise AutomationError(
            f"等待快麦通 SSO 完成超时（{operation_timeout} 秒），最后页面：{scm_page.url}"
        )

    await scm_page.wait_for_load_state("domcontentloaded", timeout=operation_timeout_ms)
    await wait_for_scm_api_session(scm_page, style_code, operation_timeout, logger)
    if auth_state_path is not None:
        await save_auth_state(context, auth_state_path, logger)

    center_url = center_url_for(scm_page.url)
    if scm_page.url.split("?", 1)[0] != center_url:
        await scm_page.goto(center_url, wait_until="domcontentloaded", timeout=operation_timeout_ms)
    center_deadline = time.monotonic() + operation_timeout
    while time.monotonic() < center_deadline:
        current_path = urlsplit(scm_page.url).path
        if current_path.startswith("/login"):
            raise AutomationError("快麦通 API 曾通过校验，但打开商品中心时会话失效并跳回登录页")
        if await visible(scm_page.get_by_text("商品中心", exact=True).first):
            break
        await asyncio.sleep(0.25)
    else:
        raise AutomationError(
            f"已从 ERP 打开快麦通，但商品中心在 {operation_timeout} 秒内未成功加载；"
            f"当前页面：{scm_page.url}"
        )
    if auth_state_path is not None:
        await save_auth_state(context, auth_state_path, logger)
    logger.info("已通过 ERP 单点登录进入快麦通商品中心")
    return scm_page


def find_records(payload: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            for record in records:
                if isinstance(record, dict):
                    yield record
        for value in payload.values():
            if isinstance(value, (dict, list)):
                yield from find_records(value)
    elif isinstance(payload, list):
        for value in payload:
            if isinstance(value, (dict, list)):
                yield from find_records(value)


async def api_find_product(page: Any, style_code: str, logger: logging.Logger) -> Optional[Dict[str, Any]]:
    """在已登录页面内调用快麦同源查询 API，自动携带登录 Cookie。"""
    body = {
        "pageNo": 1,
        "pageSize": 20,
        "outerIds": [style_code],
        "companyNames": [],
        "shopNames": [],
        "api_name": "item_base_page",
    }
    try:
        payload = await page.evaluate(
            """
            async (body) => {
              const response = await fetch('/item/base/page.json', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
              });
              const text = await response.text();
              let data = null;
              try { data = JSON.parse(text); } catch (_) {}
              return {ok: response.ok, status: response.status, data, text: text.slice(0, 500)};
            }
            """,
            body,
        )
    except Exception as exc:
        logger.warning("API 查询失败，将使用页面查询兜底：%s", exc)
        return None

    if not payload.get("ok") or not isinstance(payload.get("data"), dict):
        logger.warning("API 查询返回异常（HTTP %s），将使用 DOM 兜底", payload.get("status"))
        return None
    data = payload["data"]
    if int(data.get("result", 0) or 0) != 1:
        logger.warning("API 查询未成功：%s", data.get("message") or data.get("errmsg") or data.get("result"))
        return None

    matches = []
    for record in find_records(data.get("data")):
        outer_id = normalize_cell(record.get("outerId") or record.get("outerIds"))
        if outer_id == style_code:
            matches.append(record)
    if len(matches) > 1:
        raise AutomationError(f"API 查到 {len(matches)} 个款式编码 {style_code} 的商品，无法安全确定编辑对象")
    if matches:
        record = matches[0]
        logger.info(
            "API 已定位目标商品：outerId=%s, baseItemId=%s",
            style_code,
            record.get("baseItemId") or record.get("id"),
        )
        return record
    logger.warning("API 未找到款式编码 %s，将使用页面查询复核", style_code)
    return None


async def first_visible(locator: Any) -> Optional[Any]:
    for index in range(await locator.count()):
        item = locator.nth(index)
        if await visible(item):
            return item
    return None


async def open_product_editor(
    page: Any,
    style_code: str,
    logger: logging.Logger,
    timeout_seconds: int = 300,
) -> Any:
    timeout_ms = timeout_seconds * 1000
    center_url = center_url_for(page.url)
    if page.url.split("?", 1)[0] != center_url:
        await page.goto(center_url, wait_until="domcontentloaded", timeout=timeout_ms)

    style_input = None
    search_deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < search_deadline:
        style_input = await first_visible(page.locator('input[placeholder*="多个款式编码"]'))
        if style_input is None:
            # 有些账号显示的文案不含“多个”。
            style_input = await first_visible(page.locator('input[placeholder*="款式编码"]'))
        if style_input is not None:
            break
        await asyncio.sleep(0.25)
    if style_input is None:
        raise AutomationError(f"商品中心在 {timeout_seconds} 秒内未加载款式编码查询框")

    await style_input.fill(style_code)
    query_button = await first_visible(page.get_by_role("button", name=re.compile(r"^\s*查询\s*$")))
    if query_button is None:
        raise AutomationError("商品中心中找不到“查询”按钮")
    await query_button.click()

    rows = page.locator(".el-table__body-wrapper tbody tr").filter(has_text=style_code)
    try:
        await rows.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise AutomationError(f"页面查询后未找到款式编码 {style_code}") from exc

    visible_rows = []
    for index in range(await rows.count()):
        candidate = rows.nth(index)
        if await visible(candidate):
            visible_rows.append(candidate)
    if not visible_rows:
        raise AutomationError(f"找到了 {style_code} 的数据，但表格行不可见")
    if len(visible_rows) > 1:
        raise AutomationError(f"页面查到 {len(visible_rows)} 个款式编码 {style_code} 的商品，无法安全确定编辑对象")
    target_row = visible_rows[0]
    edit_link = await first_visible(target_row.get_by_text("编辑", exact=True))
    if edit_link is None:
        raise AutomationError(f"款式编码 {style_code} 所在行没有“编辑”入口")
    await edit_link.click()

    drawer = page.locator("#prod-center-edit-dialog")
    await drawer.wait_for(state="visible", timeout=timeout_ms)

    base_tab = await first_visible(drawer.get_by_text("基础资料", exact=True))
    if base_tab is not None:
        classes = await base_tab.get_attribute("class") or ""
        if "is-active" not in classes:
            await base_tab.click()
            await page.wait_for_timeout(500)

    logger.info("编辑抽屉已打开，等待基础资料加载完成（最多 %s 秒）", timeout_seconds)
    style_item = await form_item(drawer, "款式编码", timeout_seconds=timeout_seconds)
    style_input_in_drawer = style_item.locator("input").first
    ready_deadline = time.monotonic() + timeout_seconds
    last_value = ""
    while time.monotonic() < ready_deadline:
        try:
            last_value = (await style_input_in_drawer.input_value()).strip()
            loading_masks = await drawer.locator(".el-loading-mask:visible").count()
            if last_value == style_code and loading_masks == 0:
                break
        except Exception:
            pass
        await asyncio.sleep(0.25)
    else:
        raise AutomationError(
            f"基础资料在 {timeout_seconds} 秒内未加载完成："
            f"期望款式编码 {style_code}，页面值 {last_value!r}"
        )
    logger.info("已打开 %s 的基础资料编辑页，表单加载完成", style_code)
    return drawer


async def form_item(
    drawer: Any,
    label: str,
    timeout_seconds: float = 300,
    poll_interval: float = 0.25,
) -> Any:
    """等待慢速编辑表单实际渲染出目标字段，而不是只等待抽屉标题。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            items = drawer.locator(".el-form-item")
            for index in range(await items.count()):
                item = items.nth(index)
                labels = item.locator(":scope > .el-form-item__label, :scope > label.el-form-item__label")
                if not await labels.count():
                    continue
                text = (await labels.first.inner_text()).strip().rstrip("：:").strip()
                if text == label:
                    return item
        except Exception:
            # Vue 重新渲染表单时 locator 可能短暂失效，下一轮读取新 DOM。
            pass
        await asyncio.sleep(poll_interval)
    raise AutomationError(f"等待基础资料字段“{label}”加载超时（{timeout_seconds:g} 秒）")


async def fill_input(item: Any, value: str, label: str) -> Any:
    input_box = item.locator("input").first
    if not await input_box.count():
        raise AutomationError(f"字段“{label}”中找不到输入框")
    await item.scroll_into_view_if_needed()
    await input_box.fill(value)
    await input_box.press("Tab")
    actual = await input_box.input_value()
    if actual.strip() != value.strip():
        raise AutomationError(f"字段“{label}”填写后校验失败：{actual!r}")
    return input_box


async def delete_uploaded_images(scope: Any, page: Any) -> int:
    deleted = 0
    while True:
        images = scope.locator(".sc-upload .file-img")
        before = await images.count()
        if before == 0:
            return deleted

        image = images.first
        button = image.locator(".del-btn").first
        if not await button.count():
            return deleted

        # 快麦通仅在鼠标悬停图片时显示删除按钮。先走正常悬停交互；
        # 如果动画或遮罩层让按钮仍不可见，则在精确定位后触发同一 DOM click 事件。
        try:
            await image.hover(timeout=2000)
            if await visible(button):
                await button.click(timeout=2000)
            else:
                await button.evaluate("element => element.click()")
        except Exception:
            await button.evaluate("element => element.click()")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if await images.count() < before:
                break
            await asyncio.sleep(0.1)
        else:
            raise AutomationError("点击图片删除按钮后，页面未移除原图")
        deleted += 1


async def wait_for_image_uploads(
    scope: Any,
    expected: int,
    label: str,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_count = -1
    while time.monotonic() < deadline:
        count = await scope.locator(".sc-upload .file-img").count()
        if count != last_count:
            logging.getLogger("kuaimai_erp").info("%s上传进度：%s/%s", label, count, expected)
            last_count = count
        upload_errors = await scope.locator(".file-input.error-warp").count()
        uploading = await scope.get_by_text("上传中", exact=True).count()
        if upload_errors:
            raise AutomationError(f"{label}上传失败，页面显示 {upload_errors} 个错误位")
        if count == expected and uploading == 0:
            return
        await asyncio.sleep(0.5)
    raise AutomationError(f"{label}上传超时，已完成 {last_count}/{expected}")


async def replace_image_group(
    page: Any,
    item: Any,
    paths: Sequence[Path],
    label: str,
    timeout_seconds: int,
) -> None:
    await item.scroll_into_view_if_needed()
    deleted = await delete_uploaded_images(item, page)
    if deleted:
        logging.getLogger("kuaimai_erp").info("%s：已删除 %s 张原图", label, deleted)
    inputs = item.locator('input[type="file"]')
    if not await inputs.count():
        raise AutomationError(f"{label}区域找不到本地上传控件")
    await inputs.first.set_input_files([str(path) for path in paths])
    await wait_for_image_uploads(item, len(paths), label, timeout_seconds)


async def replace_sku_images(
    page: Any,
    item: Any,
    paths: Sequence[Path],
    timeout_seconds: int,
) -> int:
    await item.scroll_into_view_if_needed()
    first_spec = item.locator(".block-specification .specification-value").first
    slots = first_spec.locator(".specification-value-flex_img")
    slot_count = await slots.count()
    if slot_count == 0:
        raise AutomationError("商品规格中找不到 SKU 图上传位")
    if slot_count != len(paths):
        raise AutomationError(
            f"SKU 图数量（{len(paths)}）与第一规格值数量（{slot_count}）不一致，"
            "为避免图片和颜色错配，程序已停止。"
        )
    for index, path in enumerate(paths):
        slot = slots.nth(index)
        await delete_uploaded_images(slot, page)
        file_input = slot.locator('input[type="file"]')
        if not await file_input.count():
            raise AutomationError(f"第 {index + 1} 个 SKU 图位找不到本地上传控件")
        await file_input.first.set_input_files(str(path))
        await wait_for_image_uploads(slot, 1, f"SKU 图 {index + 1}", timeout_seconds)
    return slot_count


async def set_base_price(drawer: Any, price: str) -> Any:
    block = drawer.locator(".block-specification-list").first
    await block.scroll_into_view_if_needed()
    batch_items = block.locator(".sku-batch-item")
    price_item = None
    for index in range(await batch_items.count()):
        candidate = batch_items.nth(index)
        label = candidate.locator(".sku-batch-item_label")
        if await label.count() and "基本售价" in (await label.inner_text()):
            price_item = candidate
            break
    if price_item is None:
        raise AutomationError("规格明细中找不到“基本售价”批量输入框")

    input_box = price_item.locator("input").first
    await input_box.fill(price)
    await input_box.press("Tab")
    batch_button = await first_visible(block.get_by_role("button", name=re.compile(r"^\s*批量设置\s*$")))
    if batch_button is None:
        raise AutomationError("规格明细中找不到“批量设置”按钮")
    await batch_button.click()
    await asyncio.sleep(0.5)
    actual = await input_box.input_value()
    try:
        if Decimal(actual) != Decimal(price):
            raise AutomationError(f"基本售价校验失败：期望 {price}，页面为 {actual}")
    except InvalidOperation as exc:
        raise AutomationError(f"基本售价页面值无效：{actual!r}") from exc
    return input_box


async def collect_visible_errors(drawer: Any) -> List[str]:
    errors: List[str] = []
    locator = drawer.locator(".el-form-item__error:visible")
    for index in range(await locator.count()):
        text = (await locator.nth(index).inner_text()).strip()
        if text and text not in errors:
            errors.append(text)
    return errors


async def click_save_and_confirm(
    page: Any,
    drawer: Any,
    sync_erp: bool,
    timeout_seconds: int,
    logger: logging.Logger,
) -> Dict[str, Any]:
    save_responses: List[Any] = []

    def on_response(response: Any) -> None:
        if "/item/base/edit.json" in response.url:
            save_responses.append(response)

    page.on("response", on_response)
    save_buttons = drawer.locator(".drawer-footer button")
    save_button = None
    for index in range(await save_buttons.count()):
        candidate = save_buttons.nth(index)
        if not await visible(candidate):
            continue
        text = re.sub(r"\s+", "", (await candidate.inner_text()))
        if text == "保存":
            save_button = candidate
            break
    if save_button is None:
        raise AutomationError("商品编辑页找不到“保存”按钮")

    await save_button.scroll_into_view_if_needed()
    await save_button.click()
    logger.info("已点击“保存”，等待后端确认")
    started_at = time.monotonic()
    deadline = time.monotonic() + timeout_seconds
    sync_dialog_handled = False
    unbound_dialog_handled = False

    while time.monotonic() < deadline:
        success = page.locator(".el-message--success").filter(has_text="保存成功")
        if await visible(success.first):
            return {"result": 1, "confirmed_by": "toast"}

        if save_responses:
            response = save_responses[-1]
            try:
                payload = await response.json()
            except Exception:
                payload = {"http_status": response.status}
            if int(payload.get("result", 0) or 0) == 1:
                return {"result": 1, "confirmed_by": "api", "payload": payload}
            raise AutomationError(
                "保存接口返回失败："
                + str(payload.get("message") or payload.get("errmsg") or payload)
            )

        sync_dialog = page.locator(".el-dialog:visible").filter(has_text="以下商品将同步至系统资料")
        if not sync_dialog_handled and await visible(sync_dialog.first):
            action = "确 定" if sync_erp else "跳过同步"
            button = sync_dialog.first.get_by_role("button", name=re.compile(r"^\s*" + re.escape(action).replace(r"\ ", r"\s*") + r"\s*$"))
            if not await button.count() and sync_erp:
                button = sync_dialog.first.get_by_text("确 定", exact=True)
            if not await button.count():
                raise AutomationError(f"ERP 同步确认框中找不到“{action}”按钮")
            await button.first.click()
            sync_dialog_handled = True
            logger.info("ERP 同步确认：%s", "同步" if sync_erp else "跳过同步，仅保存快麦通资料")

        unbound_dialog = page.locator(".el-dialog:visible").filter(has_text="商品规格未关联ERP商品")
        if not unbound_dialog_handled and await visible(unbound_dialog.first):
            await unbound_dialog.first.get_by_role("button", name="我知道了").click()
            unbound_dialog_handled = True
            logger.warning("页面提示 SKU 未关联 ERP，已按页面流程继续保存")

        duplicate_dialog = page.locator(".el-message-box:visible").filter(has_text=re.compile("编码.*重复|存在SPU编码相同"))
        if await visible(duplicate_dialog.first):
            raise AutomationError("保存时出现编码重复确认框，程序未自动创建新编码，请人工复核")

        if time.monotonic() - started_at > 3:
            errors = await collect_visible_errors(drawer)
            if errors:
                raise AutomationError("保存被页面校验拦截：" + "；".join(errors))

        await asyncio.sleep(0.25)

    errors = await collect_visible_errors(drawer)
    if errors:
        raise AutomationError("保存超时，页面校验错误：" + "；".join(errors))
    warnings: List[str] = []
    warning_locator = page.locator(".el-message--warning:visible, .el-message--error:visible")
    for index in range(await warning_locator.count()):
        text = (await warning_locator.nth(index).inner_text()).strip()
        if text:
            warnings.append(text)
    suffix = "：" + "；".join(warnings) if warnings else ""
    raise AutomationError("等待保存成功超时" + suffix)


async def run_browser_automation(args: argparse.Namespace, product: ProductData, artifact_dir: Path, logger: logging.Logger) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise AutomationError("缺少 Playwright，请先运行：python3 -m pip install -r requirements.txt") from exc

    async with async_playwright() as playwright:
        remote_browser = None
        auth_state_path: Optional[Path] = None
        restored_scm_verified = False
        if args.cdp_url:
            remote_browser = await playwright.chromium.connect_over_cdp(args.cdp_url)
            if not remote_browser.contexts:
                raise AutomationError(f"CDP 浏览器没有可用上下文：{args.cdp_url}")
            context = remote_browser.contexts[0]
            page = next((p for p in context.pages if is_scm_url(p.url)), None)
            if page is None:
                page = await context.new_page()
        else:
            profile_dir = Path(args.user_data_dir).expanduser().resolve()
            profile_dir.mkdir(parents=True, exist_ok=True)
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=args.headless,
                no_viewport=True,
                args=["--start-maximized"],
            )
            auth_state_path = Path(args.auth_state).expanduser().resolve()
            await restore_auth_state(context, auth_state_path, logger)
            restored_scm_verified = saved_scm_state_is_verified(auth_state_path)
            page = context.pages[0] if context.pages else await context.new_page()

        page.set_default_timeout(args.timeout * 1000)
        try:
            reused_scm = False
            if restored_scm_verified or (args.cdp_url and is_scm_url(page.url)):
                try:
                    page = await try_reuse_verified_scm_session(
                        page,
                        product.style_code,
                        args.timeout,
                        logger,
                        auth_state_path=auth_state_path,
                    )
                    reused_scm = True
                except Exception as exc:
                    logger.info("已保存的快麦通会话不可复用，回退 ERP 单点登录：%s", exc)

            if not reused_scm:
                page = await enter_kuaimai_from_erp(
                    page,
                    args.login_timeout,
                    args.headless,
                    logger,
                    operation_timeout_seconds=args.timeout,
                    auth_state_path=auth_state_path,
                    style_code=product.style_code,
                )
            page.set_default_timeout(args.timeout * 1000)
            record = await api_find_product(page, product.style_code, logger)
            if record is None:
                logger.warning("继续使用 DOM 查询目标商品")

            drawer = await open_product_editor(
                page,
                product.style_code,
                logger,
                timeout_seconds=args.timeout,
            )

            style_item = await form_item(drawer, "款式编码", timeout_seconds=args.timeout)
            style_value = await style_item.locator("input").first.input_value()
            if style_value.strip() != product.style_code:
                raise AutomationError(
                    f"编辑页款式编码不匹配：期望 {product.style_code}，实际 {style_value}"
                )

            title_item = await form_item(drawer, "商品名称", timeout_seconds=args.timeout)
            title_input = await fill_input(title_item, product.title, "商品名称")
            logger.info("已填写商品名称")

            await replace_image_group(
                page,
                await form_item(drawer, "商品主图", timeout_seconds=args.timeout),
                product.main_images,
                "1:1 主图",
                args.upload_timeout,
            )
            await replace_image_group(
                page,
                await form_item(drawer, "3:4主图", timeout_seconds=args.timeout),
                product.main_images_34,
                "3:4 主图",
                args.upload_timeout,
            )
            await replace_image_group(
                page,
                await form_item(drawer, "商品详情图", timeout_seconds=args.timeout),
                product.detail_images,
                "商品详情图",
                args.upload_timeout,
            )
            sku_count = await replace_sku_images(
                page,
                await form_item(drawer, "商品规格", timeout_seconds=args.timeout),
                product.sku_images,
                args.upload_timeout,
            )
            logger.info("已替换 %s 张 SKU 图", sku_count)

            price_input = await set_base_price(drawer, product.base_price)
            logger.info("已将基本售价批量设置为 %s", product.base_price)

            # 保存前做一次关键值复核。
            if (await title_input.input_value()).strip() != product.title:
                raise AutomationError("保存前复核失败：商品名称发生变化")
            if Decimal(await price_input.input_value()) != Decimal(product.base_price):
                raise AutomationError("保存前复核失败：基本售价发生变化")
            errors = await collect_visible_errors(drawer)
            if errors:
                raise AutomationError("保存前页面存在校验错误：" + "；".join(errors))

            await safe_screenshot(page, artifact_dir / "before-save.png")
            if not args.save:
                logger.info("已完成填写与校验；--no-save 已启用，未点击保存")
                return

            result = await click_save_and_confirm(
                page,
                drawer,
                args.sync_erp,
                args.timeout,
                logger,
            )
            (artifact_dir / "save-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            await safe_screenshot(page, artifact_dir / "after-save.png")
            logger.info("保存成功（确认来源：%s）", result.get("confirmed_by"))
        except Exception:
            await safe_screenshot(page, artifact_dir / "error.png")
            raise
        finally:
            if args.cdp_url:
                # 不关闭用户自行启动的 CDP Chrome。
                pass
            else:
                await context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 Excel 和产品图片文件夹自动编辑快麦 ERP 基础资料")
    parser.add_argument("--excel-url", default=DEFAULT_EXCEL_URL, help="产品信息.xlsx 的 smb:// 或本地路径")
    parser.add_argument("--dry-run", action="store_true", help="只读取并校验 Excel/图片，不打开浏览器")
    parser.add_argument("--save", dest="save", action="store_true", default=True, help="最后点击保存（默认）")
    parser.add_argument("--no-save", dest="save", action="store_false", help="填写并校验，但不保存")
    parser.add_argument("--sync-erp", action="store_true", help="出现 ERP 同步确认框时选择同步；默认跳过同步")
    parser.add_argument("--headless", action="store_true", help="无头模式（仅适用于专用 Chrome 配置已登录）")
    parser.add_argument("--cdp-url", help="连接已开启远程调试的 Chrome，例如 http://127.0.0.1:9222")
    parser.add_argument(
        "--user-data-dir",
        default=str(Path(__file__).resolve().parent / "output/kuaimai/chrome-profile"),
        help="自动化专用 Chrome 用户数据目录",
    )
    parser.add_argument(
        "--auth-state",
        default=str(Path(__file__).resolve().parent / "output/kuaimai/auth-state.json"),
        help="会话登录状态保存文件",
    )
    parser.add_argument("--login-timeout", type=int, default=1200, help="首次手工登录等待秒数（默认 1200）")
    parser.add_argument("--timeout", type=int, default=300, help="页面操作/保存超时秒数（默认 300）")
    parser.add_argument("--upload-timeout", type=int, default=600, help="每组图片上传超时秒数（默认 600）")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    artifact_dir = Path(__file__).resolve().parent / "output/kuaimai/runs" / timestamp
    logger = setup_logging(artifact_dir)
    try:
        product = read_product_data(resolve_excel_path(args.excel_url))
        summary = product_summary(product)
        (artifact_dir / "input-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "输入校验通过：%s | 售价 %s | 1:1 %s 张 | 3:4 %s 张 | 详情 %s 张 | SKU %s 张",
            product.style_code,
            product.base_price,
            len(product.main_images),
            len(product.main_images_34),
            len(product.detail_images),
            len(product.sku_images),
        )
        if args.dry_run:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            logger.info("dry-run 完成，未打开浏览器")
            return 0
        asyncio.run(run_browser_automation(args, product, artifact_dir, logger))
        return 0
    except KeyboardInterrupt:
        logger.error("用户中止了程序")
        return 130
    except AutomationError as exc:
        logger.error("%s", exc)
        return 2
    except Exception:
        logger.exception("程序出现未预期错误")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
