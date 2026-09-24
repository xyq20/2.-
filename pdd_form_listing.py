"""DOM writer for the FastMai Pinduoduo product-information tab.

This adapter fills only the form.  It deliberately contains no save or
publish method: the common runner retains the final write gate.
"""

from __future__ import annotations

from field_policies import without_color_attributes

import asyncio
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from attribute_runtime import AttributeRequest
from category_profile import choose_category_candidate
from canonical_fields import is_learning_managed_field
from pdd_data import PddFields
from learning_models import CandidateValue, canonical_sha256
from money_values import MoneyValueError, normalize_money_value
from pdd_listing import parse_pdd_attribute_fields
from platform_candidate_source import (
    CandidateSourceError,
    DomCandidate,
    reconcile_candidates,
    validate_observed_selection,
)
from platform_schema import FieldSchema
from taobao_listing import (
    TaobaoListing,
    TaobaoListingError,
    excel_aliases,
    material_name_groups,
    normalize_label,
    normalize_option,
    parse_taobao_materials,
    material_value_groups,
    selection_value_groups,
    selection_value_groups_for_control,
)


class PddFormListingError(RuntimeError):
    """A PDD form issue that can be shown directly to the operator."""


# These are deterministic historic spreadsheet headings, not fuzzy matches.
PDD_FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    normalize_label("版型"): (normalize_label("服装版型"), normalize_label("服饰版型")),
    normalize_label("服装版型"): (normalize_label("服饰版型"), normalize_label("版型")),
    normalize_label("服饰版型"): (normalize_label("服装版型"), normalize_label("版型")),
    normalize_label("面料俗称"): (normalize_label("面料"), normalize_label("面料材质")),
    normalize_label("材质"): (normalize_label("材质成分"),),
    # Excel keeps the internal style identifier under a historic compound
    # heading.  PDD exposes the same value as the category property
    # “商品货号”, so this must be an explicit field-name mapping rather than
    # a fuzzy fallback.
    normalize_label("商品货号"): tuple(
        normalize_label(value)
        for value in ("货号", "商家外部编码", "款式编码")
    ),
}

PDD_IGNORED_CATEGORY_FIELDS = frozenset((normalize_label("裆部结构"),))

# These are top-of-page product identity controls.  They are inherited from
# the already saved base form, not PDD category properties; matching the
# Excel's “商品分类” here would try to replace a category selector.
PDD_INHERITED_FIELDS = frozenset(
    normalize_label(value) for value in ("商品分类", "商品标题", "商品描述", "品牌")
)
PDD_BATCH_ORDER = ("拼单价", "单买价", "库存")
PDD_PROPERTIES_ENDPOINT = "/pdd/getCategoryProperties.json"
PDD_CATEGORY_MARKERS = frozenset(
    normalize_label(value)
    for value in (
        "面料俗称", "材质", "款式", "裤长", "版型", "风格", "适用年龄",
        "流行元素", "弹力", "商品货号", "上市时节", "功能", "是否加绒",
        "成分含量", "适用场景", "裆部结构",
    )
)

# The PDD tab is one long form.  These headings begin content that follows
# “类目属性” but does not belong to it.  They are used only as a DOM section
# boundary; field labels are still matched exclusively against Excel keys.
PDD_CATEGORY_END_HEADINGS = re.compile(
    r"^\\s*\\*?\\s*(?:商品轮播图|商品详情图|商品规格与库存|服务与承诺|运费模板|上架设置)\\s*[：:]?\\s*$"
)


def _is_decimal(value: str) -> bool:
    try:
        Decimal(value)
        return True
    except (InvalidOperation, ValueError):
        return False


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
        raise PddFormListingError(
            "Excel 中缺少拼多多{0}字段（可识别：{1}）".format(
                label, "/".join(aliases)
            )
        )
    values = {value for _key, value in matches}
    if len(values) != 1:
        raise PddFormListingError(
            "拼多多{0}匹配到多个 Excel 字段：{1}".format(
                label, "、".join(key for key, _value in matches)
            )
        )
    return matches[0][1]


def _special_category_values(
    fields: Mapping[str, str], page_label: str, expected: str
) -> Optional[Tuple[str, ...]]:
    """Translate only PDD's known display formats before exact option matching."""
    normalized_label = normalize_label(page_label)
    if normalized_label in {normalize_label("面料俗称"), normalize_label("材质")}:
        names = tuple("/".join(group) for group in material_name_groups(expected))
        return names or None
    if normalized_label == normalize_label("是否加绒"):
        option = {
            normalize_option("是"): "加绒",
            normalize_option("否"): "不加绒",
        }.get(normalize_option(expected))
        return (option,) if option else None
    return None


class PddFormListing(TaobaoListing):
    """Fill PDD category data, its price/inventory batch row and presale."""

    def _start_api_capture(self) -> None:
        previous_handler = getattr(self, "_api_response_handler", None)
        if previous_handler is not None:
            try:
                self.page.remove_listener("response", previous_handler)
            except Exception:
                pass
        self._api_observations: List[Mapping[str, Any]] = []
        self._api_capture_tasks: List[asyncio.Task[Any]] = []

        def handle_response(response: Any) -> None:
            try:
                path = urlsplit(response.url).path
            except Exception:
                return
            if path != PDD_PROPERTIES_ENDPOINT:
                return
            task = asyncio.create_task(self._capture_api_response(response))
            self._api_capture_tasks.append(task)

        self._api_response_handler = handle_response
        self.page.on("response", handle_response)

    async def _capture_api_response(self, response: Any) -> None:
        observation: Dict[str, Any] = {
            "path": PDD_PROPERTIES_ENDPOINT,
            "http_status": int(response.status),
            "body": "unavailable",
            "attribute_fields": (),
            "category_id": "",
        }
        try:
            payload = await response.json()
            observation["body"] = "json"
            observation["attribute_fields"] = parse_pdd_attribute_fields(payload)
            query = parse_qs(urlsplit(response.url).query)
            for key in ("leafCategoryId", "categoryId", "cid"):
                values = tuple(query.get(key, ()))
                if len(values) == 1 and str(values[0]).strip():
                    observation["category_id"] = str(values[0]).strip()
                    break
        except Exception:
            pass
        self._api_observations.append(observation)

    async def _captured_api_field(self, page_label: str) -> Tuple[FieldSchema, str]:
        tasks = tuple(getattr(self, "_api_capture_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        wanted = normalize_label(page_label)
        # A category change can produce more than one response in one drawer.
        # The newest response containing this field is the active category;
        # ambiguity within that single response still fails closed.
        for observation in reversed(getattr(self, "_api_observations", ())):
            category_id = str(observation.get("category_id") or "").strip()
            fields = {
                (str(field.source_id or ""), field.schema_key): field
                for field in observation.get("attribute_fields", ())
                if normalize_label(field.label) == wanted
            }
            if not fields:
                continue
            if len(fields) != 1:
                raise PddFormListingError(
                    "拼多多属性“{0}”接口字段定义不唯一".format(page_label)
                )
            if not category_id:
                raise PddFormListingError(
                    "拼多多属性“{0}”缺少接口类目 ID".format(page_label)
                )
            return next(iter(fields.values())), category_id
        raise PddFormListingError(
            "拼多多属性“{0}”缺少接口 JSON 字段定义".format(page_label)
        )

    async def _resolve_learning_groups(
        self,
        page_label: str,
        select: Any,
        groups: Sequence[Sequence[str]],
        *,
        observed_values: Sequence[str] = (),
    ) -> Tuple[Tuple[str, ...], ...]:
        runtime = getattr(self, "attribute_runtime", None)
        normalized_groups = tuple(tuple(str(value) for value in group) for group in groups)
        if runtime is None:
            return normalized_groups
        managed = is_learning_managed_field("pdd", page_label)
        try:
            field, category_id = await self._captured_api_field(page_label)
        except PddFormListingError as exc:
            if self.logger is not None:
                self.logger.info(
                    "拼多多属性“%s”：接口字段未定位，改用当前 DOM 匹配/直接输入与回读：%s",
                    page_label,
                    exc,
                )
            return normalized_groups
        if not field.source_id:
            if self.logger is not None:
                self.logger.info(
                    "拼多多属性“%s”：接口字段 ID 为空，改用当前 DOM 匹配与回读",
                    page_label,
                )
            return normalized_groups
        if observed_values:
            try:
                candidates = validate_observed_selection(
                    field.option_values,
                    observed_values,
                )
            except CandidateSourceError as exc:
                if self.logger is not None:
                    self.logger.info(
                        "拼多多属性“%s”已保存值无法与当前接口 JSON "
                        "唯一关联，仅保留 DOM 回读，本次不重复入库：%s",
                        page_label,
                        exc.reason_code,
                    )
                return normalized_groups
        else:
            multi = await select.locator(".el-select__tags").count() > 0
            await self._open_select(select, multi=multi)
            try:
                _dropdown, options = await self._visible_dom_options(select)
            finally:
                try:
                    await self._dismiss_select_dropdown(select)
                except Exception:
                    pass
            try:
                candidates = reconcile_candidates(
                    field.option_values,
                    tuple(
                        DomCandidate(
                            str(option.get("value") or ""),
                            str(option.get("name") or ""),
                            not bool(option.get("disabled")),
                        )
                        for option in options
                    ),
                )
            except CandidateSourceError as exc:
                if not managed:
                    return normalized_groups
                raise PddFormListingError(
                    "拼多多属性“{0}”接口候选与页面候选不一致：{1}".format(
                        page_label, exc.reason_code
                    )
                ) from exc
        schema_version = canonical_sha256(
            {
                "platform_id": "pdd",
                "category_leaf_id": category_id,
                "field_id": str(field.source_id),
                "options": [
                    {"value_id": value.value_id, "label": value.label}
                    for value in candidates
                ],
            }
        )
        resolved_groups = []
        for group in normalized_groups:
            request_candidates, excel_value = await self._excel_before_review(
                select, candidates, group, label=page_label,
                preflight_request=AttributeRequest(
                    platform_id="pdd", category_leaf_id=category_id,
                    field_id=str(field.source_id), field_label=page_label,
                    candidates=tuple(CandidateValue(v.value_id, v.label) for v in candidates),
                    excel_value="/".join(group), evidence={},
                    custom_allowed=bool(field.custom_allowed), schema_version=schema_version,
                ),
            )
            resolved = await runtime.resolve(
                AttributeRequest(
                    platform_id="pdd",
                    category_leaf_id=category_id,
                    field_id=str(field.source_id),
                    field_label=page_label,
                    candidates=request_candidates,
                    excel_value=excel_value,
                    evidence={"excel": bool(excel_value)},
                    custom_allowed=bool(field.custom_allowed),
                    schema_version=schema_version,
                    control_type="select",
                )
            )
            if resolved is None:
                return ()
            resolved_groups.append((resolved.label,))
        return tuple(resolved_groups)

    async def _wait_for_loading_masks(self, timeout_seconds: float = 30) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self.drawer.locator(".el-loading-mask:visible").count() == 0:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.05)
        raise PddFormListingError("拼多多资料加载遮罩在 30 秒内未消失")

    async def open(self) -> "PddFormListing":
        self._start_api_capture()
        tab = self.drawer.get_by_role("tab", name="拼多多资料", exact=True)
        try:
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click(timeout=30_000)
            panel = self.drawer.get_by_role("tabpanel", name="拼多多资料", exact=True)
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise PddFormListingError("找不到可切换的“拼多多资料”页签") from exc
        self.panel = panel
        await self._wait_for_loading_masks()
        if self.logger is not None:
            self.logger.info("拼多多资料页签已打开")
        return self

    async def _category_text(self) -> str:
        try:
            if self.panel is not None:
                preferred = self.panel.locator(
                    ".platform-category-input .current, "
                    ".platform-category-input .category-path"
                ).first
                if await preferred.count():
                    text = (await preferred.inner_text()).strip()
                    if text:
                        return text
            text = await super()._category_text()
            # Some PDD revisions place the asynchronous recommendation under
            # the same category container.  It is not part of the selected
            # category path, so retain the first non-recommendation line.
            lines = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and "推荐" not in line and "点击使用" not in line
            ]
            return lines[0] if lines else text
        except TaobaoListingError as exc:
            raise PddFormListingError(str(exc).replace("淘宝", "拼多多")) from exc

    async def _attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        if self.panel is None:
            raise PddFormListingError("请先调用 open() 打开拼多多资料")
        items = await self._category_attribute_form_items()
        category_top, category_end = await self._category_section_vertical_bounds()
        result: Dict[str, Tuple[str, Any]] = {}
        counts: Dict[str, int] = {}
        for index in range(await items.count()):
            item = items.nth(index)
            if not await item.is_visible():
                continue
            item_box = await item.bounding_box()
            if item_box is not None:
                item_top = item_box["y"]
                # A broad ancestor can contain the entire PDD tab.  In that
                # case keep only controls physically inside the category
                # section, excluding later freight/service controls.
                if category_top is not None and item_top <= category_top:
                    continue
                if category_end is not None and item_top >= category_end:
                    continue
            label_node = item.locator(":scope > .el-form-item__label").first
            if not await label_node.count():
                continue
            label = re.sub(r"^\s*\*\s*", "", (await label_node.inner_text()).strip())
            label = label.replace("重要", "").strip()
            normalized = normalize_label(label)
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            key = normalized if counts[normalized] == 1 else "{0}#{1}".format(
                normalized, counts[normalized]
            )
            result[key] = (label, item)
        if not result:
            raise PddFormListingError("拼多多类目属性区域为空")
        return result

    async def _category_section_vertical_bounds(self) -> Tuple[Optional[float], Optional[float]]:
        """Return the visible category heading and its next section boundary.

        Production PDD templates do not give category attributes a stable
        wrapper.  Their layout is nevertheless ordered: category properties
        follow ``类目属性`` and stop at the next well-known product section.
        Bounding boxes are used only to narrow an already visible DOM scope;
        no field is inferred from its position.
        """
        if self.panel is None:
            raise PddFormListingError("请先调用 open() 打开拼多多资料")
        category_top: Optional[float] = None
        headings = self.panel.get_by_text("类目属性", exact=True)
        for index in range(await headings.count()):
            heading = headings.nth(index)
            if not await heading.is_visible():
                continue
            box = await heading.bounding_box()
            if box is not None:
                category_top = box["y"]
                break
        if category_top is None:
            return None, None

        boundary_top: Optional[float] = None
        boundaries = self.panel.get_by_text(PDD_CATEGORY_END_HEADINGS)
        for index in range(await boundaries.count()):
            boundary = boundaries.nth(index)
            if not await boundary.is_visible():
                continue
            box = await boundary.bounding_box()
            if box is None or box["y"] <= category_top:
                continue
            if boundary_top is None or box["y"] < boundary_top:
                boundary_top = box["y"]
        return category_top, boundary_top

    async def _category_attribute_form_items(self) -> Any:
        """Return only the form items belonging to PDD's category section.

        Unlike the Taobao form, PDD does not expose a stable wrapper class.
        Its visible “类目属性” title is stable, so select the smallest ancestor
        containing the largest set of known category labels.  This excludes
        later logistics and service sections even when Excel uses the same
        label as a non-category field (for example “运费设置”).
        """
        if self.panel is None:
            raise PddFormListingError("请先调用 open() 打开拼多多资料")
        headings = self.panel.get_by_text("类目属性", exact=True)
        candidates = []
        for heading_index in range(await headings.count()):
            heading = headings.nth(heading_index)
            if not await heading.is_visible():
                continue
            root = heading
            for _depth in range(7):
                probes = (root, root.locator("xpath=following-sibling::*[1]"))
                for probe in probes:
                    form_items = probe.locator(".el-form-item")
                    count = await form_items.count()
                    if not count:
                        continue
                    labels = form_items.locator(":scope > .el-form-item__label")
                    markers = set()
                    for label_index in range(await labels.count()):
                        label = labels.nth(label_index)
                        if await label.is_visible():
                            markers.add(normalize_label(await label.inner_text()))
                    matched = len(markers.intersection(PDD_CATEGORY_MARKERS))
                    if matched:
                        candidates.append((matched, count, form_items))
                root = root.locator("xpath=..")
        if candidates:
            # Prefer the category root with the most recognised fields; a
            # tie goes to the smaller root rather than the whole tab panel.
            _matched, _count, form_items = sorted(
                candidates, key=lambda item: (-item[0], item[1])
            )[0]
            return form_items

        # A conservative fallback is kept for old templates where the title
        # is absent.  It still fills only exact Excel matches.
        return self.panel.locator(".el-form-item")

    async def fill_attribute(
        self,
        label: str,
        expected: object,
        *,
        exact_values: Optional[Sequence[str]] = None,
        item: Optional[Any] = None,
    ) -> Optional[Tuple[str, ...]]:
        """Fill one real PDD Element Form item without relying on wrapper CSS."""
        if item is None:
            records = [
                record
                for key, record in (await self._attribute_items()).items()
                if key.split("#", 1)[0] == normalize_label(label)
            ]
            if len(records) != 1:
                raise PddFormListingError(
                    "拼多多类目中属性“{0}”匹配数为 {1}".format(label, len(records))
                )
            page_label, item = records[0]
        else:
            page_label = label
        await item.scroll_into_view_if_needed()
        selects = item.locator(":scope > .el-form-item__content .el-select")
        standalone_inputs = item.locator(
            ":scope > .el-form-item__content input:not([readonly])"
        )
        visible_inputs = []
        for index in range(await standalone_inputs.count()):
            candidate = standalone_inputs.nth(index)
            if await candidate.is_visible() and not await candidate.evaluate(
                "element => Boolean(element.closest('.el-select'))"
            ):
                visible_inputs.append(candidate)
        if await selects.count():
            if await selects.count() != 1:
                raise PddFormListingError("拼多多属性“{0}”下拉框不唯一".format(page_label))
            select = selects.first
            multi = await select.locator(".el-select__tags").count() > 0
            groups = (
                material_value_groups(page_label, exact_values)
                if exact_values is not None
                else selection_value_groups_for_control(
                    page_label, expected, multi=multi
                )
            )
            if not groups:
                raise PddFormListingError("拼多多属性“{0}”期望值为空".format(page_label))
            current = tuple(
                value
                for value in await self._read_select_values(select, multi=multi)
                if normalize_option(value)
            )
            remaining = list(current)
            current_matches = []
            for group in groups:
                match_index = next(
                    (
                        index
                        for index, current_value in enumerate(remaining)
                        if any(
                            normalize_option(current_value)
                            == normalize_option(candidate)
                            for candidate in group
                        )
                    ),
                    None,
                )
                if match_index is None:
                    break
                current_matches.append(remaining.pop(match_index))
            if len(current_matches) == len(groups) and not remaining:
                await self._resolve_learning_groups(
                    page_label,
                    select,
                    tuple((value,) for value in current_matches),
                    observed_values=current_matches,
                )
                if self.logger is not None:
                    self.logger.info(
                        "拼多多属性“%s”当前值已匹配，完成轻量学习后跳过下拉点击",
                        page_label,
                    )
                return tuple(current_matches)
            groups = await self._resolve_learning_groups(page_label, select, groups)
            if not groups:
                if self.logger is not None:
                    self.logger.info(
                        "拼多多属性“%s”已加入本平台待审核汇总，继续填写后续字段",
                        page_label,
                    )
                return None
            return await self._select_values(select, groups, label=page_label, multi=multi)
        if len(visible_inputs) != 1:
            raise PddFormListingError(
                "拼多多属性“{0}”不是唯一的下拉或文本输入控件".format(page_label)
            )
        expected_text = str(expected).strip()
        input_box = visible_inputs[0]
        if (await input_box.input_value()).strip() != expected_text:
            await input_box.fill(expected_text)
            await input_box.press("Tab")
        actual = (await input_box.input_value()).strip()
        if actual != expected_text:
            raise PddFormListingError(
                "拼多多属性“{0}”填写后校验失败：期望 {1!r}，页面为 {2!r}".format(
                    page_label, expected_text, actual
                )
            )
        return (actual,)

    async def apply_recommended_category(self) -> str:
        """Use a unique dynamic recommendation, otherwise retain a real category."""
        if self.panel is None:
            raise PddFormListingError("请先调用 open() 打开拼多多资料")
        try:
            current = await self._category_text()
        except PddFormListingError:
            current = ""

        deadline = asyncio.get_running_loop().time() + 45
        recommended: List[Tuple[Any, str]] = []
        while asyncio.get_running_loop().time() < deadline:
            recommended = []
            buttons = self.panel.get_by_role("button", name="点击使用", exact=True)
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                if not await button.is_visible():
                    continue
                row = button.locator("xpath=..")
                row_text = re.sub(r"\s+", " ", (await row.inner_text()).strip())
                expected = re.sub(r"^\s*推荐\s*", "", row_text)
                expected = re.sub(r"\s*点击使用\s*$", "", expected).strip()
                if expected:
                    recommended.append((button, expected))
            if recommended:
                break
            if current and normalize_label(current) not in {
                normalize_label("请选择"),
                normalize_label("请选择类目"),
                normalize_label("商品分类"),
            }:
                if not self.category_hints:
                    return current
                chosen, _strategy = choose_category_candidate(
                    (current,), self.category_hints
                )
                if chosen:
                    return current
                raise PddFormListingError(
                    f"拼多多已选类目与 Excel 商品分类不匹配：{current!r}"
                )
            await asyncio.sleep(0.25)

        if not recommended:
            raise PddFormListingError("拼多多页面在 45 秒内没有可用推荐类目")
        if self.category_hints:
            chosen, strategy = choose_category_candidate(
                tuple(expected for _button, expected in recommended),
                self.category_hints,
            )
            matches = [
                item for item in recommended
                if self._normalize_category_path(item[1])
                == self._normalize_category_path(chosen)
            ] if chosen else []
            if len(matches) != 1:
                raise PddFormListingError(
                    "拼多多预测类目中没有与 Excel 商品分类唯一匹配的项："
                    f"{len(matches)}"
                )
            button, expected = matches[0]
            if self.logger is not None:
                self.logger.info(
                    "拼多多预测类目按 Excel 选择第 %s 个：%s（%s）",
                    next(index + 1 for index, item in enumerate(recommended) if item[0] == button),
                    expected,
                    strategy,
                )
        else:
            if len(recommended) != 1:
                raise PddFormListingError(
                    "拼多多页面预测类目不是唯一项：{0}".format(len(recommended))
                )
            button, expected = recommended[0]
        await button.scroll_into_view_if_needed()
        await button.click()
        self.category_clicked = True
        await self._wait_for_loading_masks()
        try:
            await self._attribute_items()
        except PddFormListingError as exc:
            raise PddFormListingError("拼多多推荐类目应用后，类目属性未渲染") from exc
        actual = await self._category_text()
        if self._normalize_category_path(actual) != self._normalize_category_path(expected):
            raise PddFormListingError(
                "拼多多类目应用后校验失败：推荐 {0!r}，页面为 {1!r}".format(
                    expected, actual
                )
            )
        if self.logger is not None:
            self.logger.info("已应用页面动态推荐的拼多多类目：%s", actual)
        return actual

    async def _attribute_assignments(
        self, fields: Mapping[str, str], page_items: Mapping[str, Tuple[str, Any]]
    ) -> Dict[str, Tuple[str, str]]:
        target_sources: Dict[str, List[Tuple[str, str]]] = {}
        page_items = without_color_attributes(page_items)
        for key, value in fields.items():
            aliases = set(excel_aliases(key))
            for normalized_page, (page_label, _item) in page_items.items():
                base = normalized_page.split("#", 1)[0]
                if base in PDD_IGNORED_CATEGORY_FIELDS or base in PDD_INHERITED_FIELDS:
                    continue
                accepted = {base}
                accepted.update(PDD_FIELD_ALIASES.get(base, ()))
                if aliases.intersection(accepted):
                    target_sources.setdefault(normalized_page, []).append(
                        (str(key), str(value).strip())
                    )
        assignments: Dict[str, Tuple[str, str]] = {}
        for normalized_page, (page_label, _item) in page_items.items():
            sources = target_sources.get(normalized_page, ())
            if len(sources) > 1:
                values = {value for _key, value in sources}
                if len(values) != 1:
                    raise PddFormListingError(
                        "拼多多属性“{0}”匹配到多个 Excel 字段：{1}".format(
                            page_label, "、".join(key for key, _value in sources)
                        )
                    )
            if sources:
                assignments[normalized_page] = (page_label, sources[0][1])
        return assignments

    async def fill_category_attributes(self, fields: PddFields) -> Mapping[str, Any]:
        category = await self.apply_recommended_category()
        page_items = without_color_attributes(await self._attribute_items())
        assignments = await self._attribute_assignments(fields.fields, page_items)
        applied: Dict[str, Tuple[str, ...]] = {}
        skipped: Dict[str, str] = {}
        ignored = []
        for normalized_page, (page_label, item) in page_items.items():
            base = normalized_page.split("#", 1)[0]
            report_label = page_label if "#" not in normalized_page else "{0}#{1}".format(
                page_label, normalized_page.split("#", 1)[1]
            )
            if base in PDD_INHERITED_FIELDS:
                continue
            if base in PDD_IGNORED_CATEGORY_FIELDS:
                ignored.append(report_label)
                continue
            assignment = assignments.get(normalized_page)
            if assignment is None:
                continue
            _source_label, expected = assignment
            exact_values = _special_category_values(
                fields.fields, page_label, expected
            )
            try:
                if (
                    normalize_label(page_label)
                    in {
                        normalize_label("面料"),
                        normalize_label("面料俗称"),
                        normalize_label("材质"),
                    }
                    and exact_values is not None
                    and len(exact_values) >= 2
                ):
                    actual = await self._fill_fabric_attribute(
                        page_label, exact_values, item=item,
                        writer=lambda value, values: self.fill_attribute(
                            page_label, value, exact_values=values, item=item
                        ),
                    )
                else:
                    actual = await self.fill_attribute(
                        page_label,
                        expected,
                        exact_values=exact_values,
                        item=item,
                    )
            except TaobaoListingError as exc:
                raise PddFormListingError(str(exc).replace("淘宝", "拼多多")) from exc
            if actual is None:
                skipped[report_label] = expected
            else:
                applied[report_label] = actual
        if self.logger is not None:
            self.logger.info(
                "拼多多类目属性填写完成：已填 %s 项，跳过 %s 项，按规则留空 %s 项",
                len(applied), len(skipped), len(ignored)
            )
        return {
            "category": category,
            "category_clicked": self.category_clicked,
            "attributes": applied,
            "skipped_values": skipped,
            "ignored_fields": tuple(ignored),
        }

    async def _batch_input(self, label: str) -> Any:
        if self.panel is None:
            raise PddFormListingError("请先调用 open() 打开拼多多资料")
        direct = self.panel.locator('[data-pdd-batch-field="{0}"] input'.format(label))
        if await direct.count() == 1:
            return direct.first
        label_nodes = self.panel.get_by_text(
            re.compile(r"^\\s*{0}\\s*[：:]?\\s*$".format(re.escape(label)))
        )
        matches = []
        for index in range(await label_nodes.count()):
            node = label_nodes.nth(index)
            if not await node.is_visible():
                continue
            for depth in range(3):
                parent = (
                    node
                    if depth == 0
                    else node.locator(
                        "xpath=" + "/".join(".." for _ in range(depth))
                    )
                )
                inputs = parent.locator(
                    'input:not([type="hidden"]):not([readonly]):visible'
                )
                if await inputs.count() == 1:
                    matches.append(inputs.first)
                    break
        if len(matches) != 1:
            # 生产页的价格库存标签有时由组件 slot 生成，页面可见但没有
            # 独立文字节点。回退到“批量设置”按钮的最近无表格祖先；该区
            # 在页面固定按 拼单价、单买价、库存、重量、SKU 编码 排序。
            button = self.panel.get_by_role("button", name="批量设置", exact=True)
            if await button.count() == 1:
                await button.first.scroll_into_view_if_needed()
                root = button.first
                for depth in range(1, 8):
                    root = root.locator("xpath=..")
                    if await root.locator(".el-table, table").count():
                        continue
                    inputs = root.locator(
                        'input:not([type="hidden"]):not([readonly]):visible'
                    )
                    count = await inputs.count()
                    if len(PDD_BATCH_ORDER) <= count <= 5:
                        return inputs.nth(PDD_BATCH_ORDER.index(label))
            raise PddFormListingError(
                "拼多多批量字段“{0}”输入框不唯一：{1}".format(label, len(matches))
            )
        return matches[0]

    async def _fill_batch_number(self, label: str, expected: str) -> str:
        if not _is_decimal(expected) or Decimal(expected) < 0:
            raise PddFormListingError("Excel 拼多多{0}不是非负数字：{1!r}".format(label, expected))
        input_box = await self._batch_input(label)
        current = (await input_box.input_value()).strip()
        if not _numeric_equal(current, expected):
            await self._enter_number_as_user(input_box, expected)
        actual = (await input_box.input_value()).strip()
        if not _numeric_equal(actual, expected):
            raise PddFormListingError(
                "拼多多批量字段“{0}”回读失败：期望 {1!r}，页面为 {2!r}".format(
                    label, expected, actual
                )
            )
        return actual

    @staticmethod
    async def _enter_number_as_user(input_box: Any, expected: str) -> None:
        """Enter a number with keyboard events and finish the component edit.

        The production PDD grid has two layers of state: the visible native
        input and the row model serialized by Save.  Assigning only the native
        value can look correct while leaving the row model unchanged.  A real
        typing sequence followed by blur keeps both layers in sync.
        """

        await input_box.click()
        await input_box.fill("")
        await input_box.press_sequentially(expected)
        await input_box.press("Tab")

    async def _sku_table(self) -> Any:
        if self.panel is None:
            raise PddFormListingError("请先调用 open() 打开拼多多资料")
        tables = self.panel.locator(".el-table:visible")
        if await tables.count() == 0:
            tables = self.panel.locator("table:visible")
        matches = []
        for index in range(await tables.count()):
            table = tables.nth(index)
            headers = await table.locator(
                ".el-table__header-wrapper th, thead th"
            ).all_inner_texts()
            normalized = [normalize_label(header) for header in headers]
            if all(
                any(
                    value == normalize_label(label)
                    or value.startswith(normalize_label(label))
                    for value in normalized
                )
                for label in PDD_BATCH_ORDER
            ):
                matches.append(table)
        if len(matches) != 1:
            raise PddFormListingError("拼多多 SKU 表格不是唯一项：{0}".format(len(matches)))
        return matches[0]

    async def _sku_table_snapshot(self) -> Mapping[str, Any]:
        # Element UI renders one logical grid as an `.el-table` wrapper plus
        # one or more nested native tables.  Prefer the wrapper so the same
        # SKU grid is never counted twice; plain-table fixtures remain a
        # supported fallback.
        table = await self._sku_table()
        return await table.evaluate(
            """root => {
              const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const headers = Array.from(root.querySelectorAll(
                '.el-table__header-wrapper th, thead th'
              )).map(node => clean(node.innerText));
              const rowNodes = root.querySelectorAll(
                '.el-table__body-wrapper tbody tr, tbody tr'
              );
              const rows = Array.from(rowNodes).map(row => Array.from(
                row.querySelectorAll(':scope > td')
              ).map(cell => {
                const inputs = Array.from(cell.querySelectorAll('input'))
                  .filter(input => input.type !== 'checkbox' && input.type !== 'radio')
                  .map(input => clean(input.value));
                return inputs.length ? inputs.join('|') : clean(cell.innerText);
              }));
              return {headers, rows};
            }"""
        )

    @staticmethod
    def _sku_column_index(headers: Sequence[str], label: str) -> int:
        wanted = normalize_label(label)
        matches = [
            index
            for index, header in enumerate(headers)
            if normalize_label(header) == wanted or normalize_label(header).startswith(wanted)
        ]
        if len(matches) != 1:
            raise PddFormListingError(
                "拼多多 SKU 表格列“{0}”不是唯一项：{1}".format(label, matches)
            )
        return matches[0]

    def _validate_sku_snapshot(
        self, snapshot: Mapping[str, Any], expected: Mapping[str, str]
    ) -> Tuple[Mapping[str, str], ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        rows = tuple(snapshot.get("rows", ()))
        if not rows:
            raise PddFormListingError("拼多多 SKU 表格没有可校验的明细行")
        indices = {label: self._sku_column_index(headers, label) for label in expected}
        result = []
        errors = []
        for row_number, raw_row in enumerate(rows, 1):
            values = {
                label: str(raw_row[index]) if index < len(raw_row) else ""
                for label, index in indices.items()
            }
            for label, expected_value in expected.items():
                if not _numeric_equal(values[label], expected_value):
                    errors.append("第{0}行{1}={2!r}".format(row_number, label, values[label]))
            result.append(values)
        if errors:
            raise PddFormListingError("拼多多批量设置后校验失败：" + "；".join(errors[:12]))
        return tuple(result)

    @staticmethod
    def _expected_price_inventory(fields: Mapping[str, str]) -> Mapping[str, str]:
        try:
            group_price = normalize_money_value(
                _required_excel_value(fields, ("拼单价", "拼团价"), "拼单价")
            )
            single_price = normalize_money_value(
                _required_excel_value(fields, ("单买价", "单独购买价"), "单买价")
            )
        except MoneyValueError as exc:
            raise PddFormListingError(f"Excel 拼多多价格{exc}") from exc
        expected = {
            "拼单价": group_price,
            "单买价": single_price,
            # PDD 的“库存”对应 Excel 通用“数量”。不要把抖音专用的
            # “现货库存”混入候选；两者常同时存在且语义不同。
            "库存": _required_excel_value(fields, ("库存", "数量"), "库存"),
        }
        if not _is_decimal(expected["库存"]):
            raise PddFormListingError("Excel 拼多多库存不是数字：{0!r}".format(expected["库存"]))
        inventory = Decimal(expected["库存"])
        if not inventory.is_finite() or inventory % 1:
            raise PddFormListingError("Excel 拼多多库存必须是整数：{0!r}".format(expected["库存"]))
        return expected

    async def fill_price_inventory_batch(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        expected = self._expected_price_inventory(fields)
        if self.logger is not None:
            self.logger.info("拼多多价格库存：开始填写批量值")
        for label, value in expected.items():
            await self._fill_batch_number(label, value)
        if self.panel is None:
            raise PddFormListingError("请先调用 open() 打开拼多多资料")
        button = self.panel.get_by_role("button", name="批量设置", exact=True)
        if await button.count() != 1:
            raise PddFormListingError("拼多多“批量设置”按钮不是唯一项：{0}".format(await button.count()))
        if self.logger is not None:
            self.logger.info("拼多多价格库存：批量值已填，点击一次“批量设置”")
        await button.click()
        if self.logger is not None:
            self.logger.info("拼多多价格库存：批量设置按钮已返回，开始校验规格行")
        deadline = asyncio.get_running_loop().time() + 10
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                rows = self._validate_sku_snapshot(await self._sku_table_snapshot(), expected)
                if self.logger is not None:
                    self.logger.info("拼多多价格库存：%s 行批量值校验通过", len(rows))
                return {"batch_clicked": True, "row_count": len(rows), "values": expected, "rows": rows}
            except PddFormListingError as exc:
                last_error = exc
                await asyncio.sleep(0.1)
        raise PddFormListingError(str(last_error or "拼多多批量设置超时"))

    async def apply_timed_presale(self) -> Mapping[str, str]:
        try:
            presale = await self._ensure_radio("是否预售", "时段预售")
            form_item = await self._form_item("支付成功后")
            selects = form_item.locator(".el-select")
            if await selects.count() != 1:
                raise PddFormListingError("拼多多“支付成功后”下拉框不唯一")
            actual = await self._select_values(
                selects.first, (("15天",),), label="支付成功后", multi=False
            )
        except TaobaoListingError as exc:
            raise PddFormListingError(str(exc).replace("淘宝", "拼多多")) from exc
        if actual is None:
            raise PddFormListingError("拼多多“支付成功后”没有“15天”精确候选")
        return {"是否预售": presale, "支付成功后": actual[0]}

    async def _settled_visible_validation_errors(
        self, timeout_seconds: float = 3
    ) -> Tuple[str, ...]:
        """Wait for Element UI's transient batch-validation message to settle."""

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_errors: Tuple[str, ...] = ()
        while True:
            try:
                # Read labels and messages in one browser-side snapshot.  The
                # batch validator removes these nodes asynchronously; walking
                # live locators one-by-one can otherwise wait the full global
                # timeout after a node disappears between count/inner_text.
                last_errors = tuple(
                    await self.panel.evaluate(
                        """root => Array.from(
                          root.querySelectorAll('.el-form-item__error')
                        ).filter(error => {
                          const style = getComputedStyle(error);
                          return style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && error.getClientRects().length > 0;
                        }).map(error => {
                          const item = error.closest('.el-form-item');
                          const label = item && item.querySelector(
                            ':scope > .el-form-item__label'
                          );
                          const labelText = label && label.innerText.trim()
                            ? label.innerText.trim() : '未命名字段';
                          return `${labelText}：${error.innerText.trim()}`;
                        }).filter((value, index, values) =>
                          value && values.indexOf(value) === index
                        )"""
                    )
                )
            except Exception as exc:
                if asyncio.get_running_loop().time() >= deadline:
                    raise PddFormListingError(
                        "读取拼多多页面校验状态超时"
                    ) from exc
                await asyncio.sleep(0.1)
                continue
            if not last_errors or asyncio.get_running_loop().time() >= deadline:
                return last_errors
            await asyncio.sleep(0.1)

    async def apply_excel_fields(self, fields: PddFields) -> Mapping[str, Any]:
        attributes = await self.fill_category_attributes(fields)
        # Selecting timed presale makes the production PDD page rebuild the
        # SKU grid.  Do it first so the final price/inventory batch is never
        # discarded by that later re-render.
        if self.logger is not None:
            self.logger.info("拼多多价格库存：先设置时段预售 15 天")
        presale = await self.apply_timed_presale()
        if self.logger is not None:
            self.logger.info("拼多多价格库存：时段预售设置完成")
        batch = await self.fill_price_inventory_batch(fields.fields)
        # Later controls can trigger a PDD grid re-render.  Always read the
        # final grid again immediately before the common runner may save it.
        expected = self._expected_price_inventory(fields.fields)
        final_rows = self._validate_sku_snapshot(
            await self._sku_table_snapshot(), expected
        )
        batch = dict(batch)
        batch["rows"] = final_rows
        batch["row_count"] = len(final_rows)
        if self.logger is not None:
            self.logger.info("拼多多价格库存：等待页面校验状态稳定")
        visible_errors = await self._settled_visible_validation_errors()
        if (
            visible_errors
            and not (
                self.attribute_runtime is not None
                and getattr(
                    self.attribute_runtime, "has_deferred_reviews", False
                )
            )
        ):
            raise PddFormListingError("拼多多页面校验错误：" + "；".join(visible_errors))
        return {
            "attributes": attributes,
            "sku_batch": batch,
            "timed_presale": presale,
            "deferred_validation_errors": (
                visible_errors
                if self.attribute_runtime is not None
                and getattr(
                    self.attribute_runtime, "has_deferred_reviews", False
                )
                else ()
            ),
        }

    async def verify_persisted_price_inventory(
        self, fields: Mapping[str, str]
    ) -> Mapping[str, Any]:
        """Read back the reopened PDD grid after Save."""

        expected = self._expected_price_inventory(fields)
        deadline = asyncio.get_running_loop().time() + 10
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                rows = self._validate_sku_snapshot(
                    await self._sku_table_snapshot(), expected
                )
                return {
                    "row_count": len(rows),
                    "values": expected,
                    "rows": rows,
                }
            except PddFormListingError as exc:
                last_error = exc
                await asyncio.sleep(0.1)
        raise PddFormListingError(
            "拼多多保存后价格库存复核超时：{0}".format(
                last_error or "规格表未加载"
            )
        )
