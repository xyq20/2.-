"""DOM writer for FastMai's Youzan product-information tab.

The adapter fills and validates only the Youzan form.  Saving and publishing
remain owned by the common runner in ``kuaimai_erp.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from attribute_runtime import AttributeRequest
from learning_models import CandidateValue, canonical_sha256
from platform_candidate_source import (
    CandidateSourceError,
    DomCandidate,
    reconcile_candidates,
)
from platform_schema import FieldOption

from taobao_listing import (
    TaobaoListing,
    TaobaoListingError,
    excel_aliases,
    normalize_label,
    normalize_option,
    parse_taobao_fabrics,
    selection_value_groups,
)
from youzan_data import YouzanFields


class YouzanFormListingError(RuntimeError):
    """A Youzan form problem that can be shown directly to the operator."""


YOUZAN_FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    normalize_label("面料"): tuple(
        normalize_label(value) for value in ("面料材质", "面料俗称")
    ),
    normalize_label("款式"): tuple(normalize_label(value) for value in ("裤型",)),
    normalize_label("季节"): tuple(normalize_label(value) for value in ("适用季节",)),
    normalize_label("年份"): tuple(
        normalize_label(value) for value in ("上市时间", "上市年份季节", "上市时节")
    ),
    normalize_label("性别"): tuple(normalize_label(value) for value in ("适用性别",)),
    normalize_label("厚薄"): tuple(normalize_label(value) for value in ("厚度",)),
    normalize_label("款式细节"): tuple(
        normalize_label(value) for value in ("流行元素",)
    ),
    normalize_label("货号"): tuple(
        normalize_label(value) for value in ("商家外部编码", "款式编码")
    ),
    normalize_label("工艺处理"): tuple(
        normalize_label(value) for value in ("服饰工艺", "工艺")
    ),
    normalize_label("款式版型"): tuple(
        normalize_label(value) for value in ("服饰版型", "服装版型", "版型")
    ),
    normalize_label("主材含量"): tuple(
        normalize_label(value)
        for value in ("里料材质成分含量", "材质成分含量", "面料材质成分含量")
    ),
}
YOUZAN_INHERITED_FIELDS = frozenset(
    normalize_label(value)
    for value in (
        "商品类型",
        "商品类目",
        "商品名",
        "分享描述",
        "商品卖点",
        "商品图",
        "商品图片",
    )
)
YOUZAN_BATCH_ORDER = ("价格", "库存", "重量(kg)")
YOUZAN_FREIGHT_TEMPLATES = {
    "pants": "T恤、裤子、饰品邮费模版",
    "coat": "鞋子、皮衣、外套邮费模版",
}
YOUZAN_ATTRIBUTE_PATHS = frozenset(
    ("/yz/getCategoryProperties", "/yz/getCategoryProperties.json")
)


def _numeric_equal(actual: str, expected: str) -> bool:
    try:
        return Decimal(actual.replace(",", "")) == Decimal(expected.replace(",", ""))
    except (InvalidOperation, ValueError):
        return False


def _required_excel_value(
    fields: Mapping[str, str], aliases: Sequence[str], label: str
) -> str:
    wanted = {normalize_label(alias) for alias in aliases}
    matches = [
        (str(key), str(value).strip())
        for key, value in fields.items()
        if wanted.intersection(excel_aliases(key)) and str(value).strip()
    ]
    if not matches:
        raise YouzanFormListingError(
            "Excel 中缺少有赞{0}字段（可识别：{1}）".format(
                label, "/".join(aliases)
            )
        )
    values = {value for _key, value in matches}
    if len(values) != 1:
        raise YouzanFormListingError(
            "有赞{0}匹配到多个 Excel 字段：{1}".format(
                label, "、".join(key for key, _value in matches)
            )
        )
    return matches[0][1]


def _category_parts(value: object) -> Tuple[str, ...]:
    return tuple(
        normalize_label(part)
        for part in re.split(r"[>＞/／]", str(value or ""))
        if normalize_label(part)
    )


class YouzanFormListing(TaobaoListing):
    """Fill and verify Youzan category, SKU, inventory and logistics data."""

    # FastMai marks remote Youzan candidates as Element UI ``created`` options
    # even though they came from the platform API.  Exact visible text is still
    # required and is read back after the click.
    allow_created_exact_dom_option = True

    def __init__(
        self,
        page: Any,
        drawer: Any,
        logger: Any,
        *,
        attribute_runtime: Optional[Any] = None,
    ) -> None:
        super().__init__(
            page,
            drawer,
            logger,
            attribute_runtime=attribute_runtime,
        )
        self._youzan_api_generation = 0
        self._youzan_api_category_id = ""
        self._youzan_api_fields: Dict[str, Tuple[Mapping[str, Any], ...]] = {}
        self._youzan_request_contexts: Dict[int, Tuple[int, str]] = {}
        self._youzan_response_tasks: set[asyncio.Task[Any]] = set()
        self._install_youzan_attribute_listener()

    @staticmethod
    def _request_parameter(request: Any, name: str) -> str:
        try:
            query = parse_qs(urlsplit(str(request.url)).query, keep_blank_values=True)
            values = [
                str(value).strip()
                for value in query.get(name, ())
                if str(value).strip()
            ]
        except Exception:
            values = []
        if len(values) == 1:
            return values[0]
        raw = str(getattr(request, "post_data", "") or "")
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = parse_qs(raw, keep_blank_values=True)
        if isinstance(parsed, Mapping):
            value = parsed.get(name)
            if isinstance(value, Sequence) and not isinstance(value, str):
                values = [str(item).strip() for item in value if str(item).strip()]
                return values[0] if len(values) == 1 else ""
            return str(value or "").strip()
        return ""

    def _install_youzan_attribute_listener(self) -> None:
        if not hasattr(self.page, "on"):
            return

        def on_request(request: Any) -> None:
            try:
                path = urlsplit(str(request.url)).path
            except Exception:
                return
            if path not in YOUZAN_ATTRIBUTE_PATHS:
                return
            category_id = self._request_parameter(request, "categoryId")
            if not category_id:
                return
            self._youzan_api_generation += 1
            self._youzan_api_category_id = category_id
            self._youzan_api_fields = {}
            self._youzan_request_contexts[id(request)] = (
                self._youzan_api_generation,
                category_id,
            )

        def on_response(response: Any) -> None:
            request = getattr(response, "request", None)
            context = self._youzan_request_contexts.pop(id(request), None)
            if context is None:
                return
            task = asyncio.create_task(
                self._consume_youzan_attribute_response(response, context)
            )
            self._youzan_response_tasks.add(task)
            task.add_done_callback(self._youzan_response_tasks.discard)

        def on_request_failed(request: Any) -> None:
            self._youzan_request_contexts.pop(id(request), None)

        self.page.on("request", on_request)
        self.page.on("response", on_response)
        self.page.on("requestfailed", on_request_failed)

    async def _consume_youzan_attribute_response(
        self,
        response: Any,
        context: Tuple[int, str],
    ) -> None:
        generation, category_id = context
        if generation != self._youzan_api_generation:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if generation != self._youzan_api_generation:
            return
        data = payload.get("data", payload) if isinstance(payload, Mapping) else {}
        result = data.get("result", data) if isinstance(data, Mapping) else {}
        raw_fields = result.get("publicPropertys") if isinstance(result, Mapping) else None
        if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
            return
        records: Dict[str, List[Mapping[str, Any]]] = {}
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping) or raw_field.get("propertyGroup") != 1:
                continue
            prop = raw_field.get("property")
            if not isinstance(prop, Mapping):
                continue
            field_id = str(prop.get("id") or "").strip()
            label = str(prop.get("name") or "").strip()
            if not field_id or not label:
                continue
            raw_options = prop.get("valueNames")
            if not isinstance(raw_options, Sequence) or isinstance(
                raw_options, (str, bytes)
            ):
                raw_options = ()
            options = tuple(
                FieldOption(str(value).strip(), str(value).strip(), position)
                for position, value in enumerate(raw_options)
                if str(value).strip()
            )
            value_type = int(prop.get("valueType") or 0)
            records.setdefault(normalize_label(label), []).append(
                {
                    "id": field_id,
                    "label": label,
                    "options": options,
                    "custom_allowed": value_type == 1,
                    "category_id": category_id,
                }
            )
        self._youzan_api_fields = {key: tuple(value) for key, value in records.items()}

    async def _drain_youzan_attribute_tasks(self) -> None:
        await asyncio.sleep(0)
        while self._youzan_response_tasks:
            await asyncio.gather(
                *tuple(self._youzan_response_tasks), return_exceptions=True
            )

    async def _captured_youzan_field(
        self,
        page_label: str,
        *,
        timeout_seconds: float = 3.0,
    ) -> Mapping[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await self._drain_youzan_attribute_tasks()
            records = self._youzan_api_fields.get(normalize_label(page_label), ())
            if len(records) == 1 and self._youzan_api_category_id:
                return records[0]
            await asyncio.sleep(0.05)
        records = self._youzan_api_fields.get(normalize_label(page_label), ())
        raise YouzanFormListingError(
            "有赞属性“{0}”缺少唯一的接口 JSON 字段：字段数 {1}".format(
                page_label,
                len(records),
            )
        )

    async def _dismiss_select_dropdown(self, select: Any) -> None:
        for _attempt in range(2):
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            await asyncio.sleep(0.05)
            if await self.page.locator(".el-select-dropdown:visible").count() == 0:
                return
        await self.page.evaluate(
            """() => {
              for (const type of ['mousedown', 'mouseup', 'click']) {
                document.body.dispatchEvent(new MouseEvent(type, {
                  bubbles: true, cancelable: true, view: window
                }));
              }
            }"""
        )
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if await self.page.locator(".el-select-dropdown:visible").count() == 0:
                return
            await asyncio.sleep(0.05)
        raise YouzanFormListingError("有赞属性下拉层无法关闭")

    async def _wait_for_loading_masks(self, timeout_seconds: float = 30) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self.drawer.locator(".el-loading-mask:visible").count() == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.05)
        raise YouzanFormListingError("有赞资料加载遮罩在 30 秒内未消失")

    async def open(self) -> "YouzanFormListing":
        tab = self.drawer.get_by_role("tab", name="有赞资料", exact=True)
        try:
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click(timeout=30_000)
            panel = self.drawer.get_by_role(
                "tabpanel", name="有赞资料", exact=True
            )
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise YouzanFormListingError("找不到可切换的“有赞资料”页签") from exc
        self.panel = panel
        await self._wait_for_loading_masks()
        if self.logger is not None:
            self.logger.info("有赞资料页签已打开")
        return self

    async def select_physical_product(self) -> Mapping[str, str]:
        try:
            selected = await self._ensure_radio("商品类型", "实物商品")
        except TaobaoListingError as exc:
            raise YouzanFormListingError(str(exc).replace("淘宝", "有赞")) from exc
        if self.logger is not None:
            self.logger.info("有赞商品类型已选中：%s", selected)
        return {"product_type": selected}

    async def _category_dialog(self) -> Any:
        dialogs = self.page.locator(".el-dialog:visible")
        if await dialogs.count() == 0:
            dialogs = self.page.locator("[role=dialog]:visible")
        matches = []
        for index in range(await dialogs.count()):
            dialog = dialogs.nth(index)
            text = normalize_label(await dialog.inner_text())
            if normalize_label("修改类目") not in text and not await dialog.locator(
                "input:visible"
            ).count():
                continue
            matches.append(dialog)
        if len(matches) != 1:
            raise YouzanFormListingError("有赞修改类目弹窗不是唯一项：{0}".format(len(matches)))
        return matches[0]

    async def _category_search_input(self, dialog: Any) -> Any:
        direct = dialog.locator("[data-category-search]:visible")
        if await direct.count() == 1:
            return direct.first
        inputs = dialog.locator(
            'input:not([type="hidden"]):not([readonly]):visible'
        )
        if await inputs.count() != 1:
            raise YouzanFormListingError(
                "有赞修改类目弹窗搜索框不是唯一项：{0}".format(
                    await inputs.count()
                )
            )
        return inputs.first

    async def _visible_category_rows(self) -> List[Tuple[str, Any]]:
        snapshots = await self.page.evaluate(
            """() => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const rendered = element => {
                const style = getComputedStyle(element);
                return Boolean(element.getClientRects().length)
                  && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const elements = Array.from(document.querySelectorAll('*'));
              return elements.map((element, index) => ({
                index,
                inScope: Boolean(element.closest(
                  '.el-dialog, [role=dialog], .el-autocomplete-suggestion, .el-popper'
                )),
                text: rendered(element) ? clean(element.innerText) : '',
                hasSameTextChild: rendered(element) && Array.from(
                  element.querySelectorAll('*')
                ).some(child => rendered(child)
                  && clean(child.innerText) === clean(element.innerText))
              }));
            }"""
        )
        nodes = self.page.locator("*")
        rows: List[Tuple[str, Any]] = []
        for snapshot in snapshots:
            text = str(snapshot.get("text") or "")
            if (
                snapshot.get("inScope")
                and text
                and not snapshot.get("hasSameTextChild")
                and len(_category_parts(text)) >= 2
            ):
                rows.append((text, nodes.nth(int(snapshot["index"]))))
        return rows

    @staticmethod
    def _category_paths_from_json(payload: Any) -> Tuple[str, ...]:
        paths: List[str] = []

        def add(parts: Sequence[Any]) -> None:
            cleaned = tuple(str(value).strip() for value in parts if str(value).strip())
            if len(cleaned) >= 2:
                rendered = " > ".join(cleaned)
                if rendered not in paths:
                    paths.append(rendered)

        def walk(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    normalized_key = normalize_label(key)
                    if (
                        isinstance(child, Sequence)
                        and not isinstance(child, (str, bytes, bytearray))
                        and "category" in normalized_key.casefold()
                        and all(isinstance(item, (str, int)) for item in child)
                    ):
                        add(child)
                    walk(child)
                return
            if isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                if value and all(isinstance(item, str) for item in value):
                    add(value)
                for child in value:
                    walk(child)
                return
            if isinstance(value, str) and len(_category_parts(value)) >= 2:
                add(re.split(r"[>＞/／]", value))

        walk(payload)
        return tuple(paths)

    @staticmethod
    def _category_target_parts(search_term: str) -> Tuple[str, ...]:
        if normalize_label(search_term) == normalize_label("休闲裤"):
            return tuple(
                normalize_label(value) for value in ("服装鞋包", "男装", "休闲裤")
            )
        return (normalize_label(search_term),)

    async def _category_text(self) -> str:
        if self.panel is None:
            raise YouzanFormListingError("请先调用 open() 打开有赞资料")
        preferred = self.panel.locator(
            ".platform-category-input .current, .platform-category-input .category-path"
        )
        values = []
        for index in range(await preferred.count()):
            node = preferred.nth(index)
            if await node.is_visible():
                text = re.sub(r"\s+", " ", (await node.inner_text()).strip())
                if text:
                    values.append(text)
        if not values:
            values.extend(
                await self.panel.evaluate(
                    """root => {
                      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
                      const paths = [];
                      for (const element of root.querySelectorAll('*')) {
                        const style = getComputedStyle(element);
                        if (!element.getClientRects().length
                            || style.display === 'none' || style.visibility === 'hidden') continue;
                        const text = clean(element.innerText);
                        const match = text.match(/^商品类目\s*[：:]\s*(.*?)(?:\s*修改类目|\s*同步平台类目|$)/);
                        if (match && match[1]) paths.push(clean(match[1]));
                      }
                      return paths;
                    }"""
                )
            )
        values = list(dict.fromkeys(values))
        if len(values) != 1:
            raise YouzanFormListingError("有赞页面已选类目不是唯一项：{0}".format(len(values)))
        return values[0]

    async def apply_category(self, category_path: Sequence[str]) -> Mapping[str, Any]:
        path = tuple(str(value).strip() for value in category_path if str(value).strip())
        if not path:
            raise YouzanFormListingError("Excel 中缺少有赞商品分类")
        if self.panel is None:
            raise YouzanFormListingError("请先调用 open() 打开有赞资料")
        buttons = self.panel.get_by_role("button", name="修改类目", exact=True)
        if await buttons.count() != 1:
            raise YouzanFormListingError("有赞“修改类目”按钮不是唯一项：{0}".format(await buttons.count()))
        await buttons.first.click()
        dialog = await self._category_dialog()
        search_term = path[0]
        search = await self._category_search_input(dialog)
        target_parts = self._category_target_parts(search_term)
        api_paths: List[str] = []
        api_urls: List[str] = []
        response_tasks: List[asyncio.Task[Any]] = []

        async def capture_category_response(response: Any) -> None:
            try:
                headers = await response.all_headers()
                if "json" not in str(headers.get("content-type") or "").casefold():
                    return
                payload = await response.json()
                rendered = str(payload)
                if search_term not in rendered:
                    return
                for api_path in self._category_paths_from_json(payload):
                    if api_path not in api_paths:
                        api_paths.append(api_path)
                if response.url not in api_urls:
                    api_urls.append(response.url)
            except Exception:
                return

        def on_category_response(response: Any) -> None:
            response_tasks.append(asyncio.create_task(capture_category_response(response)))

        self.page.on("response", on_category_response)
        await search.fill(search_term)
        deadline = asyncio.get_running_loop().time() + 30
        candidates: List[Tuple[str, Any]] = []
        api_matches: List[str] = []
        try:
            while asyncio.get_running_loop().time() < deadline:
                api_matches = [
                    value
                    for value in api_paths
                    if (
                        _category_parts(value) == target_parts
                        if len(target_parts) > 1
                        else _category_parts(value)
                        and _category_parts(value)[-1] == target_parts[0]
                    )
                ]
                rows = await self._visible_category_rows()
                candidates = [
                    (text, node)
                    for text, node in rows
                    if (
                        _category_parts(text) == target_parts
                        if len(target_parts) > 1
                        else _category_parts(text)
                        and _category_parts(text)[-1] == target_parts[0]
                    )
                ]
                if len(api_matches) == 1 and len(candidates) == 1:
                    break
                if len(api_matches) > 1 or len(candidates) > 1:
                    break
                await asyncio.sleep(0.1)
        finally:
            self.page.remove_listener("response", on_category_response)
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)
        if len(api_matches) != 1 or len(candidates) != 1:
            raise YouzanFormListingError(
                "有赞类目接口或 DOM 完整路径不是唯一项：接口 {0}，DOM {1}".format(
                    len(api_matches), len(candidates)
                )
            )
        selected_text, selected_node = candidates[0]
        await selected_node.click()
        confirms = dialog.get_by_role(
            "button", name=re.compile(r"^\s*确\s*定\s*$")
        )
        if await confirms.count() != 1:
            raise YouzanFormListingError("有赞修改类目弹窗“确定”按钮不是唯一项")
        await confirms.first.click()
        await dialog.wait_for(state="hidden", timeout=20_000)
        actual = await self._category_text()
        if _category_parts(actual) != _category_parts(selected_text):
            raise YouzanFormListingError(
                "有赞类目确认后回读失败：期望 {0!r}，页面为 {1!r}".format(
                    selected_text, actual
                )
            )
        self.category_clicked = True
        await self._wait_for_loading_masks()
        if self.logger is not None:
            self.logger.info(
                "已按接口结构定位并通过 DOM 选择有赞类目：%s；接口=%s",
                actual,
                "、".join(api_urls),
            )
        return {
            "path": path,
            "search_term": search_term,
            "selected": actual,
            "api_candidate_count": len(api_matches),
            "api_urls": tuple(api_urls),
        }

    async def _attribute_vertical_bounds(self) -> Tuple[Optional[float], Optional[float]]:
        if self.panel is None:
            raise YouzanFormListingError("请先调用 open() 打开有赞资料")
        start: Optional[float] = None
        end: Optional[float] = None
        headings = self.panel.get_by_text(
            re.compile(r"^\s*(?:类目参数|规格明细|价格库存)\s*[：:]?\s*$")
        )
        for index in range(await headings.count()):
            heading = headings.nth(index)
            if not await heading.is_visible():
                continue
            box = await heading.bounding_box()
            if box is None:
                continue
            label = normalize_label(await heading.inner_text())
            if label == normalize_label("类目参数"):
                start = box["y"]
            elif start is not None and box["y"] > start:
                end = box["y"] if end is None else min(end, box["y"])
        return start, end

    async def _attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        if self.panel is None:
            raise YouzanFormListingError("请先调用 open() 打开有赞资料")
        top, end = await self._attribute_vertical_bounds()
        items = self.panel.locator(".el-form-item:visible")
        result: Dict[str, Tuple[str, Any]] = {}
        counts: Dict[str, int] = {}
        for index in range(await items.count()):
            item = items.nth(index)
            box = await item.bounding_box()
            if box is not None:
                if top is not None and box["y"] <= top:
                    continue
                if end is not None and box["y"] >= end:
                    continue
            label_node = item.locator(":scope > .el-form-item__label").first
            if not await label_node.count() or not await label_node.is_visible():
                continue
            label = re.sub(r"^\s*\*\s*", "", (await label_node.inner_text()).strip())
            normalized = normalize_label(label)
            if not normalized or normalized in YOUZAN_INHERITED_FIELDS:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            key = normalized if counts[normalized] == 1 else "{0}#{1}".format(
                normalized, counts[normalized]
            )
            result[key] = (label, item)
        if not result:
            raise YouzanFormListingError("有赞类目参数区域为空或未渲染")
        return result

    @staticmethod
    async def _attribute_is_required(item: Any) -> bool:
        classes = set((await item.get_attribute("class") or "").split())
        if "is-required" in classes:
            return True
        label = item.locator(":scope > .el-form-item__label").first
        return bool(
            await label.count()
            and re.match(r"^\s*\*", (await label.inner_text()).strip())
        )

    async def _attribute_assignments(
        self,
        fields: Mapping[str, str],
        page_items: Mapping[str, Tuple[str, Any]],
    ) -> Dict[str, Tuple[str, str]]:
        direct_sources: Dict[str, List[Tuple[str, str]]] = {}
        alias_sources: Dict[str, List[Tuple[str, str]]] = {}
        for excel_key, raw_value in fields.items():
            aliases = set(excel_aliases(excel_key))
            for normalized_page, (_page_label, _item) in page_items.items():
                base = normalized_page.split("#", 1)[0]
                if base in aliases:
                    direct_sources.setdefault(normalized_page, []).append(
                        (str(excel_key), str(raw_value).strip())
                    )
                elif aliases.intersection(YOUZAN_FIELD_ALIASES.get(base, ())):
                    alias_sources.setdefault(normalized_page, []).append(
                        (str(excel_key), str(raw_value).strip())
                    )
        assignments: Dict[str, Tuple[str, str]] = {}
        for normalized_page, (page_label, _item) in page_items.items():
            matches = direct_sources.get(normalized_page) or alias_sources.get(
                normalized_page, ()
            )
            if not matches:
                continue
            values = {value for _key, value in matches}
            if len(values) != 1:
                raise YouzanFormListingError(
                    "有赞属性“{0}”匹配到多个 Excel 字段：{1}".format(
                        page_label, "、".join(key for key, _value in matches)
                    )
                )
            assignments[normalized_page] = (page_label, matches[0][1])
        return assignments

    @staticmethod
    def _special_attribute_values(
        fields: Mapping[str, str], page_label: str, expected: str
    ) -> Optional[Tuple[str, ...]]:
        normalized = normalize_label(page_label)
        if normalized == normalize_label("面料"):
            try:
                values = tuple(component.name for component in parse_taobao_fabrics(fields))
            except TaobaoListingError as exc:
                raw = str(expected).strip()
                if "%" in raw or "％" in raw:
                    raise YouzanFormListingError(
                        str(exc).replace("淘宝", "有赞")
                    ) from exc
                values = tuple(
                    part.strip()
                    for part in re.split(r"[、,，;+＋;/／]", raw)
                    if part.strip()
                )
                if not values:
                    raise YouzanFormListingError(
                        "Excel 中的有赞面料为空"
                    ) from exc
            return values or None
        if normalized == normalize_label("年份"):
            year = re.search(r"(?:19|20)\d{2}", str(expected))
            return (year.group(0),) if year else None
        return None

    async def _resolve_learning_select_groups(
        self,
        page_label: str,
        select: Any,
        groups: Sequence[Sequence[str]],
    ) -> Tuple[str, ...]:
        runtime = self.attribute_runtime
        if runtime is None:
            raise YouzanFormListingError("有赞属性学习运行器未启用")
        multi = await select.locator(".el-select__tags").count() > 0
        await self._open_select(select, multi=multi)
        try:
            _dropdown, dom_options = await self._visible_dom_options(select)
            field = await self._captured_youzan_field(page_label)
        finally:
            try:
                await self._dismiss_select_dropdown(select)
            except Exception:
                pass
        api_options = tuple(field.get("options") or ())
        if not api_options and field.get("custom_allowed") is True:
            # Youzan valueType=1 is an API-declared free-form selector.  It has
            # no constrained platform candidate set, so retain the Excel value
            # and let the existing Element-UI custom-entry path verify it.
            return tuple(group[0] for group in groups if group)
        try:
            candidates = reconcile_candidates(
                api_options,
                tuple(
                    DomCandidate(
                        str(option.get("value") or ""),
                        str(option.get("name") or ""),
                        not bool(option.get("disabled")),
                    )
                    for option in dom_options
                ),
            )
        except CandidateSourceError as exc:
            raise YouzanFormListingError(
                "有赞属性“{0}”接口候选与页面候选不一致：{1}".format(
                    page_label,
                    exc.reason_code,
                )
            ) from exc
        field_id = str(field.get("id") or "").strip()
        category_id = str(field.get("category_id") or "").strip()
        schema_version = canonical_sha256(
            {
                "platform_id": "yz",
                "category_leaf_id": category_id,
                "field_id": field_id,
                "options": [
                    {"value_id": value.value_id, "label": value.label}
                    for value in candidates
                ],
            }
        )
        resolved_values = []
        for group in groups:
            exact = tuple(
                value.label
                for value in candidates
                if any(
                    normalize_option(value.label) == normalize_option(alias)
                    for alias in group
                )
            )
            excel_value = exact[0] if len(exact) == 1 else "/".join(group)
            resolved = await runtime.resolve(
                AttributeRequest(
                    platform_id="yz",
                    category_leaf_id=category_id,
                    field_id=field_id,
                    field_label=page_label,
                    candidates=tuple(
                        CandidateValue(value.value_id, value.label)
                        for value in candidates
                    ),
                    excel_value=excel_value,
                    evidence={"excel": bool(excel_value.strip())},
                    custom_allowed=bool(field.get("custom_allowed")),
                    schema_version=schema_version,
                    control_type="select",
                )
            )
            resolved_values.append(resolved.label)
        return tuple(resolved_values)

    async def _fill_attribute(
        self,
        page_label: str,
        item: Any,
        expected: str,
        *,
        exact_values: Optional[Sequence[str]] = None,
        required: bool,
    ) -> Optional[Tuple[str, ...]]:
        await item.scroll_into_view_if_needed()
        selects = item.locator(":scope > .el-form-item__content .el-select:visible")
        if await selects.count():
            if await selects.count() != 1:
                raise YouzanFormListingError("有赞属性“{0}”下拉框不唯一".format(page_label))
            select = selects.first
            multi = await select.locator(".el-select__tags").count() > 0
            groups = (
                tuple((str(value),) for value in exact_values)
                if exact_values is not None
                else selection_value_groups(page_label, expected)
            )
            if not groups or (not multi and len(groups) != 1):
                raise YouzanFormListingError("有赞属性“{0}”候选分组无法用于当前控件".format(page_label))
            if self.attribute_runtime is not None:
                groups = tuple(
                    (value,)
                    for value in await self._resolve_learning_select_groups(
                        page_label,
                        select,
                        groups,
                    )
                )
            direct_values = tuple(group[0] for group in groups if group)
            if await self._select_explicitly_has_no_data(select, multi=multi):
                return await self._set_select_values_directly(
                    select,
                    direct_values,
                    label=page_label,
                    multi=multi,
                )
            try:
                actual = await self._select_values(
                    select, groups, label=page_label, multi=multi
                )
            except TaobaoListingError as exc:
                if "未找到可见选项" in str(exc):
                    try:
                        await self._dismiss_select_dropdown(select)
                    except Exception:
                        pass
                    return await self._set_select_values_directly(
                        select,
                        direct_values,
                        label=page_label,
                        multi=multi,
                    )
                raise YouzanFormListingError(
                    "有赞属性“{0}”填写失败：{1}".format(
                        page_label, str(exc).replace("淘宝", "有赞")
                    )
                ) from exc
            if actual is None:
                return await self._set_select_values_directly(
                    select,
                    direct_values,
                    label=page_label,
                    multi=multi,
                )
            return actual

        inputs = item.locator(
            ':scope > .el-form-item__content input:not([readonly]):not([type="hidden"]):visible'
        )
        if await inputs.count() != 1:
            raise YouzanFormListingError("有赞属性“{0}”可填控件不是唯一项".format(page_label))
        input_box = inputs.first
        expected_text = str(expected).strip()
        if (await input_box.input_value()).strip() != expected_text:
            await input_box.fill(expected_text)
            await input_box.press("Tab")
        actual_text = (await input_box.input_value()).strip()
        if actual_text != expected_text:
            raise YouzanFormListingError(
                "有赞属性“{0}”回读失败：{1!r}".format(page_label, actual_text)
            )
        return (actual_text,)

    async def _select_explicitly_has_no_data(
        self,
        select: Any,
        *,
        multi: bool,
    ) -> bool:
        """Fast-path Element's explicit ``无数据`` state.

        The Youzan adapter allows custom values.  Waiting for the generic
        remote-option timeout after Element has already rendered its empty
        message only slows the workflow and cannot produce a platform option.
        """

        try:
            await self._open_select(select, multi=multi)
            deadline = asyncio.get_running_loop().time() + 1.5
            while asyncio.get_running_loop().time() < deadline:
                dropdown = await self._active_select_dropdown(select)
                if dropdown is None:
                    await asyncio.sleep(0.05)
                    continue
                options = dropdown.locator(
                    ".el-select-dropdown__item:visible:not(.is-disabled)"
                )
                if await options.count():
                    await self._dismiss_select_dropdown(select)
                    return False
                empty = dropdown.locator(".el-select-dropdown__empty:visible")
                if await empty.count():
                    text = normalize_label(await empty.first.inner_text())
                    if text in {normalize_label("无数据"), normalize_label("暂无数据")}:
                        await self._dismiss_select_dropdown(select)
                        return True
                await asyncio.sleep(0.05)
        except (TaobaoListingError, YouzanFormListingError):
            pass
        try:
            await self._dismiss_select_dropdown(select)
        except Exception:
            pass
        return False

    async def _set_select_values_directly(
        self,
        select: Any,
        values: Sequence[str],
        *,
        label: str,
        multi: bool,
    ) -> Tuple[str, ...]:
        """Apply the first Excel candidate through Element UI and read it back."""

        direct_values = tuple(str(value).strip() for value in values if str(value).strip())
        if not direct_values:
            raise YouzanFormListingError(
                "有赞属性“{0}”没有可直接填写的 Excel 值".format(label)
            )
        if not multi and len(direct_values) != 1:
            raise YouzanFormListingError(
                "有赞属性“{0}”是单选，不能直接填写多个值".format(label)
            )
        try:
            await self._dismiss_select_dropdown(select)
        except Exception:
            pass
        if multi:
            await self._clear_multi_select(select)
        applied = await select.evaluate(
            """async (element, payload) => {
              const values = payload.values;
              const multiple = payload.multiple;
              const component = element.__vue__;
              const input = multiple
                ? (element.querySelector('.el-select__tags input.el-select__input')
                  || element.querySelector('input'))
                : (element.querySelector('input.el-input__inner')
                  || element.querySelector('input'));
              if (component && typeof component.handleOptionSelect === 'function') {
                for (const value of values) {
                  component.handleOptionSelect({
                    value,
                    currentLabel: value,
                    label: value,
                    created: true,
                    disabled: false,
                    visible: true
                  }, true);
                  if (typeof component.$nextTick === 'function') {
                    await new Promise(resolve => component.$nextTick(resolve));
                  }
                }
                if (!multiple) {
                  component.selectedLabel = values[0];
                  component.query = values[0];
                }
                return 'element-ui';
              }
              if (!input || multiple) return '';
              input.removeAttribute('readonly');
              input.focus();
              const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, 'value'
              ).set;
              setter.call(input, values[0]);
              input.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'insertText', data: values[0]
              }));
              input.dispatchEvent(new Event('change', {bubbles: true}));
              input.dispatchEvent(new FocusEvent('blur', {bubbles: true}));
              return 'native-input';
            }""",
            {"values": direct_values, "multiple": multi},
        )
        if not applied:
            raise YouzanFormListingError(
                "有赞属性“{0}”无平台候选，且控件不支持直接输入".format(label)
            )
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            actual = await self._read_select_values(select, multi=multi)
            if all(
                any(
                    normalize_option(actual_value) == normalize_option(expected)
                    for actual_value in actual
                )
                for expected in direct_values
            ):
                try:
                    await self._dismiss_select_dropdown(select)
                except Exception:
                    pass
                if self.logger is not None:
                    self.logger.info(
                        "有赞属性“%s”无精确平台候选，已直接填写 Excel 首选值：%s；方式=%s",
                        label,
                        " / ".join(direct_values),
                        applied,
                    )
                return actual
            await asyncio.sleep(0.05)
        raise YouzanFormListingError(
            "有赞属性“{0}”直接填写后回读失败：期望 {1}，页面为 {2}".format(
                label, direct_values, await self._read_select_values(select, multi=multi)
            )
        )

    async def fill_category_attributes(
        self, fields: YouzanFields, *, style_code: str
    ) -> Mapping[str, Any]:
        page_items = await self._attribute_items()
        assignments = await self._attribute_assignments(fields.fields, page_items)
        applied: Dict[str, Tuple[str, ...]] = {}
        skipped: Dict[str, str] = {}
        unmatched_required = []
        for normalized_page, (page_label, item) in page_items.items():
            required = await self._attribute_is_required(item)
            assignment = assignments.get(normalized_page)
            if normalize_label(page_label) == normalize_label("货号"):
                assignment = (page_label, style_code)
            if assignment is None:
                if required:
                    unmatched_required.append(page_label)
                continue
            _source, expected = assignment
            actual = await self._fill_attribute(
                page_label,
                item,
                expected,
                exact_values=self._special_attribute_values(
                    fields.fields, page_label, expected
                ),
                required=required,
            )
            if actual is None:
                skipped[page_label] = expected
            else:
                applied[page_label] = actual
        if unmatched_required:
            raise YouzanFormListingError(
                "Excel 中缺少有赞必填属性：" + "、".join(unmatched_required)
            )
        if self.logger is not None:
            self.logger.info("有赞类目属性填写完成：已填 %s 项", len(applied))
        return {
            "attributes": applied,
            "skipped_no_exact_candidate": skipped,
            "unmatched_page_fields": tuple(
                page_label
                for key, (page_label, _item) in page_items.items()
                if key not in assignments and normalize_label(page_label) != normalize_label("货号")
            ),
        }

    async def _batch_input(self, label: str) -> Any:
        if self.panel is None:
            raise YouzanFormListingError("请先调用 open() 打开有赞资料")
        direct = self.panel.locator(
            '[data-youzan-batch="{0}"] input:visible'.format(label)
        )
        if await direct.count() == 1:
            return direct.first
        nodes = self.panel.get_by_text(
            re.compile(r"^\s*{0}\s*[：:]?\s*$".format(re.escape(label)))
        )
        matches = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if not await node.is_visible():
                continue
            root = node
            for _depth in range(4):
                inputs = root.locator(
                    'input:not([type="hidden"]):not([readonly]):visible'
                )
                if await inputs.count() == 1 and not await root.locator(
                    ".el-table, table"
                ).count():
                    matches.append(inputs.first)
                    break
                root = root.locator("xpath=..")
        if len(matches) != 1:
            button = self.panel.get_by_role("button", name="批量设置", exact=True)
            if await button.count() == 1:
                root = button.first
                for _depth in range(7):
                    root = root.locator("xpath=..")
                    if await root.locator(".el-table, table").count():
                        continue
                    inputs = root.locator(
                        'input:not([type="hidden"]):not([readonly]):visible'
                    )
                    if await inputs.count() == len(YOUZAN_BATCH_ORDER):
                        return inputs.nth(YOUZAN_BATCH_ORDER.index(label))
            raise YouzanFormListingError(
                "有赞批量字段“{0}”输入框不是唯一项：{1}".format(
                    label, len(matches)
                )
            )
        return matches[0]

    @staticmethod
    async def _enter_number_as_user(input_box: Any, expected: str) -> None:
        await input_box.click()
        await input_box.fill("")
        await input_box.press_sequentially(expected)
        await input_box.press("Tab")

    async def _fill_batch_number(self, label: str, expected: str) -> str:
        try:
            number = Decimal(expected)
        except InvalidOperation as exc:
            raise YouzanFormListingError("Excel 有赞{0}不是数字：{1!r}".format(label, expected)) from exc
        if not number.is_finite() or number < 0:
            raise YouzanFormListingError("Excel 有赞{0}必须是非负数".format(label))
        input_box = await self._batch_input(label)
        if not _numeric_equal((await input_box.input_value()).strip(), expected):
            await self._enter_number_as_user(input_box, expected)
        actual = (await input_box.input_value()).strip()
        if not _numeric_equal(actual, expected):
            raise YouzanFormListingError(
                "有赞批量字段“{0}”回读失败：{1!r}".format(label, actual)
            )
        return actual

    @staticmethod
    def _expected_sku_values(fields: Mapping[str, str]) -> Mapping[str, str]:
        expected = {
            "价格": _required_excel_value(
                fields,
                ("价格", "一口价", "基本售价", "商品价格", "售卖价", "售价"),
                "价格",
            ),
            "库存": _required_excel_value(fields, ("数量", "库存"), "库存"),
            "重量(kg)": "1",
        }
        price = Decimal(expected["价格"])
        inventory = Decimal(expected["库存"])
        if not price.is_finite() or price <= 0:
            raise YouzanFormListingError("Excel 有赞价格必须大于 0")
        if not inventory.is_finite() or inventory < 0 or inventory % 1:
            raise YouzanFormListingError("Excel 有赞库存必须是非负整数")
        return expected

    async def _sku_table_snapshot(self) -> Mapping[str, Any]:
        if self.panel is None:
            raise YouzanFormListingError("请先调用 open() 打开有赞资料")
        tables = self.panel.locator(".el-table:visible")
        if await tables.count() == 0:
            tables = self.panel.locator("table:visible")
        matches = []
        for index in range(await tables.count()):
            table = tables.nth(index)
            snapshot = await table.evaluate(
                """root => {
                  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
                  const headers = Array.from(root.querySelectorAll(
                    '.el-table__header-wrapper th, thead th'
                  )).map(node => clean(node.innerText));
                  const rows = Array.from(root.querySelectorAll(
                    '.el-table__body-wrapper tbody tr, tbody tr'
                  )).map(row => Array.from(row.querySelectorAll(':scope > td')).map(cell => {
                    const inputs = Array.from(cell.querySelectorAll('input'))
                      .filter(input => input.type !== 'checkbox' && input.type !== 'radio')
                      .map(input => clean(input.value));
                    return inputs.length ? inputs.join('|') : clean(cell.innerText);
                  }));
                  return {headers, rows};
                }"""
            )
            normalized = tuple(normalize_label(value) for value in snapshot["headers"])
            if all(
                any(
                    header == normalize_label(label)
                    or header.startswith(normalize_label(label))
                    for header in normalized
                )
                for label in YOUZAN_BATCH_ORDER
            ):
                matches.append(snapshot)
        if len(matches) != 1:
            raise YouzanFormListingError("有赞 SKU 表格不是唯一项：{0}".format(len(matches)))
        return matches[0]

    @staticmethod
    def _sku_column_index(headers: Sequence[str], label: str) -> int:
        wanted = normalize_label(label)
        indexes = [
            index
            for index, header in enumerate(headers)
            if normalize_label(header) == wanted
            or normalize_label(header).startswith(wanted)
        ]
        if len(indexes) != 1:
            raise YouzanFormListingError(
                "有赞 SKU 表格列“{0}”不是唯一项：{1}".format(label, indexes)
            )
        return indexes[0]

    def _validate_sku_snapshot(
        self, snapshot: Mapping[str, Any], expected: Mapping[str, str]
    ) -> Tuple[Mapping[str, str], ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        raw_rows = tuple(snapshot.get("rows", ()))
        if not raw_rows:
            raise YouzanFormListingError("有赞 SKU 表格没有可校验的明细行")
        indexes = {label: self._sku_column_index(headers, label) for label in expected}
        rows = []
        errors = []
        for row_number, raw_row in enumerate(raw_rows, 1):
            actual = {
                label: str(raw_row[index]) if index < len(raw_row) else ""
                for label, index in indexes.items()
            }
            for label, expected_value in expected.items():
                if not _numeric_equal(actual[label], expected_value):
                    errors.append("第{0}行{1}={2!r}".format(row_number, label, actual[label]))
            rows.append(actual)
        if errors:
            raise YouzanFormListingError("有赞批量设置后校验失败：" + "；".join(errors[:12]))
        return tuple(rows)

    async def fill_sku_batch(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        expected = self._expected_sku_values(fields)
        for label, value in expected.items():
            await self._fill_batch_number(label, value)
        if self.panel is None:
            raise YouzanFormListingError("请先调用 open() 打开有赞资料")
        button = self.panel.get_by_role("button", name="批量设置", exact=True)
        if await button.count() != 1:
            raise YouzanFormListingError("有赞“批量设置”按钮不是唯一项：{0}".format(await button.count()))
        if self.logger is not None:
            self.logger.info("有赞 SKU 批量值已填，点击一次“批量设置”")
        await button.first.click()
        deadline = asyncio.get_running_loop().time() + 15
        previous: Optional[Tuple[Mapping[str, str], ...]] = None
        stable = 0
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                rows = self._validate_sku_snapshot(
                    await self._sku_table_snapshot(), expected
                )
                if rows == previous:
                    stable += 1
                else:
                    previous = rows
                    stable = 1
                if stable >= 2:
                    if self.logger is not None:
                        self.logger.info("有赞 SKU 批量设置回读通过：%s 行", len(rows))
                    return {
                        "batch_clicked": True,
                        "row_count": len(rows),
                        "values": expected,
                        "rows": rows,
                    }
            except YouzanFormListingError as exc:
                last_error = exc
            await asyncio.sleep(0.15)
        raise YouzanFormListingError(str(last_error or "有赞批量设置超时"))

    async def _select_exact_unique(self, item: Any, expected: str, label: str) -> str:
        selects = item.locator(".el-select:visible")
        if await selects.count() != 1:
            raise YouzanFormListingError("有赞{0}下拉框不是唯一项".format(label))
        select = selects.first
        try:
            actual = await self._select_values(
                select,
                ((expected,),),
                label=label,
                multi=False,
            )
        except TaobaoListingError as exc:
            if "未找到可见选项" not in str(exc):
                raise YouzanFormListingError(str(exc).replace("淘宝", "有赞")) from exc
            try:
                await self._dismiss_select_dropdown(select)
            except Exception:
                pass
            actual = None
        if actual is not None:
            return actual[0]

        # ``None`` can mean either no exact platform candidate or an ambiguous
        # duplicate.  Re-open briefly and preserve the duplicate guard before
        # applying the user-approved direct-input fallback.
        matches: List[Mapping[str, Any]] = []
        try:
            await self._open_select(select, multi=False)
            _dropdown, options = await self._visible_dom_options(
                select, timeout_seconds=0.75
            )
            matches = [
                option
                for option in options
                if normalize_option(option.get("name", ""))
                == normalize_option(expected)
            ]
        except TaobaoListingError:
            matches = []
        finally:
            try:
                await self._dismiss_select_dropdown(select)
            except Exception:
                pass
        if len(matches) > 1:
            raise YouzanFormListingError(
                "有赞{0}候选不是唯一项：{1}".format(label, len(matches))
            )
        direct = await self._set_select_values_directly(
            select,
            (expected,),
            label=label,
            multi=False,
        )
        return direct[0]

    async def _total_weight_input(self) -> Any:
        try:
            item = await self._form_item("重量")
        except TaobaoListingError as exc:
            raise YouzanFormListingError(str(exc).replace("淘宝", "有赞")) from exc
        inputs = item.locator(
            'input:not([readonly]):not([type="hidden"]):visible'
        )
        if await inputs.count() != 1:
            raise YouzanFormListingError("有赞商品总重量输入框不是唯一项")
        return inputs.first

    async def _fill_total_weight(self) -> str:
        input_box = await self._total_weight_input()
        if not _numeric_equal((await input_box.input_value()).strip(), "1"):
            await self._enter_number_as_user(input_box, "1")
        actual = (await input_box.input_value()).strip()
        if not _numeric_equal(actual, "1"):
            raise YouzanFormListingError("有赞商品总重量回读失败：{0!r}".format(actual))
        return "1"

    async def fill_sales_and_logistics(self, garment_kind: str) -> Mapping[str, Any]:
        template = YOUZAN_FREIGHT_TEMPLATES.get(garment_kind)
        if template is None:
            raise YouzanFormListingError("无法判定有赞运费品类：{0}".format(garment_kind))
        weight = await self._fill_total_weight()
        try:
            inventory = await self._ensure_radio("库存扣减方式", "付款减库存")
            delivery_checked = await self._ensure_checkbox("配送方式", "快递发货")
            freight_item = await self._form_item("运费设置")
        except TaobaoListingError as exc:
            raise YouzanFormListingError(str(exc).replace("淘宝", "有赞")) from exc
        if not delivery_checked:
            raise YouzanFormListingError("有赞快递发货勾选失败")
        selected_template = await self._select_exact_unique(
            freight_item, template, "运费模板"
        )
        report = {
            "weight": weight,
            "inventory_deduction": inventory,
            "delivery": ("快递发货",),
            "freight_template": selected_template,
        }
        if self.logger is not None:
            self.logger.info("有赞库存、配送与运费模板已填写并回读通过")
        return report

    async def _choice_is_checked(
        self, form_label: str, option_text: str, input_type: str
    ) -> bool:
        try:
            item = await self._form_item(form_label)
        except TaobaoListingError as exc:
            raise YouzanFormListingError(str(exc).replace("淘宝", "有赞")) from exc
        selector = "label.el-radio" if input_type == "radio" else "label.el-checkbox"
        matches = []
        for index in range(await item.locator(selector).count()):
            option = item.locator(selector).nth(index)
            label = option.locator(
                ".el-radio__label" if input_type == "radio" else ".el-checkbox__label"
            ).first
            if await label.count() and normalize_label(
                await label.inner_text()
            ) == normalize_label(option_text):
                matches.append(option)
        if len(matches) != 1:
            raise YouzanFormListingError(
                "有赞“{0}”选项“{1}”不是唯一项：{2}".format(
                    form_label, option_text, len(matches)
                )
            )
        return await matches[0].locator("input[type={0}]".format(input_type)).first.is_checked()

    async def verify_persisted_values(
        self, fields: YouzanFields
    ) -> Mapping[str, Any]:
        """Read critical values after save without changing the form."""

        actual_category = await self._category_text()
        target = self._category_target_parts(fields.category_path[0])
        actual_parts = _category_parts(actual_category)
        category_matches = (
            actual_parts == target
            if len(target) > 1
            else bool(actual_parts) and actual_parts[-1] == target[0]
        )
        if not category_matches:
            raise YouzanFormListingError(
                "有赞保存后类目回读失败：{0!r}".format(actual_category)
            )

        persisted_attributes = await self._verify_persisted_attributes(fields)

        expected_sku = self._expected_sku_values(fields.fields)
        rows = self._validate_sku_snapshot(
            await self._sku_table_snapshot(), expected_sku
        )
        weight = (await (await self._total_weight_input()).input_value()).strip()
        if not _numeric_equal(weight, "1"):
            raise YouzanFormListingError(
                "有赞保存后商品总重量回读失败：{0!r}".format(weight)
            )
        inventory_checked = await self._choice_is_checked(
            "库存扣减方式", "付款减库存", "radio"
        )
        delivery_checked = await self._choice_is_checked(
            "配送方式", "快递发货", "checkbox"
        )
        if not inventory_checked or not delivery_checked:
            raise YouzanFormListingError(
                "有赞保存后库存扣减或配送方式没有持久化"
            )
        try:
            freight_item = await self._form_item("运费设置")
            freight_select = freight_item.locator(".el-select:visible")
            if await freight_select.count() != 1:
                raise YouzanFormListingError("有赞保存后运费模板下拉框不是唯一项")
            freight_values = await self._read_select_values(
                freight_select.first, multi=False
            )
        except TaobaoListingError as exc:
            raise YouzanFormListingError(str(exc).replace("淘宝", "有赞")) from exc
        expected_template = YOUZAN_FREIGHT_TEMPLATES.get(fields.garment_kind)
        if (
            expected_template is None
            or len(freight_values) != 1
            or normalize_option(freight_values[0]) != normalize_option(expected_template)
        ):
            raise YouzanFormListingError(
                "有赞保存后运费模板回读失败：{0}".format(freight_values)
            )
        errors = await self._visible_validation_errors()
        if errors:
            raise YouzanFormListingError(
                "有赞保存后仍有页面校验错误：" + "；".join(errors)
            )
        return {
            "category": actual_category,
            "attributes": persisted_attributes,
            "sku_values": expected_sku,
            "row_count": len(rows),
            "weight": weight,
            "inventory_deduction": "付款减库存",
            "delivery": ("快递发货",),
            "freight_template": freight_values[0],
        }

    async def _verify_persisted_attributes(
        self, fields: YouzanFields
    ) -> Mapping[str, Tuple[str, ...]]:
        """Read back every Excel-mapped category attribute without writing."""

        page_items = await self._attribute_items()
        assignments = await self._attribute_assignments(fields.fields, page_items)
        persisted: Dict[str, Tuple[str, ...]] = {}
        errors: List[str] = []
        for normalized_page, (_source, expected) in assignments.items():
            page_label, item = page_items[normalized_page]
            groups = (
                tuple((str(value),) for value in special)
                if (
                    special := self._special_attribute_values(
                        fields.fields, page_label, expected
                    )
                ) is not None
                else selection_value_groups(page_label, expected)
            )
            selects = item.locator(":scope > .el-form-item__content .el-select:visible")
            if await selects.count() == 1:
                select = selects.first
                multi = await select.locator(".el-select__tags").count() > 0
                actual = await self._read_select_values(select, multi=multi)
            else:
                inputs = item.locator(
                    ':scope > .el-form-item__content input:not([readonly]):not([type="hidden"]):visible'
                )
                if await inputs.count() != 1:
                    errors.append("{0}=控件不唯一".format(page_label))
                    continue
                actual = ((await inputs.first.input_value()).strip(),)
            if not groups or not all(
                any(
                    normalize_option(actual_value) == normalize_option(candidate)
                    for actual_value in actual
                    for candidate in group
                )
                for group in groups
            ):
                errors.append(
                    "{0}=期望 {1}，页面为 {2}".format(page_label, groups, actual)
                )
                continue
            persisted[page_label] = actual
        if errors:
            raise YouzanFormListingError(
                "有赞保存后类目属性回读失败：" + "；".join(errors[:12])
            )
        return persisted

    async def _visible_validation_errors(self) -> Tuple[str, ...]:
        if self.panel is None:
            return ()
        return tuple(
            await self.panel.evaluate(
                """root => Array.from(root.querySelectorAll('.el-form-item__error'))
                  .filter(error => {
                    const style = getComputedStyle(error);
                    return error.getClientRects().length > 0
                      && style.display !== 'none' && style.visibility !== 'hidden';
                  })
                  .map(error => String(error.innerText || '').trim())
                  .filter((value, index, values) => value && values.indexOf(value) === index)"""
            )
        )

    async def apply_excel_fields(
        self, fields: YouzanFields, *, style_code: str
    ) -> Mapping[str, Any]:
        product_type = await self.select_physical_product()
        category = await self.apply_category(fields.category_path)
        attributes = await self.fill_category_attributes(fields, style_code=style_code)
        sku_batch = await self.fill_sku_batch(fields.fields)
        logistics = await self.fill_sales_and_logistics(fields.garment_kind)
        errors = await self._visible_validation_errors()
        if errors:
            raise YouzanFormListingError("有赞页面校验错误：" + "；".join(errors))
        return {
            "product_type": product_type,
            "category": category,
            "attributes": attributes,
            "sku_batch": sku_batch,
            "sales_and_logistics": logistics,
        }


__all__ = [
    "YOUZAN_FREIGHT_TEMPLATES",
    "YouzanFormListing",
    "YouzanFormListingError",
]
