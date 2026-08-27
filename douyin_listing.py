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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit


# 2026-08-27 在真实抖音资料页选择预测类目后观测到的精确路径。
# 请勿放宽为 list/query 等通用子串，避免误解析其他业务接口。
CATEGORY_PROPERTIES_ENDPOINT = "/fxg/getCategoryProperties.json"
CATEGORY_PROPERTIES_QUIET_SECONDS = 0.2
CATEGORY_PROPERTIES_TIMEOUT_SECONDS = 2.0


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
    return tuple(_normalize_label(part) for part in text.split("/") if _normalize_label(part))


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
                    if _normalize_label(await labels.first.inner_text()) == wanted:
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
        await self._wait_for_field("导购短标题")
        await self._drain_property_response_tasks()
        return self

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
        normalize_path = lambda value: re.sub(r"[\s>]", "", value).casefold()
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
        await input_box.fill(str(value))
        await input_box.press("Tab")
        actual = await input_box.input_value()
        if actual != str(value):
            raise DouyinListingError(
                f"导购短标题填写后校验失败：期望 {value!r}，页面为 {actual!r}"
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
                await search.click()
                return
        input_box = select.locator("input.el-input__inner").first
        if not await input_box.count():
            raise DouyinListingError("属性下拉框中找不到可点击输入框")
        await input_box.click()

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
    ) -> Tuple[Any, List[Mapping[str, str]]]:
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            dropdown = await self._active_select_dropdown(select)
            if dropdown is None:
                await asyncio.sleep(0.05)
                continue
            result: List[Mapping[str, str]] = []
            options = dropdown.locator(".el-select-dropdown__item")
            for index in range(await options.count()):
                option = options.nth(index)
                classes = await option.get_attribute("class") or ""
                if "is-disabled" in classes:
                    continue
                try:
                    if not await option.is_visible():
                        continue
                except Exception:
                    continue
                name = (await option.inner_text()).strip()
                if name:
                    result.append({"name": name, "id": "", "index": str(index)})
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
        multi: bool,
        api_options: Optional[Sequence[Mapping[str, str]]] = None,
    ) -> Tuple[str, ...]:
        if multi:
            await self._clear_multi_select(select)
        for expected in expected_values:
            if api_options:
                # API 给出稳定 ID/name 时先用其检查缺失或重名。
                choose_unique_option(expected, api_options)
            await self._open_select(select, multi=multi)
            dropdown, dom_options = await self._visible_dom_options(select)
            chosen = choose_unique_option(expected, dom_options)
            option = dropdown.locator(".el-select-dropdown__item").nth(int(chosen["index"]))
            await option.click()

        actual = await self._read_select_values(select, multi=multi)
        expected_counter = Counter(normalize_option(value) for value in expected_values)
        actual_counter = Counter(normalize_option(value) for value in actual)
        if actual_counter != expected_counter:
            raise DouyinListingError(
                f"属性选择后校验失败：期望 {list(expected_values)!r}，页面为 {list(actual)!r}"
            )
        return actual

    async def fill_attribute(self, label: str, expected: str) -> Tuple[str, ...]:
        """在指定属性内精确选择；仅真实多选控件才拆分斜杠值。"""
        item = await self._attribute_item(label)
        await item.scroll_into_view_if_needed()
        selects = item.locator(":scope > .el-form-item__content > .el-select")
        if not await selects.count():
            selects = item.locator(".el-select")
        if await selects.count():
            select = selects.first
            multi = await select.locator(".el-select__tags").count() > 0
            expected_values = (
                tuple(part.strip() for part in str(expected).split("/") if part.strip())
                if multi
                else (str(expected).strip(),)
            )
            if not expected_values or any(not value.strip() for value in expected_values):
                raise DouyinListingError(f"属性“{label}”期望值为空")

            api_property = await self._api_property(label)
            api_options: Optional[Sequence[Mapping[str, str]]] = None
            if api_property and api_property.get("options"):
                api_options = api_property["options"]  # type: ignore[assignment]
            return await self._select_values(
                select,
                expected_values,
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
        await input_box.fill(str(expected))
        await input_box.press("Tab")
        actual = await input_box.input_value()
        if actual != str(expected):
            raise DouyinListingError(
                f"属性“{label}”填写后校验失败：期望 {expected!r}，页面为 {actual!r}"
            )
        return (actual,)

    async def apply_category_and_fields(self, fields: Any) -> Mapping[str, Any]:
        """应用类目、短标题和所有 Excel 属性；未匹配的属性立即报错。"""
        category = await self.apply_first_recommended_category()
        page_items = await self._attribute_items()

        excel_attributes = list(fields.attributes.items())
        target_sources: Dict[str, List[Tuple[object, object]]] = {}
        unmatched: List[str] = []
        for key, value in excel_attributes:
            aliases = set(_excel_aliases(key))
            matches = [
                (normalized_label, page_label)
                for normalized_label, (page_label, _item) in page_items.items()
                if normalized_label in aliases
            ]
            if not matches:
                unmatched.append(str(key))
                continue
            if len(matches) > 1:
                labels = "、".join(page_label for _normalized, page_label in matches)
                raise DouyinListingError(
                    f"Excel 抖音属性“{key}”同时匹配多个页面字段：{labels}"
                )
            normalized_label, _page_label = matches[0]
            target_sources.setdefault(normalized_label, []).append((key, value))

        if unmatched:
            names = "、".join(unmatched)
            raise DouyinListingError(
                "以下 Excel 抖音属性未按规范化别名精确匹配当前页面字段："
                f"{names}"
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
        for page_label, value in assignments:
            # 已唯一映射的页面字段不能静默跳过；选项缺失/重名立即终止。
            applied[page_label] = await self.fill_attribute(page_label, str(value))
        return {"category": category, "short_title": short_title, "attributes": applied}

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

        item = await self._wait_for_field("面料材质")
        from kuaimai_erp import sync_image_group

        await sync_image_group(
            self.page,
            item,
            tuple(Path(path) for path in wash_label_paths),
            "抖音水洗标/吊牌图",
            300,
        )

        rows = item.locator(".measure-item")
        add_button = item.get_by_role("button", name="+ 添加材质", exact=True)
        while await rows.count() < len(materials):
            before = await rows.count()
            if not await add_button.count():
                raise DouyinListingError("面料材质区域中找不到“添加材质”按钮")
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
                multi=False,
            )
            percent_input = await self._percentage_input(row)
            await percent_input.fill(str(percentages[index]))
            await percent_input.press("Tab")
            raw_percentage = (await percent_input.input_value()).strip()
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
