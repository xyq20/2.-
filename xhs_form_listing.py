"""DOM writer for FastMai's Xiaohongshu product-information tab.

The adapter stops after filling and validating the form.  It intentionally has
no save or publish method; the common runner owns the final write gate.
"""

from __future__ import annotations

from field_policies import without_color_attributes

from store_freight import sync_store_freight

import asyncio
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from attribute_runtime import AttributeRequest
from category_profile import category_search_terms
from canonical_fields import is_learning_managed_field
from learning_models import CandidateValue, canonical_sha256
from money_values import MoneyValueError, normalize_money_value
from platform_candidate_source import (
    CandidateSourceError,
    DomCandidate,
    reconcile_candidates,
)
from platform_schema import FieldOption, field_options
from taobao_listing import (
    TaobaoListing,
    TaobaoListingError,
    excel_aliases,
    normalize_label,
    normalize_option,
    parse_taobao_fabrics,
    parse_taobao_materials,
    selection_value_groups,
    selection_value_groups_for_control,
)
from xhs_data import XhsFields
from xhs_listing import normalize_xhs_attribute_options


class XhsFormListingError(RuntimeError):
    """A Xiaohongshu form problem that can be shown directly to the operator."""


XHS_INHERITED_FIELDS = frozenset(
    normalize_label(value)
    for value in ("商品分类", "商品标题", "商品名称", "商品英文名", "品牌", "货号")
)
XHS_FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    normalize_label("服装版型"): (
        normalize_label("服饰版型"),
        normalize_label("版型"),
    ),
    normalize_label("服饰版型"): (
        normalize_label("服装版型"),
        normalize_label("版型"),
    ),
    normalize_label("厚薄"): (normalize_label("厚度"),),
    normalize_label("面料"): (
        normalize_label("面料材质"),
        normalize_label("面料俗称"),
    ),
    normalize_label("材质成分"): (normalize_label("材质"),),
    normalize_label("上市年份季节"): (normalize_label("上市时节"),),
}
XHS_BATCH_ORDER = ("售价", "市场价", "库存")
XHS_ATTRIBUTE_END_HEADINGS = re.compile(r"^\s*价格库存\s*[：:]?\s*$")
XHS_CATEGORY_QUERY_PATH = "/category/base/queryCategoryList.json"
XHS_ATTRIBUTE_LIST_PATH = "/xhs/getAttributeList.json"
XHS_ATTRIBUTE_VALUES_PATH = "/xhs/getAttributeValues.json"
XHS_CATEGORY_RESULT_SELECTOR = (
    "[data-xhs-category]:visible, "
    ".el-popover.el-popper:visible .categoryList-wrap > .text.item:visible, "
    ".el-autocomplete-suggestion:visible li:visible, "
    ".el-autocomplete-suggestion:visible [role=option]:visible, "
    ".el-autocomplete-suggestion:visible .el-autocomplete-suggestion__item:visible"
)


def _numeric_equal(actual: str, expected: str) -> bool:
    try:
        return Decimal(actual.replace(",", "")) == Decimal(expected.replace(",", ""))
    except (InvalidOperation, ValueError):
        return False


def _is_nonnegative_decimal(value: str) -> bool:
    try:
        return Decimal(value) >= 0
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
        raise XhsFormListingError(
            "Excel 中缺少小红书{0}字段（可识别：{1}）".format(
                label, "/".join(aliases)
            )
        )
    if money:
        try:
            matches = [
                (key, normalize_money_value(value)) for key, value in matches
            ]
        except MoneyValueError as exc:
            raise XhsFormListingError(f"Excel 小红书{label}{exc}") from exc
    distinct = {value for _key, value in matches}
    if len(distinct) != 1:
        raise XhsFormListingError(
            "小红书{0}匹配到多个 Excel 字段：{1}".format(
                label, "、".join(key for key, _value in matches)
            )
        )
    return matches[0][1]


def title_without_neigborl(value: object) -> str:
    """Remove only the brand token forbidden by Xiaohongshu's title rule."""
    title = str(value or "").strip()
    cleaned = re.sub(r"neigborl", "", title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"([【\[（(])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([】\]）)])", r"\1", cleaned)
    cleaned = re.sub(r"([】\]）)])\s+", r"\1", cleaned)
    return cleaned.strip()


def _category_normalized(value: object) -> str:
    return re.sub(r"[\s>＞/／,，、;；]+", "", str(value or "")).casefold()


def _category_parts(value: object) -> Tuple[str, ...]:
    """Return the visible platform path as exact, ordered route segments."""
    return tuple(
        normalize_label(part)
        for part in re.split(r"[>＞/／]", str(value or ""))
        if normalize_label(part)
    )


def _contains_ordered_parts(
    candidate_parts: Sequence[str], expected_parts: Sequence[str]
) -> bool:
    """Exact segment matching only; never infer a category from a substring."""
    cursor = 0
    for expected in expected_parts:
        while cursor < len(candidate_parts) and candidate_parts[cursor] != expected:
            cursor += 1
        if cursor == len(candidate_parts):
            return False
        cursor += 1
    return True


class XhsFormListing(TaobaoListing):
    """Fill Xiaohongshu category data, presale, batch fields and 3:4 main art."""

    freight_platform_id = "xhs"

    # XHS remote selects render an exact server-search result as an Element
    # ``created`` option even though the control is not free-form.  It is safe
    # to click only because the inherited matcher still requires one exact,
    # visible and enabled DOM option, followed by value readback.
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
        self._xhs_api_generation = 0
        self._xhs_api_category_id = ""
        self._xhs_api_shop_id = ""
        self._xhs_api_fields: Dict[str, Tuple[Mapping[str, Any], ...]] = {}
        self._xhs_api_options: Dict[Tuple[int, str], Tuple[FieldOption, ...]] = {}
        self._xhs_active_option_attempts: set[Tuple[int, str]] = set()
        self._xhs_request_contexts: Dict[int, Tuple[str, int, str]] = {}
        self._xhs_response_tasks: set[asyncio.Task[Any]] = set()
        self._install_xhs_attribute_listener()

    @staticmethod
    def _request_parameter(request: Any, name: str) -> str:
        try:
            query = parse_qs(urlsplit(str(request.url)).query, keep_blank_values=True)
            values = [str(value).strip() for value in query.get(name, ()) if str(value).strip()]
        except Exception:
            values = []
        if len(values) == 1:
            return values[0]
        try:
            form = parse_qs(str(request.post_data or ""), keep_blank_values=True)
            values = [str(value).strip() for value in form.get(name, ()) if str(value).strip()]
        except Exception:
            values = []
        if len(values) == 1:
            return values[0]
        try:
            payload = getattr(request, "post_data_json", None)
            if callable(payload):
                payload = payload()
            if not isinstance(payload, Mapping):
                payload = json.loads(str(request.post_data or "{}"))
            candidates = []
            if isinstance(payload, Mapping):
                candidates.append(payload.get(name))
                for container_name in ("params", "data", "body"):
                    container = payload.get(container_name)
                    if isinstance(container, Mapping):
                        candidates.append(container.get(name))
            values = [
                str(value).strip()
                for value in candidates
                if value not in (None, "") and str(value).strip()
            ]
        except Exception:
            values = []
        return values[0] if len(values) == 1 else ""

    def _install_xhs_attribute_listener(self) -> None:
        if not hasattr(self.page, "on"):
            return

        def on_request(request: Any) -> None:
            try:
                path = urlsplit(str(request.url)).path
            except Exception:
                return
            if path == XHS_ATTRIBUTE_LIST_PATH:
                category_id = self._request_parameter(request, "leafCategoryId")
                if not category_id:
                    return
                self._xhs_api_generation += 1
                self._xhs_api_category_id = category_id
                self._xhs_api_shop_id = self._request_parameter(request, "shopId")
                self._xhs_api_fields = {}
                self._xhs_api_options = {}
                self._xhs_active_option_attempts = set()
                self._xhs_request_contexts[id(request)] = (
                    "fields",
                    self._xhs_api_generation,
                    category_id,
                )
            elif path == XHS_ATTRIBUTE_VALUES_PATH:
                attribute_id = next(
                    (
                        self._request_parameter(request, key)
                        for key in ("attributeId", "attributeV3Id", "attrId", "id")
                        if self._request_parameter(request, key)
                    ),
                    "",
                )
                if self._xhs_api_generation and attribute_id:
                    self._xhs_request_contexts[id(request)] = (
                        "values",
                        self._xhs_api_generation,
                        attribute_id,
                    )
                elif self.logger is not None:
                    try:
                        query_keys = sorted(
                            parse_qs(
                                urlsplit(str(request.url)).query,
                                keep_blank_values=True,
                            ).keys()
                        )
                    except Exception:
                        query_keys = []
                    try:
                        body = getattr(request, "post_data_json", None)
                        if callable(body):
                            body = body()
                        body_keys = sorted(body.keys()) if isinstance(body, Mapping) else []
                    except Exception:
                        body_keys = []
                    self.logger.warning(
                        "小红书候选接口请求未识别字段 ID：method=%s，query_keys=%s，body_keys=%s",
                        getattr(request, "method", ""),
                        query_keys,
                        body_keys,
                    )

        def on_response(response: Any) -> None:
            request = getattr(response, "request", None)
            context = self._xhs_request_contexts.pop(id(request), None)
            if context is None:
                return
            task = asyncio.create_task(self._consume_xhs_attribute_response(response, context))
            self._xhs_response_tasks.add(task)
            task.add_done_callback(self._xhs_response_tasks.discard)

        def on_request_failed(request: Any) -> None:
            self._xhs_request_contexts.pop(id(request), None)

        self.page.on("request", on_request)
        self.page.on("response", on_response)
        self.page.on("requestfailed", on_request_failed)

    async def _consume_xhs_attribute_response(
        self,
        response: Any,
        context: Tuple[str, int, str],
    ) -> None:
        kind, generation, identity = context
        if generation != self._xhs_api_generation:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if generation != self._xhs_api_generation:
            return
        data = self._xhs_payload_data(payload)
        if not isinstance(data, Mapping):
            return
        if kind == "fields":
            raw_fields = data.get("attributeV3s")
            if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
                return
            records: Dict[str, List[Mapping[str, Any]]] = {}
            for raw_field in raw_fields:
                if not isinstance(raw_field, Mapping):
                    continue
                field_id = str(raw_field.get("id") or "").strip()
                label = str(raw_field.get("name") or raw_field.get("label") or "").strip()
                if not field_id or not label:
                    continue
                records.setdefault(normalize_label(label), []).append(
                    {
                        "id": field_id,
                        "label": label,
                        "custom_allowed": raw_field.get("customizable") is True,
                    }
                )
                inline_values = raw_field.get("values")
                if isinstance(inline_values, Sequence) and not isinstance(
                    inline_values, (str, bytes)
                ):
                    self._xhs_api_options[(generation, field_id)] = field_options(
                        normalize_xhs_attribute_options(inline_values),
                        source="api",
                    )
            self._xhs_api_fields = {
                key: tuple(value) for key, value in records.items()
            }
            return

        raw_values = data.get("values")
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes)
        ):
            raw_values = data.get("attributeValueV3s")
        if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
            raw_values = ()
        options = field_options(
            normalize_xhs_attribute_options(raw_values),
            source="api",
        )
        self._xhs_api_options[(generation, identity)] = options
        if not options and self.logger is not None:
            self.logger.warning(
                "小红书候选接口返回未含候选数组：字段 %s，data_keys=%s",
                identity,
                sorted(data.keys()),
            )

    @staticmethod
    def _xhs_payload_data(payload: Any) -> Mapping[str, Any]:
        current = payload
        for _attempt in range(4):
            if not isinstance(current, Mapping):
                return {}
            if "result" in current and "data" in current:
                if current.get("result") is True or str(current.get("result")) == "1":
                    current = current.get("data")
                    continue
                return {}
            if isinstance(current.get("success"), bool) and "data" in current:
                if current.get("success"):
                    current = current.get("data")
                    continue
                return {}
            if "code" in current and "data" in current:
                if str(current.get("code")).upper() in {
                    "0", "1", "200", "OK", "SUCCESS"
                }:
                    current = current.get("data")
                    continue
                return {}
            if "data" in current and isinstance(current.get("data"), Mapping):
                current = current.get("data")
                continue
            return current
        return current if isinstance(current, Mapping) else {}

    async def _request_xhs_attribute_options(
        self, field_id: str
    ) -> Tuple[FieldOption, ...]:
        """Read one missing option list without depending on a UI refetch."""
        try:
            payload = await self.page.evaluate(
                """async ({shopId, attributeId}) => {
                  const url = new URL('/xhs/getAttributeValues.json', window.location.origin);
                  url.searchParams.set('shopId', shopId || '');
                  url.searchParams.set('attributeId', attributeId);
                  url.searchParams.set('api_name', 'xhs_getAttributeValues');
                  const response = await fetch(url.toString(), {
                    credentials: 'same-origin',
                    headers: {'Accept': 'application/json'}
                  });
                  if (!response.ok) throw new Error(`HTTP ${response.status}`);
                  return await response.json();
                }""",
                {
                    "shopId": self._xhs_api_shop_id,
                    "attributeId": field_id,
                },
            )
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning(
                    "小红书属性候选主动读取失败（字段 %s）：%s",
                    field_id,
                    exc,
                )
            return ()
        data = self._xhs_payload_data(payload)
        raw_values = data.get("values")
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes)
        ):
            raw_values = data.get("attributeValueV3s")
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes)
        ):
            raw_values = ()
        options = field_options(
            normalize_xhs_attribute_options(raw_values), source="api"
        )
        if options:
            self._xhs_api_options[(self._xhs_api_generation, field_id)] = options
        elif self.logger is not None:
            self.logger.warning(
                "小红书候选主动读取未含候选数组：字段 %s，data_keys=%s",
                field_id,
                sorted(data.keys()),
            )
        return options

    async def _drain_xhs_attribute_tasks(self) -> None:
        await asyncio.sleep(0)
        while self._xhs_response_tasks:
            await asyncio.gather(*tuple(self._xhs_response_tasks), return_exceptions=True)

    async def _captured_xhs_field(
        self,
        page_label: str,
        *,
        timeout_seconds: float = 3.0,
    ) -> Tuple[Mapping[str, Any], Tuple[FieldOption, ...], str]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await self._drain_xhs_attribute_tasks()
            records = self._xhs_api_fields.get(normalize_label(page_label), ())
            if len(records) == 1:
                field_id = str(records[0].get("id") or "")
                options = self._xhs_api_options.get(
                    (self._xhs_api_generation, field_id),
                    (),
                )
                attempt_key = (self._xhs_api_generation, field_id)
                if (
                    not options
                    and field_id
                    and self._xhs_api_category_id
                    and attempt_key not in self._xhs_active_option_attempts
                ):
                    self._xhs_active_option_attempts.add(attempt_key)
                    options = await self._request_xhs_attribute_options(field_id)
                if options and self._xhs_api_category_id:
                    return records[0], options, self._xhs_api_category_id
            await asyncio.sleep(0.05)
        records = self._xhs_api_fields.get(normalize_label(page_label), ())
        raise XhsFormListingError(
            "小红书属性“{0}”缺少唯一且完整的接口 JSON 候选：字段数 {1}".format(
                page_label,
                len(records),
            )
        )

    async def _wait_for_loading_masks(self, timeout_seconds: float = 45) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self.drawer.locator(".el-loading-mask:visible").count() == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)
        raise XhsFormListingError("小红书资料加载遮罩在 45 秒内未消失")

    async def open(self) -> "XhsFormListing":
        tab = self.drawer.get_by_role("tab", name="小红书资料", exact=True)
        try:
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click(timeout=30_000)
            panel = self.drawer.get_by_role("tabpanel", name="小红书资料", exact=True)
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise XhsFormListingError("找不到可切换的“小红书资料”页签") from exc
        self.panel = panel
        await self._wait_for_loading_masks()
        if self.logger is not None:
            self.logger.info("小红书资料页签已打开")
        return self

    async def _category_dialog(self) -> Any:
        visible_dialogs = self.page.locator(".el-dialog:visible")
        matches = []
        for index in range(await visible_dialogs.count()):
            dialog = visible_dialogs.nth(index)
            if normalize_label(await dialog.inner_text()).startswith(normalize_label("修改类目")):
                matches.append(dialog)
        if len(matches) == 1:
            return matches[0]

        role_dialogs = self.page.get_by_role("dialog", name="修改类目", exact=True)
        visible_roles = []
        for index in range(await role_dialogs.count()):
            dialog = role_dialogs.nth(index)
            if await dialog.is_visible():
                visible_roles.append(dialog)
        if len(visible_roles) == 1:
            return visible_roles[0]
        raise XhsFormListingError("小红书修改类目弹窗不是唯一项：{0}".format(len(matches)))

    async def _category_search_input(self, dialog: Any) -> Any:
        inputs = dialog.locator('input:not([type="hidden"]):visible')
        if await inputs.count() != 1:
            raise XhsFormListingError(
                "小红书修改类目弹窗搜索框不是唯一项：{0}".format(await inputs.count())
            )
        return inputs.first

    async def _category_result_nodes(self) -> List[Tuple[Any, str]]:
        """Read category search rows from the page-level Element portal.

        Element mounts the result list next to ``body`` rather than beneath the
        ``修改类目`` dialog.  Looking only inside the dialog made a visible
        result list appear empty to the automation.  Keep this list narrow to
        real option rows, rather than searching arbitrary dialog text.
        """
        nodes = self.page.locator(XHS_CATEGORY_RESULT_SELECTOR)
        found: List[Tuple[Any, str]] = []
        seen = set()
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            text = " ".join((await node.inner_text()).split())
            key = _category_normalized(text)
            if not key or key in seen:
                continue
            seen.add(key)
            found.append((node, text))
        return found

    @staticmethod
    def _choose_category_text(
        result_texts: Sequence[str], category_path: Sequence[str]
    ) -> Tuple[str, str]:
        """Select a single exact platform route from the Excel category path.

        A page may return a ready-made platform path such as
        ``男装 > 休闲裤 > 工装休闲裤``.  Prefer a complete ordered match.  Some
        source workbooks include an internal middle category that Xiaohongshu
        does not expose; in that case an exact first segment plus an exact,
        unique final leaf is still deterministic and is explicitly recorded in
        the report.  Any duplicate or incomplete result remains an error.
        """
        expected = tuple(normalize_label(part) for part in category_path)
        complete = [
            text
            for text in result_texts
            if _contains_ordered_parts(_category_parts(text), expected)
        ]
        if len(complete) == 1:
            return complete[0], "full_ordered_path"
        if len(complete) > 1:
            raise XhsFormListingError(
                "小红书类目完整路径不是唯一精确项：{0}".format(len(complete))
            )

        # Platform taxonomies can omit a supplier-internal middle layer.  This
        # fallback still requires both Excel endpoints to be exact and unique;
        # it never selects merely because a string contains a category word.
        if len(expected) >= 2:
            root_leaf = [
                text
                for text in result_texts
                if expected[0] in _category_parts(text)
                and _category_parts(text)
                and _category_parts(text)[-1] == expected[-1]
            ]
            if len(root_leaf) == 1:
                return root_leaf[0], "first_and_leaf_exact"
            if len(root_leaf) > 1:
                raise XhsFormListingError(
                    "小红书类目首段和末级叶子不是唯一精确项：{0}".format(
                        len(root_leaf)
                    )
                )

        # New-category workbooks may list cross-platform category hints rather
        # than one marketplace path.  Try the most specific exact leaf first;
        # every accepted leaf must still be unique in the platform JSON.
        for hint in category_search_terms(category_path):
            wanted = normalize_label(hint)
            leaf_matches = [
                text
                for text in result_texts
                if _category_parts(text) and _category_parts(text)[-1] == wanted
            ]
            # Excel 类目有时携带“男士/女士”等供应商层级，而平台结果
            # 只保留“男装/女装”根类目。用人群根类目消除同名末级项，
            # 例如“卫衣”同时出现在女装、童装、运动和男装下。
            gender_roots = ()
            expected_text = " ".join(expected)
            if "男士" in expected_text or "男装" in expected_text:
                gender_roots = ("男装",)
            elif "女士" in expected_text or "女装" in expected_text:
                gender_roots = ("女装",)
            if len(leaf_matches) > 1 and gender_roots:
                narrowed = [
                    text for text in leaf_matches
                    if any(root in _category_parts(text) for root in gender_roots)
                ]
                if len(narrowed) == 1:
                    return narrowed[0], "gender_root_exact_hint_leaf"
            if len(leaf_matches) == 1:
                return leaf_matches[0], "unique_exact_hint_leaf"
            if len(leaf_matches) > 1:
                raise XhsFormListingError(
                    "小红书类目提示词末级不是唯一精确项：{0}={1}".format(
                        hint, len(leaf_matches)
                    )
                )
        raise XhsFormListingError("小红书类目搜索结果中没有 Excel 的完整路径或唯一精确提示词")

    @staticmethod
    def _category_paths_from_json(payload: Any) -> Tuple[str, ...]:
        """Extract only display paths from FastMai's read-only category JSON."""
        if not isinstance(payload, Mapping):
            return ()
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return ()
        records = data.get("records")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            return ()
        paths: List[str] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            raw_parts = record.get("categoryNameList")
            if not isinstance(raw_parts, Sequence) or isinstance(
                raw_parts, (str, bytes)
            ):
                continue
            parts = tuple(str(part).strip() for part in raw_parts if str(part).strip())
            if parts:
                rendered = " > ".join(parts)
                if rendered not in paths:
                    paths.append(rendered)
        return tuple(paths)

    async def _wait_for_category_result(
        self, category_path: Sequence[str], json_paths: Sequence[str]
    ) -> Tuple[Any, str, str]:
        deadline = asyncio.get_running_loop().time() + 45
        last_results: List[Tuple[Any, str]] = []
        while asyncio.get_running_loop().time() < deadline:
            last_results = await self._category_result_nodes()
            if json_paths and last_results:
                try:
                    target_text, strategy = self._choose_category_text(
                        json_paths, category_path
                    )
                except XhsFormListingError as exc:
                    # JSON ambiguity is already complete evidence: do not
                    # wait for a DOM race to choose an arbitrary row.
                    if "不是唯一" in str(exc):
                        raise
                    await asyncio.sleep(0.15)
                    continue
                matches = [
                    (node, text)
                    for node, text in last_results
                    if _category_normalized(text) == _category_normalized(target_text)
                ]
                if len(matches) == 1:
                    node, text = matches[0]
                    return node, text, strategy
                if len(matches) > 1:
                    raise XhsFormListingError(
                        "小红书类目 JSON 对应的 DOM 行不是唯一项：{0}".format(
                            len(matches)
                        )
                    )
            await asyncio.sleep(0.15)
        if not json_paths:
            raise XhsFormListingError("小红书类目搜索 JSON 在 45 秒内未返回候选")
        rendered = "；".join(text for _node, text in last_results[:12])
        raise XhsFormListingError(
            "小红书类目 JSON 已定位，但对应 DOM 行未出现：{0}".format(
                rendered or "页面未返回候选"
            )
        )

    async def _click_category_segment(self, segment: str) -> None:
        """Fallback for a true cascader: click one exact visible child label."""
        deadline = asyncio.get_running_loop().time() + 45
        wanted = normalize_label(segment)
        while asyncio.get_running_loop().time() < deadline:
            results = await self._category_result_nodes()
            matches = [
                node
                for node, text in results
                if normalize_label(text) == wanted
            ]
            if len(matches) == 1:
                await matches[0].scroll_into_view_if_needed()
                await matches[0].click(timeout=10_000)
                await self._wait_for_loading_masks()
                return
            if len(matches) > 1:
                raise XhsFormListingError(
                    "小红书类目层级“{0}”不是唯一精确项：{1}".format(
                        segment, len(matches)
                    )
                )
            await asyncio.sleep(0.15)
        raise XhsFormListingError(
            "小红书类目搜索/下级菜单未出现精确项：{0}".format(segment)
        )

    @staticmethod
    def _selected_category_text(dialog_text: str) -> str:
        for line in reversed(dialog_text.splitlines()):
            match = re.match(r"^\s*已选\s*[：:]\s*(.*?)\s*$", line)
            if match:
                return match.group(1)
        return ""

    async def apply_category(self, category_path: Sequence[str]) -> Mapping[str, Any]:
        """Search the first Excel segment, then verify the complete route."""
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        path = tuple(str(item).strip() for item in category_path if str(item).strip())
        if not path:
            raise XhsFormListingError("Excel 小红书商品分类没有可选择的层级")
        modify = self.panel.get_by_role("button", name="修改类目", exact=True)
        if await modify.count() != 1:
            raise XhsFormListingError("小红书页面找不到唯一的“修改类目”按钮")
        started = asyncio.get_running_loop().time()
        await modify.click(timeout=30_000)
        dialog = await self._category_dialog()
        search = await self._category_search_input(dialog)
        json_paths: List[str] = []
        response_tasks: List[asyncio.Task[Any]] = []

        async def capture_category_response(response: Any) -> None:
            try:
                if XHS_CATEGORY_QUERY_PATH not in response.url:
                    return
                headers = await response.all_headers()
                content_type = str(headers.get("content-type") or "").casefold()
                if "json" not in content_type:
                    return
                for rendered in self._category_paths_from_json(await response.json()):
                    if rendered not in json_paths:
                        json_paths.append(rendered)
            except Exception:
                # A response can be aborted while the keyword is being typed;
                # the completed category response remains the only evidence we
                # use for a DOM click.
                return

        def on_category_response(response: Any) -> None:
            response_tasks.append(asyncio.create_task(capture_category_response(response)))

        self.page.on("response", on_category_response)
        try:
            # Only the actual search-result portal is scanned.  The page also
            # keeps a large cascader tree in the DOM; enumerating that tree was
            # the source of a 25+ second delay and is not valid click evidence.
            search_term = path[0]
            await search.fill(search_term, timeout=10_000)
            search_filled_at = asyncio.get_running_loop().time()
            target, target_text, strategy = await self._wait_for_category_result(
                path, json_paths
            )
            result_ready_at = asyncio.get_running_loop().time()
            await target.scroll_into_view_if_needed()
            await target.click(timeout=10_000)
            clicked = tuple(path)
        finally:
            self.page.remove_listener("response", on_category_response)
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)

        selected = ""
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            selected = self._selected_category_text(await dialog.inner_text())
            if selected:
                break
            await asyncio.sleep(0.1)
        selected_parts = _category_parts(selected)
        expected_parts = tuple(normalize_label(segment) for segment in path)
        selected_ok = _contains_ordered_parts(selected_parts, expected_parts)
        if strategy in {"first_and_leaf_exact", "gender_root_exact_hint_leaf"}:
            selected_ok = (
                (
                    expected_parts[0] in selected_parts
                    or (
                        strategy == "gender_root_exact_hint_leaf"
                        and any(root in selected_parts for root in ("男装", "女装"))
                    )
                )
                and bool(selected_parts)
                and (
                    selected_parts[-1] == expected_parts[-1]
                    or (
                        strategy == "gender_root_exact_hint_leaf"
                        and selected_parts[-1] in expected_parts
                    )
                )
            )
        elif strategy == "unique_exact_hint_leaf":
            selected_ok = bool(selected_parts) and selected_parts[-1] in set(
                expected_parts
            )
        if not selected_ok:
            raise XhsFormListingError(
                "小红书类目确认前路径不匹配：目标 {0}，页面为 {1!r}".format(
                    " / ".join(path), selected
                )
            )

        confirm = dialog.get_by_role("button", name=re.compile(r"^\s*确\s*定\s*$"))
        if await confirm.count() != 1:
            raise XhsFormListingError("小红书修改类目弹窗找不到唯一“确定”按钮")
        await confirm.click(timeout=10_000)
        await dialog.wait_for(state="hidden", timeout=30_000)
        self.category_clicked = True
        await self._wait_for_loading_masks()
        if self.logger is not None:
            self.logger.info(
                "已按 Excel 层级选择小红书类目：%s；页面路径：%s；规则：%s；"
                "耗时：弹窗/输入 %.2fs，JSON+DOM %.2fs，总计 %.2fs",
                " / ".join(path),
                selected,
                strategy,
                search_filled_at - started,
                result_ready_at - search_filled_at,
                asyncio.get_running_loop().time() - started,
            )
        return {
            "path": path,
            "clicked_segments": tuple(clicked),
            "selected": selected,
            "matched_result": target_text,
            "search_term": search_term,
            "strategy": strategy,
            "json_candidate_count": len(json_paths),
        }

    async def _heading_vertical_bounds(self) -> Tuple[Optional[float], Optional[float]]:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        start: Optional[float] = None
        end: Optional[float] = None
        headings = self.panel.get_by_text(
            re.compile(r"^\s*(?:商品属性|价格库存)\s*[：:]?\s*$")
        )
        for index in range(await headings.count()):
            heading = headings.nth(index)
            if not await heading.is_visible():
                continue
            text = re.sub(r"\s*[：:]\s*$", "", (await heading.inner_text()).strip())
            box = await heading.bounding_box()
            if box is None:
                continue
            if text == "商品属性":
                start = box["y"]
            elif XHS_ATTRIBUTE_END_HEADINGS.fullmatch(text):
                if start is not None and box["y"] > start:
                    end = box["y"] if end is None else min(end, box["y"])
        return start, end

    async def _attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        top, end = await self._heading_vertical_bounds()
        items = self.panel.locator(".el-form-item:visible")
        result: Dict[str, Tuple[str, Any]] = {}
        counts: Dict[str, int] = {}
        for index in range(await items.count()):
            item = items.nth(index)
            label_node = item.locator(":scope > .el-form-item__label").first
            if not await label_node.count():
                label_node = item.locator(".el-form-item__label").first
            if not await label_node.count() or not await label_node.is_visible():
                continue
            box = await item.bounding_box()
            if box is not None:
                if top is not None and box["y"] <= top:
                    continue
                if end is not None and box["y"] >= end:
                    continue
            label = re.sub(r"^\s*\*\s*", "", (await label_node.inner_text()).strip())
            normalized = normalize_label(label)
            if not normalized or normalized in XHS_INHERITED_FIELDS:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            key = normalized if counts[normalized] == 1 else "{0}#{1}".format(
                normalized, counts[normalized]
            )
            result[key] = (label, item)
        if not result:
            raise XhsFormListingError("小红书商品属性区域为空或未渲染")
        return result

    @staticmethod
    async def _attribute_is_required(item: Any) -> bool:
        """Read Element's required marker without relying on CSS pseudo text."""
        classes = set((await item.get_attribute("class") or "").split())
        if "is-required" in classes:
            return True
        label = item.locator(":scope > .el-form-item__label").first
        if not await label.count():
            label = item.locator(".el-form-item__label").first
        return bool(
            await label.count()
            and re.match(r"^\s*\*", (await label.inner_text()).strip())
        )

    async def _attribute_assignments(
        self,
        fields: Mapping[str, str],
        page_items: Mapping[str, Tuple[str, Any]],
    ) -> Dict[str, Tuple[str, str]]:
        # An explicitly named Excel column wins over an alias.  For example,
        # a source sheet can contain both ``厚度=常规款`` and ``厚薄=常规``;
        # Xiaohongshu's field is ``厚薄`` and must use the latter rather than
        # treating the two source concepts as conflicting values.
        direct_sources: Dict[str, List[Tuple[str, str]]] = {}
        page_items = without_color_attributes(page_items)
        alias_sources: Dict[str, List[Tuple[str, str]]] = {}
        for excel_key, raw_value in fields.items():
            aliases = set(excel_aliases(excel_key))
            for normalized_page, (page_label, _item) in page_items.items():
                base = normalized_page.split("#", 1)[0]
                if base in aliases:
                    direct_sources.setdefault(normalized_page, []).append(
                        (str(excel_key), str(raw_value).strip())
                    )
                elif aliases.intersection(XHS_FIELD_ALIASES.get(base, ())):
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
                raise XhsFormListingError(
                    "小红书属性“{0}”匹配到多个 Excel 字段：{1}".format(
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
        if normalized in {normalize_label("面料"), normalize_label("材质成分")}:
            try:
                materials = (
                    parse_taobao_fabrics(fields)
                    if normalized == normalize_label("面料")
                    else parse_taobao_materials(fields)
                )
            except TaobaoListingError as exc:
                raise XhsFormListingError(str(exc).replace("淘宝", "小红书")) from exc
            values = tuple(component.name for component in materials)
            return values or None
        if normalized == normalize_label("是否加绒"):
            mapped = {"是": "加绒", "否": "不加绒"}.get(str(expected).strip())
            return (mapped,) if mapped else None
        return None

    async def _resolve_learning_select_groups(
        self,
        page_label: str,
        select: Any,
        groups: Sequence[Sequence[str]],
    ) -> Optional[Tuple[str, ...]]:
        runtime = self.attribute_runtime
        managed = is_learning_managed_field("xhs", page_label)
        if runtime is None:
            raise XhsFormListingError("小红书属性学习运行器未启用")
        multi = await select.locator(".el-select__tags").count() > 0
        await self._open_select(select, multi=multi)
        try:
            _dropdown, dom_options = await self._visible_dom_options(select)
            try:
                field, api_options, category_id = await self._captured_xhs_field(
                    page_label
                )
            except XhsFormListingError as exc:
                if self.logger is not None:
                    self.logger.info(
                        "小红书属性“%s”：接口字段或候选未定位，"
                        "改用当前 DOM 匹配/直接输入与回读：%s",
                        page_label,
                        exc,
                    )
                return None
        finally:
            try:
                await self._dismiss_select_dropdown(select)
            except Exception:
                pass
        try:
            candidates = reconcile_candidates(
                api_options,
                tuple(
                    DomCandidate(
                        (
                            ""
                            if normalize_option(option.get("value") or "")
                            == normalize_option(option.get("name") or "")
                            else str(option.get("value") or "")
                        ),
                        str(option.get("name") or ""),
                        not bool(option.get("disabled")),
                    )
                    for option in dom_options
                ),
            )
        except CandidateSourceError as exc:
            if not managed:
                return None
            if self.logger is not None:
                self.logger.error(
                    "小红书属性“%s”候选校验明细：API=%s，DOM=%s",
                    page_label,
                    [
                        (value.value_id, value.label)
                        for value in api_options
                    ],
                    [
                        (
                            str(option.get("value") or ""),
                            str(option.get("name") or ""),
                            not bool(option.get("disabled")),
                        )
                        for option in dom_options
                    ],
                )
            raise XhsFormListingError(
                "小红书属性“{0}”接口候选与页面候选不一致：{1}".format(
                    page_label,
                    exc.reason_code,
                )
            ) from exc
        field_id = str(field.get("id") or "").strip()
        if not field_id or not str(category_id).strip():
            if self.logger is not None:
                self.logger.info(
                    "小红书属性“%s”：接口字段 ID 或类目 ID 不完整，"
                    "改用当前 DOM 匹配与回读",
                    page_label,
                )
            return None
        schema_version = canonical_sha256(
            {
                "platform_id": "xhs",
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
            request_candidates, excel_value = await self._excel_before_review(
                select, candidates, group, label=page_label,
                preflight_request=AttributeRequest(
                    platform_id="xhs", category_leaf_id=category_id,
                    field_id=field_id, field_label=page_label,
                    candidates=tuple(CandidateValue(v.value_id, v.label) for v in candidates),
                    excel_value="/".join(group), evidence={},
                    custom_allowed=bool(field.get("custom_allowed")), schema_version=schema_version,
                ),
            )
            resolved = await runtime.resolve(
                AttributeRequest(
                    platform_id="xhs",
                    category_leaf_id=category_id,
                    field_id=field_id,
                    field_label=page_label,
                    candidates=request_candidates,
                    excel_value=excel_value,
                    evidence={"excel": bool(excel_value.strip())},
                    custom_allowed=bool(field.get("custom_allowed")),
                    schema_version=schema_version,
                    control_type="select",
                )
            )
            if resolved is None:
                return ()
            resolved_values.append(resolved.label)
        return tuple(resolved_values)

    async def _fill_attribute(
        self,
        page_label: str,
        item: Any,
        expected: str,
        *,
        exact_values: Optional[Sequence[str]] = None,
        required: bool = True,
    ) -> Optional[Tuple[str, ...]]:
        await item.scroll_into_view_if_needed()
        selects = item.locator(".el-select:visible")
        if await selects.count() != 1:
            raise XhsFormListingError(
                "小红书属性“{0}”下拉框不是唯一项：{1}".format(
                    page_label, await selects.count()
                )
            )
        select = selects.first
        multi = await select.locator(".el-select__tags").count() > 0
        groups = (
            tuple((str(value),) for value in exact_values)
            if exact_values is not None
            else selection_value_groups_for_control(
                page_label, expected, multi=multi
            )
        )
        if not groups:
            raise XhsFormListingError("小红书属性“{0}”期望值为空".format(page_label))
        # 小红书页面部分控件带有多选外观，但保存接口的
        # XhsGoodsProperty.value 实际声明为 String；发送数组会直接触发
        # Jackson START_ARRAY 反序列化错误。提交前保留第一个精确值，
        # 确保请求与接口协议一致。
        if self.attribute_runtime is not None:
            resolved_values = await self._resolve_learning_select_groups(
                page_label,
                select,
                groups,
            )
            if resolved_values == ():
                if self.logger is not None:
                    self.logger.info(
                        "小红书属性“%s”已加入本平台待审核汇总，继续填写后续字段",
                        page_label,
                    )
                return None
            if resolved_values is not None:
                groups = tuple((value,) for value in resolved_values)
        try:
            actual = await self._select_values(select, groups, label=page_label, multi=multi)
        except TaobaoListingError as exc:
            raise XhsFormListingError(
                "小红书属性“{0}”选择失败：{1}".format(
                    page_label, str(exc).replace("淘宝", "小红书")
                )
            ) from exc
        if actual is None and len(groups) == 1:
            # Xiaohongshu's remote selects can expose an exact, clickable DOM
            # option as ``created`` even when Element's caninputcustom flag is
            # false.  The shared Taobao helper correctly refuses that shape;
            # for XHS we may still click it when the visible text is uniquely
            # exact, then require a successful control readback.
            actual = await self._select_unique_exact_dom_option(
                select,
                groups[0],
                page_label=page_label,
                multi=multi,
            )
        if actual is None:
            candidates = " / ".join("/".join(group) for group in groups)
            if not required:
                if self.logger is not None:
                    self.logger.warning(
                        "小红书可选属性“%s”没有 Excel 值的精确候选，已跳过：%s",
                        page_label,
                        candidates,
                    )
                return None
            raise XhsFormListingError(
                "小红书属性“{0}”没有 Excel 值的精确候选：{1}".format(
                    page_label, candidates
                )
            )
        return actual

    async def _select_unique_exact_dom_option(
        self,
        select: Any,
        candidates: Sequence[str],
        *,
        page_label: str,
        multi: bool,
    ) -> Optional[Tuple[str, ...]]:
        try:
            await self._open_select(select, multi=multi)
            dropdown, options = await self._visible_dom_options(
                select, timeout_seconds=2
            )
        except TaobaoListingError:
            return None
        matches = [
            (candidate, option)
            for candidate in candidates
            for option in options
            if normalize_option(option.get("name", ""))
            == normalize_option(candidate)
        ]
        if len(matches) != 1:
            await self._dismiss_select_dropdown(select)
            return None
        candidate, option = matches[0]
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
            int(option["index"]),
        )
        if not clicked:
            return None
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            actual = await self._read_select_values(select, multi=multi)
            if any(
                normalize_option(value) == normalize_option(candidate)
                for value in actual
            ):
                await self._dismiss_select_dropdown(select)
                if self.logger is not None:
                    self.logger.info(
                        "小红书属性“%s”已通过唯一精确 DOM 候选填写：%s",
                        page_label,
                        candidate,
                    )
                return actual
            await asyncio.sleep(0.05)
        await self._dismiss_select_dropdown(select)
        return None

    async def fill_category_attributes(self, fields: XhsFields) -> Mapping[str, Any]:
        page_items = without_color_attributes(await self._attribute_items())
        assignments = await self._attribute_assignments(fields.fields, page_items)
        applied: Dict[str, Tuple[str, ...]] = {}
        skipped: Dict[str, str] = {}
        for normalized_page, (page_label, item) in page_items.items():
            assignment = assignments.get(normalized_page)
            if assignment is None:
                continue
            _source_label, expected = assignment
            exact_values = self._special_attribute_values(
                fields.fields, page_label, expected
            )
            required = await self._attribute_is_required(item)
            fabric_label = normalize_label(page_label)
            fabric_values = exact_values
            if fabric_label in {
                normalize_label("面料分类"),
            } and (not fabric_values or len(fabric_values) < 2):
                fabric_values = self._special_attribute_values(
                    fields.fields, "材质成分", expected
                )
            if (
                fabric_label in {
                    normalize_label("面料"),
                    normalize_label("面料分类"),
                }
                and fabric_values is not None
                and len(fabric_values) >= 2
            ):
                actual = await self._fill_fabric_attribute(
                    page_label,
                    fabric_values,
                    item=item,
                    raw_value=expected,
                    writer=lambda value, values: self._fill_attribute(
                        page_label,
                        item,
                        value,
                        exact_values=values,
                        required=required,
                    ),
                )
            else:
                actual = await self._fill_attribute(
                    page_label,
                    item,
                    expected,
                    exact_values=exact_values,
                    required=required,
                )
            if actual is None:
                skipped[page_label] = expected
            else:
                applied[page_label] = actual
        if self.logger is not None:
            self.logger.info("小红书类目属性填写完成：已填 %s 项", len(applied))
        return {
            "attributes": applied,
            "skipped_no_exact_candidate": skipped,
            "unmatched_page_fields": tuple(
                page_label
                for normalized_page, (page_label, _item) in page_items.items()
                if normalized_page not in assignments
            ),
        }

    async def _find_named_input(self, label: str) -> Any:
        """Re-query after category rendering and scroll only the editor for lazy fields."""
        deadline = asyncio.get_running_loop().time() + 12
        step = 0
        while True:
            control = await self._find_named_input_once(label)
            if control is not None:
                await control.scroll_into_view_if_needed(timeout=5000)
                return control
            if asyncio.get_running_loop().time() >= deadline:
                raise XhsFormListingError(
                    "小红书字段“{0}”等待渲染并滚动查找后仍未找到输入框".format(label)
                )
            if step == 0 and self.logger is not None:
                self.logger.info("小红书字段“%s”暂未出现，等待渲染并在编辑区域滚动查找", label)
            await self.panel.evaluate('''(panel, step) => {
              const roots = [panel, ...panel.querySelectorAll('*')];
              for (let p = panel.parentElement; p && !['BODY','HTML'].includes(p.tagName); p = p.parentElement) roots.push(p);
              for (const el of roots) {
                if (!/(auto|scroll)/.test(getComputedStyle(el).overflowY) || el.clientHeight <= 0 || el.scrollHeight <= el.clientHeight) continue;
                const bottom = el.scrollHeight - el.clientHeight;
                const next = step === 0 || el.scrollTop >= bottom - 2 ? 0 : Math.min(bottom, el.scrollTop + el.clientHeight * 0.75);
                el.scrollTo({top: next, behavior: 'instant'});
              }
            }''', step)
            step += 1
            await asyncio.sleep(0.4)

    async def _find_named_input_once(self, label: str) -> Any:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        direct = self.panel.locator('[data-xhs-field="{0}"]'.format(label)).locator('input:not([type="hidden"]):visible, textarea:visible')
        if await direct.count() == 1:
            return direct.first
        label_nodes = self.panel.get_by_text(
            re.compile(r"^\s*\*?\s*{0}\s*[：:]?\s*$".format(re.escape(label)))
        )
        matches = []
        for index in range(await label_nodes.count()):
            node = label_nodes.nth(index)
            if not await node.is_visible():
                continue
            root = node
            for _depth in range(5):
                inputs = root.locator('input:not([type="hidden"]):visible, textarea:visible')
                if await inputs.count() == 1:
                    matches.append(inputs.first)
                    break
                if await root.evaluate("e => e.classList.contains('el-form-item')"):
                    break
                root = root.locator("xpath=..")
        if len(matches) > 1:
            raise XhsFormListingError(
                "小红书字段“{0}”输入框不是唯一项：{1}".format(label, len(matches))
            )
        return matches[0] if matches else None

    async def fill_identity(self, title: str, style_code: str) -> Mapping[str, str]:
        expected_title = title_without_neigborl(title)
        if not expected_title:
            raise XhsFormListingError("移除 NEIGBORL 后小红书商品标题为空")
        if re.search(r"neigborl", expected_title, re.IGNORECASE):
            raise XhsFormListingError("小红书商品标题仍包含 NEIGBORL")
        if len(expected_title) > 60:
            raise XhsFormListingError(
                "移除 NEIGBORL 后小红书商品标题仍超过 60 字：{0}".format(len(expected_title))
            )
        expected = {"商品标题": expected_title, "货号": str(style_code).strip()}
        actual: Dict[str, str] = {}
        for label, value in expected.items():
            if not value:
                raise XhsFormListingError("小红书{0}不能为空".format(label))
            control = await self._find_named_input(label)
            if (await control.input_value()).strip() != value:
                await control.fill(value)
                await control.press("Tab")
            current = (await control.input_value()).strip()
            if current != value:
                raise XhsFormListingError(
                    "小红书{0}回读失败：期望 {1!r}，页面为 {2!r}".format(
                        label, value, current
                    )
                )
            actual[label] = current
        if re.search(r"neigborl", actual["商品标题"], re.IGNORECASE):
            raise XhsFormListingError("小红书商品标题回读仍包含 NEIGBORL")
        return actual

    async def _ensure_radio(self, option_text: str) -> str:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        options = self.panel.locator("label.el-radio:visible")
        matches = []
        for index in range(await options.count()):
            option = options.nth(index)
            if normalize_label(await option.inner_text()) == normalize_label(option_text):
                matches.append(option)
        if len(matches) != 1:
            raise XhsFormListingError(
                "小红书单选项“{0}”不是唯一项：{1}".format(option_text, len(matches))
            )
        option = matches[0]
        radio = option.locator('input[type="radio"]').first
        if not await radio.is_checked():
            await option.click()
        if not await radio.is_checked():
            raise XhsFormListingError("小红书单选项未生效：{0}".format(option_text))
        return option_text

    async def _presale_day_control(self) -> Tuple[Any, bool]:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        direct = self.panel.locator('[data-xhs-presale-days]:visible')
        if await direct.count() == 1:
            select = direct.first.locator(".el-select:visible")
            return (select.first if await select.count() == 1 else direct.first, await select.count() == 1)
        matches: List[Tuple[Any, bool]] = []
        labels = self.panel.get_by_text(re.compile(r"^\s*(?:\*?\s*)?(?:预售发货时间|付款后)\s*[：:]?\s*$"))
        for index in range(await labels.count()):
            label = labels.nth(index)
            if not await label.is_visible():
                continue
            root = label
            for _depth in range(5):
                selects = root.locator(".el-select:visible")
                inputs = root.locator('input:not([type="hidden"]):visible')
                if await selects.count() == 1:
                    matches.append((selects.first, True))
                    break
                if await inputs.count() == 1:
                    matches.append((inputs.first, False))
                    break
                root = root.locator("xpath=..")
        if len(matches) != 1:
            raise XhsFormListingError(
                "小红书预售发货天数控件不是唯一项：{0}".format(len(matches))
            )
        return matches[0]

    async def apply_full_payment_presale(self) -> Mapping[str, str]:
        full_mode = await self._ensure_radio("全款预售模式")
        await self._wait_for_loading_masks()
        timed_mode = await self._ensure_radio("时段预售")
        control, is_select = await self._presale_day_control()
        if is_select:
            try:
                actual_values = await self._select_values(
                    control, (("15", "15天"),), label="预售发货时间", multi=False
                )
            except TaobaoListingError as exc:
                raise XhsFormListingError(str(exc).replace("淘宝", "小红书")) from exc
            if actual_values is None:
                raise XhsFormListingError("小红书预售发货时间没有“15天”精确候选")
            days = actual_values[0]
        else:
            current = (await control.input_value()).strip()
            if not _numeric_equal(current, "15"):
                await control.fill("15")
                await control.press("Tab")
            days = (await control.input_value()).strip()
            if not _numeric_equal(days, "15"):
                raise XhsFormListingError(
                    "小红书预售发货时间回读失败：期望 15，页面为 {0!r}".format(days)
                )
        return {"发货模式": full_mode, "预售类型": timed_mode, "付款后": days}

    async def _batch_input(self, label: str) -> Any:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        direct = self.panel.locator('[data-xhs-batch-field="{0}"] input:visible'.format(label))
        if await direct.count() == 1:
            return direct.first
        button = self.panel.get_by_role("button", name="批量设置", exact=True)
        if await button.count() != 1:
            raise XhsFormListingError("小红书“批量设置”按钮不是唯一项：{0}".format(await button.count()))
        root = button.first
        for _depth in range(1, 8):
            root = root.locator("xpath=..")
            if await root.locator(".el-table, table").count():
                continue
            inputs = root.locator('input:not([type="hidden"]):not([readonly]):visible')
            count = await inputs.count()
            if len(XHS_BATCH_ORDER) <= count <= 5:
                return inputs.nth(XHS_BATCH_ORDER.index(label))
        raise XhsFormListingError("小红书批量字段“{0}”输入框未找到".format(label))

    async def _fill_batch_number(self, label: str, expected: str) -> str:
        if not _is_nonnegative_decimal(expected):
            raise XhsFormListingError("Excel 小红书{0}不是非负数字：{1!r}".format(label, expected))
        control = await self._batch_input(label)
        current = (await control.input_value()).strip()
        if not _numeric_equal(current, expected):
            await control.fill(expected)
            await control.press("Tab")
        actual = (await control.input_value()).strip()
        if not _numeric_equal(actual, expected):
            raise XhsFormListingError(
                "小红书批量字段“{0}”回读失败：期望 {1!r}，页面为 {2!r}".format(
                    label, expected, actual
                )
            )
        return actual

    async def _sku_table_snapshot(self) -> Mapping[str, Any]:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        tables = self.panel.locator(".el-table:visible")
        if await tables.count() == 0:
            tables = self.panel.locator("table:visible")
        matches = []
        for index in range(await tables.count()):
            table = tables.nth(index)
            snapshot = await table.evaluate(
                """root => {
                  const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
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
            normalized = tuple(normalize_label(value) for value in snapshot.get("headers", ()))
            if all(
                any(header == normalize_label(label) or header.startswith(normalize_label(label)) for header in normalized)
                for label in ("售价", "库存")
            ):
                matches.append(snapshot)
        if len(matches) != 1:
            raise XhsFormListingError("小红书 SKU 表格不是唯一项：{0}".format(len(matches)))
        return matches[0]

    @staticmethod
    def _sku_column_index(headers: Sequence[str], label: str) -> int:
        expected = normalize_label(label)
        indexes = [
            index for index, value in enumerate(headers)
            if normalize_label(value) == expected or normalize_label(value).startswith(expected)
        ]
        if len(indexes) != 1:
            raise XhsFormListingError(
                "小红书 SKU 表格列“{0}”不是唯一项：{1}".format(label, indexes)
            )
        return indexes[0]

    def _validate_sku_snapshot(
        self, snapshot: Mapping[str, Any], expected: Mapping[str, str]
    ) -> Tuple[Mapping[str, str], ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        rows = tuple(snapshot.get("rows", ()))
        if not rows:
            raise XhsFormListingError("小红书 SKU 表格没有可校验的明细行")
        indexes = {label: self._sku_column_index(headers, label) for label in expected}
        result = []
        errors = []
        for number, raw_row in enumerate(rows, 1):
            actual = {
                label: str(raw_row[index]) if index < len(raw_row) else ""
                for label, index in indexes.items()
            }
            for label, expected_value in expected.items():
                if not _numeric_equal(actual[label], expected_value):
                    errors.append("第{0}行{1}={2!r}".format(number, label, actual[label]))
            result.append(actual)
        if errors:
            raise XhsFormListingError("小红书批量设置后校验失败：" + "；".join(errors[:12]))
        return tuple(result)

    async def fill_price_inventory_batch(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        expected = {
            "售价": _required_excel_value(
                fields, ("售价", "售卖价", "价格", "基本售价"), "售价", money=True
            ),
            "库存": _required_excel_value(fields, ("数量", "库存"), "库存"),
        }
        inventory = Decimal(expected["库存"])
        if not inventory.is_finite() or inventory < 0 or inventory % 1:
            raise XhsFormListingError("Excel 小红书库存必须是非负整数：{0!r}".format(expected["库存"]))
        for label, value in expected.items():
            await self._fill_batch_number(label, value)
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        button = self.panel.get_by_role("button", name="批量设置", exact=True)
        if await button.count() != 1:
            raise XhsFormListingError("小红书“批量设置”按钮不是唯一项：{0}".format(await button.count()))
        await button.click()
        deadline = asyncio.get_running_loop().time() + 15
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                rows = self._validate_sku_snapshot(await self._sku_table_snapshot(), expected)
                return {
                    "batch_clicked": True,
                    "row_count": len(rows),
                    "values": expected,
                    "rows": rows,
                }
            except XhsFormListingError as exc:
                last_error = exc
                await asyncio.sleep(0.15)
        raise XhsFormListingError(str(last_error or "小红书批量设置超时"))

    async def _main_image_item(self) -> Any:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        direct = self.panel.locator('[data-xhs-image-group="main"]:visible')
        if await direct.count() == 1:
            return direct.first
        labels = self.panel.get_by_text(re.compile(r"^\s*\*?\s*主图\s*[：:]?\s*$"))
        matches = []
        for index in range(await labels.count()):
            label = labels.nth(index)
            if not await label.is_visible():
                continue
            root = label
            for _depth in range(8):
                if await root.locator('input[type="file"]').count():
                    matches.append(root)
                    break
                root = root.locator("xpath=..")
        if len(matches) != 1:
            raise XhsFormListingError("小红书主图上传区域不是唯一项：{0}".format(len(matches)))
        return matches[0]

    async def sync_main_images(
        self,
        portrait_paths: Sequence[Any],
        *,
        timeout_seconds: int,
        uploader: Any,
    ) -> Mapping[str, Any]:
        """Replace XHS main art with the product's 3:4 source images."""
        paths = tuple(portrait_paths)
        if not paths:
            raise XhsFormListingError("小红书没有可上传的 3:4 主图")
        item = await self._main_image_item()
        action = await uploader(
            self.page,
            item,
            paths,
            "小红书3:4主图",
            timeout_seconds,
            force_replace=True,
        )
        return {"source": "3:4主图", "count": len(paths), "action": action}

    async def _verify_persisted_identity(
        self, title: str, style_code: str
    ) -> Mapping[str, str]:
        expected = {
            "商品标题": title_without_neigborl(title),
            "货号": str(style_code).strip(),
        }
        actual: Dict[str, str] = {}
        errors: List[str] = []
        for label, expected_value in expected.items():
            control = await self._find_named_input(label)
            current = (await control.input_value()).strip()
            actual[label] = current
            if current != expected_value:
                errors.append(
                    "{0}=期望 {1!r}，页面为 {2!r}".format(
                        label, expected_value, current
                    )
                )
        if errors:
            raise XhsFormListingError(
                "小红书保存后身份字段回读失败：" + "；".join(errors)
            )
        return actual

    async def _verify_persisted_attributes(
        self, expected_attributes: Mapping[str, Sequence[str]]
    ) -> Mapping[str, Tuple[str, ...]]:
        page_items = await self._attribute_items()
        persisted: Dict[str, Tuple[str, ...]] = {}
        errors: List[str] = []
        for expected_label, expected_values in expected_attributes.items():
            matches = [
                (page_label, item)
                for page_label, item in page_items.values()
                if normalize_label(page_label) == normalize_label(expected_label)
            ]
            if len(matches) != 1:
                errors.append(
                    "{0}=页面字段不唯一({1})".format(expected_label, len(matches))
                )
                continue
            page_label, item = matches[0]
            selects = item.locator(".el-select:visible")
            if await selects.count() != 1:
                errors.append("{0}=下拉控件不唯一".format(page_label))
                continue
            select = selects.first
            multi = await select.locator(".el-select__tags").count() > 0
            actual = await self._read_select_values(select, multi=multi)
            expected_counter = Counter(
                normalize_option(value)
                for value in expected_values
                if normalize_option(value)
            )
            actual_counter = Counter(
                normalize_option(value) for value in actual if normalize_option(value)
            )
            if actual_counter != expected_counter:
                errors.append(
                    "{0}=期望 {1}，页面为 {2}".format(
                        page_label, tuple(expected_values), actual
                    )
                )
                continue
            persisted[page_label] = tuple(actual)
        if errors:
            raise XhsFormListingError(
                "小红书保存后类目属性回读失败：" + "；".join(errors[:12])
            )
        return persisted

    async def _persisted_radio_is_checked(self, option_text: str) -> bool:
        if self.panel is None:
            raise XhsFormListingError("请先调用 open() 打开小红书资料")
        matches = []
        options = self.panel.locator("label.el-radio:visible")
        for index in range(await options.count()):
            option = options.nth(index)
            if normalize_label(await option.inner_text()) == normalize_label(option_text):
                matches.append(option)
        if len(matches) != 1:
            raise XhsFormListingError(
                "小红书保存后单选项“{0}”不唯一：{1}".format(
                    option_text, len(matches)
                )
            )
        option = matches[0]
        control = option.locator('input[type="radio"]').first
        return await control.is_checked() or "is-checked" in set(
            (await option.get_attribute("class") or "").split()
        )

    async def verify_persisted_values(
        self,
        fields: XhsFields,
        *,
        title: str,
        style_code: str,
        expected_attributes: Mapping[str, Sequence[str]],
        expected_freight: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        """Read critical Xiaohongshu values after save without changing them."""
        identity = await self._verify_persisted_identity(title, style_code)
        attributes = await self._verify_persisted_attributes(expected_attributes)
        freight = await sync_store_freight(self, fields.fields, read_only=True, expected=expected_freight)

        full_mode = await self._persisted_radio_is_checked("全款预售模式")
        timed_mode = await self._persisted_radio_is_checked("时段预售")
        if not full_mode or not timed_mode:
            raise XhsFormListingError("小红书保存后全款时段预售没有持久化")
        day_control, is_select = await self._presale_day_control()
        if is_select:
            days_values = await self._read_select_values(day_control, multi=False)
            days = days_values[0] if len(days_values) == 1 else ""
        else:
            days = (await day_control.input_value()).strip()
        if normalize_option(days) not in {
            normalize_option("15"),
            normalize_option("15天"),
        }:
            raise XhsFormListingError(
                "小红书保存后预售天数回读失败：{0!r}".format(days)
            )

        expected_sku = {
            "售价": _required_excel_value(
                fields.fields, ("售价", "售卖价", "价格", "基本售价"), "售价", money=True
            ),
            "库存": _required_excel_value(fields.fields, ("数量", "库存"), "库存"),
        }
        rows = self._validate_sku_snapshot(
            await self._sku_table_snapshot(), expected_sku
        )
        errors = await self._visible_validation_errors()
        if errors:
            raise XhsFormListingError(
                "小红书保存后仍有页面校验错误：" + "；".join(errors)
            )
        return {
            "identity": identity,
            "attributes": attributes,
            "presale": {
                "发货模式": "全款预售模式",
                "预售类型": "时段预售",
                "付款后": days,
            },
            "sku_values": expected_sku,
            "row_count": len(rows),
            "freight": freight,
        }

    async def apply_excel_fields(
        self,
        fields: XhsFields,
        *,
        title: str,
        style_code: str,
        portrait_paths: Sequence[Any],
        timeout_seconds: int,
        uploader: Any,
    ) -> Mapping[str, Any]:
        category = await self.apply_category(fields.category_path)
        identity = await self.fill_identity(title, style_code)
        attributes = await self.fill_category_attributes(fields)
        presale = await self.apply_full_payment_presale()
        batch = await self.fill_price_inventory_batch(fields.fields)
        freight = await sync_store_freight(self, fields.fields)
        images = await self.sync_main_images(
            portrait_paths, timeout_seconds=timeout_seconds, uploader=uploader
        )
        try:
            errors = await self._visible_validation_errors()
        except TaobaoListingError as exc:
            raise XhsFormListingError(str(exc).replace("淘宝", "小红书")) from exc
        if (
            errors
            and not (
                self.attribute_runtime is not None
                and getattr(
                    self.attribute_runtime, "has_deferred_reviews", False
                )
            )
        ):
            raise XhsFormListingError("小红书页面校验错误：" + "；".join(errors))
        return {
            "category": category,
            "identity": identity,
            "attributes": attributes,
            "presale": presale,
            "sku_batch": batch,
            "main_images": images,
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
