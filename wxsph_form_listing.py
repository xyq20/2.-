"""DOM writer and post-save verifier for the WeChat Store product tab.

The common runner owns save/publish actions.  This adapter only fills the
platform form and reads it back; publishing remains disabled by the registry.
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
from wxsph_listing import parse_wxsph_attribute_fields

from taobao_listing import (
    TaobaoListingError,
    excel_aliases,
    normalize_label,
    normalize_option,
    selection_value_groups,
)
from wxsph_data import WxsphFields
from youzan_form_listing import YouzanFormListing, YouzanFormListingError


class WxsphFormListingError(RuntimeError):
    """A WeChat Store form problem that can be shown to the operator."""


WXSPH_BATCH_ORDER = ("售卖价", "市场价", "库存")
WXSPH_DELIVERY_MODE = "全款预售"
WXSPH_DELIVERY_NODE = "买家付款后"
WXSPH_DELIVERY_DAYS = "15"
WXSPH_INHERITED_FIELDS = frozenset(
    normalize_label(value)
    for value in (
        "商品分类",
        "商品标题",
        "商品描述",
        "商品图",
        "商品图片",
        "商品商家编码",
    )
)
WXSPH_FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    normalize_label("细节/工艺/流行元素"): tuple(
        # 微信小店把三种语义合并成一个控件，但用户给定的
        # 页面值是“口袋”。因此只读 Excel 的流行元素/款式细节，
        # 不把独立的“水洗”工艺错当成第二个冲突来源。
        normalize_label(value) for value in ("细节", "流行元素", "款式细节")
    ),
    normalize_label("适用对象"): tuple(
        # Excel 已有直接的“适用人群/适用对象”；独立的适用性别
        # 属于另一个维度，不可与它合并。
        normalize_label(value) for value in ("适用人群",)
    ),
    normalize_label("上市时间"): tuple(
        normalize_label(value)
        for value in ("上市年份季节", "上市时节", "上市年份")
    ),
    # 当前 Excel 的“裤脚口款式/服饰版型/裤脚款式/版型”是该
    # 平台的直接来源；“裤型/款式”是另一属性，不合并。
    normalize_label("版型"): (),
    normalize_label("面料材质"): tuple(
        normalize_label(value)
        for value in ("面料", "面料俗称", "水洗标", "吊牌图")
    ),
    normalize_label("里料材质"): (normalize_label("里料"),),
    normalize_label("材质成分"): (normalize_label("材质"),),
}
WXSPH_ATTRIBUTE_END_HEADINGS = re.compile(
    r"^\s*\*?\s*(?:规格明细|价格库存|图文信息|运费模板|上架设置|更多设置)\s*[：:]?\s*$"
)
_CONTENT_LABELS = frozenset(
    normalize_label(value)
    for value in ("里料材质成分含量", "材质成分含量", "面料材质成分含量")
)
_WXSPH_READ_ENDPOINTS = frozenset(
    {
        "/wxsph/detail.json",
        "/wxsph/getCategoryProperties.json",
        "/wxsph/getTemplateList.json",
    }
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
        raise WxsphFormListingError(
            "Excel 中缺少微信小店{0}字段（可识别：{1}）".format(
                label, "/".join(aliases)
            )
        )
    values = {value for _key, value in matches}
    if len(values) != 1:
        raise WxsphFormListingError(
            "微信小店{0}匹配到多个 Excel 字段：{1}".format(
                label, "、".join(key for key, _value in matches)
            )
        )
    return matches[0][1]


def _first_excel_value(
    fields: Mapping[str, str], aliases: Sequence[str]
) -> Optional[str]:
    wanted = {normalize_label(alias) for alias in aliases}
    for key, value in fields.items():
        if wanted.intersection(excel_aliases(key)):
            text = str(value).strip()
            if text:
                return text
    return None


def _material_name(value: str) -> str:
    text = str(value).strip()
    text = re.sub(r"\s*[（(]\s*\d+(?:\.\d+)?\s*[%％]\s*[）)]\s*$", "", text)
    text = re.sub(r"\s*/\s*\d+(?:\.\d+)?\s*[%％]\s*$", "", text)
    return text.strip()


def _material_percentage(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*[%％]", str(value))
    if match is None:
        return None
    number = Decimal(match.group(1))
    if not number.is_finite() or number < 0 or number > 100:
        return None
    return format(number.normalize(), "f")


def _material_content_text(value: str) -> str:
    name = _material_name(value)
    percentage = _material_percentage(value)
    if percentage is None:
        return name
    return "{0}{1}%".format(name, percentage)


class WxsphFormListing(YouzanFormListing):
    """Fill and read back WeChat Store attributes, SKU values and weight."""

    # 快麦在共享编辑抽屉中连续切换平台时，微信小店的页签和
    # 基础信息会先显示，类目属性则由另一个异步请求稍后渲染。
    attribute_wait_timeout_seconds = 30.0
    attribute_retry_after_seconds = 12.0
    attribute_stable_seconds = 0.75

    def _start_api_capture(self) -> None:
        previous_handler = getattr(self, "_api_response_handler", None)
        if previous_handler is not None:
            try:
                self.page.remove_listener("response", previous_handler)
            except Exception:
                pass
        self._api_observations: Dict[str, Mapping[str, Any]] = {}
        self._api_capture_tasks: List[asyncio.Task[Any]] = []

        def handle_response(response: Any) -> None:
            try:
                path = urlsplit(response.url).path
            except Exception:
                return
            if path not in _WXSPH_READ_ENDPOINTS:
                return
            task = asyncio.create_task(self._capture_api_response(path, response))
            self._api_capture_tasks.append(task)

        self._api_response_handler = handle_response
        self.page.on("response", handle_response)

    @staticmethod
    def _api_attribute_labels(payload: Any) -> Tuple[str, ...]:
        labels: List[str] = []
        label_keys = {
            "name",
            "label",
            "propertyName",
            "propName",
            "attrName",
            "attributeName",
        }

        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    if key in label_keys and isinstance(child, str):
                        text = child.strip()
                        if text and len(text) <= 80 and text not in labels:
                            labels.append(text)
                    visit(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit(child)
            elif isinstance(value, str):
                # 部分快麦接口把业务 data 再编码为一层 JSON 字符串。
                text = value.strip()
                if text.startswith(("{", "[")):
                    try:
                        visit(json.loads(text))
                    except (TypeError, ValueError):
                        pass

        visit(payload)
        return tuple(labels)

    async def _capture_api_response(self, path: str, response: Any) -> None:
        observation: Dict[str, Any] = {
            "path": path,
            "http_status": int(response.status),
            "body": "unavailable",
            "attribute_labels": (),
            "attribute_fields": (),
            "category_id": "",
        }
        try:
            payload = await response.json()
            observation["body"] = "json"
            observation["attribute_labels"] = self._api_attribute_labels(payload)
            if path == "/wxsph/getCategoryProperties.json":
                observation["attribute_fields"] = parse_wxsph_attribute_fields(
                    payload
                )
                query = parse_qs(urlsplit(response.url).query)
                category_ids = tuple(query.get("categoryId", ()))
                if len(category_ids) == 1:
                    observation["category_id"] = str(category_ids[0])
        except Exception:
            # 只记录脱敏后的结构摘要；响应正文和查询参数均不落盘。
            pass
        self._api_observations[path] = observation

    async def _captured_api_field(self, page_label: str) -> Tuple[Any, str]:
        tasks = tuple(getattr(self, "_api_capture_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        wanted = normalize_label(page_label)
        matches = []
        category_ids = []
        for observation in getattr(self, "_api_observations", {}).values():
            category_id = str(observation.get("category_id") or "").strip()
            if category_id and category_id not in category_ids:
                category_ids.append(category_id)
            for field in observation.get("attribute_fields", ()):
                if normalize_label(field.label) == wanted:
                    matches.append(field)
        unique = {
            (field.source_id, field.schema_key): field
            for field in matches
        }
        if len(unique) != 1:
            raise WxsphFormListingError(
                "微信小店属性“{0}”缺少唯一的接口 JSON 字段定义".format(
                    page_label
                )
            )
        if len(category_ids) != 1:
            raise WxsphFormListingError(
                "微信小店属性“{0}”缺少唯一的接口类目 ID".format(page_label)
            )
        return next(iter(unique.values())), category_ids[0]

    async def _resolve_learning_select_value(
        self,
        page_label: str,
        item: Any,
        excel_value: str,
    ) -> str:
        runtime = getattr(self, "attribute_runtime", None)
        if runtime is None:
            return excel_value
        field, category_id = await self._captured_api_field(page_label)
        if not field.source_id:
            raise WxsphFormListingError(
                "微信小店属性“{0}”的接口字段 ID 为空".format(page_label)
            )
        selects = item.locator(
            ":scope > .el-form-item__content .el-select:visible"
        )
        if await selects.count() != 1:
            raise WxsphFormListingError(
                "微信小店属性“{0}”下拉框不唯一".format(page_label)
            )
        select = selects.first
        multi = await select.locator(".el-select__tags").count() > 0
        await self._open_select(select, multi=multi)
        try:
            _dropdown, options = await self._visible_dom_options(select)
        finally:
            try:
                await self._dismiss_select_dropdown(select)
            except Exception:
                pass
        dom_values = tuple(
            DomCandidate(
                str(option.get("value") or ""),
                str(option.get("name") or ""),
                not bool(option.get("disabled")),
            )
            for option in options
        )
        try:
            candidates = reconcile_candidates(field.option_values, dom_values)
        except CandidateSourceError as exc:
            raise WxsphFormListingError(
                "微信小店属性“{0}”接口候选与页面候选不一致：{1}".format(
                    page_label, exc.reason_code
                )
            ) from exc
        schema_version = canonical_sha256(
            {
                "platform_id": "wxsph",
                "category_leaf_id": category_id,
                "field_id": str(field.source_id),
                "options": [
                    {"value_id": value.value_id, "label": value.label}
                    for value in candidates
                ],
            }
        )
        resolved = await runtime.resolve(
            AttributeRequest(
                platform_id="wxsph",
                category_leaf_id=category_id,
                field_id=str(field.source_id),
                field_label=page_label,
                candidates=tuple(
                    CandidateValue(value.value_id, value.label)
                    for value in candidates
                ),
                excel_value=str(excel_value).strip(),
                evidence={"excel": bool(str(excel_value).strip())},
                custom_allowed=bool(field.custom_allowed),
                schema_version=schema_version,
                control_type="select",
            )
        )
        return resolved.label

    async def _api_dom_validation(
        self, dom_labels: Sequence[str]
    ) -> Mapping[str, Any]:
        tasks = tuple(getattr(self, "_api_capture_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        handler = getattr(self, "_api_response_handler", None)
        if handler is not None:
            try:
                self.page.remove_listener("response", handler)
            except Exception:
                pass
            self._api_response_handler = None

        observations = getattr(self, "_api_observations", {})
        normalized_dom = {normalize_label(label): label for label in dom_labels}
        matching_by_endpoint: Dict[str, Tuple[str, ...]] = {}
        api_by_normalized: Dict[str, str] = {}
        candidate_label_count = 0
        for path, observation in observations.items():
            labels = tuple(observation.get("attribute_labels", ()))
            candidate_label_count += len(labels)
            matching = tuple(
                normalized_dom[normalize_label(label)]
                for label in labels
                if normalize_label(label) in normalized_dom
            )
            matching_by_endpoint[path] = tuple(dict.fromkeys(matching))
            for label in labels:
                normalized = normalize_label(label)
                if normalized in normalized_dom:
                    api_by_normalized[normalized] = normalized_dom[normalized]
        matched = tuple(
            label for label in dom_labels if normalize_label(label) in api_by_normalized
        )
        if api_by_normalized and len(matched) == len(tuple(dom_labels)):
            status = "matched_all"
        elif matched:
            status = "partial_match"
        elif observations:
            status = "endpoints_observed"
        else:
            status = "not_observed"
        return {
            "status": status,
            "endpoints": tuple(
                {
                    "path": path,
                    "http_status": observations[path]["http_status"],
                    "body": observations[path]["body"],
                    "matching_dom_attribute_labels": matching_by_endpoint[path],
                }
                for path in sorted(observations)
            ),
            "dom_attribute_labels": tuple(dom_labels),
            "api_candidate_label_count": candidate_label_count,
            "api_attribute_labels": tuple(api_by_normalized.values()),
            "matched_attribute_labels": matched,
        }

    async def _raise_as_wxsph(self, awaitable: Any) -> Any:
        try:
            return await awaitable
        except (YouzanFormListingError, TaobaoListingError) as exc:
            raise WxsphFormListingError(
                str(exc).replace("有赞", "微信小店").replace("淘宝", "微信小店")
            ) from exc

    async def open(self) -> "WxsphFormListing":
        self._start_api_capture()
        tab = self.drawer.get_by_role(
            "tab", name="微信小店（视频号）资料", exact=True
        )
        try:
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click(timeout=30_000)
            panel = self.drawer.get_by_role(
                "tabpanel", name="微信小店（视频号）资料", exact=True
            )
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise WxsphFormListingError(
                "找不到可切换的“微信小店（视频号）资料”页签"
            ) from exc
        self.panel = panel
        try:
            await super()._wait_for_loading_masks()
        except YouzanFormListingError as exc:
            raise WxsphFormListingError(str(exc).replace("有赞", "微信小店")) from exc
        if self.logger is not None:
            self.logger.info("微信小店（视频号）资料页签已打开")
        return self

    async def _attribute_vertical_bounds(self) -> Tuple[Optional[float], Optional[float]]:
        if self.panel is None:
            raise WxsphFormListingError("请先打开微信小店资料")
        start: Optional[float] = None
        end: Optional[float] = None
        headings = self.panel.get_by_text(
            re.compile(r"^\s*(?:类目属性|商品属性|规格明细|价格库存|图文信息|运费模板|上架设置|更多设置)\s*[：:]?\s*$")
        )
        for index in range(await headings.count()):
            heading = headings.nth(index)
            if not await heading.is_visible():
                continue
            box = await heading.bounding_box()
            if box is None:
                continue
            label = normalize_label(await heading.inner_text())
            if label in {normalize_label("类目属性"), normalize_label("商品属性")}:
                start = box["y"]
            elif start is not None and box["y"] > start:
                end = box["y"] if end is None else min(end, box["y"])
        return start, end

    async def _collect_attribute_items_once(self) -> Dict[str, Tuple[str, Any]]:
        if self.panel is None:
            raise WxsphFormListingError("请先打开微信小店资料")
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
            if not normalized or normalized in WXSPH_INHERITED_FIELDS:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            key = normalized if counts[normalized] == 1 else "{0}#{1}".format(
                normalized, counts[normalized]
            )
            result[key] = (label, item)
        return result

    async def _reactivate_attribute_tab(self) -> None:
        """在不刷新页面的前提下重新激活一次微信小店页签。"""
        tab = self.drawer.get_by_role(
            "tab", name="微信小店（视频号）资料", exact=True
        )
        await tab.wait_for(state="visible", timeout=10_000)
        await tab.click(timeout=10_000)
        panel = self.drawer.get_by_role(
            "tabpanel", name="微信小店（视频号）资料", exact=True
        )
        await panel.wait_for(state="visible", timeout=10_000)
        self.panel = panel
        try:
            await super()._wait_for_loading_masks(timeout_seconds=10)
        except YouzanFormListingError as exc:
            raise WxsphFormListingError(
                str(exc).replace("有赞", "微信小店")
            ) from exc

    async def _attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        """等待动态类目属性真实渲染且字段集合稳定。"""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.attribute_wait_timeout_seconds
        retried = False
        stable_signature: Optional[Tuple[str, ...]] = None
        stable_since: Optional[float] = None

        while loop.time() < deadline:
            items = await self._collect_attribute_items_once()
            now = loop.time()
            signature = tuple(items)
            if signature:
                if signature != stable_signature:
                    stable_signature = signature
                    stable_since = now
                elif stable_since is not None and (
                    now - stable_since >= self.attribute_stable_seconds
                ):
                    if self.logger is not None:
                        self.logger.info(
                            "微信小店类目属性已稳定渲染：%s 项（等待 %.1f 秒）",
                            len(items),
                            now - started,
                        )
                    return items
            else:
                stable_signature = None
                stable_since = None

            if (
                not retried
                and now - started >= self.attribute_retry_after_seconds
            ):
                retried = True
                if self.logger is not None:
                    self.logger.warning(
                        "微信小店类目属性仍未就绪，不刷新网页，重新激活页签后继续等待"
                    )
                await self._reactivate_attribute_tab()
                stable_signature = None
                stable_since = None
            await asyncio.sleep(0.25)

        raise WxsphFormListingError(
            "微信小店类目属性在 {0:.0f} 秒内未稳定渲染（已重试页签）".format(
                self.attribute_wait_timeout_seconds
            )
        )

    async def _attribute_assignments(
        self,
        fields: Mapping[str, str],
        page_items: Mapping[str, Tuple[str, Any]],
    ) -> Dict[str, Tuple[str, str]]:
        sources: Dict[str, List[Tuple[str, str]]] = {}
        for excel_key, raw_value in fields.items():
            excel_names = set(excel_aliases(excel_key))
            for normalized_page, (page_label, _item) in page_items.items():
                base = normalized_page.split("#", 1)[0]
                accepted = {base}
                explicit_aliases = WXSPH_FIELD_ALIASES.get(base)
                if explicit_aliases is None:
                    accepted.update(excel_aliases(page_label))
                else:
                    accepted.update(explicit_aliases)
                if excel_names.intersection(accepted):
                    sources.setdefault(normalized_page, []).append(
                        (str(excel_key), str(raw_value).strip())
                    )
        assignments: Dict[str, Tuple[str, str]] = {}
        for normalized_page, (page_label, _item) in page_items.items():
            matches = sources.get(normalized_page, ())
            if not matches:
                continue
            values = {value for _key, value in matches}
            if len(values) != 1:
                raise WxsphFormListingError(
                    "微信小店属性“{0}”匹配到多个 Excel 值：{1}".format(
                        page_label, "、".join(key for key, _value in matches)
                    )
                )
            assignments[normalized_page] = (page_label, matches[0][1])
        return assignments

    @staticmethod
    async def _editable_value_inputs(item: Any) -> List[Any]:
        candidates = item.locator(
            ':scope > .el-form-item__content input:not([readonly]):not([type="hidden"]):visible'
        )
        result = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            inside_select = await candidate.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-select ')]"
            ).count()
            if not inside_select:
                result.append(candidate)
        return result

    @classmethod
    async def _is_select(cls, item: Any) -> bool:
        # 成分含量在生产页是“可编辑数值框 + % 单位下拉”。
        # 有可编辑框时必须当成输入型，不能把单位选择器当成
        # “95%及以上”的属性下拉。
        if await cls._editable_value_inputs(item):
            return False
        return await item.locator(
            ":scope > .el-form-item__content .el-select:visible"
        ).count() > 0

    def _resolved_attribute_value(
        self,
        fields: Mapping[str, str],
        page_label: str,
        expected: str,
        *,
        is_select: bool,
    ) -> Tuple[Optional[str], Optional[Tuple[str, ...]], str]:
        normalized = normalize_label(page_label)
        if normalized in _CONTENT_LABELS:
            if is_select:
                return "95%及以上", ("95%及以上",), "select_content_band"
            aliases = (
                ("面料材质", "面料俗称", "水洗标", "吊牌图", "面料")
                if normalized == normalize_label("面料材质成分含量")
                else ("里料材质", "里料")
                if normalized == normalize_label("里料材质成分含量")
                else ("材质成分", "材质")
            )
            percentage = _material_percentage(_first_excel_value(fields, aliases))
            return percentage, None, "numeric_percentage" if percentage else "optional_blank"

        if normalized in {
            normalize_label("面料材质"),
            normalize_label("里料材质"),
        }:
            material = _material_name(expected)
            return material, (material,), "material_name"

        if normalized == normalize_label("材质成分") and not is_select:
            content = _material_content_text(expected)
            return content, None, "material_content_text"

        if normalized == normalize_label("弹力") and is_select:
            candidates = tuple(
                value for value in re.split(r"[/／]", str(expected)) if value.strip()
            )
            if any(normalize_option(value) in {normalize_option("无弹"), normalize_option("无弹力")} for value in candidates):
                return "{0}/无弹性".format(expected), None, "platform_value_alias"

        if normalized == normalize_label("适用季节") and is_select:
            if normalize_option(expected) == normalize_option("四季通用"):
                return "四季通用/四季皆可", None, "platform_value_alias"

        return expected, None, "excel"

    async def _fill_text_attribute(
        self, page_label: str, item: Any, expected: str
    ) -> Tuple[str, ...]:
        inputs = await self._editable_value_inputs(item)
        if len(inputs) != 1:
            raise WxsphFormListingError(
                "微信小店属性“{0}”可编辑输入框不是唯一项：{1}".format(
                    page_label, len(inputs)
                )
            )
        input_box = inputs[0]
        expected_text = str(expected).strip()
        if (await input_box.input_value()).strip() != expected_text:
            await input_box.fill(expected_text)
            await input_box.press("Tab")
        actual = (await input_box.input_value()).strip()
        if actual != expected_text:
            raise WxsphFormListingError(
                "微信小店属性“{0}”回读失败：{1!r}".format(page_label, actual)
            )
        return (actual,)

    async def fill_category_attributes(self, fields: WxsphFields) -> Mapping[str, Any]:
        page_items = await self._attribute_items()
        assignments = await self._attribute_assignments(fields.fields, page_items)
        applied: Dict[str, Tuple[str, ...]] = {}
        skipped_optional: Dict[str, str] = {}
        rules: Dict[str, str] = {}
        unmatched_required = []
        for normalized_page, (page_label, item) in page_items.items():
            required = await self._attribute_is_required(item)
            assignment = assignments.get(normalized_page)
            if assignment is None:
                if required:
                    unmatched_required.append(page_label)
                continue
            _source, expected = assignment
            is_select = await self._is_select(item)
            value, exact_values, rule = self._resolved_attribute_value(
                fields.fields, page_label, expected, is_select=is_select
            )
            rules[page_label] = rule
            if value is None:
                if required:
                    unmatched_required.append(page_label)
                else:
                    skipped_optional[page_label] = "Excel 未提供该材质的百分比"
                continue
            if is_select:
                value = await self._resolve_learning_select_value(
                    page_label,
                    item,
                    value,
                )
                exact_values = (value,)
                actual = await self._raise_as_wxsph(
                    self._fill_attribute(
                        page_label,
                        item,
                        value,
                        exact_values=exact_values,
                        required=required,
                    )
                )
            else:
                actual = await self._fill_text_attribute(page_label, item, value)
            if actual is not None:
                applied[page_label] = actual
        if unmatched_required:
            raise WxsphFormListingError(
                "Excel 中缺少微信小店必填属性：" + "、".join(unmatched_required)
            )
        if self.logger is not None:
            self.logger.info(
                "微信小店类目属性填写完成：已填 %s 项，按规则保持空白 %s 项",
                len(applied),
                len(skipped_optional),
            )
        return {
            "attributes": applied,
            "skipped_optional_percentages": skipped_optional,
            "rules": rules,
            "unmatched_page_fields": tuple(
                page_label
                for key, (page_label, _item) in page_items.items()
                if key not in assignments
            ),
        }

    async def _batch_input(self, label: str) -> Any:
        if self.panel is None:
            raise WxsphFormListingError("请先打开微信小店资料")
        direct = self.panel.locator(
            '[data-wxsph-batch="{0}"] input:visible'.format(label)
        )
        if await direct.count() == 1:
            return direct.first
        nodes = self.panel.get_by_text(
            re.compile(r"^\s*\*?\s*{0}\s*[：:]?\s*$".format(re.escape(label)))
        )
        matches = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if not await node.is_visible():
                continue
            root = node
            for _depth in range(5):
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
            raise WxsphFormListingError(
                "微信小店批量字段“{0}”输入框不是唯一项：{1}".format(
                    label, len(matches)
                )
            )
        return matches[0]

    @staticmethod
    def _expected_sku_values(fields: Mapping[str, str]) -> Mapping[str, str]:
        expected = {
            "售卖价": _required_excel_value(
                fields, ("售卖价", "售价", "价格", "基本售价", "商品价格"), "售卖价"
            ),
            "市场价": _required_excel_value(
                fields, ("市场价", "价格", "吊牌价", "商品价格"), "市场价"
            ),
            "库存": _required_excel_value(fields, ("数量", "库存"), "库存"),
        }
        for label in ("售卖价", "市场价"):
            try:
                value = Decimal(expected[label])
            except InvalidOperation as exc:
                raise WxsphFormListingError(
                    "Excel 微信小店{0}不是数字".format(label)
                ) from exc
            if not value.is_finite() or value <= 0:
                raise WxsphFormListingError(
                    "Excel 微信小店{0}必须大于 0".format(label)
                )
        inventory = Decimal(expected["库存"])
        if not inventory.is_finite() or inventory < 0 or inventory % 1:
            raise WxsphFormListingError("Excel 微信小店库存必须是非负整数")
        return expected

    async def _fill_batch_number(self, label: str, expected: str) -> str:
        input_box = await self._batch_input(label)
        if not _numeric_equal((await input_box.input_value()).strip(), expected):
            await self._enter_number_as_user(input_box, expected)
        actual = (await input_box.input_value()).strip()
        if not _numeric_equal(actual, expected):
            raise WxsphFormListingError(
                "微信小店批量字段“{0}”回读失败：{1!r}".format(label, actual)
            )
        return actual

    async def _sku_table_snapshot(self) -> Mapping[str, Any]:
        if self.panel is None:
            raise WxsphFormListingError("请先打开微信小店资料")
        tables = self.panel.locator(".el-table:visible")
        if await tables.count() == 0:
            tables = self.panel.locator("table:visible")
        matches = []
        for index in range(await tables.count()):
            snapshot = await tables.nth(index).evaluate(
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
                for label in WXSPH_BATCH_ORDER
            ):
                matches.append(snapshot)
        if len(matches) != 1:
            raise WxsphFormListingError(
                "微信小店 SKU 表格不是唯一项：{0}".format(len(matches))
            )
        return matches[0]

    @staticmethod
    def _sku_column_index(headers: Sequence[str], label: str) -> int:
        wanted = normalize_label(label)
        indexes = [
            index
            for index, header in enumerate(headers)
            if normalize_label(header) == wanted or normalize_label(header).startswith(wanted)
        ]
        if len(indexes) != 1:
            raise WxsphFormListingError(
                "微信小店 SKU 表格列“{0}”不是唯一项".format(label)
            )
        return indexes[0]

    def _validate_sku_snapshot(
        self, snapshot: Mapping[str, Any], expected: Mapping[str, str]
    ) -> Tuple[Mapping[str, str], ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        raw_rows = tuple(snapshot.get("rows", ()))
        if not raw_rows:
            raise WxsphFormListingError("微信小店 SKU 表格没有可校验的明细行")
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
            raise WxsphFormListingError(
                "微信小店批量设置后校验失败：" + "；".join(errors[:12])
            )
        return tuple(rows)

    async def fill_sku_batch(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        expected = self._expected_sku_values(fields)
        for label, value in expected.items():
            await self._fill_batch_number(label, value)
        if self.panel is None:
            raise WxsphFormListingError("请先打开微信小店资料")
        buttons = self.panel.get_by_role("button", name="批量设置", exact=True)
        if await buttons.count() != 1:
            raise WxsphFormListingError(
                "微信小店“批量设置”按钮不是唯一项：{0}".format(await buttons.count())
            )
        if self.logger is not None:
            self.logger.info("微信小店 SKU 批量值已填，点击一次“批量设置”")
        await buttons.first.click()
        deadline = asyncio.get_running_loop().time() + 15
        previous: Optional[Tuple[Mapping[str, str], ...]] = None
        stable = 0
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                rows = self._validate_sku_snapshot(await self._sku_table_snapshot(), expected)
                if rows == previous:
                    stable += 1
                else:
                    previous = rows
                    stable = 1
                if stable >= 2:
                    return {
                        "batch_clicked": True,
                        "row_count": len(rows),
                        "values": expected,
                        "rows": rows,
                    }
            except WxsphFormListingError as exc:
                last_error = exc
            await asyncio.sleep(0.15)
        raise WxsphFormListingError(str(last_error or "微信小店批量设置超时"))

    @staticmethod
    async def _ensure_dialog_radio(
        dialog: Any, option_text: str, *, contains: bool = False
    ) -> str:
        options = dialog.locator("label.el-radio:visible")
        matches = []
        wanted = normalize_label(option_text)
        for index in range(await options.count()):
            option = options.nth(index)
            actual = normalize_label(await option.inner_text())
            if actual == wanted or (contains and wanted in actual):
                matches.append(option)
        if len(matches) != 1:
            raise WxsphFormListingError(
                "微信小店发货弹窗单选项“{0}”不是唯一项：{1}".format(
                    option_text, len(matches)
                )
            )
        option = matches[0]
        radio = option.locator('input[type="radio"]').first
        if not await radio.is_checked():
            await option.click()
        if not await radio.is_checked():
            raise WxsphFormListingError(
                "微信小店发货弹窗单选项未生效：{0}".format(option_text)
            )
        return option_text

    @staticmethod
    async def _presale_days_input(dialog: Any) -> Any:
        direct = dialog.locator('[data-wxsph-presale-days]:visible')
        if await direct.count() == 1:
            return direct.first

        candidates = dialog.locator(
            'input:not([type="radio"]):not([type="checkbox"]):'
            'not([type="hidden"]):not([readonly]):visible'
        )
        if await candidates.count() == 1:
            return candidates.first

        labels = dialog.get_by_text(
            re.compile(r"^\s*\*?\s*发货时效\s*[：:]?\s*$")
        )
        matches = []
        for index in range(await labels.count()):
            label = labels.nth(index)
            if not await label.is_visible():
                continue
            root = label
            for _depth in range(5):
                inputs = root.locator(
                    'input:not([type="radio"]):not([type="checkbox"]):'
                    'not([type="hidden"]):not([readonly]):visible'
                )
                if await inputs.count() == 1:
                    matches.append(inputs.first)
                    break
                root = root.locator("xpath=..")
        if len(matches) != 1:
            raise WxsphFormListingError(
                "微信小店预售发货天数输入框不是唯一项：{0}".format(len(matches))
            )
        return matches[0]

    def _validate_delivery_snapshot(
        self, snapshot: Mapping[str, Any]
    ) -> Tuple[str, ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        rows = tuple(snapshot.get("rows", ()))
        if not rows:
            raise WxsphFormListingError("微信小店 SKU 表格没有可校验的发货方式")
        index = self._sku_column_index(headers, "发货方式")
        actual_values = []
        errors = []
        for row_number, row in enumerate(rows, 1):
            actual = str(row[index]) if index < len(row) else ""
            normalized = normalize_label(actual)
            if (
                normalize_label(WXSPH_DELIVERY_MODE) not in normalized
                or normalize_label("付款后") not in normalized
                or not re.search(r"15\s*天", actual)
            ):
                errors.append("第{0}行发货方式={1!r}".format(row_number, actual))
            actual_values.append(actual)
        if errors:
            raise WxsphFormListingError(
                "微信小店批量发货设置后校验失败：" + "；".join(errors[:12])
            )
        return tuple(actual_values)

    async def apply_batch_delivery(self) -> Mapping[str, Any]:
        if self.panel is None:
            raise WxsphFormListingError("请先打开微信小店资料")
        buttons = self.panel.get_by_role(
            "button", name="批量编辑发货", exact=True
        )
        if await buttons.count() != 1:
            raise WxsphFormListingError(
                "微信小店“批量编辑发货”按钮不是唯一项：{0}".format(
                    await buttons.count()
                )
            )
        await buttons.first.click()
        dialogs = self.page.locator(
            '.el-dialog:visible, [role="dialog"]:visible'
        ).filter(has_text="发货方式")
        try:
            await dialogs.first.wait_for(state="visible", timeout=15_000)
        except Exception as exc:
            raise WxsphFormListingError("微信小店“发货方式”弹窗未打开") from exc
        if await dialogs.count() != 1:
            raise WxsphFormListingError(
                "微信小店“发货方式”弹窗不是唯一项：{0}".format(
                    await dialogs.count()
                )
            )
        dialog = dialogs.first
        mode = await self._ensure_dialog_radio(dialog, WXSPH_DELIVERY_MODE)
        try:
            await dialog.locator("label.el-radio").filter(
                has_text=WXSPH_DELIVERY_NODE
            ).first.wait_for(state="visible", timeout=15_000)
        except Exception as exc:
            raise WxsphFormListingError(
                "微信小店全款预售的发货节点未加载"
            ) from exc
        node = await self._ensure_dialog_radio(
            dialog, WXSPH_DELIVERY_NODE, contains=True
        )
        days_input = await self._presale_days_input(dialog)
        if not _numeric_equal(
            (await days_input.input_value()).strip(), WXSPH_DELIVERY_DAYS
        ):
            await self._enter_number_as_user(days_input, WXSPH_DELIVERY_DAYS)
        days = (await days_input.input_value()).strip()
        if not _numeric_equal(days, WXSPH_DELIVERY_DAYS):
            raise WxsphFormListingError(
                "微信小店预售发货天数回读失败：期望 15，页面为 {0!r}".format(
                    days
                )
            )
        confirm = dialog.get_by_role("button", name="确定", exact=True)
        if await confirm.count() != 1:
            raise WxsphFormListingError(
                "微信小店发货弹窗“确定”按钮不是唯一项：{0}".format(
                    await confirm.count()
                )
            )
        await confirm.first.click()
        try:
            await dialog.wait_for(state="hidden", timeout=15_000)
        except Exception as exc:
            raise WxsphFormListingError("微信小店发货弹窗确认后未关闭") from exc

        deadline = asyncio.get_running_loop().time() + 15
        previous: Optional[Tuple[str, ...]] = None
        stable = 0
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                rows = self._validate_delivery_snapshot(
                    await self._sku_table_snapshot()
                )
                if rows == previous:
                    stable += 1
                else:
                    previous = rows
                    stable = 1
                if stable >= 2:
                    if self.logger is not None:
                        self.logger.info(
                            "微信小店发货方式已批量设置为全款预售，付款后 15 天发货"
                        )
                    return {
                        "mode": mode,
                        "node": node,
                        "days": days,
                        "row_count": len(rows),
                        "rows": rows,
                    }
            except WxsphFormListingError as exc:
                last_error = exc
            await asyncio.sleep(0.15)
        raise WxsphFormListingError(
            str(last_error or "微信小店批量发货方式回读超时")
        )

    async def _weight_input(self) -> Any:
        try:
            item = await self._form_item("重量")
        except TaobaoListingError as exc:
            raise WxsphFormListingError(str(exc).replace("淘宝", "微信小店")) from exc
        inputs = item.locator('input:not([readonly]):not([type="hidden"]):visible')
        if await inputs.count() != 1:
            raise WxsphFormListingError("微信小店商品重量输入框不是唯一项")
        return inputs.first

    async def fill_weight(self) -> str:
        input_box = await self._weight_input()
        if not _numeric_equal((await input_box.input_value()).strip(), "1"):
            await self._enter_number_as_user(input_box, "1")
        actual = (await input_box.input_value()).strip()
        if not _numeric_equal(actual, "1"):
            raise WxsphFormListingError("微信小店商品重量回读失败：{0!r}".format(actual))
        return "1"

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

    async def _verify_persisted_attributes(
        self, fields: WxsphFields
    ) -> Mapping[str, Any]:
        """Read every Excel-mapped attribute after save without changing it."""

        page_items = await self._attribute_items()
        assignments = await self._attribute_assignments(fields.fields, page_items)
        persisted: Dict[str, Tuple[str, ...]] = {}
        skipped_optional: Dict[str, str] = {}
        rules: Dict[str, str] = {}
        errors: List[str] = []

        for normalized_page, (_source, expected) in assignments.items():
            page_label, item = page_items[normalized_page]
            is_select = await self._is_select(item)
            value, exact_values, rule = self._resolved_attribute_value(
                fields.fields,
                page_label,
                expected,
                is_select=is_select,
            )
            rules[page_label] = rule

            if value is None:
                inputs = await self._editable_value_inputs(item)
                if len(inputs) != 1:
                    errors.append("{0}=可选空值控件不唯一".format(page_label))
                    continue
                actual_text = (await inputs[0].input_value()).strip()
                if actual_text:
                    errors.append(
                        "{0}=Excel 无百分比时应为空，页面为 {1!r}".format(
                            page_label, actual_text
                        )
                    )
                    continue
                skipped_optional[page_label] = "Excel 未提供该材质的百分比"
                continue

            if is_select:
                selects = item.locator(
                    ":scope > .el-form-item__content .el-select:visible"
                )
                if await selects.count() != 1:
                    errors.append("{0}=下拉控件不唯一".format(page_label))
                    continue
                select = selects.first
                multi = await select.locator(".el-select__tags").count() > 0
                actual = await self._read_select_values(select, multi=multi)
                groups = (
                    tuple((str(candidate),) for candidate in exact_values)
                    if exact_values is not None
                    else selection_value_groups(page_label, str(value))
                )
                matches = bool(groups) and all(
                    any(
                        normalize_option(actual_value)
                        == normalize_option(candidate)
                        for actual_value in actual
                        for candidate in group
                    )
                    for group in groups
                )
            else:
                inputs = await self._editable_value_inputs(item)
                if len(inputs) != 1:
                    errors.append("{0}=输入控件不唯一".format(page_label))
                    continue
                actual = ((await inputs[0].input_value()).strip(),)
                matches = (
                    _numeric_equal(actual[0], str(value))
                    if rule == "numeric_percentage"
                    else actual[0] == str(value).strip()
                )

            if not matches:
                errors.append(
                    "{0}=期望 {1!r}，页面为 {2}".format(
                        page_label, value, actual
                    )
                )
                continue
            persisted[page_label] = tuple(actual)

        if errors:
            raise WxsphFormListingError(
                "微信小店保存后类目属性回读失败：" + "；".join(errors[:12])
            )
        return {
            "attributes": persisted,
            "skipped_optional_percentages": skipped_optional,
            "rules": rules,
        }

    async def verify_persisted_values(
        self, fields: WxsphFields
    ) -> Mapping[str, Any]:
        """Verify critical WeChat Store values after a confirmed save."""

        try:
            attributes = await self._verify_persisted_attributes(fields)
            expected_sku = self._expected_sku_values(fields.fields)
            snapshot = await self._sku_table_snapshot()
            rows = self._validate_sku_snapshot(snapshot, expected_sku)
            delivery_rows = self._validate_delivery_snapshot(snapshot)
            weight = (await (await self._weight_input()).input_value()).strip()
            if not _numeric_equal(weight, "1"):
                raise WxsphFormListingError(
                    "微信小店保存后商品重量回读失败：{0!r}".format(weight)
                )
            errors = await self._visible_validation_errors()
            if errors:
                raise WxsphFormListingError(
                    "微信小店保存后仍有页面校验错误：" + "；".join(errors)
                )
        except Exception:
            await self._api_dom_validation(())
            raise

        dom_labels = tuple(attributes["attributes"]) + tuple(
            attributes["skipped_optional_percentages"]
        )
        return {
            "attributes": attributes,
            "sku_values": expected_sku,
            "row_count": len(rows),
            "delivery": {
                "mode": WXSPH_DELIVERY_MODE,
                "node": WXSPH_DELIVERY_NODE,
                "days": WXSPH_DELIVERY_DAYS,
                "rows": delivery_rows,
            },
            "weight": weight,
            "api_dom_validation": await self._api_dom_validation(dom_labels),
        }

    async def apply_excel_fields(self, fields: WxsphFields) -> Mapping[str, Any]:
        try:
            attributes = await self.fill_category_attributes(fields)
            sku_batch = await self.fill_sku_batch(fields.fields)
            delivery = await self.apply_batch_delivery()
            weight = await self.fill_weight()
            errors = await self._visible_validation_errors()
            if errors:
                raise WxsphFormListingError(
                    "微信小店页面校验错误：" + "；".join(errors)
                )
        except Exception:
            await self._api_dom_validation(())
            raise
        api_dom_validation = await self._api_dom_validation(
            tuple(attributes["attributes"])
            + tuple(attributes["skipped_optional_percentages"])
        )
        return {
            "attributes": attributes,
            "sku_batch": sku_batch,
            "delivery": delivery,
            "weight": weight,
            "api_dom_validation": api_dom_validation,
        }


__all__ = [
    "WXSPH_BATCH_ORDER",
    "WXSPH_DELIVERY_DAYS",
    "WXSPH_DELIVERY_MODE",
    "WXSPH_DELIVERY_NODE",
    "WxsphFormListing",
    "WxsphFormListingError",
]
