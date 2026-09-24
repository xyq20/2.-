"""DOM writer and post-save verifier for FastMai's JD product tab."""

from __future__ import annotations

from field_policies import (
    is_single_material_expression,
    match_option_candidates,
    skip_color_attribute,
    without_color_attributes,
)

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from attribute_runtime import AttributeRequest
from canonical_fields import is_learning_managed_field
from category_profile import preferred_category_leaf
from douyin_data import MaterialComponent
from jd_data import JdFields
from learning_models import CandidateValue, canonical_sha256
from money_values import MoneyValueError, normalize_money_value
from platform_candidate_source import (
    CandidateSourceError,
    DomCandidate,
    reconcile_candidates,
)
from platform_schema import FieldOption, FieldSchema
from taobao_listing import (
    TAOBAO_MATERIAL_OPTION_ALIASES,
    TaobaoListingError,
    excel_aliases,
    material_name_groups,
    normalize_label,
    normalize_option,
    parse_material_components,
    parse_taobao_materials,
    preferred_exact_candidate_label,
    selection_value_groups,
    value_candidates,
)
from youzan_form_listing import YouzanFormListing, YouzanFormListingError


class JdFormListingError(RuntimeError):
    """A JD form problem that can be shown directly to the operator."""


JD_CATEGORY_PATH = ("服饰内衣", "男装", "男士休闲裤", "男士休闲直筒裤")
JD_BRAND = "NEIGBORL"
JD_DELIVERY_TEMPLATE = "48小时发货"
JD_GROSS_WEIGHT = "1"
JD_SKU_THICKNESS = "常规"
JD_PROPERTIES_ENDPOINT = "/jd/getCategoryProperties.json"
JD_BATCH_ORDER = ("京东价", "库存")
JD_EXACT_OPTION_MAP: Mapping[str, Mapping[str, str]] = {
    normalize_label("面料"): {
        normalize_option("棉"): "棉布",
    },
}

JD_FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    normalize_label("面料"): tuple(
        normalize_label(value)
        for value in ("面料材质", "面料俗称", "水洗标", "吊牌图")
    ),
    normalize_label("材质"): tuple(normalize_label(value) for value in ("材质成分",)),
    normalize_label("产地"): (),
    normalize_label("款式"): tuple(normalize_label(value) for value in ("裤型",)),
    normalize_label("裤型"): tuple(normalize_label(value) for value in ("款式",)),
    normalize_label("工艺"): tuple(normalize_label(value) for value in ("服饰工艺", "工艺处理")),
    normalize_label("流行元素"): tuple(normalize_label(value) for value in ("款式细节",)),
    normalize_label("版型"): tuple(normalize_label(value) for value in ("服饰版型",)),
    normalize_label("裤脚口款式"): tuple(normalize_label(value) for value in ("裤脚款式",)),
    normalize_label("适用人群"): tuple(normalize_label(value) for value in ("适用对象",)),
    normalize_label("上市时间"): tuple(
        normalize_label(value) for value in ("上市年份季节", "上市时节")
    ),
    normalize_label("弹力"): tuple(normalize_label(value) for value in ("弹力等级",)),
    normalize_label("风格"): tuple(normalize_label(value) for value in ("细分风格", "基础风格")),
    normalize_label("品牌类型"): (),
    normalize_label("功能"): tuple(normalize_label(value) for value in ("面料功能",)),
}

JD_IGNORED_ATTRIBUTE_LABELS = frozenset(
    normalize_label(value)
    for value in (
        "颜色",
        "是否可机洗",
    )
)

JD_CLEARED_ATTRIBUTE_LABELS = frozenset(
    (normalize_label("产地"),)
)

# 京东风格是二级级联。Excel 写的是「休闲风/时尚都市」，二级页实际候选是
# 「简约风 / oversize」，按运营确认把休闲风和时尚都市兼容到简约风。
JD_CASCADER_LEAF_ALIASES: Mapping[Tuple[str, str], Tuple[str, ...]] = {
    (normalize_label("风格"), normalize_option("休闲风")): ("简约风",),
    (normalize_label("风格"), normalize_option("休闲")): ("简约风",),
    (normalize_label("风格"), normalize_option("时尚都市")): ("简约风",),
}


def _json_values(value: Any) -> Tuple[Any, ...]:
    return tuple(value) if isinstance(value, (tuple, list)) else ()


def _first_mapping_value(value: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, ""):
            return candidate
    return None


def _jd_option(option: Any, position: int) -> FieldOption:
    if not isinstance(option, Mapping):
        return FieldOption("", str(option), position)
    value_id = _first_mapping_value(
        option,
        (
            "value_id",
            "id",
            "vid",
            "valueId",
            "propertyValueId",
            "propValueId",
            "attrValueId",
            "code",
        ),
    )
    label = _first_mapping_value(
        option,
        (
            "label",
            "name",
            "valueName",
            "propertyValueName",
            "propValueName",
            "attrValueName",
            "displayName",
            "display_name",
        ),
    )
    raw_value = option.get("value")
    if value_id in (None, "") and label not in (None, "") and raw_value not in (None, ""):
        value_id = raw_value
    elif label in (None, "") and value_id not in (None, "") and raw_value not in (None, ""):
        label = raw_value
    return FieldOption(
        "" if value_id is None else str(value_id),
        "" if label is None else str(label),
        position,
    )


def parse_jd_attribute_fields(payload: Any) -> Tuple[FieldSchema, ...]:
    """Extract strict JD field/value identities from the captured JSON body.

    FastMai has returned several envelope and descriptor shapes over time.  The
    parser therefore walks the decoded response, but only accepts an object as
    a field when it owns a field ID, a field label and an explicit option list.
    Missing option IDs or labels remain empty and fail closed during API/DOM
    reconciliation instead of being invented from page text.
    """

    matches: Dict[Tuple[str, str], FieldSchema] = {}
    field_id_keys = (
        "refPid",
        "propId",
        "propertyId",
        "attrId",
        "attributeId",
        "id",
    )
    field_label_keys = (
        "propertyName",
        "propName",
        "attrName",
        "attributeName",
        "name",
        "label",
    )
    option_keys = (
        "values",
        "options",
        "attrValueList",
        "propertyValues",
        "propValues",
        "attributeValues",
        "attrValues",
        "valueList",
    )

    def visit(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("{", "[")):
                try:
                    visit(json.loads(text))
                except (TypeError, ValueError):
                    pass
            return
        if isinstance(value, (tuple, list)):
            for child in value:
                visit(child)
            return
        if not isinstance(value, Mapping):
            return

        source_id = _first_mapping_value(value, field_id_keys)
        label = _first_mapping_value(value, field_label_keys)
        raw_options: Tuple[Any, ...] = ()
        for key in option_keys:
            raw_options = _json_values(value.get(key))
            if raw_options:
                break
        if source_id not in (None, "") and label not in (None, "") and raw_options:
            options = tuple(
                _jd_option(option, position)
                for position, option in enumerate(raw_options)
            )
            key = (str(source_id), normalize_label(str(label)))
            matches[key] = FieldSchema(
                schema_key="jd:attribute:{0}".format(source_id),
                source_id=str(source_id),
                label=str(label),
                section="attributes",
                control_type="select_many"
                if value.get("multiple") is True
                or isinstance(value.get("chooseMaxNum"), int)
                and value.get("chooseMaxNum") > 1
                else "select_one",
                required=(
                    value.get("required")
                    if isinstance(value.get("required"), bool)
                    else value.get("isRequired")
                    if isinstance(value.get("isRequired"), bool)
                    else None
                ),
                multiple=value.get("multiple")
                if isinstance(value.get("multiple"), bool)
                else None,
                custom_allowed=value.get("canNote")
                if isinstance(value.get("canNote"), bool)
                else False,
                option_values=options,
                api_paths=(JD_PROPERTIES_ENDPOINT,),
            )
        for child in value.values():
            visit(child)

    visit(payload)
    return tuple(matches.values())


def _expand_cascader_candidates(label: str, candidates: Sequence[str]) -> Tuple[str, ...]:
    expanded: List[str] = []
    for candidate in candidates:
        if candidate not in expanded:
            expanded.append(candidate)
        for alias in JD_CASCADER_LEAF_ALIASES.get(
            (normalize_label(label), normalize_option(candidate)),
            (),
        ):
            if alias not in expanded:
                expanded.append(alias)
    return tuple(expanded)


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
        raise JdFormListingError(
            "Excel 中缺少京东{0}字段（可识别：{1}）".format(
                label, "/".join(aliases)
            )
        )
    if money:
        try:
            matches = [
                (key, normalize_money_value(value)) for key, value in matches
            ]
        except MoneyValueError as exc:
            raise JdFormListingError(f"Excel 京东{label}{exc}") from exc
    values = {value for _key, value in matches}
    if len(values) != 1:
        raise JdFormListingError(
            "京东{0}匹配到多个 Excel 字段：{1}".format(
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
    names = []
    for text in re.split(r"[/／,，、;；]", str(value)):
        text = re.sub(r"\s*[（(]?\s*\d+(?:\.\d+)?\s*[%％]\s*[）)]?\s*$", "", text).strip()
        if text and text not in names:
            names.append(text)
    return "/".join(names)


def _jd_material_components(value: str) -> Tuple[MaterialComponent, ...]:
    """Reuse the shared Excel composition parser for JD material rows."""
    try:
        return parse_taobao_materials({"材质成分": value})
    except TaobaoListingError as exc:
        if is_single_material_expression(value):
            return ()
        raise JdFormListingError(str(exc).replace("淘宝", "京东")) from exc


def _material_option_desired(name: str) -> str:
    aliases = TAOBAO_MATERIAL_OPTION_ALIASES.get(normalize_option(name), ())
    return "/".join((name,) + aliases)


def _material_percentage(value: str) -> Optional[str]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*[%％]", str(value))
    if match is None:
        return None
    number = Decimal(match.group(1))
    if not number.is_finite() or number < 0 or number > 100:
        return None
    return format(number.normalize(), "f")


def _kilograms(value: str) -> str:
    text = str(value).strip().casefold().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(kg|公斤|g|克)?", text)
    if match is None:
        raise JdFormListingError("Excel 克重无法换算为公斤：{0!r}".format(value))
    number = Decimal(match.group(1))
    if match.group(2) in {"g", "克"}:
        number /= Decimal("1000")
    if not number.is_finite() or number <= 0:
        raise JdFormListingError("Excel 克重必须大于 0")
    return format(number.normalize(), "f")


def _category_parts(value: object) -> Tuple[str, ...]:
    return tuple(
        normalize_label(part)
        for part in re.split(r"[>＞/／]", str(value or ""))
        if normalize_label(part)
    )


def _jd_category_hints(fields: Mapping[str, str]) -> Tuple[str, ...]:
    raw_value = _required_excel_value(fields, ("商品分类",), "商品分类")
    hints = tuple(
        part.strip()
        for part in re.split(r"[/／>＞]", raw_value)
        if part.strip()
    )
    if not hints:
        raise JdFormListingError("Excel 京东商品分类没有可用提示词")
    return hints


def jd_category_target(fields: Mapping[str, str]) -> Tuple[Tuple[str, ...], str]:
    """Choose the most specific Excel hint; the page still proves uniqueness."""

    hints = _jd_category_hints(fields)
    target = preferred_category_leaf(hints)
    if not target:
        raise JdFormListingError("Excel 京东商品分类无法生成搜索词")
    return hints, target


def _image_color_histogram(path: Path) -> Any:
    """Return a garment-focused HSV histogram for conservative color grouping."""
    try:
        import cv2
        import numpy as np

        encoded = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            raise ValueError("decode failed")
        height, width = image.shape[:2]
        crop = image[
            max(0, int(height * 0.10)) : max(1, int(height * 0.95)),
            max(0, int(width * 0.15)) : max(1, int(width * 0.85)),
        ]
        crop = cv2.resize(crop, (96, 96), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        saturation = hsv[:, :, 1]
        value = hsv[:, :, 2]
        # Ignore bright, weakly saturated studio backgrounds while retaining
        # dark neutral garments such as black and grey.
        mask = (((saturation >= 22) & (value <= 248)) | (value <= 185)).astype("uint8") * 255
        if int(np.count_nonzero(mask)) < 96 * 96 // 12:
            mask = None
        histogram = cv2.calcHist([hsv], [0, 1, 2], mask, [18, 6, 6], [0, 180, 0, 256, 0, 256])
        cv2.normalize(histogram, histogram, alpha=1.0, norm_type=cv2.NORM_L1)
        return histogram
    except Exception as exc:
        raise JdFormListingError("京东图片颜色识别无法读取：{0}".format(path)) from exc


def classify_image_indices_by_color(
    square_paths: Sequence[Path],
    portrait_paths: Sequence[Path],
    sku_paths: Sequence[Path],
    color_count: int,
) -> Tuple[Tuple[int, ...], ...]:
    """Classify paired square/portrait images using ordered SKU color refs.

    Square and portrait images are paired strictly by filename order.  Each
    SKU reference is first anchored to a unique closest square image.  Images
    that are not confidently color-specific (for example a label close-up)
    are shared by every color instead of being guessed into the wrong group.
    """
    if color_count < 1:
        raise JdFormListingError("京东页面没有可处理的颜色")
    if len(square_paths) != len(portrait_paths):
        raise JdFormListingError(
            "京东 1:1 商品图与 3:4 长图数量不一致，无法一一对应：{0}/{1}".format(
                len(square_paths), len(portrait_paths)
            )
        )
    if not square_paths:
        raise JdFormListingError("京东没有可追加的商品图")
    square_order = tuple(path.stem.casefold() for path in square_paths)
    portrait_order = tuple(path.stem.casefold() for path in portrait_paths)
    if square_order != portrait_order:
        raise JdFormListingError(
            "京东 1:1 商品图与 3:4 长图文件序号不一致，无法保证顺序对应"
        )
    if color_count == 1:
        if len(sku_paths) != 1:
            raise JdFormListingError(
                "京东 SKU 颜色参考图数量与页面颜色数不一致：{0}/1".format(len(sku_paths))
            )
        return (tuple(range(len(square_paths))),)
    if len(sku_paths) != color_count:
        raise JdFormListingError(
            "京东 SKU 颜色参考图数量与页面颜色数不一致：{0}/{1}".format(
                len(sku_paths), color_count
            )
        )

    try:
        import cv2
    except ImportError as exc:
        raise JdFormListingError("缺少京东图片颜色分类依赖 opencv-python-headless") from exc
    image_histograms = tuple(_image_color_histogram(path) for path in square_paths)
    reference_histograms = tuple(_image_color_histogram(path) for path in sku_paths)
    distances = tuple(
        tuple(
            float(cv2.compareHist(image_histogram, reference, cv2.HISTCMP_BHATTACHARYYA))
            for reference in reference_histograms
        )
        for image_histogram in image_histograms
    )

    # Reserve one unique, visually close anchor for every color.  Without an
    # anchor the automation cannot prove the page/SKU reference order.
    candidates = sorted(
        (distances[image_index][color_index], color_index, image_index)
        for image_index in range(len(square_paths))
        for color_index in range(color_count)
    )
    anchors: Dict[int, int] = {}
    used_images = set()
    for distance, color_index, image_index in candidates:
        if color_index in anchors or image_index in used_images:
            continue
        if distance > 0.58:
            continue
        anchors[color_index] = image_index
        used_images.add(image_index)
    if len(anchors) != color_count:
        missing = [str(index + 1) for index in range(color_count) if index not in anchors]
        raise JdFormListingError(
            "京东无法从商品图中确认第 {0} 个 SKU 颜色，已停止避免错配".format("、".join(missing))
        )

    assignments: List[List[int]] = [[] for _ in range(color_count)]
    anchored_images = {image_index: color_index for color_index, image_index in anchors.items()}
    for image_index, row in enumerate(distances):
        if image_index in anchored_images:
            assignments[anchored_images[image_index]].append(image_index)
            continue
        ranked = sorted((distance, color_index) for color_index, distance in enumerate(row))
        best_distance, best_color = ranked[0]
        margin = ranked[1][0] - best_distance
        if best_distance <= 0.58 and margin >= 0.06:
            assignments[best_color].append(image_index)
        else:
            # Non-colour-specific content is useful to every variant and keeps
            # its original source index in both paired columns.
            for indices in assignments:
                indices.append(image_index)
    return tuple(tuple(indices) for indices in assignments)


class JdFormListing(YouzanFormListing):
    """Fill and verify JD category, attributes, SKU data and delivery time."""

    allow_created_exact_dom_option = True
    attribute_wait_timeout_seconds = 30.0
    attribute_stable_seconds = 0.75

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
            if path != JD_PROPERTIES_ENDPOINT:
                return
            task = asyncio.create_task(self._capture_api_response(response))
            self._api_capture_tasks.append(task)

        self._api_response_handler = handle_response
        self.page.on("response", handle_response)

    async def _capture_api_response(self, response: Any) -> None:
        observation: Dict[str, Any] = {
            "path": JD_PROPERTIES_ENDPOINT,
            "http_status": int(response.status),
            "body": "unavailable",
            "attribute_fields": (),
            "category_id": "",
        }
        try:
            payload = await response.json()
            observation["body"] = "json"
            observation["attribute_fields"] = parse_jd_attribute_fields(payload)
            query = parse_qs(urlsplit(response.url).query)
            for key in ("categoryId", "leafCategoryId", "cid"):
                values = tuple(query.get(key, ()))
                if len(values) == 1 and str(values[0]).strip():
                    observation["category_id"] = str(values[0]).strip()
                    break
        except Exception:
            pass
        self._api_observations.append(observation)

    async def _captured_api_field_definition(self, page_label: str) -> FieldSchema:
        tasks = tuple(getattr(self, "_api_capture_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        wanted = normalize_label(page_label)
        matches: Dict[Tuple[str, str], FieldSchema] = {}
        for observation in getattr(self, "_api_observations", ()):
            for field in observation.get("attribute_fields", ()):
                if normalize_label(field.label) == wanted:
                    matches[(str(field.source_id or ""), field.schema_key)] = field
        if len(matches) != 1:
            raise JdFormListingError(
                "京东属性“{0}”缺少唯一的接口 JSON 字段定义".format(
                    page_label
                )
            )
        return next(iter(matches.values()))

    async def _captured_api_field(self, page_label: str) -> Tuple[FieldSchema, str]:
        field = await self._captured_api_field_definition(page_label)
        category_ids: List[str] = []
        for observation in getattr(self, "_api_observations", ()):
            category_id = str(observation.get("category_id") or "").strip()
            if category_id and category_id not in category_ids:
                category_ids.append(category_id)
        if len(category_ids) != 1:
            raise JdFormListingError(
                "京东属性“{0}”缺少唯一的接口类目 ID".format(page_label)
            )
        return field, category_ids[0]

    async def _select_numeric_material_option(
        self,
        select: Any,
        desired: str,
        *,
        field_label: str = "材质",
        review_unmatched: bool = True,
    ) -> Optional[Tuple[str, ...]]:
        """Click the live JD material option and prove its numeric platform ID."""
        try:
            field, category_id = await self._captured_api_field(field_label)
        except JdFormListingError:
            field = await self._captured_api_field_definition(field_label)
            category_id = ""
        desired_labels = tuple(
            part.strip() for part in re.split(r"[/／]", desired) if part.strip()
        )
        numeric_options = tuple(
            option
            for option in field.option_values
            if re.fullmatch(r"\d+", str(option.value_id).strip())
        )
        # 公共分层匹配：京东候选“氨纶(聚氨酯弹性纤维)”标注了 Excel 名“氨纶”，
        # 同时独立候选“弹性纤维”只是别名“聚氨酯弹性纤维”的子串，必须降级，
        # 否则双向子串会命中两个候选导致中断（多平台表单适配器可复用）。
        matches = tuple(
            numeric_options[index]
            for index in match_option_candidates(
                desired_labels, tuple(option.label for option in numeric_options)
            )
        )
        if len(matches) != 1:
            runtime = getattr(self, "attribute_runtime", None)
            if (
                review_unmatched
                and runtime is not None
                and category_id
                and field.source_id
            ):
                schema_version = canonical_sha256(
                    {
                        "platform_id": "jd",
                        "category_leaf_id": category_id,
                        "field_id": str(field.source_id),
                        "options": [
                            {"value_id": option.value_id, "label": option.label}
                            for option in numeric_options
                        ],
                    }
                )
                resolved = await runtime.resolve(
                    AttributeRequest(
                        platform_id="jd",
                        category_leaf_id=category_id,
                        field_id=str(field.source_id),
                        field_label=field_label,
                        candidates=tuple(
                            CandidateValue(option.value_id, option.label)
                            for option in numeric_options
                        ),
                        excel_value="/".join(desired_labels),
                        evidence={
                            "excel": bool(desired_labels),
                            "force_review": True,
                        },
                        custom_allowed=False,
                        schema_version=schema_version,
                        control_type="select",
                    )
                )
                if resolved is None:
                    return None
                matches = tuple(
                    option
                    for option in numeric_options
                    if option.value_id == resolved.value_id
                )
                if len(matches) != 1:
                    raise JdFormListingError(
                        "京东属性{0}的审核结果不在当前数字候选中".format(
                            field_label
                        )
                    )
            else:
                available = "、".join(
                    "{0}[{1}]".format(option.label, option.value_id)
                    for option in field.option_values
                    if option.label
                )
                raise JdFormListingError(
                    "京东属性{0}没有唯一的数字 ID 候选：期望 {1}；接口候选 {2}".format(
                        field_label,
                        "/".join(desired_labels) or "空",
                        available or "无",
                    )
                )
        target = matches[0]

        async def selected_ids() -> Tuple[str, ...]:
            raw_ids = await select.evaluate(
                """element => {
                  const component = element.__vue__;
                  if (!component) return [];
                  const result = [];
                  const append = value => {
                    if (Array.isArray(value)) {
                      value.forEach(append);
                      return;
                    }
                    if (value === undefined || value === null || value === '') return;
                    if (typeof value === 'object') {
                      append(value.value ?? value.id ?? value.valueId);
                      return;
                    }
                    result.push(String(value));
                  };
                  append(component.value);
                  append(component.selected && component.selected.value);
                  return [...new Set(result)];
                }"""
            )
            return tuple(
                str(value).strip() for value in raw_ids if str(value).strip()
            )

        current_labels = await self._read_select_values(select, multi=False)
        current_ids = await selected_ids()
        if (
            len(current_labels) == 1
            and normalize_option(current_labels[0]) == normalize_option(target.label)
            and any(re.fullmatch(r"\d+", value) for value in current_ids)
        ):
            return current_labels

        # A persisted text-only value makes Element UI disable the matching
        # live option as a duplicate. Clear that stale selection first so the
        # real numeric option can be clicked normally.
        if any(current_labels) or current_ids:
            await select.hover()
            clear_buttons = select.locator(
                ".el-select__clear:visible, .el-icon-circle-close:visible"
            )
            if await clear_buttons.count() == 1:
                await clear_buttons.first.click(force=True, timeout=2_000)
            else:
                cleared = await select.evaluate(
                    """async element => {
                      const component = element.__vue__;
                      if (!component) return false;
                      if (typeof component.deleteSelected === 'function') {
                        component.deleteSelected({stopPropagation() {}});
                      } else if (typeof component.$emit === 'function') {
                        component.value = component.multiple ? [] : '';
                        component.selected = component.multiple ? [] : {};
                        component.selectedLabel = '';
                        component.query = '';
                        component.$emit('input', component.value);
                        component.$emit('change', component.value);
                        component.$emit('clear');
                      } else {
                        return false;
                      }
                      if (typeof component.$nextTick === 'function') {
                        await new Promise(resolve => component.$nextTick(resolve));
                      }
                      return true;
                    }"""
                )
                if not cleared:
                    raise JdFormListingError(
                        "京东属性{0}无法清除旧文字值".format(field_label)
                    )
            clear_deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < clear_deadline:
                if not any(await self._read_select_values(select, multi=False)):
                    break
                await asyncio.sleep(0.05)
            else:
                raise JdFormListingError(
                    "京东属性{0}旧文字值清空回读失败".format(field_label)
                )

        await self._open_select(select, multi=False)
        await self._search_select(select, target.label)
        deadline = asyncio.get_running_loop().time() + 5
        dropdown = None
        last_options: List[Mapping[str, Any]] = []
        dom_matches: List[Mapping[str, Any]] = []
        while asyncio.get_running_loop().time() < deadline:
            try:
                dropdown, options = await self._visible_dom_options(
                    select, timeout_seconds=0.4
                )
            except TaobaoListingError:
                await asyncio.sleep(0.1)
                continue
            last_options = options
            dom_matches = [
                option
                for option in options
                if normalize_option(option.get("name", ""))
                == normalize_option(target.label)
                and not option.get("created")
                and not option.get("disabled")
            ]
            if dom_matches:
                break
            await asyncio.sleep(0.1)

        if len(dom_matches) != 1 or dropdown is None:
            available = "、".join(
                "{0}[{1}]".format(option.get("name", ""), option.get("value", ""))
                for option in last_options
            )
            raise JdFormListingError(
                "京东属性{0}没有唯一的同名页面候选：期望 {1}；页面候选 {2}".format(
                    field_label,
                    target.label,
                    available or "无",
                )
            )
        option = dropdown.locator(".el-select-dropdown__item").nth(
            int(dom_matches[0]["index"])
        )
        try:
            await option.click(timeout=2_000)
        except Exception as exc:
            raise JdFormListingError(
                "京东属性{0}候选点击失败：{1}[{2}]".format(
                    field_label,
                    target.label, target.value_id
                )
            ) from exc

        deadline = asyncio.get_running_loop().time() + 2
        last_labels: Tuple[str, ...] = ()
        last_ids: Tuple[str, ...] = ()
        while asyncio.get_running_loop().time() < deadline:
            last_labels = await self._read_select_values(select, multi=False)
            last_ids = await selected_ids()
            if (
                len(last_labels) == 1
                and normalize_option(last_labels[0]) == normalize_option(target.label)
                and any(re.fullmatch(r"\d+", value) for value in last_ids)
            ):
                await self._dismiss_select_dropdown(select)
                if self.logger is not None:
                    self.logger.info(
                        "京东属性%s已按名称选择并绑定数字值：%s[%s]",
                        field_label,
                        target.label,
                        "/".join(
                            value
                            for value in last_ids
                            if re.fullmatch(r"\d+", value)
                        ),
                    )
                return last_labels
            await asyncio.sleep(0.05)
        raise JdFormListingError(
            "京东属性{0}数字值回读失败：期望名称 {1}，页面文字={2}，底层值={3}".format(
                field_label,
                target.label,
                last_labels,
                last_ids,
            )
        )

    async def _bound_select_values(self, select: Any) -> Tuple[str, ...]:
        """Read the Element UI select's underlying bound values."""
        raw_ids = await select.evaluate(
            """element => {
              const component = element.__vue__;
              if (!component) return [];
              const result = [];
              const append = value => {
                if (Array.isArray(value)) {
                  value.forEach(append);
                  return;
                }
                if (value === undefined || value === null || value === '') return;
                if (typeof value === 'object') {
                  append(value.value ?? value.id ?? value.valueId);
                  return;
                }
                result.push(String(value));
              };
              append(component.value);
              append(component.selected && component.selected.value);
              return [...new Set(result)];
            }"""
        )
        return tuple(
            str(value).strip() for value in raw_ids if str(value).strip()
        )

    async def _clear_non_numeric_select_value(
        self, select: Any, page_label: str
    ) -> None:
        """Drop a self-typed text binding so JD publish gets a numeric valueId.

        快麦把表单模型里的值原样传给京东铺货 API；历史运行直填过的自造
        文字（created 选项）会让 API 报“属性值需是数字格式”。仅当底层
        绑定确有非数字值时才清空，真实数字绑定与空值保持不动。
        """
        values = await self._bound_select_values(select)
        if not values or all(re.fullmatch(r"\d+", value) for value in values):
            return
        cleared = await select.evaluate(
            """async element => {
              const component = element.__vue__;
              if (!component) return false;
              if (typeof component.deleteSelected === 'function') {
                component.deleteSelected({stopPropagation() {}});
              } else if (typeof component.$emit === 'function') {
                component.value = component.multiple ? [] : '';
                component.selected = component.multiple ? [] : {};
                component.selectedLabel = '';
                component.$emit('input', component.value);
                component.$emit('change', component.value);
                component.$emit('clear');
              } else {
                return false;
              }
              if (typeof component.$nextTick === 'function') {
                await new Promise(resolve => component.$nextTick(resolve));
              }
              return true;
            }"""
        )
        if not cleared:
            raise JdFormListingError(
                "京东属性“{0}”无法清除自造文字值".format(page_label)
            )
        if self.logger is not None:
            self.logger.info(
                "京东属性“%s”底层是自造文字值（铺货 API 要求数字 valueId），已清空待人工审核",
                page_label,
            )

    async def _interface_field_has_options(self, page_label: str) -> bool:
        """True when the captured JD schema defines valueId candidates."""
        try:
            field = await self._captured_api_field_definition(page_label)
        except JdFormListingError:
            return False
        return bool(field.option_values)

    async def _resolve_learning_select_value(
        self,
        page_label: str,
        select: Any,
        desired: str,
    ) -> Optional[str]:
        runtime = getattr(self, "attribute_runtime", None)
        if runtime is None:
            return desired
        aliases = value_candidates(page_label, desired)
        managed = is_learning_managed_field("jd", page_label)
        try:
            field, category_id = await self._captured_api_field(page_label)
        except JdFormListingError as exc:
            if self.logger is not None:
                self.logger.info(
                    "京东属性“%s”：接口字段未定位，改用当前 DOM 匹配/直接输入与回读：%s",
                    page_label,
                    exc,
                )
            return desired
        if not field.source_id:
            if self.logger is not None:
                self.logger.info(
                    "京东属性“%s”：接口字段 ID 为空，改用当前 DOM 匹配与回读",
                    page_label,
                )
            return desired
        multi = await select.locator(".el-select__tags").count() > 0
        current = tuple(
            value
            for value in await self._read_select_values(select, multi=multi)
            if normalize_option(value)
        )
        current_equivalent = (
            current[0]
            if not multi
            and len(current) == 1
            and any(
                normalize_option(current[0]) == normalize_option(alias)
                for alias in aliases
            )
            else None
        )
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
            if not managed:
                return desired
            raise JdFormListingError(
                "京东属性“{0}”接口候选与页面候选不一致：{1}".format(
                    page_label, exc.reason_code
                )
            ) from exc
        schema_version = canonical_sha256(
            {
                "platform_id": "jd",
                "category_leaf_id": category_id,
                "field_id": str(field.source_id),
                "options": [
                    {"value_id": value.value_id, "label": value.label}
                    for value in candidates
                ],
            }
        )
        # Every live field with a known schema can be reviewed, including
        # fields newly introduced by another category. Preserve Excel OR order.
        exact = (
            None
            if multi and re.search(r"[,，、;；]", str(desired))
            else preferred_exact_candidate_label(
                tuple(value.label for value in candidates),
                aliases,
            )
        )
        if current_equivalent is not None:
            current_candidate = preferred_exact_candidate_label(
                tuple(value.label for value in candidates),
                (current_equivalent,),
            )
            if current_candidate is not None:
                exact = current_candidate
                if self.logger is not None:
                    self.logger.info(
                        "京东属性“%s”页面已是 Excel 等价候选 %s，保持当前值",
                        page_label,
                        current_candidate,
                    )
        request_candidates = tuple(
            CandidateValue(value.value_id, value.label) for value in candidates
        )
        reuse = getattr(runtime, "reusable_choice", None)
        reusable = reuse(AttributeRequest(
            platform_id="jd", category_leaf_id=category_id,
            field_id=str(field.source_id), field_label=page_label,
            candidates=request_candidates, excel_value=str(desired).strip(),
            evidence={}, custom_allowed=True, schema_version=schema_version,
            control_type="multi_select" if multi else "select",
        )) if callable(reuse) else None
        if (
            exact is None
            and str(desired).strip()
            and reusable is None
            and not field.option_values
        ):
            # Exhaust the Excel input route before requesting human review.
            # 京东类目属性提交铺货时必须是平台数字 valueId；接口已给出候选
            # 列表的字段禁止直填自造文字，否则铺货 API 报
            # “属性[...]的值需是数字格式”。直填仅保留给无候选的自由文本字段。
            direct = re.split(r"[/／]", desired)[0].strip()
            try:
                actual = await self._set_select_values_directly(
                    select, (direct,), label=page_label, multi=multi
                )
                if actual == (direct,):
                    exact = direct
                    request_candidates += (CandidateValue(direct, direct),)
            except (YouzanFormListingError, TaobaoListingError, JdFormListingError):
                if self.logger is not None:
                    self.logger.info("京东属性%s直接输入未通过回读，加入审核汇总", page_label)
        resolved = await runtime.resolve(
            AttributeRequest(
                platform_id="jd",
                category_leaf_id=category_id,
                field_id=str(field.source_id),
                field_label=page_label,
                candidates=request_candidates,
                excel_value=exact if exact is not None else str(desired).strip(),
                evidence={"excel": bool(str(desired).strip())},
                # Operator input is permitted; the writer still verifies it.
                custom_allowed=True,
                schema_version=schema_version,
                control_type="multi_select" if multi else "select",
            )
        )
        if resolved is None:
            return None
        return resolved.label

    async def _raise_as_jd(self, awaitable: Any) -> Any:
        try:
            return await awaitable
        except (YouzanFormListingError, TaobaoListingError) as exc:
            raise JdFormListingError(
                str(exc).replace("有赞", "京东").replace("淘宝", "京东")
            ) from exc

    async def _open_cascader(self, cascader: Any) -> None:
        """打开级联选择器下拉菜单"""
        input_box = cascader.locator("input.el-input__inner").first
        if not await input_box.count():
            raise JdFormListingError("级联选择器中找不到可点击输入框")
        await input_box.click(timeout=4000)

    async def _visible_cascader_options(
        self, cascader: Any, timeout_seconds: float = 5
    ) -> List[Mapping[str, Any]]:
        """获取当前可见的级联选择器选项"""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            menus = self.page.locator(".el-cascader-menu:visible")
            if await menus.count() == 0:
                await asyncio.sleep(0.05)
                continue

            # 获取最后一个可见菜单（当前活动的菜单）
            last_menu = menus.last
            result = await last_menu.evaluate(
                """element => Array.from(
                    element.querySelectorAll('.el-cascader-node')
                  ).map((node, index) => ({
                    name: (node.querySelector('.el-cascader-node__label') || {}).textContent || '',
                    index: String(index),
                    hasChildren: !!node.querySelector('.el-icon-arrow-right'),
                    isActive: node.classList.contains('is-active'),
                    disabled: node.classList.contains('is-disabled')
                  })).filter(item => item.name.trim() && !item.disabled)
                """
            )
            if result:
                return result
            await asyncio.sleep(0.05)
        raise JdFormListingError("打开京东属性下拉框后未找到可见选项")

    @staticmethod
    def _match_cascader_option(
        options: Sequence[Mapping[str, Any]], candidates: Sequence[str]
    ) -> Optional[Mapping[str, Any]]:
        for candidate in candidates:
            matches = [
                option
                for option in options
                if normalize_option(option["name"]) == normalize_option(candidate)
            ]
            if len(matches) == 1:
                return matches[0]
        return None

    async def _click_cascader_option(self, option: Mapping[str, Any]) -> None:
        menus = self.page.locator(".el-cascader-menu:visible")
        if await menus.count() == 0:
            raise JdFormListingError("京东属性级联菜单已关闭")
        last_menu = menus.last
        before_menus = await menus.count()
        nodes = last_menu.locator(".el-cascader-node")
        await nodes.nth(int(option["index"])).click(timeout=2000)
        if not option.get("hasChildren"):
            return
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            if await self.page.locator(".el-cascader-menu:visible").count() > before_menus:
                return
            await asyncio.sleep(0.05)

    async def _select_cascader_values(
        self,
        cascader: Any,
        expected_groups: Sequence[Sequence[str]],
        label: str,
    ) -> Optional[Tuple[str, ...]]:
        """Select a cascader path. Comma groups are levels; leftover OR values
        from the same group are tried on the next menu so a parent like 休闲风
        can still reach a leaf such as 时尚都市."""
        groups = [tuple(group) for group in expected_groups if group]
        if not groups:
            return None
        await cascader.scroll_into_view_if_needed()
        await self._open_cascader(cascader)
        chosen_values: List[str] = []
        leftover: Tuple[str, ...] = ()
        group_index = 0
        while True:
            options = await self._visible_cascader_options(cascader)
            if group_index < len(groups):
                raw_candidates = groups[group_index]
            elif leftover:
                raw_candidates = leftover
            elif not chosen_values:
                return None
            else:
                raise JdFormListingError(
                    "京东属性{0}未选到叶子项：已选 {1}".format(
                        label, " > ".join(chosen_values)
                    )
                )
            sources = raw_candidates + (
                (chosen_values[-1],) if chosen_values else ()
            )
            candidates = _expand_cascader_candidates(label, sources)
            chosen = self._match_cascader_option(options, candidates)
            if chosen is None:
                if not chosen_values:
                    return None
                names = "、".join(
                    option["name"].strip() for option in options
                ) or "无"
                raise JdFormListingError(
                    "京东属性{0}第{1}级没有 Excel 精确候选；当前候选：{2}".format(
                        label, len(chosen_values) + 1, names
                    )
                )
            await self._click_cascader_option(chosen)
            chosen_name = chosen["name"].strip()
            chosen_values.append(chosen_name)
            leftover = tuple(
                candidate
                for candidate in candidates
                if normalize_option(candidate) != normalize_option(chosen_name)
            )
            if group_index < len(groups):
                group_index += 1
            if not chosen.get("hasChildren"):
                break
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        if self.logger is not None:
            self.logger.info(
                "京东属性%s级联已选：%s", label, " > ".join(chosen_values)
            )
        return tuple(chosen_values)

    async def open(self) -> "JdFormListing":
        self._start_api_capture()
        tab = self.drawer.get_by_role("tab", name="京东资料", exact=True)
        try:
            await tab.wait_for(state="visible", timeout=30_000)
            if (await tab.get_attribute("aria-selected") or "").casefold() != "true":
                await tab.click(timeout=30_000)
            panel = self.drawer.get_by_role("tabpanel", name="京东资料", exact=True)
            await panel.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise JdFormListingError("找不到可切换的京东资料页签") from exc
        self.panel = panel
        await self._raise_as_jd(super()._wait_for_loading_masks())
        if self.logger is not None:
            self.logger.info('京东资料页签已打开')
        return self

    async def _category_text(self) -> str:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        category = self.panel.locator(".platform-category-input").first
        if await category.count() and await category.is_visible():
            text = re.sub(r"\s+", " ", (await category.inner_text()).strip())
            text = re.sub(r"^商品分类\s*[：:]?\s*", "", text)
            text = re.split(r"\s*(?:修改类目|同步平台类目)\s*", text, maxsplit=1)[0].strip()
            if text:
                return text
        values = await self.panel.evaluate(
            """root => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const result = [];
              for (const element of root.querySelectorAll('*')) {
                const style = getComputedStyle(element);
                if (!element.getClientRects().length || style.display === 'none'
                    || style.visibility === 'hidden') continue;
                const text = clean(element.innerText);
                const match = text.match(/^商品分类\s*[：:]\s*(.*?)(?:\s*修改类目|\s*同步平台类目|$)/);
                if (match && match[1]) result.push(clean(match[1]));
              }
              return [...new Set(result)];
            }"""
        )
        if len(values) != 1:
            raise JdFormListingError("京东页面已选类目不是唯一项：{0}".format(len(values)))
        return str(values[0])

    @staticmethod
    def _json_category_nodes(payload: Any, target: str) -> Tuple[Mapping[str, Any], ...]:
        """Return the API objects that identify the exact target category."""
        wanted = normalize_label(target)
        matches: List[Mapping[str, Any]] = []

        def walk(value: Any, path: Tuple[str, ...]) -> None:
            if isinstance(value, Mapping):
                exact = any(
                    isinstance(child, str) and normalize_label(child) == wanted
                    for child in value.values()
                )
                if exact:
                    record: Dict[str, Any] = {"json_path": "/".join(path)}
                    for key, child in value.items():
                        if child is None or isinstance(child, (str, int, float, bool)):
                            record[str(key)] = child
                    matches.append(record)
                for key, child in value.items():
                    walk(child, path + (str(key),))
                return
            if isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    walk(child, path + (str(index),))
                return
            if isinstance(value, str):
                text = value.strip()
                if text.startswith(("{", "[")):
                    try:
                        walk(json.loads(text), path + ("json-string",))
                    except (TypeError, ValueError):
                        return

        walk(payload, ())
        deduplicated: Dict[str, Mapping[str, Any]] = {}
        for match in matches:
            key = json.dumps(match, ensure_ascii=False, sort_keys=True, default=str)
            deduplicated[key] = match
        return tuple(deduplicated.values())

    @staticmethod
    def _json_contains_category(payload: Any, target: str) -> bool:
        return bool(JdFormListing._json_category_nodes(payload, target))

    async def _category_dom_candidates(self, dialog: Any, target: str) -> List[Any]:
        """Find the unique category leaf or the search-result breadcrumb.

        On a cold render FastMai initially exposes the search result as one
        combined breadcrumb text node (for example ``... > 男士休闲直筒裤``),
        so an exact-text locator can temporarily return zero even though the
        category API and the visible result already agree.
        """
        wanted = normalize_label(target)

        async def from_root(root: Any) -> List[Any]:
            exact = await self._innermost_visible_text(root, target)
            if exact:
                return exact
            nodes = root.get_by_text(re.compile(re.escape(target)))
            matches: List[Any] = []
            for index in range(await nodes.count()):
                node = nodes.nth(index)
                if not await node.is_visible():
                    continue
                text = normalize_label(await node.inner_text())
                if not text.endswith(wanted):
                    continue
                nested = node.locator(":scope *").get_by_text(
                    re.compile(re.escape(target))
                )
                has_visible_child = False
                for child_index in range(await nested.count()):
                    if await nested.nth(child_index).is_visible():
                        has_visible_child = True
                        break
                if has_visible_child:
                    continue
                matches.append(node)
            return matches

        # Element UI teleports the remote-search suggestion under <body>,
        # outside the visible dialog. Prefer the dialog's category tree, then
        # fall back to the page-level suggestion popover.
        inside = await from_root(dialog)
        return inside or await from_root(self.page)

    async def apply_category(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        category_hints, target = jd_category_target(fields)
        try:
            current = await self._category_text()
        except JdFormListingError:
            current = ""
        if _category_parts(current)[-1:] == (normalize_label(target),):
            return {
                "hints": category_hints,
                "target": target,
                "selected": current,
                "changed": False,
            }
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        modify = self.panel.get_by_role("button", name="修改类目", exact=True)
        if await modify.count() != 1:
            raise JdFormListingError("京东修改类目按钮不是唯一项")
        await modify.click()
        dialog = self.page.get_by_role("dialog", name="修改类目", exact=True)
        try:
            await dialog.wait_for(state="visible", timeout=10_000)
        except Exception as exc:
            raise JdFormListingError("京东修改类目弹窗未出现") from exc
        search = dialog.get_by_placeholder("请输入类目关键词，支持模糊查询", exact=True)
        if await search.count() != 1:
            inputs = dialog.locator('input:not([type="hidden"]):not([readonly]):visible')
            if await inputs.count() != 1:
                raise JdFormListingError("京东修改类目弹窗缺少唯一搜索框")
            search = inputs.first

        api_matches: List[str] = []
        api_candidates: List[Mapping[str, Any]] = []
        tasks: List[asyncio.Task[Any]] = []

        async def capture(response: Any) -> None:
            try:
                headers = await response.all_headers()
                if "json" not in str(headers.get("content-type") or "").casefold():
                    return
                payload = await response.json()
                nodes = self._json_category_nodes(payload, target)
                if nodes:
                    path = urlsplit(response.url).path
                    if path not in api_matches:
                        api_matches.append(path)
                    for node in nodes:
                        record = {"response_path": path, **dict(node)}
                        if record not in api_candidates:
                            api_candidates.append(record)
            except Exception:
                return

        def handler(response: Any) -> None:
            tasks.append(asyncio.create_task(capture(response)))

        self.page.on("response", handler)
        await search.fill(target)
        deadline = asyncio.get_running_loop().time() + 30
        candidates: List[Any] = []
        stable_since: Optional[float] = None
        try:
            while asyncio.get_running_loop().time() < deadline:
                candidates = await self._category_dom_candidates(dialog, target)
                if api_matches and len(candidates) == 1:
                    if stable_since is None:
                        stable_since = asyncio.get_running_loop().time()
                    elif asyncio.get_running_loop().time() - stable_since >= 1.0:
                        break
                else:
                    stable_since = None
                await asyncio.sleep(0.1)
        finally:
            self.page.remove_listener("response", handler)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        if not api_matches or len(candidates) != 1:
            raise JdFormListingError(
                "京东类目接口与 DOM 交叉校验失败：接口 {0}，DOM {1}".format(
                    len(api_matches), len(candidates)
                )
            )
        if self.logger is not None:
            self.logger.info('京东类目 JSON 节点：%s', api_candidates)
        # 快麦页面右下角助手浮层会间歇性遮住类目候选。
        # JSON 接口已确认该候选存在，因此直接触发这个唯一 DOM
        # 节点，避免 Playwright 因非业务浮层持续等待。
        loading = self.page.locator(".el-loading-mask:visible")
        mask_deadline = asyncio.get_running_loop().time() + 15
        while await loading.count() and asyncio.get_running_loop().time() < mask_deadline:
            await asyncio.sleep(0.1)
        if await loading.count():
            raise JdFormListingError("京东类目搜索加载遮罩在 15 秒内未消失")
        if self.logger is not None:
            structure = await candidates[0].evaluate(
                """node => {
                  const result = [];
                  let current = node;
                  for (let index = 0; current && index < 5; index += 1) {
                    result.push({
                      tag: current.tagName,
                      className: String(current.className || ''),
                      role: current.getAttribute && current.getAttribute('role'),
                      html: String(current.outerHTML || '').slice(0, 600)
                    });
                    current = current.parentElement;
                  }
                  return result;
                }"""
            )
            self.logger.info('京东目标类目 DOM 结构：%s', structure)
        async def selection_state() -> Mapping[str, bool]:
            return await dialog.evaluate(
                """(root, expected) => {
                  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
                  const row = Array.from(root.querySelectorAll('.el-cascader-node'))
                    .find(node => clean(node.innerText) === expected);
                  const rowSelected = Boolean(row) && (
                    row.classList.contains('is-active') ||
                    Boolean(row.querySelector('.el-icon-check, input:checked, [aria-checked="true"]'))
                  );
                  const footerSelected = Array.from(root.querySelectorAll('*')).some(node => {
                    const text = clean(node.innerText);
                    return /^\s*已选\s*[：:]/.test(text) && text.includes(expected);
                  });
                  return {rowSelected, footerSelected};
                }""",
                target,
            )

        selected = False
        for attempt in range(5):
            # 等待 DOM 稳定
            await asyncio.sleep(0.3)
            live_candidates = await self._category_dom_candidates(dialog, target)
            if len(live_candidates) == 0:
                if self.logger is not None:
                    self.logger.warning(
                        "京东目标类目第 %s 次尝试时 DOM 节点不可见，等待重新渲染",
                        attempt + 1
                    )
                await asyncio.sleep(0.5)
                continue
            if len(live_candidates) != 1:
                if self.logger is not None:
                    self.logger.warning(
                        "京东目标类目候选数量异常：%s 个，尝试 %s/5",
                        len(live_candidates), attempt + 1
                    )
                if attempt < 4:
                    await asyncio.sleep(0.5)
                    continue
                raise JdFormListingError(
                    "京东目标类目重新渲染后不是唯一项：{0}".format(
                        len(live_candidates)
                    )
                )
            row = live_candidates[0].locator(
                "xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), ' el-cascader-node ')][1]"
            )
            if not await row.count():
                row = live_candidates[0]
            if attempt == 0 or attempt == 1:
                await row.click(force=True, timeout=5_000)
            elif attempt == 2:
                await row.evaluate(
                    """node => {
                      node.focus();
                      for (const type of ['pointerdown', 'mousedown', 'mouseup', 'click']) {
                        node.dispatchEvent(new MouseEvent(type, {
                          bubbles: true, cancelable: true, view: window
                        }));
                      }
                    }"""
                )
            else:
                invoked = await row.evaluate(
                    """node => {
                      const vm = node.__vue__;
                      if (!vm || typeof vm.handleClick !== 'function') return false;
                      vm.handleClick();
                      return true;
                    }"""
                )
                if not invoked:
                    await row.focus()
                    await row.press("Enter")
            selected_deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < selected_deadline:
                state = await selection_state()
                if state.get("rowSelected") and state.get("footerSelected"):
                    selected = True
                    break
                await asyncio.sleep(0.1)
            if selected:
                break
            if self.logger is not None:
                self.logger.warning('京东类目第 %s 次点击未选中，继续重试', attempt + 1)
            await asyncio.sleep(0.5)
        if not selected:
            raise JdFormListingError("京东类目 DOM 点击后未同时出现勾选与已选路径")
        confirm = dialog.get_by_role("button", name=re.compile(r"^\s*确\s*定\s*$"))
        if await confirm.count() != 1:
            raise JdFormListingError("京东修改类目弹窗确定按钮不是唯一项")
        await confirm.click()
        await dialog.wait_for(state="hidden", timeout=20_000)
        await self._raise_as_jd(super()._wait_for_loading_masks())
        actual = await self._category_text()
        if _category_parts(actual)[-1:] != (normalize_label(target),):
            raise JdFormListingError(
                "京东类目确认后回读失败：页面为 {0!r}".format(actual)
            )
        self.category_clicked = True
        if self.logger is not None:
            self.logger.info(
                "京东类目已通过 JSON 接口与 DOM 交叉校验并选中：%s",
                actual,
            )
        return {
            "hints": category_hints,
            "target": target,
            "selected": actual,
            "changed": True,
            "api_paths": tuple(api_matches),
            "api_candidates": tuple(api_candidates),
        }

    async def _form_item_exact(self, label: str, *, occurrence: Optional[int] = None) -> Any:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        labels = self.panel.get_by_text(
            re.compile(r"^\s*\*?\s*{0}\s*[：:]?\s*$".format(re.escape(label)))
        )
        matches = []
        for index in range(await labels.count()):
            node = labels.nth(index)
            if not await node.is_visible():
                continue
            item = node.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-form-item ')][1]")
            if await item.count() == 1 and item not in matches:
                matches.append(item)
        if occurrence is not None:
            if occurrence < 1 or occurrence > len(matches):
                raise JdFormListingError(
                    "京东字段{0}第 {1} 个表单项不存在（共 {2} 个）".format(
                        label, occurrence, len(matches)
                    )
                )
            return matches[occurrence - 1]
        if len(matches) != 1:
            raise JdFormListingError("京东字段{0}表单项不是唯一项：{1}".format(label, len(matches)))
        return matches[0]

    @staticmethod
    async def _editable_inputs(item: Any) -> List[Any]:
        nodes = item.locator('input:not([type="hidden"]):not([readonly]):visible')
        result = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if not await node.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-select ')]"
            ).count():
                result.append(node)
        return result

    @staticmethod
    async def _enter_as_user(input_box: Any, expected: str) -> None:
        await input_box.click()
        await input_box.fill("")
        await input_box.press_sequentially(expected)
        await input_box.press("Tab")

    async def _read_brand_value(
        self,
        brand_item: Optional[Any] = None,
    ) -> Tuple[str, Tuple[str, ...]]:
        """Read both first-open editable and persisted readonly brand controls."""
        if brand_item is None:
            brand_item = await self._form_item_exact("品牌")
        inputs = brand_item.locator('input:not([type="hidden"]):visible')
        collected: List[str] = []
        for index in range(await inputs.count()):
            value = (await inputs.nth(index).input_value()).strip()
            if value and value not in collected:
                collected.append(value)
        values = tuple(collected)
        brand = next(
            (
                value
                for value in values
                if normalize_option(value) == normalize_option(JD_BRAND)
            ),
            "",
        )
        return brand, values

    async def _fill_input_item(
        self, label: str, expected: str, *, numeric: bool = False
    ) -> str:
        item = await self._form_item_exact(label)
        inputs = await self._editable_inputs(item)
        if len(inputs) != 1:
            raise JdFormListingError("京东字段{0}输入框不是唯一项：{1}".format(label, len(inputs)))
        input_box = inputs[0]
        before = (await input_box.input_value()).strip()
        matches = _numeric_equal(before, expected) if numeric else before == expected
        if not matches:
            await self._enter_as_user(input_box, expected)
        actual = (await input_box.input_value()).strip()
        matches = _numeric_equal(actual, expected) if numeric else actual == expected
        if not matches:
            raise JdFormListingError("京东字段{0}回读失败：{1!r}".format(label, actual))
        return actual

    async def _select_exact_brand(self, brand_item: Any) -> Tuple[str, Tuple[str, ...]]:
        """Select JD's fixed brand from the current visible dropdown.

        JD's brand control can leave a stale value visible while a category
        change is still rendering.  Its option click also must be bounded: a
        generic in-page ``element.click()`` has been observed to wait forever
        on the live form.  Use Playwright's actionable click with a hard timeout
        and verify the control value immediately afterwards.
        """
        select = brand_item.locator(".el-select").first
        if not await select.count():
            raise JdFormListingError("京东品牌缺少下拉选择器")
        await self._open_select(select, multi=False)
        dropdown, options = await self._visible_dom_options(select)
        matches = [
            option
            for option in options
            if normalize_option(option.get("name", "")) == normalize_option(JD_BRAND)
        ]
        if len(matches) != 1:
            raise JdFormListingError(
                "京东品牌候选 NEIGBORL 不是唯一项：{0}".format(len(matches))
            )
        option = dropdown.locator(".el-select-dropdown__item").nth(
            int(matches[0]["index"])
        )
        try:
            await asyncio.wait_for(option.click(timeout=2_000), timeout=3.0)
        except Exception as exc:
            raise JdFormListingError("京东品牌候选 NEIGBORL 点击超时") from exc
        brand, values = await self._read_brand_value(brand_item)
        if normalize_option(brand) != normalize_option(JD_BRAND):
            raise JdFormListingError(
                "京东品牌选择后回读失败：{0!r}".format(values)
            )
        return brand, values

    async def fill_identity_and_parameters(
        self, fields: Mapping[str, str], *, style_code: str
    ) -> Mapping[str, str]:
        brand_item = await self._form_item_exact("品牌")
        brand, brand_values = await self._read_brand_value(brand_item)
        if not brand:
            if self.logger is not None:
                self.logger.info('京东品牌使用可见候选精确选择：%s', JD_BRAND)
            try:
                brand, brand_values = await self._select_exact_brand(brand_item)
            except JdFormListingError as exc:
                if self.logger is not None:
                    self.logger.warning('京东品牌下拉选择失败：%s', exc)

            # 如果下拉选择失败，尝试手动填写
            if not brand:
                inputs = await self._editable_inputs(brand_item)
                if len(inputs) >= 1:
                    if self.logger is not None:
                        self.logger.info('京东品牌下拉失败，尝试手动填写：%s', JD_BRAND)
                    await self._enter_as_user(inputs[0], JD_BRAND)
                    await asyncio.sleep(0.5)
                    brand, brand_values = await self._read_brand_value(brand_item)

            # 如果手动填写也失败，尝试一键应用
            if not brand:
                apply_brand = self.panel.get_by_role(
                    "button", name="一键应用品牌配置", exact=True
                )
                if await apply_brand.count() == 1:
                    if self.logger is not None:
                        self.logger.info('京东品牌为空，点击一键应用品牌配置')
                    await apply_brand.click(force=True, timeout=5_000)
                    deadline = asyncio.get_running_loop().time() + 15
                    while asyncio.get_running_loop().time() < deadline:
                        await self._raise_as_jd(super()._wait_for_loading_masks())
                        brand, brand_values = await self._read_brand_value(brand_item)
                        if brand:
                            break
                        await asyncio.sleep(0.1)
                elif self.logger is not None:
                    self.logger.warning(
                        "京东品牌为空，但一键应用品牌配置按钮数量异常：%s",
                        await apply_brand.count()
                    )

        if normalize_option(brand) != normalize_option(JD_BRAND):
            # 如果还是不匹配，记录详细信息
            if self.logger is not None:
                self.logger.error(
                    "京东品牌配置失败：期望=%s, 实际=%s, 所有值=%s",
                    JD_BRAND, brand, brand_values
                )
            raise JdFormListingError(
                "京东品牌配置失败：期望 {0!r}，实际 {1!r}".format(JD_BRAND, brand_values)
            )

        origin = _required_excel_value(fields, ("产地",), "产地")
        result = {
            "品牌": brand,
            "货号": await self._fill_input_item("货号", style_code),
            "商品毛重(公斤)": await self._fill_input_item(
                "商品毛重(公斤)", JD_GROSS_WEIGHT, numeric=True
            ),
        }
        # 京东页中"商品参数"和"商品属性"各有一个产地；
        # 这里明确取上方商品参数项；下方商品属性中的产地由
        # fill_attributes 主动保持为空，不复用 Excel 产地。
        origin_item = await self._form_item_exact("产地", occurrence=1)
        actual = await self._raise_as_jd(
            self._fill_attribute("产地", origin_item, origin, required=True)
        )
        if actual is None:
            raise JdFormListingError("京东产地没有可用的 Excel 精确候选")
        result["产地"] = actual[0]
        return result

    async def _attribute_bounds(self) -> Tuple[Optional[float], Optional[float]]:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        start: Optional[float] = None
        end: Optional[float] = None
        headings = self.panel.get_by_text(
            re.compile(r"^\s*(?:商品属性|销售属性|商品规格|规格明细|商品图片|物流信息|售后服务)\s*[：:]?\s*$")
        )
        for index in range(await headings.count()):
            heading = headings.nth(index)
            if not await heading.is_visible():
                continue
            box = await heading.bounding_box()
            if box is None:
                continue
            label = normalize_label(await heading.inner_text())
            if label == normalize_label("商品属性"):
                start = box["y"]
            elif start is not None and box["y"] > start:
                end = box["y"] if end is None else min(end, box["y"])
        return start, end

    async def _collect_attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        result: Dict[str, Tuple[str, Any]] = {}
        counts: Dict[str, int] = {}
        items = self.panel.locator(".el-form-item")
        # JD can render dozens of form items.  Reading visibility, bounds and
        # labels with separate Playwright calls made one scan cost several
        # seconds.  Collect the same metadata in one browser-side pass, then
        # keep using locators for the actual writes and readbacks.
        metadata = await self.panel.evaluate(
            """panel => {
                const visible = node => {
                    if (!(node instanceof Element)) return false;
                    const style = window.getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden' ||
                        style.visibility === 'collapse') return false;
                    const rect = node.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const normalized = value => String(value || '')
                    .replace(/\s+/g, '')
                    .replace(/[\uff1a:]$/, '');
                const sectionNames = new Set([
                    '\u5546\u54c1\u5c5e\u6027', '\u9500\u552e\u5c5e\u6027', '\u5546\u54c1\u89c4\u683c', '\u89c4\u683c\u660e\u7ec6',
                    '\u5546\u54c1\u56fe\u7247', '\u7269\u6d41\u4fe1\u606f', '\u552e\u540e\u670d\u52a1'
                ]);
                let top = null;
                let end = null;
                for (const node of panel.querySelectorAll('*')) {
                    if (!visible(node)) continue;
                    const name = normalized(node.innerText);
                    if (!sectionNames.has(name)) continue;
                    const y = node.getBoundingClientRect().y;
                    if (name === '\u5546\u54c1\u5c5e\u6027') {
                        top = y;
                        end = null;
                    } else if (top !== null && y > top) {
                        end = end === null ? y : Math.min(end, y);
                    }
                }
                const rows = [];
                const formItems = Array.from(panel.querySelectorAll('.el-form-item'));
                formItems.forEach((item, index) => {
                    if (!visible(item)) return;
                    const y = item.getBoundingClientRect().y;
                    if (top !== null && y <= top) return;
                    if (end !== null && y >= end) return;
                    const label = Array.from(item.children).find(
                        child => child.classList.contains('el-form-item__label') && visible(child)
                    );
                    if (!label) return;
                    rows.push({index, label: String(label.innerText || '').trim()});
                });
                return rows;
            }"""
        )
        for entry in metadata:
            index = int(entry["index"])
            item = items.nth(index)
            label = re.sub(r"^\s*\*\s*", "", str(entry.get("label", "")).strip())
            normalized = normalize_label(label)
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
            key = normalized if counts[normalized] == 1 else "{0}#{1}".format(normalized, counts[normalized])
            result[key] = (label, item)
        return result

    async def _attribute_items(self) -> Dict[str, Tuple[str, Any]]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.attribute_wait_timeout_seconds
        previous: Optional[Tuple[str, ...]] = None
        stable_since: Optional[float] = None
        while loop.time() < deadline:
            items = await self._collect_attribute_items()
            signature = tuple(items)
            now = loop.time()
            if signature:
                if signature != previous:
                    previous = signature
                    stable_since = now
                elif stable_since is not None and now - stable_since >= self.attribute_stable_seconds:
                    if self.logger is not None:
                        self.logger.info('京东商品属性已稳定渲染：%s 项', len(items))
                    return items
            await asyncio.sleep(0.25)
        raise JdFormListingError("京东商品属性在 30 秒内未稳定渲染")

    async def _attribute_assignments(
        self,
        fields: Mapping[str, str],
        page_items: Mapping[str, Tuple[str, Any]],
    ) -> Dict[str, Tuple[str, str]]:
        sources: Dict[str, List[Tuple[str, str]]] = {}
        page_items = without_color_attributes(page_items)
        for excel_key, raw_value in fields.items():
            excel_names = set(excel_aliases(excel_key))
            for page_key, (page_label, _item) in page_items.items():
                base = page_key.split("#", 1)[0]
                if (
                    base in JD_CLEARED_ATTRIBUTE_LABELS
                    or base in JD_IGNORED_ATTRIBUTE_LABELS
                ):
                    continue
                accepted = {base}
                accepted.update(JD_FIELD_ALIASES.get(base, ()))
                if excel_names.intersection(accepted):
                    sources.setdefault(page_key, []).append((str(excel_key), str(raw_value).strip()))
        assignments: Dict[str, Tuple[str, str]] = {}
        for page_key, (page_label, _item) in page_items.items():
            matches = sources.get(page_key, ())
            if not matches:
                continue
            values = {value for _key, value in matches}
            if len(values) != 1:
                raise JdFormListingError(
                    "京东属性{0}匹配到多个 Excel 值：{1}".format(
                        page_label, "、".join(key for key, _value in matches)
                    )
                )
            assignments[page_key] = (page_label, matches[0][1])
        return assignments

    @staticmethod
    async def _clear_attribute_item(page_label: str, item: Any) -> str:
        inputs = item.locator(
            'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):visible'
        )
        if await inputs.count() != 1:
            raise JdFormListingError(
                "京东属性{0}清空控件不是唯一项：{1}".format(
                    page_label, await inputs.count()
                )
            )
        input_box = inputs.first
        if not (await input_box.input_value()).strip():
            return ""
        if await input_box.get_attribute("readonly") is None:
            await input_box.click()
            await input_box.fill("")
            await input_box.press("Tab")
        else:
            clear_buttons = item.locator(
                ".el-select__clear:visible, .el-icon-circle-close:visible, "
                ".el-cascader__clearIcon:visible"
            )
            if await clear_buttons.count() != 1:
                raise JdFormListingError(
                    "京东属性{0}已有值但没有唯一清空入口".format(page_label)
                )
            await clear_buttons.first.click(force=True, timeout=2_000)
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if not (await input_box.input_value()).strip():
                return ""
            await asyncio.sleep(0.05)
        raise JdFormListingError("京东属性{0}清空后回读失败".format(page_label))

    @staticmethod
    async def _is_required(item: Any) -> bool:
        classes = set((await item.get_attribute("class") or "").split())
        label = item.locator(":scope > .el-form-item__label").first
        return "is-required" in classes or bool(
            await label.count() and re.match(r"^\s*\*", (await label.inner_text()).strip())
        )

    async def _fill_attribute_item(
        self, page_label: str, item: Any, expected: str, *, required: bool
    ) -> Optional[Tuple[str, ...]]:
        normalized = normalize_label(page_label)

        # 先检查是否是级联选择器
        cascader = item.locator(".el-cascader").first
        if await cascader.count():
            if self.logger is not None:
                self.logger.info('京东属性%s是级联选择器，尝试选择值，期望值=%s', page_label, expected)
            actual = await self._select_cascader_values(
                cascader,
                selection_value_groups(page_label, expected),
                label=page_label,
            )
            if actual is None:
                if required:
                    raise JdFormListingError("京东属性{0}没有 Excel 精确候选".format(page_label))
                return None
            return actual

        if normalized == normalize_label("材质"):
            components = _jd_material_components(expected)
            if components:
                actual = await self._fill_material_components(item, components)
                if actual is None and self.logger is not None:
                    self.logger.info(
                        "京东属性“材质”没有唯一数字候选，已加入本平台待审核汇总"
                    )
                return actual

        if normalized == normalize_label("面料"):
            selects = item.locator(":scope > .el-form-item__content .el-select:visible")
            if await selects.count() != 1:
                raise JdFormListingError("京东属性面料下拉框不是唯一项")
            select = selects.first
            fabric_names = tuple(
                "/".join(group) for group in material_name_groups(expected)
            )
            if not fabric_names:
                fabric_names = (str(expected).strip(),)

            async def write_jd_fabric(value: str, values: Sequence[str]) -> Optional[Tuple[str, ...]]:
                # 京东“面料”是单值数字下拉；多面料不能直接塞数组，
                # 交给公共方法继续尝试 Excel 原值和“其他”。
                if len(values) > 1:
                    raise JdFormListingError("京东属性面料为单值数字下拉，不能同时选择多个面料")
                target_name = str(values[0] if values else value).strip()
                return await self._select_numeric_material_option(
                    select,
                    _material_option_desired(target_name),
                    field_label=page_label,
                    review_unmatched=False,
                )

            try:
                return await self._fill_fabric_attribute(
                    page_label,
                    fabric_names,
                    item=item,
                    raw_value=expected,
                    writer=write_jd_fabric,
                )
            except (JdFormListingError, TaobaoListingError) as exc:
                raise JdFormListingError(
                    str(exc).replace("淘宝", "京东")
                ) from exc

        selects = item.locator(":scope > .el-form-item__content .el-select:visible")
        inputs = await self._editable_inputs(item)
        material = normalized in {normalize_label("面料"), normalize_label("材质")}
        if material and not await selects.count():
            add = item.get_by_role("button", name="添加", exact=True)
            if await add.count() == 1:
                await add.click(force=True, timeout=5_000)
                deadline = asyncio.get_running_loop().time() + 5
                while asyncio.get_running_loop().time() < deadline:
                    selects = item.locator(
                        ":scope > .el-form-item__content .el-select:visible"
                    )
                    inputs = await self._editable_inputs(item)
                    if await selects.count() and inputs:
                        break
                    await asyncio.sleep(0.1)
        if normalized == normalize_label("材质"):
            # 京东材质是“多行材质 + 含量”组件，不是一个多选下拉。
            # 按 Excel 中的每个成分逐行添加，确保棉94%、氨纶6%都落到页面。
            components = parse_material_components(expected)
            if len(components) > 1:
                add = item.get_by_role("button", name="添加", exact=True)
                while await selects.count() < len(components):
                    if await add.count() != 1:
                        raise JdFormListingError("京东材质缺少唯一“添加”按钮")
                    await add.click(force=True, timeout=5_000)
                    await asyncio.sleep(0.2)
                    selects = item.locator(":scope > .el-form-item__content .el-select:visible")
                inputs = await self._editable_inputs(item)
                if len(inputs) < len(components):
                    raise JdFormListingError("京东材质含量输入行数不足")
                actual_names = []
                for index, (name, percent) in enumerate(components):
                    selected = await self._select_numeric_material_option(
                        selects.nth(index), _material_option_desired(name)
                    )
                    if selected is None:
                        return None
                    actual_names.append(selected[0])
                    if percent is not None:
                        await self._enter_as_user(inputs[index], percent)
                        if not _numeric_equal((await inputs[index].input_value()).strip(), percent):
                            raise JdFormListingError("京东材质第{0}行含量回读失败".format(index + 1))
                return tuple(actual_names)
        desired = _material_name(expected) if material else expected
        desired = JD_EXACT_OPTION_MAP.get(normalized, {}).get(
            normalize_option(desired), desired
        )
        actual: Optional[Tuple[str, ...]] = None
        if await selects.count():
            # Material rows contain a value select plus an optional percentage input.
            value_select = selects.first
            multi = await value_select.locator(".el-select__tags").count() > 0
            if normalized == normalize_label("材质"):
                if multi:
                    raise JdFormListingError("京东属性材质不应为多选控件")
                actual = await self._select_numeric_material_option(
                    value_select,
                    desired,
                )
                if actual is None:
                    if self.logger is not None:
                        self.logger.info(
                            "京东属性“材质”没有唯一数字候选，已加入本平台待审核汇总"
                        )
                    return None
            else:
                desired = await self._resolve_learning_select_value(
                    page_label,
                    value_select,
                    desired,
                )
                if desired is None:
                    # 转审核前先清掉底层自造文字绑定（若有）：快麦保存的
                    # 就是表单模型值，历史直填的文字会让铺货 API 报
                    # “属性值需是数字格式”。
                    await self._clear_non_numeric_select_value(
                        value_select, page_label
                    )
                    if self.logger is not None:
                        self.logger.info(
                            "京东属性“%s”已加入本平台待审核汇总，继续填写后续字段",
                            page_label,
                        )
                    return None
                try:
                    actual = await self._raise_as_jd(
                        self._select_values(
                            value_select,
                            (tuple(part.strip() for part in re.split(r"[/／]", desired) if part.strip()),)
                            if material else selection_value_groups(page_label, desired),
                            label=page_label,
                            multi=multi,
                        )
                    )
                except JdFormListingError:
                    raise
                if actual is None:
                    if await self._interface_field_has_options(page_label):
                        # valueId 型字段不允许直填自造文字候选，否则铺货
                        # API 报“属性值需是数字格式”；清空后转人工审核。
                        await self._clear_non_numeric_select_value(
                            value_select, page_label
                        )
                        if getattr(self, "attribute_runtime", None) is None:
                            raise JdFormListingError(
                                "京东属性“{0}”没有可点选的数字候选，"
                                "且本次未连接审核服务，无法创建待审核项".format(
                                    page_label
                                )
                            )
                        if self.logger is not None:
                            self.logger.info(
                                "京东属性“%s”没有可点选的数字候选，已清空自造文字并加入待审核汇总",
                                page_label,
                            )
                        return None
                    actual = await self._raise_as_jd(
                        self._set_select_values_directly(
                            value_select, (re.split(r"[/／]", desired)[0].strip(),),
                            label=page_label, multi=multi,
                        )
                    )
        elif len(inputs) == 1:
            # 检查是否是只读的 cascader 输入框
            is_readonly = await inputs[0].get_attribute("readonly")
            cascader = item.locator(".el-cascader").first
            has_cascader = await cascader.count() > 0

            if is_readonly and has_cascader:
                # 这是级联选择器，尝试使用下拉选择逻辑
                if self.logger is not None:
                    self.logger.info('京东属性%s是级联选择器，尝试选择值', page_label)
                try:
                    actual = await self._raise_as_jd(
                        self._select_values(
                            cascader,
                            selection_value_groups(page_label, desired),
                            label=page_label,
                            multi=False,
                        )
                    )
                    if actual is None:
                        if required:
                            raise JdFormListingError("京东属性{0}没有 Excel 精确候选".format(page_label))
                        return None
                except JdFormListingError:
                    if required:
                        raise
                    return None
            elif not is_readonly:
                # 普通可编辑输入框
                expected_text = str(desired).strip()
                if (await inputs[0].input_value()).strip() != expected_text:
                    await self._enter_as_user(inputs[0], expected_text)
                actual = ((await inputs[0].input_value()).strip(),)
                if actual[0] != expected_text:
                    raise JdFormListingError("京东属性{0}回读失败".format(page_label))
            else:
                if required:
                    raise JdFormListingError("京东属性{0}是只读输入框但不是级联选择器".format(page_label))
                return None
        else:
            if required:
                structure = await item.evaluate(
                    """node => ({
                      html: String(node.outerHTML || '').slice(0, 5000),
                      selects: node.querySelectorAll('.el-select').length,
                      cascaders: node.querySelectorAll('.el-cascader').length,
                      inputs: Array.from(node.querySelectorAll('input')).map(input => ({
                        type: input.type,
                        value: input.value,
                        readOnly: input.readOnly,
                        disabled: input.disabled,
                        className: input.className
                      }))
                    })"""
                )
                if self.logger is not None:
                    self.logger.info('京东属性%s复合控件结构：%s', page_label, structure)
                raise JdFormListingError(
                    "京东属性{0}无唯一可填控件："
                    "select={1}, cascader={2}, input={3}".format(
                        page_label,
                        structure.get("selects"),
                        structure.get("cascaders"),
                        len(structure.get("inputs", ()))
                    )
                )
            return None

        if material and inputs:
            percentage = _material_percentage(expected)
            if percentage is not None:
                percent_input = inputs[0]
                if not _numeric_equal((await percent_input.input_value()).strip(), percentage):
                    await self._enter_as_user(percent_input, percentage)
                if not _numeric_equal((await percent_input.input_value()).strip(), percentage):
                    raise JdFormListingError("京东属性{0}百分比回读失败".format(page_label))
        if normalized == normalize_label("材质") and actual and is_single_material_expression(expected):
            await self._remove_extra_material_rows(item)
            remaining = item.locator(":scope > .el-form-item__content .el-select:visible")
            if await remaining.count() != 1 or tuple(await self._read_select_values(remaining.first, multi=False)) != tuple(actual):
                raise JdFormListingError("京东删除多余材质行后首行发生变化")
        return actual

    def _material_selects(self, item: Any) -> Any:
        return item.locator(":scope > .el-form-item__content .el-select:visible")

    async def _material_add_control(self, item: Any) -> Any:
        buttons = item.get_by_role("button", name="添加", exact=True)
        if await buttons.count() == 1:
            return buttons
        texts = item.get_by_text("添加", exact=True)
        visible = [
            texts.nth(index)
            for index in range(await texts.count())
        ]
        visible = [
            candidate
            for candidate in visible
            if await candidate.is_visible()
        ]
        if len(visible) == 1:
            return visible[0]
        raise JdFormListingError("京东材质找不到唯一“添加”按钮")

    async def _ensure_material_row_count(self, item: Any, expected_count: int) -> None:
        if expected_count < 1:
            raise JdFormListingError("京东材质至少需要一行")
        selects = self._material_selects(item)
        count = await selects.count()
        if count == 0:
            await (await self._material_add_control(item)).click(
                force=True, timeout=5_000
            )
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                if await selects.count() >= 1:
                    break
                await asyncio.sleep(0.1)
            else:
                raise JdFormListingError("京东材质点击“添加”后未生成材质行")
            count = await selects.count()
        while count < expected_count:
            await (await self._material_add_control(item)).click(
                force=True, timeout=5_000
            )
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                if await selects.count() == count + 1:
                    break
                await asyncio.sleep(0.1)
            else:
                raise JdFormListingError(
                    "京东材质点击“添加”后未生成第 {0} 行".format(count + 1)
                )
            count += 1
        while count > expected_count:
            deletes = item.get_by_text("删除", exact=True)
            if await deletes.count() != count:
                raise JdFormListingError(
                    "京东材质行和删除按钮数量不一致，未删除不明控件"
                )
            await deletes.last.click(timeout=3_000)
            deadline = asyncio.get_running_loop().time() + 2
            while (
                await selects.count() == count
                and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0.02)
            if await selects.count() != count - 1:
                raise JdFormListingError("京东多余材质行删除回读失败")
            count -= 1

    async def _fill_material_components(
        self,
        item: Any,
        components: Sequence[MaterialComponent],
    ) -> Optional[Tuple[str, ...]]:
        try:
            expected_rows = self._expected_material_rows(components)
        except TaobaoListingError as exc:
            raise JdFormListingError(
                str(exc).replace("淘宝", "京东").replace("材质成分", "材质")
            ) from exc
        await self._ensure_material_row_count(item, len(expected_rows))
        selected_names: List[str] = []
        for index, (name, percentage) in enumerate(expected_rows):
            selects = self._material_selects(item)
            if await selects.count() != len(expected_rows):
                raise JdFormListingError(
                    "京东材质行数不是 {0}".format(len(expected_rows))
                )
            select = selects.nth(index)
            if await select.locator(".el-select__tags").count() > 0:
                raise JdFormListingError("京东属性材质不应为多选控件")
            try:
                await self._raise_as_jd(self._dismiss_select_dropdown(select))
            except JdFormListingError:
                pass
            actual = await self._select_numeric_material_option(
                select,
                _material_option_desired(name),
            )
            if actual is None:
                return None
            selected_names.extend(actual)
            inputs = await self._editable_inputs(item)
            if len(inputs) != len(expected_rows):
                raise JdFormListingError(
                    "京东材质百分比输入框数量不是 {0}".format(len(expected_rows))
                )
            percent_text = str(int(percentage))
            percent_input = inputs[index]
            if not _numeric_equal(
                (await percent_input.input_value()).strip(), percent_text
            ):
                await self._enter_as_user(percent_input, percent_text)
            if not _numeric_equal(
                (await percent_input.input_value()).strip(), percent_text
            ):
                raise JdFormListingError(
                    "京东属性材质第 {0} 行百分比回读失败".format(index + 1)
                )
        return tuple(selected_names)

    async def _remove_extra_material_rows(self, item: Any) -> None:
        """Only after the first component was verified, remove stale tail rows."""
        await self._ensure_material_row_count(item, 1)

    async def _verify_material_components(
        self,
        components: Sequence[MaterialComponent],
        expected_attributes=None,
    ) -> Mapping[str, Any]:
        try:
            expected_rows = self._expected_material_rows(components)
        except TaobaoListingError as exc:
            raise JdFormListingError(
                str(exc).replace("淘宝", "京东").replace("材质成分", "材质")
            ) from exc
        items = await self._collect_attribute_items()
        material = next(
            (
                item
                for key, (_label, item) in items.items()
                if key.split("#", 1)[0] == normalize_label("材质")
            ),
            None,
        )
        if material is None:
            raise JdFormListingError("京东保存后材质字段消失")
        selects = self._material_selects(material)
        if await selects.count() != len(expected_rows):
            raise JdFormListingError(
                "京东保存后材质行数不一致：期望 {0}，页面 {1}".format(
                    len(expected_rows), await selects.count()
                )
            )
        filled_names = tuple(
            str(value).strip()
            for value in ((expected_attributes or {}).get("材质") or ())
            if str(value).strip()
        )
        actual_names: List[str] = []
        inputs = await self._editable_inputs(material)
        if len(inputs) != len(expected_rows):
            raise JdFormListingError("京东保存后材质百分比输入框数量不一致")
        for index, (name, percentage) in enumerate(expected_rows):
            actual = await self._read_select_values(
                selects.nth(index), multi=False
            )
            aliases = (name,) + TAOBAO_MATERIAL_OPTION_ALIASES.get(
                normalize_option(name), ()
            )
            if index < len(filled_names):
                aliases = aliases + (filled_names[index],)
            if len(actual) != 1 or not any(
                normalize_option(actual[0]) == normalize_option(alias)
                for alias in aliases
            ):
                raise JdFormListingError(
                    "京东保存后材质第 {0} 行不一致：{1}".format(index + 1, actual)
                )
            actual_names.append(actual[0])
            if not _numeric_equal(
                await inputs[index].input_value(), str(percentage)
            ):
                raise JdFormListingError("京东保存后材质百分比不一致")
        return {
            "status": "verified",
            "values": tuple(actual_names),
            "percentage": (
                str(expected_rows[0][1]) if len(expected_rows) == 1 else None
            ),
            "percentages": tuple(
                str(percentage) for _name, percentage in expected_rows
            ),
            "row_count": len(expected_rows),
        }

    async def _verify_single_material(self, fields: JdFields, expected_attributes=None):
        expression = _first_excel_value(fields.fields, ("材质", "材质成分"))
        try:
            components = parse_taobao_materials(fields.fields)
        except TaobaoListingError as exc:
            if expression and not is_single_material_expression(expression):
                raise JdFormListingError(
                    str(exc).replace("淘宝", "京东")
                ) from exc
            components = ()
        if components:
            return await self._verify_material_components(
                components, expected_attributes
            )
        if expression is None or not is_single_material_expression(expression):
            return {"status": "not_single_component"}
        items = await self._collect_attribute_items()
        material = next((item for key, (_label, item) in items.items()
                         if key.split("#", 1)[0] == normalize_label("材质")), None)
        if material is None:
            raise JdFormListingError("京东保存后材质字段消失")
        selects = self._material_selects(material)
        if await selects.count() != 1:
            raise JdFormListingError("京东保存后单成分材质仍有多余行或缺失")
        actual = await self._read_select_values(selects.first, multi=False)
        wanted = ((expected_attributes or {}).get("材质")
                  or tuple(_material_name(expression).split("/")))
        if len(actual) != 1 or not any(normalize_option(actual[0]) == normalize_option(v) for v in wanted):
            raise JdFormListingError(f"京东保存后材质不一致：{actual}")
        percentage = _material_percentage(expression)
        if percentage is not None:
            inputs = await self._editable_inputs(material)
            if len(inputs) != 1 or not _numeric_equal(await inputs[0].input_value(), percentage):
                raise JdFormListingError("京东保存后材质百分比不一致")
        return {"status": "verified", "values": actual, "percentage": percentage, "row_count": 1}

    async def _read_persisted_attribute_item(
        self, page_label: str, item: Any
    ) -> Tuple[str, ...]:
        cascader = item.locator(".el-cascader").first
        if await cascader.count():
            input_box = cascader.locator("input.el-input__inner").first
            if await input_box.count():
                text = (await input_box.input_value()).strip()
                values = tuple(
                    value.strip()
                    for value in re.split(r"\s*(?:/|>)\s*", text)
                    if value.strip()
                )
                if values:
                    return values

        selects = item.locator(":scope > .el-form-item__content .el-select:visible")
        selected: List[str] = []
        for index in range(await selects.count()):
            select = selects.nth(index)
            multi = await select.locator(".el-select__tags").count() > 0
            selected.extend(
                value
                for value in await self._read_select_values(select, multi=multi)
                if normalize_option(value)
            )
        if selected:
            return tuple(selected)

        values = tuple(
            (await input_box.input_value()).strip()
            for input_box in await self._editable_inputs(item)
            if (await input_box.input_value()).strip()
        )
        if values:
            return values
        raise JdFormListingError(
            "京东保存后属性{0}无可回读值".format(page_label)
        )

    @staticmethod
    def _persisted_attribute_values_match(
        page_label: str,
        actual: Sequence[str],
        expected: Sequence[str],
    ) -> bool:
        remaining = list(actual)
        for expected_value in expected:
            aliases = value_candidates(page_label, expected_value)
            match_index = next(
                (
                    index
                    for index, actual_value in enumerate(remaining)
                    if any(
                        normalize_option(actual_value) == normalize_option(alias)
                        for alias in aliases
                    )
                ),
                None,
            )
            if match_index is None:
                return False
            remaining.pop(match_index)
        # JD cascaders persist the complete path.  The fill path can resolve
        # Excel's alias to the leaf only, while the reopened control returns
        # both the parent and leaf (for example ``休闲风,简约风``).  The parent
        # is structural context, so accept it when the expected leaf values
        # occur uniquely and in order.
        if (
            normalize_label(page_label) == normalize_label("风格")
            and expected
            and len(actual) >= len(expected)
        ):
            cursor = 0
            for expected_value in expected:
                aliases = value_candidates(page_label, expected_value)
                match_index = next(
                    (
                        index
                        for index in range(cursor, len(actual))
                        if any(
                            normalize_option(actual[index])
                            == normalize_option(alias)
                            for alias in aliases
                        )
                    ),
                    None,
                )
                if match_index is None:
                    return False
                cursor = match_index + 1
            return True
        return not remaining

    async def _verify_persisted_attributes(
        self,
        expected_attributes: Optional[Mapping[str, Sequence[str]]],
        material_report: Mapping[str, Any],
    ) -> Dict[str, Tuple[str, ...]]:
        expected_map = dict(expected_attributes or {})
        if not expected_map:
            return {}
        items = await self._collect_attribute_items()
        verified: Dict[str, Tuple[str, ...]] = {}
        for expected_label, raw_expected in expected_map.items():
            expected_source = (
                raw_expected
                if isinstance(raw_expected, (list, tuple))
                else (raw_expected,)
            )
            expected = tuple(
                str(value).strip()
                for value in expected_source
                if value is not None and str(value).strip()
            )
            if not expected:
                continue
            if (
                normalize_label(expected_label) == normalize_label("材质")
                and material_report.get("status") == "verified"
            ):
                actual = tuple(str(value) for value in material_report.get("values", ()))
                if not self._persisted_attribute_values_match(
                    expected_label, actual, expected
                ):
                    raise JdFormListingError(
                        "京东保存后属性{0}不一致：{1}".format(
                            expected_label, actual
                        )
                    )
                verified[expected_label] = actual
                continue

            base = normalize_label(expected_label)
            records = [
                (page_label, item)
                for key, (page_label, item) in items.items()
                if key.split("#", 1)[0] == base
            ]
            if not records:
                raise JdFormListingError(
                    "京东保存后属性{0}已消失".format(expected_label)
                )
            observed: List[Tuple[str, ...]] = []
            for page_label, item in records:
                try:
                    actual = await self._read_persisted_attribute_item(page_label, item)
                except JdFormListingError:
                    continue
                observed.append(actual)
                if self._persisted_attribute_values_match(
                    page_label, actual, expected
                ):
                    verified[page_label] = actual
                    break
            else:
                raise JdFormListingError(
                    "京东保存后属性{0}不一致：期望 {1}，页面 {2}".format(
                        expected_label, expected, observed
                    )
                )
        return verified

    async def fill_attributes(self, fields: JdFields) -> Mapping[str, Any]:
        items = await self._attribute_items()
        assignments = await self._attribute_assignments(fields.fields, items)
        applied: Dict[str, Tuple[str, ...]] = {}
        skipped: Dict[str, str] = {}
        cleared: Dict[str, str] = {}
        ignored = []
        missing_required = []
        applied_bases = set()
        for key, (page_label, item) in items.items():
            base = key.split("#", 1)[0]
            if base in JD_IGNORED_ATTRIBUTE_LABELS or skip_color_attribute(page_label):
                ignored.append(page_label)
                if self.logger is not None:
                    self.logger.info('京东商品属性“%s”按规则不填写', page_label)
                continue
            # JD's material control is represented by nested form items with
            # the same label.  Selecting the first row replaces the nested DOM,
            # so a later duplicate occurrence from the original snapshot no
            # longer exists.  Once the logical field has been filled and read
            # back, do not treat that vanished duplicate as data loss.
            if base in applied_bases:
                if self.logger is not None and "#" in key:
                    self.logger.info(
                        '京东属性“%s”重复控件已随页面重渲染合并，已按首个控件回读结果跳过',
                        page_label,
                    )
                continue
            # 选择一个属性后，京东表单会重新渲染后续复合字段。
            # Playwright 的 nth locator 会因 DOM 顺序变化指向错项，
            # 所以每次写入前按字段名重新解析当前页面结构。
            current_items = await self._collect_attribute_items()
            current = current_items.get(key)
            if current is None:
                same_label = [
                    value
                    for current_key, value in current_items.items()
                    if current_key.split("#", 1)[0] == base
                ]
                if len(same_label) == 1:
                    current = same_label[0]
                    if self.logger is not None:
                        self.logger.info(
                            '京东属性“%s”重渲染后序号变化，已按唯一字段名重新定位',
                            page_label,
                        )
                elif assignments.get(key) is None:
                    # An optional/unmapped control can legitimately disappear
                    # when another attribute changes the dependent schema.
                    continue
            if current is None:
                raise JdFormListingError(
                    "京东属性{0}在页面重新渲染后消失".format(page_label)
                )
            page_label, item = current
            if base in JD_CLEARED_ATTRIBUTE_LABELS:
                cleared[page_label] = await self._clear_attribute_item(page_label, item)
                if self.logger is not None:
                    self.logger.info('京东商品属性“%s”按规则保持空白', page_label)
                continue
            required = await self._is_required(item)
            assignment = assignments.get(key)
            if assignment is None:
                if required and key.split("#", 1)[0] not in JD_IGNORED_ATTRIBUTE_LABELS:
                    missing_required.append(page_label)
                continue
            _source, expected = assignment
            actual = await self._fill_attribute_item(page_label, item, expected, required=required)
            if actual is None:
                skipped[page_label] = expected
            else:
                applied[page_label] = actual
                applied_bases.add(base)
        if missing_required:
            raise JdFormListingError("Excel 中缺少京东必填属性：" + "、".join(missing_required))
        return {
            "attributes": applied,
            "cleared_fields": cleared,
            "ignored_fields": tuple(ignored),
            "skipped_no_exact_candidate": skipped,
            "unmatched_page_fields": tuple(
                page_label
                for key, (page_label, _item) in items.items()
                if key not in assignments
                and key.split("#", 1)[0] not in JD_CLEARED_ATTRIBUTE_LABELS
                and key.split("#", 1)[0] not in JD_IGNORED_ATTRIBUTE_LABELS
            ),
        }

    async def _color_spec_value_snapshot(self) -> Tuple[str, ...]:
        """Return the echoed values under JD's single ``颜色`` spec group.

        The persisted JD editor can render a colour name outside its binding
        dialog even though the dialog's colour field is empty.  Marking the
        inputs in the page lets the caller reopen each value without relying
        on generated Vue class names.  Only the group whose spec-name input is
        exactly ``颜色`` is considered; size and every other spec stay untouched.
        """
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        result = await self.panel.evaluate(
            """panel => {
              const clean = value => String(value || '')
                .replace(/\s+/g, '')
                .replace(/[\uff1a:]$/, '');
              const visible = node => {
                if (!(node instanceof Element)) return false;
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none'
                  && style.visibility !== 'hidden'
                  && rect.width > 0 && rect.height > 0;
              };
              const hasExactText = (root, expected) =>
                Array.from(root.querySelectorAll('*')).some(node =>
                  visible(node) && clean(node.innerText) === expected
                );

              panel.querySelectorAll('[data-codex-jd-color-value]').forEach(node =>
                node.removeAttribute('data-codex-jd-color-value')
              );
              const nameInputs = Array.from(panel.querySelectorAll('input'))
                .filter(input => visible(input) && clean(input.value) === '\u989c\u8272');
              const groups = [];
              for (const nameInput of nameInputs) {
                let root = nameInput.parentElement;
                while (root && root !== panel) {
                  const inputs = Array.from(root.querySelectorAll('input')).filter(input => {
                    const type = String(input.type || '').toLowerCase();
                    return visible(input)
                      && !['hidden', 'checkbox', 'radio'].includes(type)
                      && clean(input.value);
                  });
                  const values = inputs.filter(input => input !== nameInput);
                  if (hasExactText(root, '\u89c4\u683c\u503c') && values.length) {
                    groups.push({root, nameInput, values});
                    break;
                  }
                  root = root.parentElement;
                }
              }
              if (groups.length !== 1) {
                return {groupCount: groups.length, values: []};
              }
              const values = groups[0].values.map((input, index) => {
                input.setAttribute('data-codex-jd-color-value', String(index));
                return String(input.value || '').trim();
              });
              return {groupCount: 1, values};
            }"""
        )
        group_count = int(result.get("groupCount") or 0)
        values = tuple(str(value).strip() for value in result.get("values", ()) if str(value).strip())
        if group_count != 1:
            raise JdFormListingError(
                "京东规格名“颜色”分组不是唯一项：{0}".format(group_count)
            )
        if not values:
            raise JdFormListingError("京东规格名“颜色”下没有回显的规格值")
        if len({normalize_option(value) for value in values}) != len(values):
            raise JdFormListingError("京东颜色规格值存在重复，无法唯一重填")
        return values

    async def _visible_color_spec_dialog(self, timeout_seconds: float = 5) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            dialogs = self.page.locator(".el-dialog:visible, [role=dialog]:visible")
            matches = []
            for index in range(await dialogs.count()):
                dialog = dialogs.nth(index)
                titles = dialog.locator(
                    ".el-dialog__title:visible, [role=heading]:visible"
                )
                title_values = []
                for title_index in range(await titles.count()):
                    title_values.append(
                        normalize_label(await titles.nth(title_index).inner_text())
                    )
                if normalize_label("颜色") in title_values:
                    matches.append(dialog)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise JdFormListingError(
                    "京东颜色规格值弹窗不是唯一项：{0}".format(len(matches))
                )
            await asyncio.sleep(0.05)
        raise JdFormListingError("点击京东颜色规格值后未出现“颜色”弹窗")

    async def _dialog_form_item_exact(self, dialog: Any, label: str) -> Any:
        labels = dialog.get_by_text(
            re.compile(r"^\s*\*?\s*{0}\s*[\uff1a:]?\s*$".format(re.escape(label)))
        )
        matches = []
        for index in range(await labels.count()):
            node = labels.nth(index)
            if not await node.is_visible():
                continue
            item = node.locator(
                "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-form-item ')][1]"
            )
            if await item.count() == 1:
                matches.append(item)
        if len(matches) != 1:
            raise JdFormListingError(
                "京东颜色规格弹窗字段{0}不是唯一项：{1}".format(label, len(matches))
            )
        return matches[0]

    async def _reapply_color_dialog_value(self, dialog: Any, expected: str) -> str:
        item = await self._dialog_form_item_exact(dialog, "颜色")
        selects = item.locator(":scope > .el-form-item__content .el-select:visible")
        if await selects.count():
            if await selects.count() != 1:
                raise JdFormListingError("京东颜色规格弹窗下拉框不唯一")
            select = selects.first
            chosen = await self._raise_as_jd(
                self._choose_one_option(
                    select,
                    (expected,),
                    label="颜色规格值",
                    multi=False,
                )
            )
            if chosen is None:
                raise JdFormListingError(
                    "京东颜色规格弹窗没有回显值“{0}”的精确候选".format(expected)
                )
            actual_values = await self._read_select_values(select, multi=False)
            actual = actual_values[0] if actual_values else ""
            await self._raise_as_jd(self._dismiss_select_dropdown(select))
        else:
            inputs = await self._editable_inputs(item)
            if len(inputs) != 1:
                raise JdFormListingError(
                    "京东颜色规格弹窗可填输入框不是唯一项：{0}".format(len(inputs))
                )
            input_box = inputs[0]
            await self._enter_as_user(input_box, expected)
            # Some JD categories render the colour control as autocomplete
            # rather than el-select.  When an exact suggestion appears, click
            # it to restore the platform-side value binding; otherwise the
            # editable value itself is retained and verified below.
            exact = []
            if await item.locator(".el-autocomplete").count():
                options = self.page.locator(
                    ".el-autocomplete-suggestion:visible li:visible, "
                    "[role=listbox]:visible [role=option]:visible"
                )
                deadline = asyncio.get_running_loop().time() + 1
                while asyncio.get_running_loop().time() < deadline:
                    exact = [
                        options.nth(index)
                        for index in range(await options.count())
                        if normalize_option(await options.nth(index).inner_text())
                        == normalize_option(expected)
                    ]
                    if exact:
                        break
                    await asyncio.sleep(0.05)
            if len(exact) > 1:
                raise JdFormListingError(
                    "京东颜色规格弹窗候选“{0}”不唯一".format(expected)
                )
            if len(exact) == 1:
                await exact[0].click(timeout=2_000)
            actual = (await input_box.input_value()).strip()
        if normalize_option(actual) != normalize_option(expected):
            raise JdFormListingError(
                "京东颜色规格弹窗重填回读失败：期望 {0!r}，页面为 {1!r}".format(
                    expected, actual
                )
            )
        return actual

    async def reapply_echoed_color_spec_values(self) -> Mapping[str, Any]:
        """Re-enter each value echoed under the JD colour specification."""
        expected_values = await self._color_spec_value_snapshot()
        applied = []
        for expected in expected_values:
            current_values = await self._color_spec_value_snapshot()
            matching_indexes = [
                index
                for index, value in enumerate(current_values)
                if normalize_option(value) == normalize_option(expected)
            ]
            if len(matching_indexes) != 1:
                raise JdFormListingError(
                    "京东颜色规格值“{0}”重新定位失败".format(expected)
                )
            value_input = self.panel.locator(
                '[data-codex-jd-color-value="{0}"]'.format(matching_indexes[0])
            )
            if await value_input.count() != 1:
                raise JdFormListingError(
                    "京东颜色规格值“{0}”输入框不唯一".format(expected)
                )
            await value_input.click(force=True, timeout=4_000)
            dialog = await self._visible_color_spec_dialog()
            actual = await self._reapply_color_dialog_value(dialog, expected)
            confirm = dialog.get_by_role(
                "button", name=re.compile(r"^\s*\u786e\s*\u5b9a\s*$")
            )
            if await confirm.count() != 1:
                raise JdFormListingError("京东颜色规格弹窗“确定”不是唯一项")
            await confirm.click(timeout=4_000)
            await dialog.wait_for(state="hidden", timeout=10_000)
            after = await self._color_spec_value_snapshot()
            if not any(
                normalize_option(value) == normalize_option(expected) for value in after
            ):
                raise JdFormListingError(
                    "京东颜色规格值“{0}”确定后外层回读失败".format(expected)
                )
            applied.append(actual)
            if self.logger is not None:
                self.logger.info("京东颜色规格值已按页面回显重填并确认：%s", actual)
        return {"spec_name": "颜色", "values": tuple(applied)}

    async def _batch_input(self, label: str) -> Any:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        nodes = self.panel.get_by_text(re.compile(r"^\s*{0}\s*[：:]?\s*$".format(re.escape(label))))
        matches = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if not await node.is_visible():
                continue
            root = node
            for _depth in range(5):
                inputs = root.locator('input:not([type="hidden"]):not([readonly]):visible')
                if await inputs.count() == 1 and not await root.locator(".el-table, table").count():
                    matches.append(inputs.first)
                    break
                root = root.locator("xpath=..")
        if len(matches) != 1:
            raise JdFormListingError("京东批量字段{0}输入框不是唯一项：{1}".format(label, len(matches)))
        return matches[0]

    @staticmethod
    def _expected_sku(fields: Mapping[str, str]) -> Mapping[str, str]:
        result = {
            "京东价": _required_excel_value(
                fields, ("京东价", "价格", "基本售价", "商品价格"), "京东价", money=True
            ),
            "库存": _required_excel_value(fields, ("库存", "数量"), "库存"),
        }
        for label, value in result.items():
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise JdFormListingError("Excel 京东{0}不是数字".format(label)) from exc
            if not number.is_finite() or number <= 0 if label == "京东价" else number < 0 or number % 1:
                raise JdFormListingError("Excel 京东{0}值无效：{1!r}".format(label, value))
        return result

    async def _sku_table_snapshot(self) -> Mapping[str, Any]:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
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
            if all(any(header == normalize_label(label) or header.startswith(normalize_label(label)) for header in normalized) for label in JD_BATCH_ORDER):
                matches.append(snapshot)
        if len(matches) != 1:
            raise JdFormListingError("京东 SKU 表格不是唯一项：{0}".format(len(matches)))
        return matches[0]

    @staticmethod
    def _column_index(headers: Sequence[str], label: str) -> int:
        wanted = normalize_label(label)
        indexes = [index for index, header in enumerate(headers) if normalize_label(header) == wanted or normalize_label(header).startswith(wanted)]
        if len(indexes) != 1:
            raise JdFormListingError("京东 SKU 列{0}不是唯一项".format(label))
        return indexes[0]

    def _validate_sku(self, snapshot: Mapping[str, Any], expected: Mapping[str, str]) -> Tuple[Mapping[str, str], ...]:
        headers = tuple(str(value) for value in snapshot.get("headers", ()))
        raw_rows = tuple(snapshot.get("rows", ()))
        if not raw_rows:
            raise JdFormListingError("京东 SKU 表格没有明细行")
        indexes = {label: self._column_index(headers, label) for label in expected}
        rows = []
        errors = []
        for number, raw_row in enumerate(raw_rows, 1):
            row = {label: str(raw_row[index]) if index < len(raw_row) else "" for label, index in indexes.items()}
            for label, value in expected.items():
                if not _numeric_equal(row[label], value):
                    errors.append("第{0}行{1}={2!r}".format(number, label, row[label]))
            rows.append(row)
        if errors:
            raise JdFormListingError("京东批量设置后校验失败：" + "；".join(errors[:12]))
        return tuple(rows)

    async def fill_sku_batch(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        expected = self._expected_sku(fields)
        for label, value in expected.items():
            input_box = await self._batch_input(label)
            if not _numeric_equal((await input_box.input_value()).strip(), value):
                await self._enter_as_user(input_box, value)
        buttons = self.panel.get_by_role("button", name="批量设置", exact=True)
        candidates = []
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            if await button.is_visible() and not await button.locator("xpath=ancestor::th").count():
                candidates.append(button)
        if len(candidates) != 1:
            raise JdFormListingError("京东价格库存批量设置按钮不是唯一项：{0}".format(len(candidates)))
        await candidates[0].click()
        deadline = asyncio.get_running_loop().time() + 15
        last_error: Optional[Exception] = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                rows = self._validate_sku(await self._sku_table_snapshot(), expected)
                return {"batch_clicked": True, "row_count": len(rows), "values": expected, "rows": rows}
            except JdFormListingError as exc:
                last_error = exc
                await asyncio.sleep(0.15)
        raise JdFormListingError(str(last_error or "京东批量设置超时"))

    async def apply_sku_thickness(self) -> Mapping[str, str]:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        triggers = await self._header_actions("SKU属性", "批量设置")
        if len(triggers) == 0:
            raise JdFormListingError('京东 SKU 属性没有可定位的"批量设置"按钮')
        if len(triggers) != 1:
            raise JdFormListingError("京东 SKU 属性批量设置不是唯一项：{0}".format(len(triggers)))
        await triggers[0].click()
        dialog: Optional[Any] = None
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            dialogs = self.page.locator(".el-dialog:visible, [role=dialog]:visible")
            matches = []
            for index in range(await dialogs.count()):
                candidate = dialogs.nth(index)
                title = await self._innermost_visible_text(candidate, "SKU属性")
                if title:
                    matches.append(candidate)
            if len(matches) == 1:
                dialog = matches[0]
                break
            if len(matches) > 1:
                raise JdFormListingError(
                    "京东 SKU 属性批量弹窗不是唯一项：{0}".format(len(matches))
                )
            await asyncio.sleep(0.1)
        if dialog is None:
            raise JdFormListingError("京东 SKU 属性批量弹窗未出现")

        async def visible_thickness_labels() -> List[Any]:
            labels = dialog.get_by_text(re.compile(r"^\s*厚度\s*[：:]?\s*$"))
            return [
                labels.nth(index)
                for index in range(await labels.count())
                if await labels.nth(index).is_visible()
            ]

        labels = await visible_thickness_labels()
        if not labels:
            refreshes = await self._innermost_visible_text(dialog, "刷新数据")
            if len(refreshes) != 1:
                raise JdFormListingError(
                    "京东 SKU 属性首次弹窗缺少唯一“刷新数据”入口"
                )
            response_paths: List[str] = []
            thickness_paths: List[str] = []
            tasks: List[asyncio.Task[Any]] = []

            async def capture(response: Any) -> None:
                try:
                    headers = await response.all_headers()
                    if "json" not in str(headers.get("content-type") or "").casefold():
                        return
                    payload = await response.json()
                    path = urlsplit(response.url).path
                    if path not in response_paths:
                        response_paths.append(path)
                    if self._json_contains_text(payload, "厚度") and path not in thickness_paths:
                        thickness_paths.append(path)
                except Exception:
                    return

            def handler(response: Any) -> None:
                tasks.append(asyncio.create_task(capture(response)))

            self.page.on("response", handler)
            try:
                await refreshes[0].click()
                refresh_deadline = asyncio.get_running_loop().time() + 30
                while asyncio.get_running_loop().time() < refresh_deadline:
                    await self._raise_as_jd(super()._wait_for_loading_masks())
                    labels = await visible_thickness_labels()
                    if labels:
                        break
                    await asyncio.sleep(0.15)
            finally:
                self.page.remove_listener("response", handler)
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            if self.logger is not None:
                self.logger.info(
                    "京东 SKU 属性刷新交叉校验：JSON响应=%s，含厚度响应=%s，DOM厚度=%s",
                    response_paths,
                    thickness_paths,
                    len(labels),
                )
        if len(labels) != 1:
            raise JdFormListingError(
                "京东 SKU 属性刷新后厚度字段不是唯一项：{0}".format(len(labels))
            )
        item = labels[0].locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-form-item ')][1]")
        actual = await self._raise_as_jd(
            self._fill_attribute("厚度", item, JD_SKU_THICKNESS, required=True)
        )
        if actual is None:
            raise JdFormListingError("京东 SKU 厚度没有常规精确候选")
        confirm = dialog.get_by_role("button", name=re.compile(r"^\s*确\s*定\s*$"))
        if await confirm.count() != 1:
            raise JdFormListingError("京东 SKU 属性弹窗确定不是唯一项")
        await confirm.click()
        await dialog.wait_for(state="hidden", timeout=10_000)
        return {"厚度": actual[0]}

    @staticmethod
    def _json_contains_text(payload: Any, target: str) -> bool:
        """Return whether a decoded JSON payload contains the exact text."""
        wanted = normalize_label(target)
        if isinstance(payload, Mapping):
            return any(
                JdFormListing._json_contains_text(key, target)
                or JdFormListing._json_contains_text(value, target)
                for key, value in payload.items()
            )
        if isinstance(payload, (list, tuple)):
            return any(JdFormListing._json_contains_text(value, target) for value in payload)
        if isinstance(payload, str):
            if normalize_label(payload) == wanted:
                return True
            text = payload.strip()
            if text.startswith(("{", "[")):
                try:
                    return JdFormListing._json_contains_text(json.loads(text), target)
                except (TypeError, ValueError):
                    return False
        return False

    async def _header_actions(self, marker: str, name: str) -> List[Any]:
        """Return exact actions whose own table header contains ``marker``.

        The live JD header renders ``SKU属性`` and its action in one wrapper,
        so locating a standalone header node by exact text returns zero.
        """
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        wanted = normalize_label(marker)
        matches: List[Any] = []
        for action in await self._innermost_visible_text(self.panel, name):
            header = action.locator("xpath=ancestor::th[1]")
            if not await header.count():
                header = action.locator(
                    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-table__cell ')][1]"
                )
            if await header.count() and wanted in normalize_label(await header.inner_text()):
                matches.append(action)
        return matches

    async def fill_summary_prices(self, fields: Mapping[str, str]) -> Mapping[str, str]:
        jd_price = _required_excel_value(
            fields, ("京东价", "价格", "基本售价", "商品价格"), "京东价", money=True
        )
        market = _required_excel_value(
            fields, ("市场价", "价格", "吊牌价", "商品价格"), "市场价", money=True
        )
        return {
            "京东价": await self._fill_input_item("京东价（元）", jd_price, numeric=True),
            "市场价": await self._fill_input_item("市场价（元）", market, numeric=True),
        }

    async def fill_delivery_template(self) -> str:
        item = await self._form_item_exact("发货时效")
        actual = await self._raise_as_jd(
            self._fill_attribute("发货时效", item, JD_DELIVERY_TEMPLATE, required=True)
        )
        if actual is None:
            raise JdFormListingError("京东发货时效没有48小时发货精确候选")
        return actual[0]

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

    async def _innermost_visible_text(self, root: Any, name: str) -> List[Any]:
        nodes = root.get_by_text(name, exact=True)
        matches = []
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if not await node.is_visible():
                continue
            nested = node.locator(":scope *").get_by_text(name, exact=True)
            has_visible_child = False
            for nested_index in range(await nested.count()):
                if await nested.nth(nested_index).is_visible():
                    has_visible_child = True
                    break
            if not has_visible_child:
                matches.append(node)
        return matches

    async def _click_section_action(self, section: str, name: str) -> None:
        """Click one exact action even when the page repeats its section title."""
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        actions = await self._innermost_visible_text(self.panel, name)
        if len(actions) != 1:
            raise JdFormListingError(
                "京东{0}动作“{1}”不是唯一项：{2}".format(
                    section, name, len(actions)
                )
            )
        await actions[0].scroll_into_view_if_needed()
        await actions[0].click(timeout=5_000)
        if self.logger is not None:
            self.logger.info("京东已点击图片动作：%s", name)

    async def _media_upload_diagnostics(self) -> Tuple[Mapping[str, Any], ...]:
        if self.panel is None:
            return ()
        return tuple(
            await self.panel.evaluate(
                """root => Array.from(root.querySelectorAll('.sc-upload'))
                  .filter(upload => {
                    const style = getComputedStyle(upload);
                    return upload.getClientRects().length > 0
                      && style.display !== 'none' && style.visibility !== 'hidden';
                  })
                  .map((upload, index) => {
                    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
                    const ancestors = [];
                    let current = upload.parentElement;
                    for (let depth = 0; current && current !== root && depth < 4; depth += 1) {
                      ancestors.push({
                        tag: current.tagName,
                        className: clean(current.className).slice(0, 120),
                        text: clean(current.innerText).slice(0, 220),
                      });
                      current = current.parentElement;
                    }
                    return {
                      index,
                      fileImages: upload.querySelectorAll('.file-img').length,
                      decodedImages: Array.from(upload.querySelectorAll('img')).filter(
                        image => image.complete && image.naturalWidth > 0
                      ).length,
                      ancestors,
                    };
                  })"""
            )
        )

    async def _sku_color_image_groups(self) -> Tuple[Mapping[str, Any], ...]:
        """Pair each color's 商品展示图 and 规格长图 groups.

        The same 商品展示图 caption is also used by the public image block.
        Color blocks are therefore anchored by their own 使用商品图片 action and
        paired by DOM order.  The method marks only the paired upload groups so
        later mutations cannot accidentally touch the public product images.
        """
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        result = await self.panel.evaluate(
            """root => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const visible = element => {
                const style = getComputedStyle(element);
                return element.getClientRects().length > 0
                  && style.display !== 'none' && style.visibility !== 'hidden';
              };
              const follows = (first, second) => Boolean(
                first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING
              );
              root.querySelectorAll('[data-codex-jd-color-display], [data-codex-jd-color-long], [data-codex-jd-public-display]')
                .forEach(element => {
                  element.removeAttribute('data-codex-jd-color-display');
                  element.removeAttribute('data-codex-jd-color-long');
                  element.removeAttribute('data-codex-jd-public-display');
                });
              const itemGroup = item => Array.from(item.querySelectorAll('.muti-upload'))
                .find(group => group.closest('.el-form-item') === item && visible(group));
              const items = Array.from(root.querySelectorAll('.el-form-item')).filter(visible);
              const displays = items.filter(item => {
                const text = clean(item.innerText).replace(/^\*\s*/, '');
                return text.startsWith('商品展示图') && Boolean(itemGroup(item));
              });
              const longs = items.filter(item => {
                const text = clean(item.innerText).replace(/^\*\s*/, '');
                return (text.startsWith('规格长图') || item.classList.contains('showSearchImg'))
                  && Boolean(itemGroup(item));
              });
              let buttons = Array.from(root.querySelectorAll('button, [role="button"]'))
                .filter(element => visible(element) && clean(element.innerText) === '使用商品图片');
              buttons = buttons.filter(element => !buttons.some(
                other => other !== element && element.contains(other)
              ));
              const colorName = button => {
                const ignored = new Set(['使用商品图片', '商品展示图', '规格长图']);
                const dataOwner = button.closest('[data-color]');
                if (dataOwner && clean(dataOwner.dataset.color)) return clean(dataOwner.dataset.color);
                let cursor = button;
                for (let depth = 0; cursor && cursor !== root && depth < 6; depth += 1) {
                  let sibling = cursor.previousElementSibling;
                  while (sibling) {
                    const text = clean(sibling.innerText || sibling.textContent).replace(/^\*\s*/, '');
                    if (text && text.length <= 32 && !ignored.has(text)) return text;
                    sibling = sibling.previousElementSibling;
                  }
                  const parent = cursor.parentElement;
                  if (parent) {
                    const clone = parent.cloneNode(true);
                    clone.querySelectorAll('button, [role="button"]').forEach(node => node.remove());
                    const text = clean(clone.innerText || clone.textContent).replace(/^\*\s*/, '');
                    if (text && text.length <= 32 && !ignored.has(text)) return text;
                  }
                  cursor = parent;
                }
                return '';
              };
              const pairs = [];
              const usedDisplays = new Set();
              const usedLongs = new Set();
              const errors = [];
              buttons.forEach((button, index) => {
                const next = buttons[index + 1] || null;
                const within = item => follows(button, item) && (!next || follows(item, next));
                const displayMatches = displays.filter(item => within(item));
                const longMatches = longs.filter(item => within(item));
                const color = colorName(button);
                if (!color) errors.push(`第 ${index + 1} 个颜色名无法回读`);
                if (displayMatches.length !== 1) {
                  errors.push(`${color || `第${index + 1}个颜色`} 商品展示图=${displayMatches.length}`);
                }
                if (longMatches.length !== 1) {
                  errors.push(`${color || `第${index + 1}个颜色`} 规格长图=${longMatches.length}`);
                }
                if (displayMatches.length !== 1 || longMatches.length !== 1) return;
                const display = itemGroup(displayMatches[0]);
                const longImage = itemGroup(longMatches[0]);
                display.setAttribute('data-codex-jd-color-display', String(index));
                longImage.setAttribute('data-codex-jd-color-long', String(index));
                usedDisplays.add(displayMatches[0]);
                usedLongs.add(longMatches[0]);
                pairs.push({index, color});
              });
              const publicDisplays = displays.filter(item => !usedDisplays.has(item));
              if (publicDisplays.length !== 1) {
                errors.push(`公共商品展示图=${publicDisplays.length}`);
              } else {
                itemGroup(publicDisplays[0]).setAttribute('data-codex-jd-public-display', 'true');
              }
              if (!buttons.length) errors.push('未找到颜色分组的使用商品图片按钮');
              if (new Set(pairs.map(pair => pair.color)).size !== pairs.length) {
                errors.push('颜色名重复或无法唯一区分');
              }
              return {pairs, errors};
            }"""
        )
        errors = tuple(str(value) for value in result.get("errors", ()))
        if errors:
            if self.logger is not None:
                self.logger.info(
                    "京东颜色图片分组定位失败 DOM 摘要：%s",
                    json.dumps(await self._media_upload_diagnostics(), ensure_ascii=False),
                )
            raise JdFormListingError("京东颜色图片分组无法一一对应：" + "；".join(errors))
        pairs: List[Mapping[str, Any]] = []
        for pair in result.get("pairs", ()):
            index = int(pair["index"])
            display = self.panel.locator(
                '[data-codex-jd-color-display="{0}"]'.format(index)
            )
            long_image = self.panel.locator(
                '[data-codex-jd-color-long="{0}"]'.format(index)
            )
            if await display.count() != 1 or await long_image.count() != 1:
                raise JdFormListingError("京东颜色图片分组 DOM 标记回读失败")
            pairs.append(
                {
                    "index": index,
                    "color": str(pair["color"]),
                    "display": display,
                    "long": long_image,
                }
            )
        return tuple(pairs)

    async def _delete_uploaded_image_at(self, scope: Any, index: int) -> None:
        images = scope.locator(".file-img")
        before = await images.count()
        if index < 0 or index >= before:
            raise JdFormListingError("京东商品展示图没有可删除的第 {0} 张图".format(index + 1))
        image = images.nth(index)
        button = image.locator(".del-btn").first
        if not await button.count():
            raise JdFormListingError("京东商品展示图第 {0} 张缺少删除按钮".format(index + 1))
        try:
            # The delete control is present but CSS-hidden until hover.  A DOM
            # click invokes the same Vue handler without paying a hover timeout
            # for every image in a multi-colour product.
            await button.evaluate("element => element.click()")
        except Exception:
            await image.hover(timeout=2000)
            await button.click(timeout=2000)
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            if await images.count() < before:
                return
            await asyncio.sleep(0.1)
        raise JdFormListingError("京东商品展示图删除后页面未更新")

    async def _delete_first_uploaded_image(self, scope: Any) -> None:
        await self._delete_uploaded_image_at(scope, 0)

    async def scroll_label_into_view(self, label: str) -> None:
        if self.panel is None:
            raise JdFormListingError("请先打开京东资料")
        node = self.panel.get_by_text(
            re.compile(r"^\s*\*?\s*{0}\s*[：:]?\s*$".format(re.escape(label)))
        ).first
        if await node.count():
            await node.scroll_into_view_if_needed()

    async def scroll_color_image_groups_into_view(self) -> None:
        groups = await self._sku_color_image_groups()
        await groups[0]["display"].scroll_into_view_if_needed()

    async def append_sku_images(
        self,
        *,
        image_indices_by_color: Optional[Sequence[Sequence[int]]] = None,
        square_paths: Sequence[Path] = (),
        portrait_paths: Sequence[Path] = (),
        sku_paths: Sequence[Path] = (),
    ) -> Mapping[str, Any]:
        """Append both image sets and normalize each color pair in place."""
        initial_groups = await self._sku_color_image_groups()
        public = self.panel.locator('[data-codex-jd-public-display="true"]')
        public_before = await public.locator(".file-img").count()
        # 追加商品的多颜色 SKU 已由页面“一键追加到所有 SKU 图/长图”
        # 生成完整图片。此场景不再用视觉识别强行区分颜色，避免颜色
        # 图片无法建立锚点时阻断铺货；各颜色保留页面追加后的图片即可。
        if (
            image_indices_by_color is None
            and len(initial_groups) <= 1
            and (square_paths or portrait_paths or sku_paths)
        ):
            image_indices_by_color = classify_image_indices_by_color(
                square_paths,
                portrait_paths,
                sku_paths,
                len(initial_groups),
            )
        elif image_indices_by_color is None and len(initial_groups) > 1:
            if self.logger is not None:
                self.logger.info(
                    "京东检测到 %s 个颜色规格，按追加后的页面图片直接铺货，跳过颜色图片识别",
                    len(initial_groups),
                )
        if image_indices_by_color is not None and len(image_indices_by_color) != len(initial_groups):
            raise JdFormListingError(
                "京东图片颜色分类数与页面颜色数不一致：{0}/{1}".format(
                    len(image_indices_by_color), len(initial_groups)
                )
            )
        requested_indices = (
            tuple(
                tuple(dict.fromkeys(int(index) for index in indices))
                for indices in image_indices_by_color
            )
            if image_indices_by_color is not None
            else None
        )
        initial_counts = [
            (
                await group["display"].locator(".file-img").count(),
                await group["long"].locator(".file-img").count(),
            )
            for group in initial_groups
        ]
        already_normalized = requested_indices is not None and all(
            bool(keep) and display == long_count == len(keep)
            for (display, long_count), keep in zip(initial_counts, requested_indices)
        )
        if already_normalized:
            reports = [
                {
                    "color": group["color"],
                    "product_display_before": display,
                    "product_display_remaining": display,
                    "long_image_count": long_count,
                    "source_image_positions": [index + 1 for index in keep],
                    "sequence_verified": True,
                    "already_normalized": True,
                }
                for group, (display, long_count), keep in zip(
                    initial_groups,
                    initial_counts,
                    requested_indices,
                )
            ]
            if self.logger is not None:
                self.logger.info(
                    "京东二次打开的颜色图片已是保存后的对应状态，跳过重复追加和删除：%s",
                    "、".join(
                        "{0}={1}/{2}".format(
                            item["color"],
                            item["product_display_remaining"],
                            item["long_image_count"],
                        )
                        for item in reports
                    ),
                )
            return {
                "sku_images": "already_normalized",
                "sku_long_images": "already_normalized",
                "public_product_display_unchanged": public_before,
                "colors": reports,
            }
        await self._click_section_action("商品图片", "一键追加到所有sku图")
        await self._raise_as_jd(super()._wait_for_loading_masks())
        await self._click_section_action("商品长图", "一键追加到所有sku长图")
        await self._raise_as_jd(super()._wait_for_loading_masks())
        # 整包追加两色各 5 张图时，慢网络下上传解码可能超过 20 秒
        # （2026-09-21 18:00 线上偶发失败），放宽到 60 秒。
        # 首次填写时展示组含色块图（display == long+1）；重开已保存商品时
        # 色块图不在组内，展示组与长图组同为 5/5（2026-09-21 18:05 线上），
        # 两种稳定状态都接受。
        deadline = asyncio.get_running_loop().time() + 60
        groups = initial_groups
        counts: List[Tuple[int, int]] = []

        def counts_settled() -> bool:
            return bool(counts) and all(
                display in (long_count, long_count + 1) and long_count >= 1
                for display, long_count in counts
            )

        while asyncio.get_running_loop().time() < deadline:
            groups = await self._sku_color_image_groups()
            counts = [
                (
                    await group["display"].locator(".file-img").count(),
                    await group["long"].locator(".file-img").count(),
                )
                for group in groups
            ]
            if counts_settled():
                break
            await asyncio.sleep(0.1)
        if not counts_settled():
            if self.logger is not None:
                self.logger.info(
                    "京东颜色图片追加未完成时的上传区 DOM 摘要：%s",
                    json.dumps(
                        await self._media_upload_diagnostics(),
                        ensure_ascii=False,
                    ),
                )
            raise JdFormListingError("京东颜色分组的商品展示图与规格长图未按顺序追加完成")

        reports: List[Mapping[str, Any]] = []
        source_count = counts[0][1]
        if any(long_count != source_count for _display, long_count in counts):
            raise JdFormListingError("京东各颜色规格长图数量不一致")
        if requested_indices is None:
            normalized_indices = tuple(tuple(range(source_count)) for _group in groups)
        else:
            normalized_indices = requested_indices
            invalid = [
                index
                for indices in normalized_indices
                for index in indices
                if index < 0 or index >= source_count
            ]
            if invalid:
                raise JdFormListingError("京东图片颜色分类产生越界顺序：{0}".format(invalid))
            missing = sorted(set(range(source_count)).difference(
                index for indices in normalized_indices for index in indices
            ))
            if missing or any(not indices for indices in normalized_indices):
                raise JdFormListingError("京东图片颜色分类不完整，未归类顺序：{0}".format(missing))

        for group, (display_before, long_before), keep in zip(groups, counts, normalized_indices):
            if display_before == long_before + 1:
                # 首次填写时展示组第一位是色块图，需要删除让商品图从 1 开始；
                # 重开已保存商品时展示组没有色块图，直接删除会误删真实商品图。
                await self._delete_first_uploaded_image(group["display"])
            rejected = sorted(set(range(source_count)).difference(keep), reverse=True)
            for index in rejected:
                await self._delete_uploaded_image_at(group["display"], index)
                await self._delete_uploaded_image_at(group["long"], index)
            display_remaining = await group["display"].locator(".file-img").count()
            long_remaining = await group["long"].locator(".file-img").count()
            if display_remaining != len(keep) or long_remaining != len(keep):
                raise JdFormListingError(
                    "京东颜色 {0} 的商品展示图/规格长图数量回读失败：{1}/{2}".format(
                        group["color"], display_remaining, long_remaining
                    )
                )
            reports.append(
                {
                    "color": group["color"],
                    "product_display_before": display_before,
                    "product_display_remaining": display_remaining,
                    "long_image_count": long_remaining,
                    "source_image_positions": [index + 1 for index in keep],
                    "sequence_verified": True,
                }
            )

        groups = await self._sku_color_image_groups()
        public_after = await self.panel.locator(
            '[data-codex-jd-public-display="true"]'
        ).locator(".file-img").count()
        if public_after != public_before:
            raise JdFormListingError(
                "京东公共商品展示图被误改：{0}->{1}".format(public_before, public_after)
            )
        if self.logger is not None:
            self.logger.info(
                "京东已点击商品图片“一键追加到所有sku图”和商品长图“一键追加到所有sku长图”，"
                "逐颜色删除分组商品展示图第一张，公共商品图保持 %s 张：%s",
                public_after,
                "、".join(
                    "{0}={1}/{2}".format(
                        item["color"], item["product_display_remaining"], item["long_image_count"]
                    )
                    for item in reports
                ),
            )
        return {
            "sku_images": "appended",
            "sku_long_images": "appended",
            "public_product_display_unchanged": public_after,
            "colors": reports,
        }

    async def apply_excel_fields(
        self,
        fields: JdFields,
        *,
        style_code: str,
        square_paths: Sequence[Path] = (),
        portrait_paths: Sequence[Path] = (),
        sku_paths: Sequence[Path] = (),
    ) -> Mapping[str, Any]:
        category = await self.apply_category(fields.fields)
        # Category confirmation returns before all dependent JD controls have
        # necessarily finished replacing their previous values.  Wait for the
        # attribute section to settle so identity fields cannot read a stale
        # brand that is cleared moments later by the category re-render.
        await self._attribute_items()
        identity = await self.fill_identity_and_parameters(fields.fields, style_code=style_code)
        attributes = await self.fill_attributes(fields)
        color_spec_values = await self.reapply_echoed_color_spec_values()
        sku = await self.fill_sku_batch(fields.fields)
        sku_attributes = await self.apply_sku_thickness()
        prices = await self.fill_summary_prices(fields.fields)
        delivery = await self.fill_delivery_template()
        images = await self.append_sku_images(
            square_paths=square_paths,
            portrait_paths=portrait_paths,
            sku_paths=sku_paths,
        )
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
            raise JdFormListingError("京东页面校验错误：" + "；".join(errors))
        return {
            "category": category,
            "identity_and_parameters": identity,
            "attributes": attributes,
            "color_spec_values": color_spec_values,
            "sku_batch": sku,
            "sku_attributes": sku_attributes,
            "summary_prices": prices,
            "delivery_template": delivery,
            "images": images,
            "deferred_validation_errors": (
                errors
                if self.attribute_runtime is not None
                and getattr(
                    self.attribute_runtime, "has_deferred_reviews", False
                )
                else ()
            ),
        }

    async def verify_persisted_values(
        self, fields: JdFields, *, style_code: str, expected_attributes=None
    ) -> Mapping[str, Any]:
        category = await self._category_text()
        _category_hints, category_target = jd_category_target(fields.fields)
        if _category_parts(category)[-1:] != (normalize_label(category_target),):
            raise JdFormListingError("京东保存后类目回读失败：{0!r}".format(category))
        brand_item = await self._form_item_exact("品牌")
        brand, brand_values = await self._read_brand_value(brand_item)
        if brand != JD_BRAND:
            raise JdFormListingError(
                "京东保存后品牌回读失败：{0!r}".format(brand_values)
            )
        expected = self._expected_sku(fields.fields)
        rows = self._validate_sku(await self._sku_table_snapshot(), expected)
        material = await self._verify_single_material(fields, expected_attributes)
        attributes = await self._verify_persisted_attributes(
            expected_attributes, material
        )
        prices = await self.fill_summary_prices(fields.fields)
        delivery = await self.fill_delivery_template()
        # The verifier only writes when a value is unexpectedly missing; all values
        # are still read back and compared before reporting success.
        return {
            "category": category,
            "brand": brand,
            "style_code": style_code,
            "sku_values": expected,
            "row_count": len(rows),
            "summary_prices": prices,
            "delivery_template": delivery,
            "material": material,
            "attributes": attributes,
        }


__all__ = [
    "JD_BATCH_ORDER",
    "JD_BRAND",
    "JD_CATEGORY_PATH",
    "JD_DELIVERY_TEMPLATE",
    "JD_GROSS_WEIGHT",
    "JD_SKU_THICKNESS",
    "JdFormListing",
    "JdFormListingError",
    "jd_category_target",
]
