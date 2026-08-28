"""快麦通抖音资料页的安全字段适配器。

类目属性优先使用页面实际调用的同源接口返回值做唯一性校验，
但仍通过可见 DOM 选项完成选择和回读。接口未观测到或返回异常时，
仅回退到当前字段内的 DOM 候选项，不做模糊匹配。
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from sync_validation import matches_any, sequences_match, split_or_values


# 2026-08-27 在真实抖音资料页选择预测类目后观测到的精确路径。
# 请勿放宽为 list/query 等通用子串，避免误解析其他业务接口。
CATEGORY_PROPERTIES_ENDPOINT = "/fxg/getCategoryProperties.json"
CATEGORY_PROPERTIES_QUIET_SECONDS = 0.2
CATEGORY_PROPERTIES_TIMEOUT_SECONDS = 2.0
SHOP_INFO_ENDPOINT = "/shop/info.json"
DISTRIBUTION_CONFIG_ENDPOINT = "/dsb/queryDistributionConfig.json"
SIZE_NAME_PATTERN = re.compile(
    r"^(?:XS|S|M|L|X{1,6}L|\d{1,2}XL)$",
    re.IGNORECASE,
)
SIZE_FIELD_LABELS = ("身高(cm)", "体重(斤)", "腰围(cm)", "臀围(cm)", "裤长(cm)")


class DouyinListingError(RuntimeError):
    """可直接向用户展示的抖音资料填写异常。"""


def normalize_option(value: object) -> str:
    """将选项文案规范化，仅忽略不影响业务含义的排版符号。"""
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    return re.sub(r"[\s，,;；、・·\-_（）()]", "", text).casefold()


def choose_unique_option(
    expected: str,
    options: Sequence[Mapping[str, str]],
) -> Mapping[str, str]:
    """仅在规范化后恰好有一个同名选项时返回它。"""
    normalized = normalize_option(expected)
    matches = [item for item in options if normalize_option(item.get("name", "")) == normalized]
    if len(matches) != 1:
        names = "、".join(str(item.get("name", "")) for item in options) or "<无>"
        raise DouyinListingError(
            f"选项 {expected!r} 匹配数为 {len(matches)}；候选：{names}"
        )
    return matches[0]


def _normalize_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    return re.sub(r"[\s*:：]+", "", text).casefold()


def _excel_aliases(value: object) -> Tuple[str, ...]:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    aliases = []
    for part in text.split("/"):
        normalized = _normalize_label(part)
        if not normalized:
            continue
        aliases.append(normalized)
        historical_alias = {"裤门禁": "裤门襟"}.get(normalized)
        if historical_alias:
            aliases.append(_normalize_label(historical_alias))
    return tuple(dict.fromkeys(aliases))


def _normalize_size_name(value: object) -> Optional[str]:
    normalized = re.sub(
        r"\s+",
        "",
        unicodedata.normalize("NFKC", "" if value is None else str(value)),
    ).upper()
    return normalized if SIZE_NAME_PATTERN.fullmatch(normalized) else None


def _form_number(value: object) -> str:
    """将 OCR 数值转为页面需要的紧凑文本。"""
    if isinstance(value, bool):
        raise DouyinListingError("尺码推荐数值不能是布尔值")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _decimal(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise DouyinListingError(f"{label}不是有效数字：{value!r}") from exc
    if not number.is_finite():
        raise DouyinListingError(f"{label}不是有效数字：{value!r}")
    return number


def _shop_aliases(item: Mapping[str, Any]) -> Tuple[str, ...]:
    aliases = []
    for key in ("title", "priorityTitle", "nick", "name"):
        text = str(item.get(key) or "").strip()
        if text and normalize_option(text) not in {normalize_option(value) for value in aliases}:
            aliases.append(text)
    return tuple(aliases)


def _freight_api_data(
    shop_payload: Mapping[str, Any],
    config_payload: Mapping[str, Any],
) -> Tuple[Tuple[Mapping[str, Any], ...], Mapping[str, Tuple[Mapping[str, str], ...]]]:
    """只保留运费匹配所需的店铺 ID/名称和模板 ID/名称。"""
    shop_data = shop_payload.get("data")
    raw_shops = shop_data.get("list") if isinstance(shop_data, Mapping) else None
    config_data = config_payload.get("data")
    raw_templates = (
        config_data.get("templateList") if isinstance(config_data, Mapping) else None
    )
    if not isinstance(raw_shops, list) or not isinstance(raw_templates, list):
        raise DouyinListingError("运费接口返回结构不完整")

    shops: List[Mapping[str, Any]] = []
    for raw_shop in raw_shops:
        if not isinstance(raw_shop, Mapping) or raw_shop.get("id") is None:
            continue
        aliases = _shop_aliases(raw_shop)
        if aliases:
            shops.append({"id": str(raw_shop["id"]), "aliases": aliases})

    templates: Dict[str, List[Mapping[str, str]]] = {}
    for raw_template in raw_templates:
        if not isinstance(raw_template, Mapping) or raw_template.get("shopId") is None:
            continue
        name = str(raw_template.get("templateName") or "").strip()
        if not name:
            continue
        option = {
            "id": str(raw_template.get("templateId") or ""),
            "name": name,
        }
        bucket = templates.setdefault(str(raw_template["shopId"]), [])
        if (option["id"], normalize_option(option["name"])) not in {
            (item["id"], normalize_option(item["name"])) for item in bucket
        }:
            bucket.append(option)
    return tuple(shops), {key: tuple(value) for key, value in templates.items()}


async def _size_header_label(header: Any) -> str:
    """读取 Element UI 表头的第一行字段名，忽略后续规则提示。"""
    label_lines = header.locator(":scope > .cell > p")
    for index in range(await label_lines.count()):
        text = (await label_lines.nth(index).inner_text()).strip()
        if text:
            return text
    for line in (await header.inner_text()).splitlines():
        text = line.strip()
        if text:
            return text
    return ""


class DouyinListing:
    """抖音资料页的类目、属性和面料填写适配器。"""

    def __init__(self, page: Any, drawer: Any, logger: Any, artifact_dir: Path) -> None:
        self.page = page
        self.drawer = drawer
        self.logger = logger
        self.artifact_dir = Path(artifact_dir)
        self.panel: Optional[Any] = None
        self._category_properties: Dict[str, Tuple[Mapping[str, Any], ...]] = {}
        self._property_response_tasks: set[asyncio.Task[Any]] = set()
        self._property_request_generations: Dict[int, Tuple[int, str]] = {}
        self._property_pending_requests: Dict[int, set[int]] = {}
        self._property_leaf_ids: Dict[int, set[str]] = {}
        self._property_last_activity = 0.0
        self._property_generation = 0
        self._property_capture_generation: Optional[int] = None
        self._category_properties_generation: Optional[int] = None
        self._category_properties_leaf_id: Optional[str] = None
        self._category_properties_seen = asyncio.Event()
        self._response_listener_installed = False

    def _install_property_response_listener(self) -> None:
        if self._response_listener_installed or not hasattr(self.page, "on"):
            return

        def on_request(request: Any) -> None:
            if not self._is_category_properties_url(getattr(request, "url", "")):
                return
            generation = self._property_capture_generation
            leaf_id = self._request_leaf_category_id(request)
            if generation is None or leaf_id is None:
                return
            request_id = id(request)
            self._property_request_generations[request_id] = (generation, leaf_id)
            self._property_pending_requests.setdefault(generation, set()).add(request_id)
            self._property_leaf_ids.setdefault(generation, set()).add(leaf_id)
            self._mark_property_activity(generation)

        def on_response(response: Any) -> None:
            if not self._is_category_properties_response(response):
                return
            request = getattr(response, "request", None)
            request_context = self._property_request_generations.pop(id(request), None)
            if request_context is None:
                # Without request-time correlation, a late response could belong to
                # the previous category and must not populate the active cache.
                return
            generation, leaf_id = request_context
            self._complete_property_request(generation, id(request))
            task = asyncio.create_task(
                self._consume_category_properties(response, generation, leaf_id)
            )
            self._property_response_tasks.add(task)
            task.add_done_callback(self._property_response_tasks.discard)

        def on_request_failed(request: Any) -> None:
            request_id = id(request)
            request_context = self._property_request_generations.pop(request_id, None)
            if request_context is not None:
                generation, _leaf_id = request_context
                self._complete_property_request(generation, request_id)

        self.page.on("request", on_request)
        self.page.on("response", on_response)
        self.page.on("requestfailed", on_request_failed)
        self._response_listener_installed = True

    def _mark_property_activity(self, generation: int) -> None:
        if self._property_capture_generation != generation:
            return
        self._property_last_activity = asyncio.get_running_loop().time()

    def _complete_property_request(self, generation: int, request_id: int) -> None:
        pending = self._property_pending_requests.get(generation)
        if pending is not None:
            pending.discard(request_id)
        self._mark_property_activity(generation)

    def _is_category_properties_url(self, raw_url: object) -> bool:
        try:
            response_url = urlsplit(str(raw_url))
            page_url = urlsplit(self.page.url)
        except Exception:
            return False
        if response_url.path != CATEGORY_PROPERTIES_ENDPOINT:
            return False
        if page_url.scheme not in {"http", "https"}:
            return False
        return (response_url.scheme, response_url.netloc) == (page_url.scheme, page_url.netloc)

    def _is_category_properties_response(self, response: Any) -> bool:
        return self._is_category_properties_url(getattr(response, "url", ""))

    @staticmethod
    def _request_leaf_category_id(request: Any) -> Optional[str]:
        try:
            values = parse_qs(str(request.post_data or ""), keep_blank_values=True).get(
                "leafCategoryId", []
            )
        except Exception:
            return None
        if len(values) != 1 or not str(values[0]).strip():
            return None
        return str(values[0]).strip()

    async def _consume_category_properties(
        self,
        response: Any,
        generation: int,
        leaf_category_id: str,
    ) -> None:
        if self._property_capture_generation != generation or not leaf_category_id:
            return
        try:
            payload = await response.json()
            # Parsing yields control; a newer category may have started meanwhile.
            if self._property_capture_generation != generation:
                return
            if not isinstance(payload, dict) or int(payload.get("result", 0) or 0) != 1:
                return
            data = payload.get("data")
            if not isinstance(data, list):
                return

            records: Dict[str, List[Mapping[str, Any]]] = {}
            for raw_record in data:
                if not isinstance(raw_record, dict):
                    continue
                label = str(raw_record.get("propertyName") or "").strip()
                if not label:
                    continue
                raw_options = raw_record.get("options", [])
                if isinstance(raw_options, str):
                    try:
                        raw_options = json.loads(raw_options)
                    except (TypeError, ValueError):
                        raw_options = []
                options = []
                if isinstance(raw_options, list):
                    for raw_option in raw_options:
                        if not isinstance(raw_option, dict) or raw_option.get("name") is None:
                            continue
                        options.append(
                            {
                                "name": str(raw_option["name"]),
                                "id": str(raw_option.get("value") or raw_option.get("id") or ""),
                            }
                        )
                records.setdefault(_normalize_label(label), []).append(
                    {
                        "name": label,
                        "type": str(raw_record.get("type") or ""),
                        "id": str(raw_record.get("propertyId") or ""),
                        "options": tuple(options),
                    }
                )
            if records and self._property_capture_generation == generation:
                if (
                    self._category_properties_leaf_id is not None
                    and self._category_properties_leaf_id != leaf_category_id
                ):
                    # More than one leaf response in one click cannot be correlated
                    # safely to the final visible category, so force DOM fallback.
                    self._invalidate_property_capture(generation)
                    return
                self._category_properties = {
                    label: tuple(items) for label, items in records.items()
                }
                self._category_properties_generation = generation
                self._category_properties_leaf_id = leaf_category_id
                self._category_properties_seen.set()
        except Exception as exc:
            self.logger.warning("抖音类目属性接口解析失败，将使用 DOM 选项：%s", exc)

    async def _drain_property_response_tasks(self) -> None:
        # response 回调可能还需一个 event-loop tick 才入队。
        await asyncio.sleep(0)
        while self._property_response_tasks:
            tasks = tuple(self._property_response_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

    async def _begin_category_property_capture(self) -> int:
        self._install_property_response_listener()
        await self._drain_property_response_tasks()
        previous_generation = self._property_capture_generation
        if previous_generation is not None:
            self._invalidate_property_capture(previous_generation)
        self._property_generation += 1
        generation = self._property_generation
        self._property_capture_generation = generation
        self._category_properties = {}
        self._category_properties_generation = None
        self._category_properties_leaf_id = None
        self._category_properties_seen = asyncio.Event()
        self._property_pending_requests[generation] = set()
        self._property_leaf_ids[generation] = set()
        self._property_last_activity = asyncio.get_running_loop().time()
        return generation

    def _invalidate_property_capture(self, generation: int) -> None:
        if self._property_capture_generation != generation:
            return
        self._property_capture_generation = None
        self._category_properties = {}
        self._category_properties_generation = None
        self._category_properties_leaf_id = None
        self._category_properties_seen.set()
        self._property_pending_requests.pop(generation, None)
        self._property_leaf_ids.pop(generation, None)

    async def _wait_for_fresh_category_properties(self, generation: int) -> bool:
        if urlsplit(self.page.url).scheme not in {"http", "https"}:
            self._invalidate_property_capture(generation)
            return False
        if self._property_capture_generation != generation:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CATEGORY_PROPERTIES_TIMEOUT_SECONDS
        while loop.time() < deadline:
            await self._drain_property_response_tasks()
            if self._property_capture_generation != generation:
                return False

            pending = self._property_pending_requests.get(generation, set())
            if not pending:
                leaf_ids = self._property_leaf_ids.get(generation, set())
                if len(leaf_ids) > 1:
                    self._invalidate_property_capture(generation)
                    return False
                fresh = (
                    self._category_properties_generation == generation
                    and bool(self._category_properties)
                    and self._category_properties_leaf_id is not None
                    and leaf_ids == {self._category_properties_leaf_id}
                )
                quiet_for = loop.time() - self._property_last_activity
                if fresh and quiet_for >= CATEGORY_PROPERTIES_QUIET_SECONDS:
                    # Keep the verified cache, but stop tagging later unrelated requests.
                    self._property_capture_generation = None
                    self._property_pending_requests.pop(generation, None)
                    self._property_leaf_ids.pop(generation, None)
                    return True

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.02, remaining))

        self._invalidate_property_capture(generation)
        return False

    async def _wait_for_loading_masks(self, timeout_seconds: float = 30) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self.drawer.locator(".el-loading-mask:visible").count() == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.05)
        raise DouyinListingError("抖音资料加载遮罩在 30 秒内未消失")

    async def _wait_for_field(self, label: str, timeout_seconds: float = 30) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        wanted = _normalize_label(label)
        while asyncio.get_running_loop().time() < deadline:
            scope = self.panel or self.drawer
            try:
                items = scope.locator(".el-form-item")
                for index in range(await items.count()):
                    item = items.nth(index)
                    labels = item.locator(
                        ":scope > .el-form-item__label, :scope > label.el-form-item__label"
                    )
                    if not await labels.count():
                        continue
                    if (
                        _normalize_label(await labels.first.inner_text()) == wanted
                        and await item.is_visible()
                    ):
                        return item
            except Exception:
                # Vue 切换类目时会短暂销毁子树，下一轮重新取 locator。
                pass
            await asyncio.sleep(0.05)
        raise DouyinListingError(f"等待抖音字段“{label}”加载超时")

    async def open(self) -> "DouyinListing":
        """切换到抖音资料页，并等待延迟渲染和遮罩结束。"""
        self._install_property_response_listener()
        tab = self.drawer.get_by_role("tab", name="抖音资料", exact=True)
        try:
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click()
        except Exception as exc:
            raise DouyinListingError("找不到可切换的“抖音资料”页签") from exc

        panel = self.drawer.get_by_role("tabpanel", name="抖音资料", exact=True)
        try:
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise DouyinListingError("抖音资料表单未渲染") from exc
        self.panel = panel
        await self._wait_for_loading_masks()
        await self._wait_for_field("商品标题")
        await self._wait_for_field("导购短标题")
        await self._drain_property_response_tasks()
        return self

    async def refresh_prediction_results(
        self,
        action_labels: Sequence[str] = ("立即生成", "刷新预测结果"),
    ) -> str:
        """让平台按当前资料生成预测，并等待异步结果稳定。"""
        if self.panel is None:
            raise DouyinListingError("请先调用 open() 打开抖音资料")

        action_control = None
        action_text = ""
        # 新款未生成过抖音资料时显示“立即生成”；已有预测
        # 的商品可能显示“刷新预测结果”。两者都不是固定必现元素。
        for label in action_labels:
            candidates = self.panel.get_by_text(label, exact=True)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if await candidate.is_visible():
                    action_control = candidate
                    action_text = label
                    break
            if action_control is not None:
                break
        if action_control is None:
            raise DouyinListingError(
                "基础资料或抖音商品标题已更新，但页面找不到"
                "“立即生成”或“刷新预测结果”，"
                "为避免使用旧类目已停止"
            )

        await action_control.scroll_into_view_if_needed()
        await action_control.click()
        if self.logger is not None:
            self.logger.info("基础资料已变更，已点击抖音“%s”", action_text)

        if action_text != "立即生成":
            # 刷新控件没有稳定的完成文案，留出请求发起和 DOM
            # 替换时间，再以遮罩消失和推荐列表稳定作为完成信号。
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(0.25)
        await self._wait_for_loading_masks()

        buttons = self.panel.get_by_role("button", name="点击使用", exact=True)
        try:
            await buttons.first.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise DouyinListingError("刷新后抖音页面没有可用的预测类目") from exc

        # 预测结果可能分批替换 DOM；连续两次读取一致后再继续。
        previous = None
        stable_reads = 0
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            current_values = []
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                if not await button.is_visible():
                    continue
                current_values.append(
                    re.sub(
                        r"\s+",
                        " ",
                        (await button.locator("xpath=..").inner_text()).strip(),
                    )
                )
            current = tuple(current_values)
            if current and current == previous:
                stable_reads += 1
                if stable_reads >= 2:
                    return action_text
            else:
                previous = current
                stable_reads = 0
            await asyncio.sleep(0.25)
        raise DouyinListingError("刷新后抖音预测类目未稳定")

    async def prepare_product_title_and_predictions(
        self,
        product_title: str,
        *,
        force_refresh: bool = False,
    ) -> Mapping[str, Any]:
        """同步抖音商品标题，必要时刷新动态预测。"""
        before = (await self.read_field_values("商品标题"))[0]
        title = await self.fill_text_field("商品标题", product_title)
        title_changed = before != title
        prediction_actions = []
        if force_refresh or title_changed:
            recommendations = self.panel.get_by_role(
                "button", name="点击使用", exact=True
            )
            has_recommendation = False
            for index in range(await recommendations.count()):
                if await recommendations.nth(index).is_visible():
                    has_recommendation = True
                    break
            if not has_recommendation:
                first_action = await self.refresh_prediction_results(("立即生成",))
                prediction_actions.append(first_action)
                # 自动生成可能同时改写标题，Excel 仍是最终权威值。
                title = await self.fill_text_field("商品标题", product_title)
            elif self.logger is not None:
                self.logger.info("页面已有当前商品的预测类目，跳过重复生成")
        elif self.logger is not None:
            self.logger.info("基础资料与抖音商品标题均未变更，跳过重复预测")
        return {
            "product_title": title,
            "product_title_changed": title_changed,
            "prediction_refreshed": bool(prediction_actions),
            "prediction_actions": prediction_actions,
        }

    async def _category_text(self) -> str:
        item = await self._wait_for_field("商品分类")
        display = item.locator(".platform-category-input").first
        if not await display.count():
            display = item.locator(".category").first
        if not await display.count():
            raise DouyinListingError("抖音商品分类区域中找不到已选类目")
        try:
            direct_text = await display.evaluate(
                """element => Array.from(element.childNodes)
                    .filter(node => node.nodeType === Node.TEXT_NODE)
                    .map(node => node.textContent || '')
                    .join(' ').trim()"""
            )
        except Exception:
            direct_text = ""
        if direct_text:
            return str(direct_text).strip()
        text = (await display.inner_text()).strip()
        return text.split("同步平台类目", 1)[0].strip()

    async def apply_first_recommended_category(self) -> str:
        """使用第一个平台预测类目，并校验最终可见路径。"""
        if self.panel is None:
            raise DouyinListingError("请先调用 open() 打开抖音资料")
        buttons = self.panel.get_by_role("button", name="点击使用", exact=True)
        try:
            await buttons.first.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise DouyinListingError("抖音页面没有可用的预测类目") from exc

        button = buttons.first
        row = button.locator("xpath=ancestor::*[contains(@class, 'prediction-item')][1]")
        if not await row.count():
            row = button.locator("xpath=..")
        row_text = (await row.inner_text()).strip()
        expected = re.sub(r"(?:^推荐\s*|点击使用\s*$)", "", row_text).strip()
        if not expected:
            raise DouyinListingError("无法读取第一个抖音预测类目")

        try:
            current = await self._category_text()
        except DouyinListingError:
            current = ""
        normalize_path = lambda value: re.sub(r"[\s>]", "", value).casefold()
        if current and normalize_path(current) == normalize_path(expected):
            if self.logger is not None:
                self.logger.info("抖音商品分类已匹配，跳过重复应用：%s", current)
            return current

        generation = await self._begin_category_property_capture()
        try:
            await button.click()
        except Exception:
            self._invalidate_property_capture(generation)
            raise
        await self._wait_for_loading_masks()
        # 类目完成后类目属性会重新渲染，用它作为稳定信号。
        try:
            await self.panel.locator(".attr-item > .el-form-item").first.wait_for(
                state="visible", timeout=30_000
            )
        except Exception:
            # 某些测试/类目可能只有基础信息，最终类目回读仍是权威校验。
            pass
        await self._wait_for_fresh_category_properties(generation)

        actual = await self._category_text()
        if normalize_path(actual) != normalize_path(expected):
            raise DouyinListingError(
                f"抖音类目应用后校验失败：期望 {expected!r}，页面为 {actual!r}"
            )
        return actual

    async def fill_short_title(self, value: str) -> str:
        """按“导购短标题”标签填写、失焦并精确回读。"""
        item = await self._wait_for_field("导购短标题")
        input_box = item.locator("input").first
        if not await input_box.count():
            raise DouyinListingError("导购短标题中找不到输入框")
        await item.scroll_into_view_if_needed()
        current = await input_box.input_value()
        if current == str(value):
            if self.logger is not None:
                self.logger.info("导购短标题已匹配，跳过填写")
            return current
        await input_box.fill(str(value))
        await input_box.press("Tab")
        actual = await input_box.input_value()
        if actual != str(value):
            raise DouyinListingError(
                f"导购短标题填写后校验失败：期望 {value!r}，页面为 {actual!r}"
            )
        return actual

    async def _plain_text_field_controls(self, label: str) -> List[Tuple[Any, Any]]:
        """返回指定标签下的直接文本输入控件，排除 AI 摘要中的同名标签。"""
        if self.panel is None:
            raise DouyinListingError("请先调用 open() 打开抖音资料")
        wanted = _normalize_label(label)
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            controls: List[Tuple[Any, Any]] = []
            items = self.panel.locator(".el-form-item")
            for index in range(await items.count()):
                item = items.nth(index)
                if not await item.is_visible():
                    continue
                labels = item.locator(
                    ":scope > .el-form-item__label, :scope > label.el-form-item__label"
                )
                if not await labels.count():
                    continue
                if _normalize_label(await labels.first.inner_text()) != wanted:
                    continue
                inputs = item.locator(
                    ":scope > .el-form-item__content > input, "
                    ":scope > .el-form-item__content > .el-input input"
                )
                for input_index in range(await inputs.count()):
                    input_box = inputs.nth(input_index)
                    if await input_box.is_visible() and not await input_box.evaluate(
                        "element => Boolean(element.closest('.el-select'))"
                    ):
                        controls.append((item, input_box))
            if controls:
                return controls
            await asyncio.sleep(0.05)
        raise DouyinListingError(f"抖音字段“{label}”找不到可见文本输入控件")

    async def read_plain_text_field(self, label: str) -> str:
        controls = await self._plain_text_field_controls(label)
        values = [(await input_box.input_value()).strip() for _item, input_box in controls]
        nonempty = [value for value in values if value]
        if len(set(nonempty)) == 1:
            return nonempty[0]
        if len(controls) == 1:
            return values[0]
        raise DouyinListingError(
            f"抖音字段“{label}”文本输入控件无法唯一确定：{values!r}"
        )

    async def fill_text_field(self, label: str, value: object) -> str:
        """填写抖音页中不属于动态类目属性区的普通文本字段。"""
        expected = str(value)
        controls = await self._plain_text_field_controls(label)
        matching = []
        writable = []
        for item, input_box in controls:
            current = (await input_box.input_value()).strip()
            if current == expected:
                matching.append((item, input_box, current))
            if not await input_box.is_disabled() and await input_box.get_attribute("readonly") is None:
                writable.append((item, input_box, current))
        if matching:
            if self.logger is not None:
                self.logger.info("抖音字段“%s”已匹配，跳过填写", label)
            return matching[0][2]
        if len(writable) == 1:
            item, input_box, current = writable[0]
        elif len(controls) == 1:
            item, input_box = controls[0]
            current = (await input_box.input_value()).strip()
        else:
            raise DouyinListingError(
                f"抖音字段“{label}”可写文本控件无法唯一确定"
            )
        if await input_box.is_disabled() or await input_box.get_attribute("readonly") is not None:
            raise DouyinListingError(
                f"抖音字段“{label}”当前为只读，且页面值 {current!r} "
                f"与 Excel {expected!r} 不一致"
            )
        await item.scroll_into_view_if_needed()
        await input_box.fill(expected)
        await input_box.press("Tab")
        actual = (await input_box.input_value()).strip()
        if actual != expected:
            raise DouyinListingError(
                f"抖音字段“{label}”填写后校验失败：期望 {expected!r}，页面为 {actual!r}"
            )
        return actual

    async def _attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        if self.panel is None:
            raise DouyinListingError("请先调用 open() 打开抖音资料")
        result: Dict[str, Tuple[str, Any]] = {}
        items = self.panel.locator(".attr-item > .el-form-item")
        for index in range(await items.count()):
            item = items.nth(index)
            label_locator = item.locator(":scope > .el-form-item__label")
            if not await label_locator.count():
                continue
            label = (await label_locator.first.inner_text()).strip().lstrip("*").rstrip("：:").strip()
            normalized = _normalize_label(label)
            if not normalized or normalized == _normalize_label("面料材质"):
                continue
            if normalized in result:
                raise DouyinListingError(f"抖音页面出现重复属性标签：{label}")
            result[normalized] = (label, item)
        return result

    async def _attribute_item(self, label: str) -> Any:
        items = await self._attribute_items()
        item = items.get(_normalize_label(label))
        if item is None:
            raise DouyinListingError(f"当前抖音类目中找不到属性“{label}”")
        return item[1]

    async def _api_property(self, label: str) -> Optional[Mapping[str, Any]]:
        await self._drain_property_response_tasks()
        if self._category_properties_generation is None:
            return None
        records = self._category_properties.get(_normalize_label(label))
        if not records:
            return None
        if len(records) != 1:
            names = "、".join(str(record.get("name", "")) for record in records)
            raise DouyinListingError(f"属性“{label}”接口定义不唯一：{names}")
        return records[0]

    async def _open_select(self, select: Any, *, multi: bool) -> None:
        if multi:
            search = select.locator(".el-select__tags input.el-select__input").first
            if await search.count():
                await search.click(timeout=4000)
                return
        input_box = select.locator("input.el-input__inner").first
        if not await input_box.count():
            raise DouyinListingError("属性下拉框中找不到可点击输入框")
        await input_box.click(timeout=4000)

    async def _dismiss_select_dropdown(self, select: Any) -> None:
        """关闭未选中值的下拉层，避免它遮住下一个属性控件。"""
        inputs = select.locator("input")
        if await inputs.count():
            try:
                await inputs.first.press("Escape", timeout=2000)
            except Exception:
                await self.page.keyboard.press("Escape")
        else:
            await self.page.keyboard.press("Escape")
        await asyncio.sleep(0.1)
        if await self.page.locator(".el-select-dropdown:visible").count():
            label = select.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
                "' el-form-item ')][1]/*[contains(concat(' ', normalize-space(@class), ' '), "
                "' el-form-item__label ')]"
            )
            if await label.count():
                await label.first.click(force=True, timeout=2000)
        deadline = asyncio.get_running_loop().time() + 1.5
        while asyncio.get_running_loop().time() < deadline:
            if not await self.page.locator(".el-select-dropdown:visible").count():
                return
            await asyncio.sleep(0.05)
        raise DouyinListingError("未匹配属性值的下拉层无法关闭")

    async def _active_select_dropdown(self, select: Any) -> Optional[Any]:
        linked_ids: List[str] = []
        controllers = select.locator("input[aria-controls], input[aria-owns]")
        for index in range(await controllers.count()):
            controller = controllers.nth(index)
            for attribute in ("aria-controls", "aria-owns"):
                value = await controller.get_attribute(attribute) or ""
                for identifier in value.split():
                    if identifier and identifier not in linked_ids:
                        linked_ids.append(identifier)

        linked = []
        valid_linked_count = 0
        for identifier in linked_ids:
            candidate = self.page.locator(f"[id={json.dumps(identifier)}]")
            if await candidate.count() != 1:
                continue
            if "el-select-dropdown" not in (await candidate.get_attribute("class") or ""):
                continue
            valid_linked_count += 1
            if await candidate.is_visible():
                linked.append(candidate)
        if len(linked) == 1:
            return linked[0]
        if len(linked) > 1:
            raise DouyinListingError("当前属性关联了多个可见下拉框，无法安全选择")
        if valid_linked_count:
            # 已有确定关联时等它完成展开，不误用其他残留 popper。
            return None

        local = select.locator(".el-select-dropdown:visible")
        if await local.count() == 1:
            return local.first
        if await local.count() > 1:
            raise DouyinListingError("当前属性内部出现多个可见下拉框，无法安全选择")

        # Element UI 默认会把 popper portal 到 body。若没有 aria 关联，只接受
        # 唯一可见 popper，或 z-index 唯一最高的活动 popper。
        global_dropdowns = self.page.locator(".el-select-dropdown:visible")
        count = await global_dropdowns.count()
        if count == 0:
            return None
        if count == 1:
            return global_dropdowns.first

        ranked = []
        for index in range(count):
            dropdown = global_dropdowns.nth(index)
            z_index = await dropdown.evaluate(
                """element => {
                    const value = Number.parseInt(getComputedStyle(element).zIndex, 10);
                    return Number.isFinite(value) ? value : 0;
                }"""
            )
            ranked.append((int(z_index), index, dropdown))
        highest = max(item[0] for item in ranked)
        top = [item for item in ranked if item[0] == highest]
        if len(top) != 1:
            raise DouyinListingError(
                f"页面同时存在 {count} 个可见属性下拉框，且活动层级不唯一"
            )
        return top[0][2]

    async def _visible_dom_options(
        self,
        select: Any,
        timeout_seconds: float = 5,
    ) -> Tuple[Any, List[Mapping[str, str]]]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            dropdown = await self._active_select_dropdown(select)
            if dropdown is None:
                await asyncio.sleep(0.05)
                continue
            # 在浏览器进程中一次取完，避免数百个候选逐项跨进程 inner_text。
            result = await dropdown.evaluate(
                """element => Array.from(
                    element.querySelectorAll('.el-select-dropdown__item')
                  ).map((option, index) => ({
                    name: (option.innerText || '').trim(),
                    id: '',
                    index: String(index),
                    disabled: option.classList.contains('is-disabled'),
                    visible: option.getClientRects().length > 0
                      && getComputedStyle(option).display !== 'none'
                      && getComputedStyle(option).visibility !== 'hidden'
                  })).filter(item => item.name && !item.disabled && item.visible)
                """
            )
            if result:
                return dropdown, result
            await asyncio.sleep(0.05)
        raise DouyinListingError("打开属性下拉框后未找到可见选项")

    async def _clear_multi_select(self, select: Any) -> None:
        while True:
            close_buttons = select.locator(".el-select__tags .el-tag__close")
            before = await close_buttons.count()
            if before == 0:
                return
            await close_buttons.first.click()
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                if await close_buttons.count() < before:
                    break
                await asyncio.sleep(0.02)
            else:
                raise DouyinListingError("清空属性多选值失败")

    async def _read_select_values(self, select: Any, *, multi: bool) -> Tuple[str, ...]:
        if multi:
            tags = select.locator(".el-select__tags .el-tag")
            values = []
            for index in range(await tags.count()):
                text = (await tags.nth(index).inner_text()).strip()
                if text:
                    values.append(text)
            return tuple(values)
        input_box = select.locator("input.el-input__inner").first
        return ((await input_box.input_value()).strip(),)

    async def _select_values(
        self,
        select: Any,
        expected_values: Sequence[str],
        *,
        label: str,
        multi: bool,
        api_options: Optional[Sequence[Mapping[str, str]]] = None,
    ) -> Tuple[str, ...]:
        current = await self._read_select_values(select, multi=multi)
        if sequences_match(current, expected_values, normalize_option):
            if self.logger is not None:
                self.logger.info("抖音属性“%s”已匹配，跳过选择", label)
            return current
        if multi:
            await self._clear_multi_select(select)
        for expected in expected_values:
            if api_options:
                # API 只作为优先索引；远程搜索型下拉的初始接口可能只返回
                # 部分候选，零匹配时继续搜索真实 DOM，重名仍立即报错。
                api_matches = [
                    item
                    for item in api_options
                    if normalize_option(item.get("name", "")) == normalize_option(expected)
                ]
                if len(api_matches) > 1:
                    choose_unique_option(expected, api_options)
            if self.logger is not None:
                self.logger.info("抖音属性“%s”：准备展开下拉", label)
            await self._open_select(select, multi=multi)
            if self.logger is not None:
                self.logger.info("抖音属性“%s”：下拉已展开，读取 DOM 候选", label)
            dropdown, dom_options = await self._visible_dom_options(select)
            if self.logger is not None:
                self.logger.info(
                    "抖音属性“%s”：读取到 %s 个 DOM 候选",
                    label,
                    len(dom_options),
                )
            matches = [
                item
                for item in dom_options
                if normalize_option(item.get("name", "")) == normalize_option(expected)
            ]
            if len(matches) > 1:
                await self._dismiss_select_dropdown(select)
                choose_unique_option(expected, dom_options)
            chosen = matches[0] if matches else None

            if chosen is None:
                # 部分 Element UI 下拉初始只渲染基础候选；必须像人工操作
                # 一样逐字输入，才会触发远程搜索/allow-create 候选。
                search = select.locator(
                    "input.el-select__input:not([readonly]), "
                    "input.el-input__inner:not([readonly])"
                ).first
                if await search.count():
                    # 输入事件必须在浏览器进程内原子完成：Vue 会在收到
                    # input 后立即重建搜索框，跨进程连续操作会握着旧节点卡住。
                    typed = await select.evaluate(
                        """async (element, value) => {
                          const input = Array.from(element.querySelectorAll('input'))
                            .find(node => !node.hasAttribute('readonly'));
                          if (!input) return false;
                          input.focus();
                          input.dispatchEvent(new CompositionEvent('compositionstart', {
                            bubbles: true,
                            data: ''
                          }));
                          const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value'
                          ).set;
                          setter.call(input, value);
                          input.dispatchEvent(new CompositionEvent('compositionupdate', {
                            bubbles: true,
                            data: value
                          }));
                          input.dispatchEvent(new InputEvent('input', {
                            bubbles: true,
                            inputType: 'insertText',
                            data: value,
                            isComposing: true
                          }));
                          input.dispatchEvent(new CompositionEvent('compositionend', {
                            bubbles: true,
                            data: value
                          }));
                          input.dispatchEvent(new Event('change', {bubbles: true}));
                          const component = element.__vue__;
                          if (component && component.filterable) {
                            component.query = value;
                            if (typeof component.handleQueryChange === 'function') {
                              component.handleQueryChange(value);
                            }
                            if (typeof component.$nextTick === 'function') {
                              await new Promise(resolve => component.$nextTick(resolve));
                            }
                          }
                          return true;
                        }""",
                        expected,
                    )
                    if not typed:
                        await self._dismiss_select_dropdown(select)
                        choose_unique_option(expected, dom_options)
                    search_deadline = asyncio.get_running_loop().time() + 3
                    while asyncio.get_running_loop().time() < search_deadline:
                        await asyncio.sleep(0.1)
                        try:
                            dropdown, dom_options = await self._visible_dom_options(
                                select,
                                timeout_seconds=0.35,
                            )
                        except DouyinListingError:
                            continue
                        matches = [
                            item
                            for item in dom_options
                            if normalize_option(item.get("name", ""))
                            == normalize_option(expected)
                        ]
                        if len(matches) > 1:
                            await self._dismiss_select_dropdown(select)
                            choose_unique_option(expected, dom_options)
                        if matches:
                            chosen = matches[0]
                            if self.logger is not None:
                                self.logger.info(
                                    "抖音属性“%s”：搜索后加载精确候选 %s",
                                    label,
                                    expected,
                                )
                            break
            if chosen is None:
                await self._dismiss_select_dropdown(select)
                choose_unique_option(expected, dom_options)
            if self.logger is not None:
                self.logger.info("抖音属性“%s”：已精确定位候选 %s", label, expected)
            # Vue 会在滚动长列表时重建 option 节点；在活动下拉容器中
            # 原子地按已唯一确定的索引重新取节点并点击，避免持有失效 Locator。
            clicked = await dropdown.evaluate(
                """(element, index) => {
                    const option = element.querySelectorAll(
                      '.el-select-dropdown__item'
                    )[index];
                    if (!option || option.classList.contains('is-disabled')) return false;
                    option.scrollIntoView({block: 'nearest'});
                    option.click();
                    return true;
                }""",
                int(chosen["index"]),
            )
            if not clicked:
                raise DouyinListingError(f"属性“{label}”的精确候选节点已失效")
            if self.logger is not None:
                self.logger.info("抖音属性“%s”：候选点击完成", label)

        actual = await self._read_select_values(select, multi=multi)
        expected_counter = Counter(normalize_option(value) for value in expected_values)
        actual_counter = Counter(normalize_option(value) for value in actual)
        if actual_counter != expected_counter:
            raise DouyinListingError(
                f"属性选择后校验失败：期望 {list(expected_values)!r}，页面为 {list(actual)!r}"
            )
        if multi:
            await self._dismiss_select_dropdown(select)
        if self.logger is not None:
            self.logger.info("抖音属性“%s”：回读完成", label)
        return actual

    async def fill_attribute(self, label: str, expected: str) -> Tuple[str, ...]:
        """在指定属性内精确选择；斜杠表示从左到右的 OR 候选。"""
        item = await self._attribute_item(label)
        await item.scroll_into_view_if_needed()
        selects = item.locator(":scope > .el-form-item__content > .el-select")
        if not await selects.count():
            selects = item.locator(".el-select")

        # 克重等字段由“数值输入框 + 单位下拉框”组成，不能把 430g
        # 当作下拉选项。只匹配表单项内容直属的独立输入控件，排除
        # el-select 内部用于搜索的 input。
        standalone_inputs = item.locator(
            ":scope > .el-form-item__content > .el-input input:not([readonly])"
        )
        if await standalone_inputs.count() != 1:
            standalone_inputs = item.locator(
                ".measure-wrap input.el-input__inner:not([readonly])"
            )
        compound_match = re.fullmatch(
            r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([^\d\s]+)\s*",
            str(expected),
        )
        if await standalone_inputs.count() == 1 and await selects.count() and compound_match:
            number, unit = compound_match.groups()
            input_box = standalone_inputs.first
            current_number = (await input_box.input_value()).strip()
            current_unit = await self._read_select_values(selects.first, multi=False)
            try:
                number_matches = _decimal(current_number, label) == _decimal(number, label)
            except DouyinListingError:
                number_matches = False
            if number_matches and matches_any(current_unit, (unit,), normalize_option):
                if self.logger is not None:
                    self.logger.info("抖音属性“%s”已匹配，跳过填写", label)
                return (f"{current_number}{current_unit[0]}",)
            await input_box.fill(number)
            await input_box.press("Tab")
            actual_number = (await input_box.input_value()).strip()
            if actual_number != number:
                raise DouyinListingError(
                    f"属性“{label}”数值填写后校验失败：期望 {number!r}，页面为 {actual_number!r}"
                )
            select = selects.first
            actual_unit = await self._select_values(
                select,
                (unit,),
                label=f"{label}单位",
                multi=False,
            )
            return (f"{actual_number}{actual_unit[0]}",)

        if await selects.count():
            select = selects.first
            multi = await select.locator(".el-select__tags").count() > 0
            alternatives = split_or_values(expected)
            if not alternatives:
                raise DouyinListingError(f"属性“{label}”期望值为空")

            current = await self._read_select_values(select, multi=multi)
            if matches_any(current, alternatives, normalize_option):
                if self.logger is not None:
                    self.logger.info("抖音属性“%s”已匹配，跳过选择", label)
                return current

            api_property = await self._api_property(label)
            api_options: Optional[Sequence[Mapping[str, str]]] = None
            if api_property and api_property.get("options"):
                api_options = api_property["options"]  # type: ignore[assignment]

            # “/”是 OR。抖音允许搜索创建值，但必须先检查所有 OR 候选
            # 中是否已有平台选项；例如 A/B 中 A 不存在、B 已存在时选 B，
            # 不能抢先创建 A。API 缺失时再读取初始 DOM 候选。
            existing_names = {
                normalize_option(option.get("name", ""))
                for option in (api_options or ())
            }
            existing_alternatives = [
                value
                for value in alternatives
                if normalize_option(value) in existing_names
            ]
            if len(alternatives) > 1 and not existing_alternatives:
                await self._open_select(select, multi=multi)
                _dropdown, dom_options = await self._visible_dom_options(select)
                await self._dismiss_select_dropdown(select)
                dom_names = {
                    normalize_option(option.get("name", ""))
                    for option in dom_options
                }
                existing_alternatives = [
                    value
                    for value in alternatives
                    if normalize_option(value) in dom_names
                ]

            chosen_expected = (
                existing_alternatives[0]
                if existing_alternatives
                else alternatives[0]
            )
            if existing_alternatives and self.logger is not None:
                self.logger.info(
                    "抖音属性“%s”：OR 候选优先使用平台已有值 %s",
                    label,
                    chosen_expected,
                )
            return await self._select_values(
                select,
                (chosen_expected,),
                label=label,
                multi=multi,
                api_options=api_options,
            )

        inputs = item.locator(":scope > .el-form-item__content input:not([readonly])")
        if not await inputs.count():
            inputs = item.locator("input:not([readonly])")
        if await inputs.count() != 1:
            raise DouyinListingError(
                f"属性“{label}”不是唯一的单选、多选或文本输入控件"
            )
        input_box = inputs.first
        current = await input_box.input_value()
        if current == str(expected):
            if self.logger is not None:
                self.logger.info("抖音属性“%s”已匹配，跳过填写", label)
            return (current,)
        await input_box.fill(str(expected))
        await input_box.press("Tab")
        actual = await input_box.input_value()
        if actual != str(expected):
            raise DouyinListingError(
                f"属性“{label}”填写后校验失败：期望 {expected!r}，页面为 {actual!r}"
            )
        return (actual,)

    async def read_field_values(self, label: str) -> Tuple[str, ...]:
        """只读回查一个已渲染字段，供保存后重新打开页面核验。"""
        item = await self._wait_for_field(label)
        await item.scroll_into_view_if_needed()

        selects = item.locator(":scope > .el-form-item__content > .el-select")
        visible_selects = []
        for index in range(await selects.count()):
            candidate = selects.nth(index)
            if await candidate.is_visible():
                visible_selects.append(candidate)

        standalone_inputs = item.locator(
            ":scope > .el-form-item__content > .el-input input:not([readonly]):not([disabled]), "
            ":scope > .el-form-item__content > input:not([readonly]):not([disabled])"
        )
        visible_inputs = []
        for index in range(await standalone_inputs.count()):
            candidate = standalone_inputs.nth(index)
            if await candidate.is_visible() and not await candidate.evaluate(
                "element => Boolean(element.closest('.el-select'))"
            ):
                visible_inputs.append(candidate)

        # 货号等字段可能在平台自动生成后变为只读，仍要可回读校验。
        if not visible_inputs:
            readonly_inputs = item.locator(":scope > .el-form-item__content input")
            for index in range(await readonly_inputs.count()):
                candidate = readonly_inputs.nth(index)
                if await candidate.is_visible() and not await candidate.evaluate(
                    "element => Boolean(element.closest('.el-select'))"
                ):
                    visible_inputs.append(candidate)

        # 克重的数值和单位嵌套在 measure-wrap 的内层表单中，不是外层
        # el-form-item__content 的直属子节点。
        if not visible_inputs:
            measure_inputs = item.locator(
                ".measure-wrap input.el-input__inner:not([readonly]):not([disabled])"
            )
            for index in range(await measure_inputs.count()):
                candidate = measure_inputs.nth(index)
                if await candidate.is_visible() and not await candidate.evaluate(
                    "element => Boolean(element.closest('.el-select'))"
                ):
                    visible_inputs.append(candidate)
        if not visible_selects:
            measure_selects = item.locator(".measure-wrap .el-select")
            for index in range(await measure_selects.count()):
                candidate = measure_selects.nth(index)
                if await candidate.is_visible():
                    visible_selects.append(candidate)

        if len(visible_selects) == 1 and len(visible_inputs) == 1:
            number = (await visible_inputs[0].input_value()).strip()
            unit = await self._read_select_values(visible_selects[0], multi=False)
            return (f"{number}{unit[0]}",)
        if len(visible_selects) == 1:
            select = visible_selects[0]
            multi = await select.locator(".el-select__tags").count() > 0
            return await self._read_select_values(select, multi=multi)
        if len(visible_selects) > 1:
            raise DouyinListingError(
                f"保存后字段“{label}”可见下拉框匹配数为 {len(visible_selects)}"
            )
        if len(visible_inputs) != 1:
            raise DouyinListingError(
                f"保存后字段“{label}”可见输入框匹配数为 {len(visible_inputs)}"
            )
        return ((await visible_inputs[0].input_value()).strip(),)

    async def verify_persisted_values(
        self,
        category: object,
        product_title: object,
        short_title: object,
        attributes: Mapping[str, Sequence[object]],
        price: object,
        spot_stock: object,
        presale_stock: object,
    ) -> Mapping[str, Any]:
        """重新打开商品后，只读核对关键抖音字段和每一行 SKU 数值。"""
        expected_category = str(category).strip()
        actual_category = await self._category_text()
        normalize_path = lambda value: re.sub(r"[\s>]", "", str(value)).casefold()
        if normalize_path(actual_category) != normalize_path(expected_category):
            raise DouyinListingError(
                f"保存后商品分类不一致：期望 {expected_category!r}，页面为 {actual_category!r}"
            )

        actual_product_title = (await self.read_field_values("商品标题"))[0]
        if actual_product_title != str(product_title):
            raise DouyinListingError(
                f"保存后商品标题不一致：期望 {product_title!r}，页面为 {actual_product_title!r}"
            )

        actual_short_title = (await self.read_field_values("导购短标题"))[0]
        if actual_short_title != str(short_title):
            raise DouyinListingError(
                f"保存后导购短标题不一致：期望 {short_title!r}，页面为 {actual_short_title!r}"
            )

        actual_attributes: Dict[str, Tuple[str, ...]] = {}
        for label, expected_values in attributes.items():
            expected = tuple(str(value) for value in expected_values)
            if _normalize_label(str(label)) == _normalize_label("货号"):
                actual = (await self.read_plain_text_field(str(label)),)
            else:
                actual = await self.read_field_values(str(label))
            if Counter(normalize_option(value) for value in actual) != Counter(
                normalize_option(value) for value in expected
            ):
                raise DouyinListingError(
                    f"保存后字段“{label}”不一致：期望 {list(expected)!r}，页面为 {list(actual)!r}"
                )
            actual_attributes[str(label)] = actual

        expected_sku = {
            "价格": _decimal(price, "价格"),
            "现货库存": _decimal(spot_stock, "现货库存"),
            "预售库存": _decimal(presale_stock, "预售库存"),
        }
        controls = await self._sku_input_controls()
        actual_sku = []
        for row_index, row_controls in enumerate(controls, start=1):
            row_values = {}
            for label, input_box in row_controls.items():
                actual = _decimal(
                    await input_box.input_value(),
                    f"保存后第 {row_index} 个 SKU {label}",
                )
                if actual != expected_sku[label]:
                    raise DouyinListingError(
                        f"保存后第 {row_index} 个 SKU 的“{label}”不一致："
                        f"期望 {expected_sku[label]}，页面为 {actual}"
                    )
                row_values[label] = format(actual, "f")
            actual_sku.append(row_values)

        return {
            "category": actual_category,
            "product_title": actual_product_title,
            "short_title": actual_short_title,
            "attributes": actual_attributes,
            "sku": actual_sku,
        }

    async def apply_category_and_fields(self, fields: Any) -> Mapping[str, Any]:
        """应用类目、短标题和当前类目页面实际存在的 Excel 属性。"""
        category = await self.apply_first_recommended_category()
        page_items = await self._attribute_items()

        excel_attributes = list(fields.attributes.items())
        goods_code_sources = [
            (key, value)
            for key, value in excel_attributes
            if {
                _normalize_label("货号"),
                _normalize_label("商家外部编码"),
            }.intersection(_excel_aliases(key))
        ]
        if len(goods_code_sources) > 1:
            keys = "、".join(str(key) for key, _value in goods_code_sources)
            raise DouyinListingError(f"抖音货号匹配到多个 Excel 字段：{keys}")
        goods_code = None
        if goods_code_sources:
            _key, goods_code_value = goods_code_sources[0]
            goods_code = await self.fill_text_field("货号", goods_code_value)

        target_sources: Dict[str, List[Tuple[object, object]]] = {}
        unmatched: List[str] = []
        for key, value in excel_attributes:
            if goods_code_sources and key == goods_code_sources[0][0]:
                continue
            aliases = set(_excel_aliases(key))
            matches = [
                (normalized_label, page_label)
                for normalized_label, (page_label, _item) in page_items.items()
                if normalized_label in aliases
            ]
            if not matches:
                unmatched.append(str(key))
                continue
            # Excel 以斜杠明确列出的多个页面名称代表共用同一值；
            # 例如“里料材质成分含量/材质成分含量”需要两处都填。
            for normalized_label, _page_label in matches:
                target_sources.setdefault(normalized_label, []).append((key, value))

        if unmatched and self.logger is not None:
            self.logger.info(
                "当前抖音类目无对应页面字段，已跳过：%s",
                "、".join(unmatched),
            )

        assignments: List[Tuple[str, object]] = []
        for normalized_label, sources in target_sources.items():
            page_label = page_items[normalized_label][0]
            if len(sources) > 1:
                keys = "、".join(str(key) for key, _value in sources)
                raise DouyinListingError(
                    f"抖音属性“{page_label}”匹配到多个 Excel 字段：{keys}"
                )
            _key, value = sources[0]
            assignments.append((page_label, value))

        # Mapping is fully validated before any title/attribute value is written.
        short_title = await self.fill_short_title(fields.short_title)
        applied: Dict[str, Tuple[str, ...]] = {}
        if goods_code is not None:
            applied["货号"] = (goods_code,)
        skipped_values: Dict[str, str] = {}
        for page_label, value in assignments:
            if self.logger is not None:
                self.logger.info("正在填写抖音属性：%s", page_label)
            try:
                applied[page_label] = await asyncio.wait_for(
                    self.fill_attribute(page_label, str(value)),
                    timeout=12,
                )
            except asyncio.TimeoutError as exc:
                raise DouyinListingError(
                    f"抖音属性“{page_label}”填写或回读超过 12 秒"
                ) from exc
            except DouyinListingError as exc:
                # 平台类目的下拉候选可能比产品表窄。无精确候选时不猜测，
                # 保留到铺货前报告；重名/歧义及控件异常仍立即终止。
                if "匹配数为 0" not in str(exc):
                    raise
                skipped_values[page_label] = str(value)
                if self.logger is not None:
                    self.logger.info(
                        "抖音属性“%s”无平台精确候选，已跳过 Excel 值：%s",
                        page_label,
                        value,
                    )
        return {
            "category": category,
            "short_title": short_title,
            "attributes": applied,
            "skipped_attributes": unmatched,
            "skipped_values": skipped_values,
        }

    async def _wait_row_count(self, rows: Any, expected: int) -> None:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if await rows.count() == expected:
                return
            await asyncio.sleep(0.05)
        raise DouyinListingError(f"面料材质行数未变为 {expected}")

    async def _percentage_input(self, row: Any) -> Any:
        preferred = row.locator(".el-input.el-input-digit > input.el-input__inner")
        if await preferred.count():
            return preferred.first
        inputs = row.locator("input:not([readonly])")
        for index in range(await inputs.count()):
            candidate = inputs.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
        raise DouyinListingError("面料材质行中找不到百分比输入框")

    async def apply_materials(
        self,
        materials: Sequence[Any],
        wash_label_paths: Sequence[Path],
    ) -> Tuple[Tuple[str, int], ...]:
        """同步水洗标，并将面料行数、选项和百分比精确对齐。"""
        if not materials:
            raise DouyinListingError("面料材质不能为空")
        percentages = []
        for component in materials:
            try:
                percentage = int(component.percentage)
            except (TypeError, ValueError) as exc:
                raise DouyinListingError("面料百分比必须是整数") from exc
            if not 0 <= percentage <= 100:
                raise DouyinListingError("面料百分比必须在 0 到 100 之间")
            percentages.append(percentage)
        if sum(percentages) != 100:
            raise DouyinListingError(
                f"面料百分比合计必须为 100，当前为 {sum(percentages)}"
            )

        summary_item = await self._wait_for_field("面料材质")
        from kuaimai_erp import sync_image_group

        upload_scope = summary_item
        if not await upload_scope.locator('input[type="file"]').count():
            if self.panel is None:
                raise DouyinListingError("抖音资料面板尚未打开")
            wash_label = self.panel.get_by_text("水洗标/吊牌图", exact=True)
            if await wash_label.count() != 1:
                raise DouyinListingError("水洗标/吊牌图标签不是唯一项")
            # 真实页面把上传组件放在“面料材质”表单项的相邻子块中；
            # 从可见业务标签向上寻找最近且包含 file input 的共同容器。
            upload_scope = wash_label.locator(
                'xpath=ancestor::*[.//input[@type="file"]][1]'
            )
            if await upload_scope.count() != 1:
                raise DouyinListingError("水洗标/吊牌图附近找不到唯一上传区域")
            if self.artifact_dir is not None:
                markup = await upload_scope.evaluate("element => element.outerHTML")
                (self.artifact_dir / "wash-label-scope.html").write_text(
                    markup,
                    encoding="utf-8",
                )

        await sync_image_group(
            self.page,
            upload_scope,
            tuple(Path(path) for path in wash_label_paths),
            "抖音水洗标/吊牌图",
            300,
        )

        if self.panel is None:
            raise DouyinListingError("抖音资料面板尚未打开")
        add_candidates = self.panel.locator(
            "xpath=.//*[contains(normalize-space(.), '添加材质') and "
            "not(.//*[contains(normalize-space(.), '添加材质')])]"
        )
        visible_add_buttons = []
        for index in range(await add_candidates.count()):
            candidate = add_candidates.nth(index)
            text = re.sub(r"\s+", "", await candidate.inner_text())
            if text in {"添加材质", "+添加材质"} and await candidate.is_visible():
                visible_add_buttons.append(candidate)
        if len(visible_add_buttons) != 1:
            raise DouyinListingError(
                f"面料材质区域的“添加材质”入口数量为 {len(visible_add_buttons)}"
            )
        add_button = visible_add_buttons[0]
        material_scope = add_button.locator(
            "xpath=ancestor::*[.//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' el-select ')] and .//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' el-icon-delete ')]][1]"
        )
        if await material_scope.count() != 1:
            raise DouyinListingError("无法从“添加材质”定位唯一材质编辑区域")

        if self.artifact_dir is not None:
            markup = await material_scope.evaluate("element => element.outerHTML")
            (self.artifact_dir / "material-scope.html").write_text(
                markup,
                encoding="utf-8",
            )

        rows = material_scope.locator(
            "xpath=.//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' el-icon-delete ')]/ancestor::*[.//*[contains(concat(' ', "
            "normalize-space(@class), ' '), ' el-select ')]][1]"
        )
        while await rows.count() < len(materials):
            before = await rows.count()
            await add_button.click()
            await self._wait_row_count(rows, before + 1)
        while await rows.count() > len(materials):
            before = await rows.count()
            delete_button = rows.nth(before - 1).locator(".el-icon-delete")
            if not await delete_button.count():
                raise DouyinListingError("多余面料行中找不到删除按钮")
            await delete_button.click()
            await self._wait_row_count(rows, before - 1)

        actual: List[Tuple[str, int]] = []
        for index, component in enumerate(materials):
            row = rows.nth(index)
            selects = row.locator(".el-select")
            if not await selects.count():
                raise DouyinListingError(f"第 {index + 1} 个面料行中找不到材质下拉框")
            selected = await self._select_values(
                selects.first,
                (str(component.name),),
                label=f"面料材质第 {index + 1} 行",
                multi=False,
            )
            percent_input = await self._percentage_input(row)
            raw_percentage = (await percent_input.input_value()).strip()
            if raw_percentage != str(percentages[index]):
                await percent_input.fill(str(percentages[index]))
                await percent_input.press("Tab")
                raw_percentage = (await percent_input.input_value()).strip()
            elif self.logger is not None:
                self.logger.info(
                    "面料材质第 %s 行百分比已匹配，跳过填写", index + 1
                )
            try:
                page_percentage = int(raw_percentage)
            except ValueError as exc:
                raise DouyinListingError(
                    f"第 {index + 1} 个面料百分比回读失败：{raw_percentage!r}"
                ) from exc
            if page_percentage != percentages[index]:
                raise DouyinListingError(
                    f"第 {index + 1} 个面料百分比校验失败："
                    f"期望 {percentages[index]}，页面为 {page_percentage}"
                )
            actual.append((selected[0], page_percentage))

        if sum(percentage for _name, percentage in actual) != 100:
            raise DouyinListingError("面料百分比页面回读合计不是 100")
        return tuple(actual)

    async def _size_table(self) -> Any:
        if self.panel is None:
            raise DouyinListingError("请先调用 open() 打开抖音资料")

        expected_headers = {_normalize_label(label) for label in SIZE_FIELD_LABELS}

        async def matching_tables(selector: str) -> List[Any]:
            matches: List[Any] = []
            candidates = self.panel.locator(selector)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    headers = candidate.locator(
                        ":scope > .el-table__header-wrapper thead th"
                    )
                    header_texts = {
                        _normalize_label(await _size_header_label(header))
                        for header in [
                            headers.nth(header_index)
                            for header_index in range(await headers.count())
                        ]
                    }
                except Exception:
                    continue
                if expected_headers.issubset(header_texts):
                    matches.append(candidate)
            return matches

        matches = await matching_tables(".el-table")
        if len(matches) != 1:
            raise DouyinListingError(
                f"尺码推荐表匹配数为 {len(matches)}，无法安全填写"
            )
        return matches[0]

    async def _size_rows(self, table: Any) -> Any:
        rows = table.locator(":scope > .el-table__body-wrapper tbody > tr")
        if not await rows.count():
            rows = table.locator("tbody > tr")
        if not await rows.count():
            raise DouyinListingError("尺码推荐表中没有可填写行")
        return rows

    async def _row_size_name(self, row: Any, row_number: int) -> str:
        values = set()
        raw_values: List[str] = []

        row_attribute = await row.get_attribute("data-size")
        if row_attribute:
            raw_values.append(row_attribute)

        preferred = row.locator(".size-name, [data-size]")
        for index in range(await preferred.count()):
            candidate = preferred.nth(index)
            attribute = await candidate.get_attribute("data-size")
            text = attribute or (await candidate.inner_text())
            if text:
                raw_values.append(text)

        # 真实 Element UI 表格的尺码通常是某个 .cell 的独立文本。
        if not raw_values:
            cells = row.locator(":scope > td")
            for index in range(await cells.count()):
                text = (await cells.nth(index).inner_text()).strip()
                if text:
                    raw_values.extend(part for part in text.splitlines() if part.strip())

        for raw_value in raw_values:
            size = _normalize_size_name(raw_value)
            if size is not None:
                values.add(size)
        if len(values) != 1:
            visible = "、".join(value.strip() for value in raw_values if value.strip()) or "<空>"
            raise DouyinListingError(
                f"尺码推荐第 {row_number} 行无法唯一识别尺码：{visible}"
            )
        return next(iter(values))

    async def _size_header_indexes(self, table: Any) -> Dict[str, int]:
        # Element UI 会在 .el-table__fixed-right 中复制一整套表头；
        # 只使用主表头，否则每列都会被误判为重复。
        headers = table.locator(":scope > .el-table__header-wrapper thead th")
        by_label: Dict[str, List[int]] = {}
        for index in range(await headers.count()):
            label = _normalize_label(await _size_header_label(headers.nth(index)))
            by_label.setdefault(label, []).append(index)

        result: Dict[str, int] = {}
        for label in SIZE_FIELD_LABELS:
            matches = by_label.get(_normalize_label(label), [])
            if len(matches) != 1:
                raise DouyinListingError(
                    f"尺码推荐列“{label}”匹配数为 {len(matches)}"
                )
            result[label] = matches[0]
        return result

    async def _size_field_inputs(
        self,
        row: Any,
        header_indexes: Mapping[str, int],
        size: str,
    ) -> Tuple[Any, ...]:
        cells = row.locator(":scope > td")
        controls = []
        for label in SIZE_FIELD_LABELS:
            column_index = header_indexes[label]
            if await cells.count() <= column_index:
                raise DouyinListingError(f"尺码 {size} 缺少“{label}”单元格")
            inputs = cells.nth(column_index).locator("input:not([readonly])")
            if await inputs.count() != 1:
                raise DouyinListingError(
                    f"尺码 {size} 的“{label}”输入框匹配数为 {await inputs.count()}"
                )
            controls.append(inputs.first)
        return tuple(controls)

    async def fill_size_recommendations(
        self,
        recommendations: Sequence[Any],
    ) -> Mapping[str, Tuple[str, ...]]:
        """按尺码文本填写身高、体重、腰围、臀围和裤长。

        所有尺码行与五列输入框先完成预检，只有页面与识别结果的
        尺码集合完全一致才开始写入，避免半张表被修改。
        """
        by_size: Dict[str, Any] = {}
        for item in recommendations:
            size = _normalize_size_name(getattr(item, "size", None))
            if size is None:
                raise DouyinListingError(
                    f"识别结果包含无效尺码：{getattr(item, 'size', None)!r}"
                )
            if size in by_size:
                raise DouyinListingError(f"识别结果包含重复尺码：{size}")
            by_size[size] = item
        if not by_size:
            raise DouyinListingError("尺码推荐识别结果为空")

        table = await self._size_table()
        await table.scroll_into_view_if_needed()
        rows = await self._size_rows(table)
        header_indexes = await self._size_header_indexes(table)

        page_rows: Dict[str, Any] = {}
        duplicate_sizes = []
        for index in range(await rows.count()):
            row = rows.nth(index)
            size = await self._row_size_name(row, index + 1)
            if size in page_rows:
                duplicate_sizes.append(size)
            else:
                page_rows[size] = row

        expected_sizes = set(by_size)
        page_sizes = set(page_rows)
        missing_sizes = sorted(expected_sizes - page_sizes)
        unexpected_sizes = sorted(page_sizes - expected_sizes)
        problems = []
        if duplicate_sizes:
            problems.append("重复：" + "、".join(sorted(set(duplicate_sizes))))
        if missing_sizes:
            problems.append("缺少：" + "、".join(missing_sizes))
        if unexpected_sizes:
            problems.append("意外：" + "、".join(unexpected_sizes))
        if problems:
            raise DouyinListingError("尺码推荐行无法唯一匹配（" + "；".join(problems) + "）")

        # 先确认每行五个控件都唯一存在，再做任何 fill。
        controls_by_size: Dict[str, Tuple[Any, ...]] = {}
        expected_values: Dict[str, Tuple[str, ...]] = {}
        for size, row in page_rows.items():
            item = by_size[size]
            controls_by_size[size] = await self._size_field_inputs(
                row, header_indexes, size
            )
            expected_values[size] = (
                f"{_form_number(item.height_min)}-{_form_number(item.height_max)}",
                f"{_form_number(item.weight_min)}-{_form_number(item.weight_max)}",
                _form_number(item.waist),
                _form_number(item.hip),
                _form_number(item.length),
            )

        actual: Dict[str, Tuple[str, ...]] = {}
        for size, controls in controls_by_size.items():
            for control, value in zip(controls, expected_values[size]):
                if (await control.input_value()).strip() != value:
                    await control.fill(value)
                    await control.press("Tab")
            row_values_list = []
            for control in controls:
                row_values_list.append((await control.input_value()).strip())
            row_values = tuple(row_values_list)
            if row_values != expected_values[size]:
                raise DouyinListingError(
                    f"尺码 {size} 回读校验失败：期望 {expected_values[size]}，"
                    f"页面为 {row_values}"
                )
            actual[size] = row_values
        return actual

    async def sync_douyin_images(
        self,
        main_images: Sequence[Path],
        main_images_34: Sequence[Path],
        detail_images: Sequence[Path],
        timeout_seconds: int = 300,
    ) -> Mapping[str, str]:
        """分别同步抖音 1:1 主图、3:4 主图和详情图。

        共享的 ``sync_image_group`` 按用户确认的数量规则决定跳过或
        整组重传；这里只保留调用方已按文件名自然排序的顺序。
        """
        if self.panel is None:
            raise DouyinListingError("请先调用 open() 打开抖音资料")
        from kuaimai_erp import sync_image_group

        groups = (
            (
                "main",
                "主图",
                tuple(Path(path) for path in main_images),
                "抖音 1:1 主图",
            ),
            (
                "main_34",
                "主图3:4",
                tuple(Path(path) for path in main_images_34),
                "抖音 3:4 主图",
            ),
            (
                "details",
                "商品详情图",
                tuple(Path(path) for path in detail_images),
                "抖音商品详情图",
            ),
        )
        results: Dict[str, str] = {}
        for key, field_label, paths, log_label in groups:
            item = await self._wait_for_field(field_label)
            results[key] = await sync_image_group(
                self.page,
                item,
                paths,
                log_label,
                timeout_seconds,
            )
        return results

    async def _section(self, title: str, timeout_seconds: float = 30) -> Any:
        if self.panel is None:
            raise DouyinListingError("请先调用 open() 打开抖音资料")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        wanted = _normalize_label(title)
        while asyncio.get_running_loop().time() < deadline:
            titles = self.panel.locator(".title")
            matches = []
            for index in range(await titles.count()):
                candidate = titles.nth(index)
                try:
                    if (
                        await candidate.is_visible()
                        and _normalize_label(await candidate.inner_text()) == wanted
                    ):
                        matches.append(candidate)
                except Exception:
                    continue
            if len(matches) == 1:
                return matches[0].locator("xpath=..")
            if len(matches) > 1:
                raise DouyinListingError(f"抖音板块“{title}”匹配数为 {len(matches)}")
            await asyncio.sleep(0.05)
        raise DouyinListingError(f"等待抖音板块“{title}”加载超时")

    async def _exact_toggle(self, scope: Any, text: str, timeout_seconds: float = 10) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        wanted = normalize_option(text)
        while asyncio.get_running_loop().time() < deadline:
            labels = scope.locator("label")
            matches = []
            for index in range(await labels.count()):
                label = labels.nth(index)
                try:
                    if not await label.is_visible():
                        continue
                    if normalize_option(await label.inner_text()) != wanted:
                        continue
                    inputs = label.locator("input[type=radio], input[type=checkbox]")
                    if await inputs.count() == 1:
                        matches.append((label, inputs.first))
                except Exception:
                    continue
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise DouyinListingError(f"选项“{text}”匹配数为 {len(matches)}")
            await asyncio.sleep(0.05)
        raise DouyinListingError(f"等待选项“{text}”加载超时")

    async def _ensure_toggle_checked(self, scope: Any, text: str) -> None:
        label, input_box = await self._exact_toggle(scope, text)
        if await input_box.is_checked():
            return
        await label.click()
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if await input_box.is_checked():
                return
            await asyncio.sleep(0.05)
        raise DouyinListingError(f"选项“{text}”点击后未处于选中状态")

    async def apply_delivery_mode(self) -> Tuple[str, str, str]:
        """按依赖顺序选择混合发货、48 小时和 15 天预售。"""
        section = await self._section("价格库存")
        await section.scroll_into_view_if_needed()
        values = ("现货预售混合模式", "48小时内发货", "15天内")
        for value in values:
            await self._ensure_toggle_checked(section, value)
        return values

    async def _sku_header_label(self, header: Any) -> str:
        cell = header.locator(":scope > .cell")
        if await cell.count():
            title = (await cell.first.get_attribute("title") or "").strip()
            if title:
                return title
            text = await cell.first.inner_text()
        else:
            text = await header.inner_text()
        return next((line.strip() for line in text.splitlines() if line.strip()), "")

    async def _sku_table_and_columns(self, section: Any) -> Tuple[Any, Mapping[str, int]]:
        required = ("价格", "现货库存", "预售库存")
        matches = []
        tables = section.locator(".el-table")
        for table_index in range(await tables.count()):
            table = tables.nth(table_index)
            try:
                if not await table.is_visible():
                    continue
                headers = table.locator(
                    ":scope > .el-table__main-wrapper > .el-table__header-wrapper thead th, "
                    ":scope > .el-table__header-wrapper thead th"
                )
                labels = [
                    await self._sku_header_label(headers.nth(index))
                    for index in range(await headers.count())
                ]
            except Exception:
                continue
            indexes: Dict[str, int] = {}
            for wanted in required:
                normalized = _normalize_label(wanted)
                found = [
                    index
                    for index, label in enumerate(labels)
                    if (
                        _normalize_label(label) == normalized
                        if wanted != "预售库存"
                        else _normalize_label(label).startswith(normalized)
                    )
                ]
                if len(found) == 1:
                    indexes[wanted] = found[0]
            if len(indexes) == len(required):
                matches.append((table, indexes))
        if len(matches) != 1:
            raise DouyinListingError(
                f"抖音 SKU 价格库存表匹配数为 {len(matches)}"
            )
        return matches[0]

    async def fill_sku_price_inventory(
        self,
        price: object,
        spot_stock: object,
        presale_stock: object,
    ) -> int:
        """逐行填写并回读价格、现货库存和预售库存。"""
        expected = {
            "价格": _decimal(price, "价格"),
            "现货库存": _decimal(spot_stock, "现货库存"),
            "预售库存": _decimal(presale_stock, "预售库存"),
        }
        if expected["价格"] < 0 or any(
            expected[label] < 0 or expected[label] != expected[label].to_integral_value()
            for label in ("现货库存", "预售库存")
        ):
            raise DouyinListingError("价格和库存数值不符合要求")

        controls = await self._sku_input_controls()

        for row_index, row_controls in enumerate(controls, start=1):
            for label, input_box in row_controls.items():
                value = format(expected[label], "f")
                raw_current = (await input_box.input_value()).strip()
                try:
                    current = _decimal(
                        raw_current,
                        f"第 {row_index} 个 SKU {label}",
                    )
                except DouyinListingError:
                    # 新生成的库存格可能初始为空；只要控件可写，
                    # 应用 Excel 值后再做严格回读，不在填写前误报。
                    current = None
                if current != expected[label]:
                    await input_box.fill(value)
                    await input_box.press("Tab")
            for label, input_box in row_controls.items():
                actual = _decimal(await input_box.input_value(), f"第 {row_index} 个 SKU {label}")
                if actual != expected[label]:
                    raise DouyinListingError(
                        f"第 {row_index} 个 SKU 的“{label}”回读失败："
                        f"期望 {expected[label]}，页面为 {actual}"
                    )
        return len(controls)

    async def _sku_input_controls(self) -> List[Mapping[str, Any]]:
        """定位唯一可见 SKU 表及其价格、现货、预售输入框。"""

        section = await self._section("价格库存")
        table, indexes = await self._sku_table_and_columns(section)
        rows = table.locator(
            ":scope > .el-table__main-wrapper > .el-table__body-wrapper tbody > tr, "
            ":scope > .el-table__body-wrapper tbody > tr"
        )
        if await rows.count() == 0:
            raise DouyinListingError("抖音 SKU 表中没有可填写行")

        # 先确认每行三个目标单元格都有唯一可写输入框。
        controls = []
        for row_index in range(await rows.count()):
            cells = rows.nth(row_index).locator(":scope > td")
            row_controls = {}
            for label, column_index in indexes.items():
                if await cells.count() <= column_index:
                    raise DouyinListingError(f"第 {row_index + 1} 个 SKU 缺少“{label}”单元格")
                inputs = cells.nth(column_index).locator(
                    "input:not([disabled]):not([readonly])"
                )
                if await inputs.count() != 1:
                    raise DouyinListingError(
                        f"第 {row_index + 1} 个 SKU 的“{label}”输入框匹配数为 "
                        f"{await inputs.count()}"
                    )
                row_controls[label] = inputs.first
            controls.append(row_controls)
        return controls

    async def _fetch_freight_payloads(
        self,
    ) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
        current = urlsplit(self.page.url)
        if current.scheme not in {"http", "https"} or not current.netloc:
            raise DouyinListingError("当前页面不是可验证的快麦同源页面")
        origin = f"{current.scheme}://{current.netloc}"
        urls = (
            origin + SHOP_INFO_ENDPOINT + "?pageNo=1&pageSize=9999&api_name=shop_info",
            origin
            + DISTRIBUTION_CONFIG_ENDPOINT
            + "?shopType=TouTiaoFXG&api_name=dsb_queryDistributionConfig",
        )
        payloads = []
        for url in urls:
            try:
                response = await self.page.request.get(url, timeout=30_000)
                if not response.ok:
                    raise DouyinListingError(
                        f"运费只读接口请求失败：{urlsplit(url).path} HTTP {response.status}"
                    )
                payload = await response.json()
            except DouyinListingError:
                raise
            except Exception as exc:
                raise DouyinListingError(
                    f"运费只读接口请求异常：{urlsplit(url).path}"
                ) from exc
            if not isinstance(payload, Mapping) or payload.get("result") != 1:
                raise DouyinListingError(
                    f"运费只读接口返回失败：{urlsplit(url).path}"
                )
            payloads.append(payload)
        return payloads[0], payloads[1]

    async def apply_freight_templates(
        self,
        aliases: Sequence[str],
        *,
        target_shops: Optional[Sequence[str]] = None,
        default_untargeted_template: Optional[str] = None,
    ) -> Mapping[str, Mapping[str, str]]:
        """API 优先匹配每个店铺，初始无候选时用 DOM 远程搜索回退。"""
        wanted_aliases_list: List[str] = []
        seen_aliases = set()
        for value in aliases:
            text = str(value).strip()
            normalized = normalize_option(text)
            if not text or not normalized or normalized in seen_aliases:
                continue
            seen_aliases.add(normalized)
            wanted_aliases_list.append(text)
        wanted_aliases = tuple(wanted_aliases_list)
        if not wanted_aliases:
            raise DouyinListingError("运费模板别名为空")
        shop_payload, config_payload = await self._fetch_freight_payloads()
        shops, templates_by_shop = _freight_api_data(shop_payload, config_payload)

        section = await self._section("运费模板")
        rows = section.locator(".set-ship")
        visible_rows = []
        seen_names = set()
        for index in range(await rows.count()):
            row = rows.nth(index)
            if not await row.is_visible():
                continue
            names = row.locator(":scope > .shop-title")
            selects = row.locator(":scope > .el-select")
            if await names.count() != 1 or await selects.count() != 1:
                raise DouyinListingError(f"第 {index + 1} 个运费店铺行结构不唯一")
            store_name = (await names.first.inner_text()).strip()
            normalized_store = normalize_option(store_name)
            if not normalized_store or normalized_store in seen_names:
                raise DouyinListingError(f"运费店铺名为空或重复：{store_name!r}")
            seen_names.add(normalized_store)
            visible_rows.append((store_name, row, selects.first))
        if not visible_rows:
            raise DouyinListingError("页面没有可见的授权店铺运费行")

        target_names = None
        if target_shops is not None:
            target_names = {
                normalize_option(value)
                for value in target_shops
                if normalize_option(value)
            }
            visible_names = {
                normalize_option(store_name) for store_name, _row, _select in visible_rows
            }
            missing_targets = target_names - visible_names
            if missing_targets:
                missing_labels = [
                    str(value)
                    for value in target_shops
                    if normalize_option(value) in missing_targets
                ]
                raise DouyinListingError(
                    "指定按 Excel 填写运费的店铺未出现："
                    + "、".join(missing_labels)
                )

        # 所有店铺与模板先通过 API 唯一匹配，再修改页面。
        assignments = []
        preserved: Dict[str, str] = {}
        for store_name, row, select in visible_rows:
            if target_names is not None and normalize_option(store_name) not in target_names:
                if default_untargeted_template:
                    assignments.append(
                        (
                            store_name,
                            select,
                            {"id": "", "name": default_untargeted_template},
                            (),
                        )
                    )
                else:
                    current = await self._read_select_values(select, multi=False)
                    preserved[store_name] = current[0] if current else ""
                    if self.logger is not None:
                        self.logger.info(
                            "店铺“%s”未配置按 Excel 填写运费，保持原值：%s",
                            store_name,
                            preserved[store_name] or "<空>",
                        )
                continue
            store_matches = [
                shop
                for shop in shops
                if normalize_option(store_name)
                in {normalize_option(value) for value in shop["aliases"]}
            ]
            if not store_matches:
                raise DouyinListingError(
                    f"店铺“{store_name}”在店铺 API 中匹配数为 0"
                )

            qualified = []
            candidate_names = []
            candidate_options: List[Mapping[str, str]] = []
            for shop in store_matches:
                shop_id = str(shop["id"])
                options = templates_by_shop.get(shop_id, ())
                candidate_options.extend(options)
                option_matches: Dict[Tuple[str, str], Mapping[str, str]] = {}
                for alias in wanted_aliases:
                    for option in options:
                        if normalize_option(option["name"]) == normalize_option(alias):
                            option_matches[
                                (option["id"], normalize_option(option["name"]))
                            ] = option
                candidate_names.extend(option["name"] for option in options)
                if len(option_matches) == 1:
                    qualified.append(
                        (shop_id, next(iter(option_matches.values())), options)
                    )

            if len(qualified) > 1:
                candidates = "、".join(dict.fromkeys(candidate_names)) or "<无>"
                raise DouyinListingError(
                    f"店铺“{store_name}”的同名 API 店铺中，指定运费模板匹配数为 "
                    f"{len(qualified)}；"
                    f"Excel 别名：{' / '.join(wanted_aliases)}；候选：{candidates}"
                )
            if not qualified:
                # queryDistributionConfig 只返回初始候选。可输入的
                # Element Select 在逐字搜索后会通过独立接口返回
                # 店铺的其他模板，因此留到 DOM 阶段再做精确搜索。
                assignments.append(
                    (store_name, select, None, tuple(candidate_options))
                )
                continue
            _shop_id, chosen, options = qualified[0]
            assignments.append((store_name, select, chosen, options))

        if not assignments:
            raise DouyinListingError("所有可见店铺都没有 Excel 指定的运费模板")

        applied: Dict[str, str] = {}
        for store_name, select, chosen, options in assignments:
            current = await self._read_select_values(select, multi=False)
            if chosen is not None:
                expected_names = (chosen["name"],)
            else:
                expected_names = wanted_aliases

            selected = None
            for expected_name in expected_names:
                if current and normalize_option(current[0]) == normalize_option(expected_name):
                    selected = current
                    break
                try:
                    selected = await self._select_values(
                        select,
                        (expected_name,),
                        label=f"运费模板-{store_name}",
                        multi=False,
                        api_options=options,
                    )
                    break
                except DouyinListingError as exc:
                    if "匹配数为 0" not in str(exc):
                        raise

            if selected is None:
                preserved[store_name] = current[0] if current else ""
                if self.logger is not None:
                    self.logger.info(
                        "店铺“%s”远程搜索仍无 Excel 指定运费模板，"
                        "保持原值：%s",
                        store_name,
                        preserved[store_name] or "<空>",
                    )
                continue
            # 远程搜索型 Element Select 点击候选后可能仍保持
            # popper 可见。在进入下一店铺前显式关闭，避免误用
            # 上一行的活动候选层。
            await self._dismiss_select_dropdown(select)
            if not matches_any(selected, expected_names, normalize_option):
                raise DouyinListingError(
                    f"店铺“{store_name}”运费模板回读失败：{selected[0]!r}"
                )
            applied[store_name] = selected[0]
        if len(applied) + len(preserved) != len(visible_rows):
            raise DouyinListingError("运费模板处理店铺数与页面不一致")
        return {"applied": applied, "preserved": preserved}

    async def validate_douyin_form(
        self,
        report: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """收集页面可见错误，并返回可写入 JSON 的铺货前报告。"""
        if self.panel is None:
            raise DouyinListingError("请先调用 open() 打开抖音资料")
        errors = []
        locator = self.panel.locator(".el-form-item__error:visible")
        for index in range(await locator.count()):
            text = (await locator.nth(index).inner_text()).strip()
            if text and text not in errors:
                errors.append(text)
        if errors:
            raise DouyinListingError("抖音资料存在页面校验错误：" + "；".join(errors))

        def serializable(value: Any) -> Any:
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, Mapping):
                return {str(key): serializable(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [serializable(item) for item in value]
            if isinstance(value, Decimal):
                return format(value, "f")
            return value

        result = serializable(report)
        result["errors"] = []
        return result
