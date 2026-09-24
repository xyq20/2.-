"""DOM writer and post-save verifier for the WeChat Store product tab.

The common runner owns save/publish actions.  This adapter only fills the
platform form and reads it back; publishing remains disabled by the registry.
"""

from __future__ import annotations

from field_policies import without_color_attributes

from store_freight import sync_store_freight

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from attribute_runtime import AttributeRequest
from canonical_fields import is_learning_managed_field
from learning_models import CandidateValue, canonical_sha256
from money_values import MoneyValueError, normalize_money_value
from platform_candidate_source import (
    CandidateSourceError,
    DomCandidate,
    reconcile_candidates,
)
from wxsph_listing import parse_wxsph_attribute_fields

from taobao_listing import (
    TaobaoListingError,
    excel_aliases,
    material_name_groups,
    normalize_label,
    normalize_option,
    preferred_exact_candidate_label,
    selection_value_groups,
    selection_value_groups_for_control,
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
    fields: Mapping[str, str], aliases: Sequence[str], label: str, *, money: bool = False
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
    if money:
        try:
            matches = [
                (key, normalize_money_value(value)) for key, value in matches
            ]
        except MoneyValueError as exc:
            raise WxsphFormListingError(f"Excel 微信小店{label}{exc}") from exc
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


def _material_name_candidates(value: str) -> Tuple[str, ...]:
    """Return one slash-joined OR group per comma-separated material.

    Excel uses ``/`` as ordered OR.  A composition suffix belongs to the
    material alternative itself; commas, enumeration commas and semicolons
    introduce another material that a multi-select must choose as well.
    """
    return tuple("/".join(group) for group in material_name_groups(value))


def _material_name(value: str) -> str:
    candidates = _material_name_candidates(value)
    return candidates[0] if candidates else ""


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
    # 微信小店的“材质成分”是普通文本框。多成分时必须保留 Excel
    # 的完整组合（例如“棉94%，氨纶6%”），不能只提取第一种材质。
    source = str(value).strip()
    if len(re.findall(r"\d+(?:\.\d+)?\s*[%％]", source)) >= 2:
        return source
    name = _material_name(value)
    percentage = _material_percentage(value)
    if percentage is None:
        return name
    return "{0}{1}%".format(name, percentage)


class WxsphFormListing(YouzanFormListing):
    """Fill and read back WeChat Store attributes, SKU values and weight."""

    attribute_platform_id = "wxsph"

    # 快麦在共享编辑抽屉中连续切换平台时，微信小店的页签和
    # 基础信息会先显示，类目属性则由另一个异步请求稍后渲染。
    attribute_wait_timeout_seconds = 30.0
    attribute_retry_after_seconds = 12.0
    attribute_stable_seconds = 0.75

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
        # Install before the product editor is opened whenever the runner can
        # construct this collector early. FastMai may eagerly request WeChat's
        # category schema during the first drawer load and never request it
        # again when the fifth platform tab is selected.
        self._start_api_capture()
        self.base_item_id = ""
        self._resolved_api_options: Dict[str, Dict[str, Tuple[str, str]]] = {}

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
        previous = self._api_observations.get(path)
        if (
            path == "/wxsph/getCategoryProperties.json"
            and previous is not None
            and previous.get("attribute_fields")
            and not observation.get("attribute_fields")
        ):
            # The page can issue a second, auxiliary category-properties call
            # with an empty data set after the real leaf-category response. Do
            # not let that later response erase the usable schema captured for
            # the form currently visible in the drawer.
            return
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

    async def _captured_api_attribute_field_count(self) -> int:
        """Return the number of parsed category fields captured so far."""
        tasks = tuple(getattr(self, "_api_capture_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return sum(
            len(tuple(observation.get("attribute_fields", ())))
            for observation in getattr(self, "_api_observations", {}).values()
        )

    async def _request_api_attribute_schema(self) -> bool:
        """Fetch the current category schema when the reused drawer stays quiet."""
        base_item_id = str(getattr(self, "base_item_id", "") or "").strip()
        if not base_item_id:
            return False
        try:
            payloads = await self.page.evaluate(
                """async ({baseItemId}) => {
                  const requestJson = async (path, params) => {
                    const url = new URL(path, window.location.origin);
                    for (const [key, value] of Object.entries(params)) {
                      url.searchParams.set(key, String(value));
                    }
                    const response = await fetch(url.toString(), {
                      credentials: 'same-origin',
                      headers: {'Accept': 'application/json'}
                    });
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    return await response.json();
                  };
                  const detail = await requestJson('/wxsph/detail.json', {
                    baseItemId, api_name: 'wxsph_detail'
                  });
                  const unwrap = value => {
                    let current = value;
                    for (let attempt = 0; attempt < 4; attempt += 1) {
                      if (!current || typeof current !== 'object') break;
                      if ('result' in current && 'data' in current) {
                        if (current.result === true || String(current.result) === '1') {
                          current = current.data; continue;
                        }
                        return {};
                      }
                      if (typeof current.success === 'boolean' && 'data' in current) {
                        if (current.success) { current = current.data; continue; }
                        return {};
                      }
                      if ('code' in current && 'data' in current) {
                        const code = String(current.code).toUpperCase();
                        if (['0', '1', '200', 'OK', 'SUCCESS'].includes(code)) {
                          current = current.data; continue;
                        }
                        return {};
                      }
                      break;
                    }
                    return current || {};
                  };
                  const detailBody = unwrap(detail);
                  const categoryId = detailBody.categoryId || detailBody.category_id || '';
                  if (!categoryId) return {detail, categoryId: '', properties: null};
                  const properties = await requestJson(
                    '/wxsph/getCategoryProperties.json',
                    {shopId: '', categoryId, api_name: 'wxsph_getCategoryProperties'}
                  );
                  return {detail, categoryId: String(categoryId), properties};
                }""",
                {"baseItemId": base_item_id},
            )
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning("微信小店主动读取类目 JSON 失败：%s", exc)
            return False
        if not isinstance(payloads, Mapping):
            return False
        properties = payloads.get("properties")
        fields = parse_wxsph_attribute_fields(properties)
        category_id = str(payloads.get("categoryId") or "").strip()
        if not fields or not category_id:
            return False
        self._api_observations["/wxsph/getCategoryProperties.json"] = {
            "path": "/wxsph/getCategoryProperties.json",
            "http_status": 200,
            "body": "json",
            "attribute_labels": self._api_attribute_labels(properties),
            "attribute_fields": fields,
            "category_id": category_id,
        }
        if self.logger is not None:
            self.logger.info(
                "微信小店类目属性接口 JSON 已主动获取：类目字段 %s 个",
                len(fields),
            )
        return True

    async def _ensure_api_attribute_schema(self) -> None:
        """Retrigger the current tab once when a shared drawer reused stale DOM."""
        if getattr(self, "attribute_runtime", None) is None:
            return
        if await self._captured_api_attribute_field_count():
            return
        if await self._request_api_attribute_schema():
            return
        if self.logger is not None:
            self.logger.warning(
                "微信小店类目属性 DOM 已就绪，但接口 JSON 尚未返回；"
                "重新激活当前页签一次"
            )
        await self._reactivate_attribute_tab()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            if await self._captured_api_attribute_field_count():
                if self.logger is not None:
                    self.logger.info("微信小店类目属性接口 JSON 已重新获取")
                return
            await asyncio.sleep(0.1)

    async def _resolve_learning_select_groups(
        self,
        page_label: str,
        item: Any,
        groups: Sequence[Sequence[str]],
    ) -> Optional[Tuple[str, ...]]:
        runtime = getattr(self, "attribute_runtime", None)
        if runtime is None:
            return None
        managed = is_learning_managed_field("wxsph", page_label)
        try:
            field, category_id = await self._captured_api_field(page_label)
        except WxsphFormListingError as exc:
            if self.logger is not None:
                self.logger.info(
                    "微信小店属性“%s”：接口字段未定位，"
                    "改用当前 DOM 匹配/直接输入与回读：%s",
                    page_label,
                    exc,
                )
            return None
        if not field.source_id:
            if self.logger is not None:
                self.logger.info(
                    "微信小店属性“%s”：接口字段 ID 为空，改用当前 DOM 匹配与回读",
                    page_label,
                )
            return None
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
            visible_candidates = reconcile_candidates(field.option_values, dom_values)
        except CandidateSourceError as exc:
            if not managed:
                return None
            raise WxsphFormListingError(
                "微信小店属性“{0}”接口候选与页面候选不一致：{1}".format(
                    page_label, exc.reason_code
                )
            ) from exc

        # The live Element dropdown can be virtualized around the persisted
        # value, so its DOM is only a verified slice rather than the complete
        # candidate list.  Once the API field is correlated with that slice,
        # retain the complete, uniquely identified API options for ordered-OR
        # resolution.  Otherwise keep the conservative visible-DOM result.
        api_candidates = tuple(field.option_values)
        api_ids = tuple(value.value_id.strip() for value in api_candidates)
        api_labels = tuple(
            normalize_option(value.label) for value in api_candidates
        )
        dom_labels = {
            normalize_option(value.label) for value in dom_values if value.enabled
        }
        api_is_usable = bool(api_candidates) and all(api_ids) and all(api_labels)
        api_is_usable = api_is_usable and len(api_ids) == len(set(api_ids))
        api_is_usable = api_is_usable and len(api_labels) == len(set(api_labels))
        api_is_usable = api_is_usable and bool(set(api_labels).intersection(dom_labels))
        candidates = api_candidates if api_is_usable else visible_candidates
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
        candidate_labels = tuple(value.label for value in candidates)
        resolved_values = []
        resolved_options: Dict[str, Tuple[str, str]] = {}
        for group in groups:
            approval_request = AttributeRequest(
                platform_id="wxsph", category_leaf_id=category_id,
                field_id=str(field.source_id), field_label=page_label,
                candidates=tuple(CandidateValue(v.value_id, v.label) for v in candidates),
                excel_value="", evidence={}, custom_allowed=bool(field.custom_allowed),
                schema_version=schema_version,
            )
            approved = (runtime.confirmed_choice(approval_request)
                        if callable(getattr(runtime, "confirmed_choice", None)) else None)
            if approved is not None:
                resolved_values.append(approved.label)
                resolved_options[normalize_option(approved.label)] = (approved.value_id, approved.label)
                if self.logger:
                    self.logger.info("微信小店属性“%s”：直接复用运营审核值 %s，不试填 Excel 原值", page_label, approved.label)
                continue
            request_candidates, excel_value = await self._excel_before_review(
                select, candidates, group, label=page_label,
                preflight_request=AttributeRequest(
                    platform_id="wxsph", category_leaf_id=category_id,
                    field_id=str(field.source_id), field_label=page_label,
                    candidates=tuple(CandidateValue(v.value_id, v.label) for v in candidates),
                    excel_value="/".join(group), evidence={},
                    custom_allowed=bool(field.custom_allowed), schema_version=schema_version,
                ),
            )
            resolved = await runtime.resolve(
                AttributeRequest(
                    platform_id="wxsph",
                    category_leaf_id=category_id,
                    field_id=str(field.source_id),
                    field_label=page_label,
                    candidates=request_candidates,
                    excel_value=excel_value,
                    evidence={"excel": bool(excel_value.strip())},
                    custom_allowed=bool(field.custom_allowed),
                    schema_version=schema_version,
                    control_type="select",
                )
            )
            if resolved is None:
                return ()
            resolved_values.append(resolved.label)
            resolved_options[normalize_option(resolved.label)] = (
                resolved.value_id,
                resolved.label,
            )
        self._resolved_api_options[normalize_label(page_label)] = resolved_options
        return tuple(resolved_values)

    async def _read_select_values(self, select: Any, *, multi: bool) -> Tuple[str, ...]:
        if multi:
            # Collapsed tags say “已选择 N”, not the selected values. Reading
            # the live selection is safe; all mutations still go through DOM.
            selected = await select.evaluate("""element => {
              const c = element.__vue__;
              if (!c || !Array.isArray(c.selected)) return null;
              return c.selected.map(o => String(
                o.currentLabel ?? o.label ?? o.displayName ?? o.value ?? '').trim());
            }""")
            if selected is not None:
                return tuple(value for value in selected if value)
        return await super()._read_select_values(select, multi=multi)

    async def _clear_multi_select(self, select: Any) -> None:
        current = await self._read_select_values(select, multi=True)
        if not current:
            return
        if await select.locator('.el-select__tags .el-tag__close').count():
            await super()._clear_multi_select(select)
            current = await self._read_select_values(select, multi=True)
        # Some folded multi-selects expose no close icons. Deselect their
        # actual selected options, not a tag count or a cached option index.
        for value in current:
            await self._open_select(select, multi=True)
            dropdown, _options = await self._visible_dom_options(select)
            option = dropdown.locator(
                '.el-select-dropdown__item.selected:visible:not(.is-disabled)'
            ).filter(has_text=re.compile(r'^\s*' + re.escape(value) + r'\s*$'))
            if await option.count() != 1:
                raise TaobaoListingError(f'微信小店无法唯一定位已选项以清空：{value}')
            await option.click(timeout=3000)
        remaining = await self._read_select_values(select, multi=True)
        if remaining:
            raise TaobaoListingError(f'微信小店清空多选失败，仍有：{remaining!r}')
        await self._dismiss_select_dropdown(select)

    async def _apply_resolved_api_options(
        self,
        page_label: str,
        item: Any,
        exact_values: Sequence[str],
    ) -> Optional[Tuple[str, ...]]:
        """Use JSON to resolve labels; select only through real DOM clicks.

        API IDs and Element option values need not use the same namespace.
        Never synthesize an option or call a component selection method.
        """
        known = self._resolved_api_options.get(normalize_label(page_label), {})
        expected = []
        for value in exact_values:
            record = known.get(normalize_option(value))
            if record is None:
                return None
            expected.append(record[1])
        if not expected:
            return None
        selects = item.locator(":scope > .el-form-item__content .el-select:visible")
        if await selects.count() != 1:
            return None
        select = selects.first
        multi = await select.locator(".el-select__tags").count() > 0
        actual = await self._read_select_values(select, multi=multi)
        if tuple(map(normalize_option, actual)) == tuple(map(normalize_option, expected)):
            return actual
        if multi:
            await self._clear_multi_select(select)
        for value in expected:
            await self._open_select(select, multi=multi)
            dropdown, options = await self._visible_dom_options(select)
            matches = self._matching_options(value, options)
            if len(matches) != 1:
                await self._dismiss_select_dropdown(select)
                return None
            # A locator is resolved again at click time; a cached array index
            # can point at another option after Element reorders its children.
            option = dropdown.locator(
                ".el-select-dropdown__item:visible:not(.is-disabled)"
            ).filter(has_text=re.compile(r"^\s*" + re.escape(str(matches[0]["name"])) + r"\s*$"))
            if await option.count() != 1:
                await self._dismiss_select_dropdown(select)
                return None
            await option.click(timeout=3000)
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            actual = await self._read_select_values(select, multi=multi)
            if tuple(map(normalize_option, actual)) == tuple(map(normalize_option, expected)):
                await self._dismiss_select_dropdown(select)
                if self.logger is not None:
                    self.logger.info(
                        "微信小店属性“%s”：JSON 确定目标，DOM 点击并回读 %s",
                        page_label, " / ".join(actual),
                    )
                return actual
            if asyncio.get_running_loop().time() >= deadline:
                await self._dismiss_select_dropdown(select)
                raise TaobaoListingError(
                    f"微信小店属性“{page_label}”DOM 点击回读不一致："
                    f"期望 {expected!r}，页面为 {list(actual)!r}"
                )
            await asyncio.sleep(0.05)

    async def _fill_attribute(
        self,
        page_label: str,
        item: Any,
        expected: str,
        *,
        exact_values: Optional[Sequence[str]] = None,
        required: bool,
    ) -> Optional[Tuple[str, ...]]:
        try:
            if exact_values is not None:
                applied = await self._apply_resolved_api_options(
                    page_label, item, exact_values
                )
                if applied is not None:
                    return applied
            return await super()._fill_attribute(
                page_label, item, expected, exact_values=exact_values,
                required=required,
            )
        except (TaobaoListingError, YouzanFormListingError) as exc:
            if getattr(self, "attribute_runtime", None) is None:
                raise
            return await self._review_failed_attribute(
                page_label, item, tuple(exact_values or (expected,)), str(exc)
            )

    async def _review_failed_attribute(self, page_label, item, expected, error):
        """A field write failure is reviewable, not a fatal platform failure."""
        select = item.locator(
            ":scope > .el-form-item__content .el-select:visible"
        ).first
        multi = await select.locator(".el-select__tags").count() > 0
        # Exhaust the custom input path before asking an operator.
        try:
            actual = await self._set_select_values_directly(
                select, expected, label=page_label, multi=multi
            )
            if actual and tuple(map(normalize_option, actual)) == tuple(map(normalize_option, expected)):
                return actual
        except (TaobaoListingError, YouzanFormListingError):
            pass
        await self._open_select(select, multi=multi)
        try:
            _dropdown, options = await self._visible_dom_options(select)
        finally:
            await self._dismiss_select_dropdown(select)
        # Do not reuse API IDs when API and rendered labels disagree.
        labels = tuple(dict.fromkeys(str(option.get("name") or "").strip()
            for option in options if not option.get("disabled") and option.get("name")))
        field, category_id = await self._captured_api_field(page_label)
        request = AttributeRequest(
            platform_id="wxsph", category_leaf_id=category_id,
            field_id=str(field.source_id), field_label=page_label,
            candidates=tuple(CandidateValue(label, label) for label in labels),
            excel_value="", custom_allowed=False,
            schema_version=canonical_sha256({"field": str(field.source_id),
                "category": category_id, "dom_labels": labels, "write_recovery": 1}),
            evidence={"force_review": True, "selection_only": True,
                "failed_value": " / ".join(expected), "write_error": error,
                "summary": "程序选择及手填未能通过回读，无法手填，请从页面候选中选择。运营确认值将覆盖 Excel 原值。"},
        )
        resolved = await self.attribute_runtime.resolve(request)
        if resolved is None:
            if self.logger:
                self.logger.warning("微信小店属性“%s”写入失败已汇总审核，继续后续字段", page_label)
            return None
        # Compare with the approved value, never with the old Excel value.
        return await self._select_values(select, ((resolved.label,),),
                                         label=page_label, multi=multi)

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
        if not hasattr(self, "_api_observations"):
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
            if await heading.evaluate("e => Boolean(e.closest('nav, .anchor-nav, [role=\"navigation\"]'))"):
                continue
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
        if (await tab.get_attribute("aria-selected") or "").casefold() == "true":
            base_tab = self.drawer.get_by_role(
                "tab", name="基础资料", exact=True
            )
            if await base_tab.count() and await base_tab.first.is_visible():
                await base_tab.first.click(timeout=10_000)
                try:
                    await tab.wait_for(state="visible", timeout=10_000)
                except Exception:
                    pass
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
        page_items = without_color_attributes(page_items)
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
        required: bool,
    ) -> Tuple[Optional[str], Optional[Tuple[str, ...]], str]:
        normalized = normalize_label(page_label)
        if normalized in _CONTENT_LABELS:
            if is_select:
                return expected, None, "select_content_band"
            # 百分比是独立字段：只在当前类目实际渲染该输入框时，
            # 使用分配给该字段的 Excel 值；不能从“面料材质”的 OR
            # 候选（例如 棉100%/棉/棉布）中擅自提取。
            percentage = _material_percentage(expected)
            return percentage, None, "numeric_percentage" if percentage else "optional_blank"

        if normalized in {
            normalize_label("面料材质"),
            normalize_label("里料材质"),
        }:
            materials = _material_name_candidates(expected)
            return "/".join(materials), None, "material_ordered_or"

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
        page_items = without_color_attributes(await self._attribute_items())
        await self._ensure_api_attribute_schema()
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
                fields.fields,
                page_label,
                expected,
                is_select=is_select,
                required=required,
            )
            rules[page_label] = rule
            if value is None:
                if required:
                    unmatched_required.append(page_label)
                else:
                    skipped_optional[page_label] = "Excel 未提供该材质的百分比"
                continue
            if is_select:
                select = item.locator(
                    ":scope > .el-form-item__content .el-select:visible"
                ).first
                multi = bool(
                    await select.count()
                    and await select.locator(".el-select__tags").count() > 0
                )
                groups = (
                    tuple((str(candidate),) for candidate in exact_values)
                    if exact_values is not None
                    else selection_value_groups_for_control(
                        page_label, value, multi=multi
                    )
                )
                resolved_values = await self._resolve_learning_select_groups(
                    page_label,
                    item,
                    groups,
                )
                if resolved_values == ():
                    skipped_optional[page_label] = "等待运营审核"
                    if self.logger is not None:
                        self.logger.info(
                            "微信小店属性“%s”已加入本平台待审核汇总，继续填写后续字段",
                            page_label,
                        )
                    continue
                if resolved_values is not None:
                    exact_values = resolved_values
            if is_select:
                fabric_label = normalize_label(page_label)
                fabric_values = exact_values
                if not fabric_values or len(fabric_values) < 2:
                    # 微信小店实际标签是“面料材质”，而 Excel 常用
                    # “材质成分/材质”承载原始多成分文本；从原值拆出
                    # 面料名称后统一走公共“多值失败回退其他”协议。
                    fabric_values = _material_name_candidates(expected)
                if (
                    fabric_label in {
                        normalize_label("面料"),
                        normalize_label("面料材质"),
                        normalize_label("面料俗称"),
                        normalize_label("材质"),
                    }
                    and len(fabric_values or ()) >= 2
                ):
                    actual = await self._raise_as_wxsph(
                        self._fill_fabric_attribute(
                            page_label,
                            fabric_values,
                            item=item,
                            raw_value=expected,
                            writer=lambda fallback_value, values: self._fill_attribute(
                                page_label,
                                item,
                                fallback_value,
                                exact_values=values,
                                required=required,
                            ),
                        )
                    )
                else:
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
                fields, ("售卖价", "售价", "价格", "基本售价", "商品价格"), "售卖价", money=True
            ),
            "市场价": _required_excel_value(
                fields, ("市场价", "价格", "吊牌价", "商品价格"), "市场价", money=True
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
        # 新版编辑器先选择预售粒度，才显示 SKU 的批量发货设置。
        modes = self.panel.get_by_role("radio", name="按规格预售", exact=True)
        if await modes.count():
            await self._ensure_dialog_radio(self.panel, "按规格预售")
            if self.logger is not None:
                self.logger.info("微信小店发货方式已确认：按规格预售")
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
        self, fields: WxsphFields, *,
        expected_attributes: Optional[Mapping[str, Sequence[str]]] = None,
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
            required = await self._attribute_is_required(item)
            value, exact_values, rule = self._resolved_attribute_value(
                fields.fields,
                page_label,
                expected,
                is_select=is_select,
                required=required,
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
                if expected_attributes is not None and page_label in expected_attributes:
                    # Carry the successfully written decision across the new
                    # reader instance; operator approval can supersede Excel.
                    exact_values = tuple(expected_attributes[page_label])
                    value = " / ".join(exact_values)
                groups = (
                    tuple((str(candidate),) for candidate in exact_values)
                    if exact_values is not None
                    else selection_value_groups_for_control(
                        page_label, str(value), multi=multi
                    )
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
                if exact_values is not None:
                    matches = matches and len(actual) == len(exact_values)
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
        self, fields: WxsphFields, *,
        expected_attributes: Optional[Mapping[str, Sequence[str]]] = None,
        expected_freight: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        """Verify critical WeChat Store values after a confirmed save."""

        try:
            attributes = await self._verify_persisted_attributes(
                fields, expected_attributes=expected_attributes
            )
            freight = await sync_store_freight(self, fields.fields, read_only=True, expected=expected_freight)
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
            "freight": freight,
        }

    async def apply_excel_fields(self, fields: WxsphFields) -> Mapping[str, Any]:
        try:
            attributes = await self.fill_category_attributes(fields)
            sku_batch = await self.fill_sku_batch(fields.fields)
            delivery = await self.apply_batch_delivery()
            weight = await self.fill_weight()
            freight = await sync_store_freight(self, fields.fields)
            errors = await self._visible_validation_errors()
            if (
                errors
                and not (
                    self.attribute_runtime is not None
                    and getattr(
                        self.attribute_runtime, "has_deferred_reviews", False
                    )
                )
            ):
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
            "freight": freight,
            "deferred_validation_errors": (
                errors
                if self.attribute_runtime is not None
                and getattr(
                    self.attribute_runtime, "has_deferred_reviews", False
                )
                else ()
            ),
        }


__all__ = [
    "WXSPH_BATCH_ORDER",
    "WXSPH_DELIVERY_DAYS",
    "WXSPH_DELIVERY_MODE",
    "WXSPH_DELIVERY_NODE",
    "WxsphFormListing",
    "WxsphFormListingError",
]
