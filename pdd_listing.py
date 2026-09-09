from __future__ import annotations

import inspect
import json
import re
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional, Tuple

from platform_discovery import (
    ApiClient,
    DiscoveryContext,
    EndpointSpec,
    GenerationTracker,
    PlatformDiscoveryError,
    SchemaFragment,
    normalize_label,
)
from platform_registry import get_platform_spec
from platform_schema import (
    CategoryCandidate,
    CategoryResolution,
    FieldSchema,
    SectionSchema,
    field_options,
    option_summary,
)


_DETAIL = EndpointSpec(
    "GET",
    "/pdd/detail.json",
    ("baseItemId", "api_name"),
    required_for="fixed",
    read_only=True,
)
_ITEM_PICTURE_QUERY = EndpointSpec(
    "POST",
    "/item/picture/query.json",
    ("api_name", "baseItemId"),
    required_for="fixed",
    read_only=True,
)
_GROUP_SPEC_NAMES = EndpointSpec(
    "POST",
    "/pdd/getGroupSpecNames.json",
    ("api_name",),
    required_for="dynamic",
    read_only=True,
)
_PREDICTION = EndpointSpec(
    "POST",
    "/publish/fast/prediction/cat.json",
    ("api_name", "baseItemId", "platformType", "title"),
    required_for="category",
    read_only=True,
)
_SPECS = EndpointSpec(
    "GET",
    "/pdd/getSpecList.json",
    ("leafCategoryId", "shopId", "api_name"),
    required_for="dynamic",
    read_only=True,
)
_BRAND_RULE = EndpointSpec(
    "GET",
    "/pdd/getBrandRequireRule.json",
    ("leafCategoryId", "shopIds", "api_name"),
    required_for="dynamic",
    read_only=True,
)
_PROPERTIES = EndpointSpec(
    "GET",
    "/pdd/getCategoryProperties.json",
    ("leafCategoryId", "shopId", "api_name"),
    required_for="dynamic",
    read_only=True,
)
_PREDICTED_PROPERTIES = EndpointSpec(
    "GET",
    "/publish/fast/prediction/cat/prop.json",
    ("baseItemId", "platformType", "catId", "shopId", "api_name"),
    required_for="dynamic",
    read_only=True,
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> Tuple[Any, ...]:
    return tuple(value) if isinstance(value, (tuple, list)) else ()


def _parts(value: Any) -> Tuple[str, ...]:
    if isinstance(value, (tuple, list)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if value in (None, ""):
        return ()
    return tuple(
        part.strip()
        for part in re.split(r"[/／>＞,，]+", str(value))
        if part.strip()
    )


def _hint_leaves(context: DiscoveryContext) -> Tuple[str, ...]:
    values = []
    for hint in context.category_hints:
        parts = _parts(hint)
        for part in parts or (str(hint).strip(),):
            normalized = normalize_label(part)
            if normalized and normalized not in values:
                values.append(normalized)
    return tuple(values)


def _candidate(value: Mapping[str, Any], rank: Optional[int] = None) -> Optional[CategoryCandidate]:
    leaf_id = (
        value.get("leftCid")
        or value.get("leaf_id")
        or value.get("categoryId")
        or value.get("cid")
    )
    leaf_name = value.get("leftName") or value.get("leafName") or value.get("name")
    path = _parts(value.get("cidNames") or value.get("path") or value.get("categoryPath"))
    if leaf_name and (not path or normalize_label(path[-1]) != normalize_label(leaf_name)):
        path += (str(leaf_name),)
    if leaf_id in (None, ""):
        return None
    if not path:
        path = (str(leaf_name or leaf_id),)
    score = value.get("score")
    try:
        score = None if score is None else float(score)
    except (TypeError, ValueError):
        score = None
    return CategoryCandidate(
        leaf_id=str(leaf_id),
        path=path,
        rank=rank,
        score=score,
        recommended=rank is not None,
        validation_status="unverified",
    )


def _control_type(value: Mapping[str, Any]) -> str:
    type_name = str(
        value.get("propertyValueType")
        or value.get("inputType")
        or value.get("type")
        or ""
    ).lower()
    maximum = value.get("chooseMaxNum")
    if "multi" in type_name or isinstance(maximum, int) and maximum > 1:
        return "select_many"
    if "select" in type_name or value.get("values"):
        return "select_one"
    if "bool" in type_name:
        return "checkbox"
    return "text"


def _property_fields(
    values: Iterable[Any],
    *,
    section: str,
    prefix: str,
    api_path: str,
    source: str = "api",
) -> Tuple[FieldSchema, ...]:
    result = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            continue
        source_id = value.get("refPid") or value.get("id") or value.get("propId")
        label = value.get("name") or value.get("label")
        if not label:
            continue
        options = _items(value.get("values")) or _items(value.get("options"))
        control_type = _control_type(value)
        required = value.get("required")
        result.append(
            FieldSchema(
                schema_key="{0}:{1}".format(prefix, source_id or index),
                source_id=None if source_id in (None, "") else str(source_id),
                label=str(label),
                section=section,
                control_type=control_type,
                required=required if isinstance(required, bool) else None,
                multiple=True if control_type == "select_many" else False if control_type == "select_one" else None,
                custom_allowed=(
                    value.get("canNote")
                    if isinstance(value.get("canNote"), bool)
                    else None
                ),
                option_summary=option_summary(options, source=source) if options else None,
                option_values=field_options(options, source=source),
                api_paths=(api_path,),
            )
        )
    return tuple(result)


def parse_pdd_attribute_fields(payload: Any) -> Tuple[FieldSchema, ...]:
    """Parse authoritative PDD category-property candidates from raw JSON."""
    matches = {}

    def visit(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("{", "[")):
                try:
                    visit(json.loads(text))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            return
        if isinstance(value, (tuple, list)):
            for child in value:
                visit(child)
            return
        if not isinstance(value, Mapping):
            return
        if "goodsPropertiesRule" in value:
            fields = _property_fields(
                _nested_rule_items(
                    value.get("goodsPropertiesRule"),
                    ("properties",),
                ),
                section="category_properties",
                prefix="pdd:property",
                api_path=_PROPERTIES.path,
            )
            for field in fields:
                matches[(str(field.source_id or ""), field.schema_key)] = field
        for child in value.values():
            visit(child)

    visit(payload)
    return tuple(matches.values())


def _descriptor_fields(values: Iterable[Any]) -> Tuple[FieldSchema, ...]:
    return _property_fields(
        values,
        section="fixed",
        prefix="pdd:fixed",
        api_path=_DETAIL.path,
    )


def _nested_rule_items(rule: Any, keys: Tuple[str, ...]) -> Tuple[Any, ...]:
    if isinstance(rule, (tuple, list)):
        return tuple(rule)
    if not isinstance(rule, Mapping):
        return ()
    for key in keys:
        values = _items(rule.get(key))
        if values:
            return values
    mapped = []
    for source_id, value in rule.items():
        if not isinstance(value, Mapping):
            continue
        normalized = dict(value)
        normalized.setdefault("refPid", source_id)
        mapped.append(normalized)
    return tuple(mapped)


def _service_rule_items(rule: Any) -> Tuple[Any, ...]:
    if isinstance(rule, (tuple, list)):
        return tuple(rule)
    if not isinstance(rule, Mapping):
        return ()

    result = []
    rule_map = _mapping(rule.get("goodsServiceRuleMap"))
    for source_id, value in rule_map.items():
        normalized = dict(value) if isinstance(value, Mapping) else {}
        normalized.setdefault("refPid", source_id)
        normalized.setdefault(
            "name",
            normalized.get("serviceName")
            or normalized.get("goodsServiceName")
            or normalized.get("label")
            or str(source_id),
        )
        if isinstance(value, bool):
            normalized.setdefault("required", value)
        result.append(normalized)

    for index, value in enumerate(_items(rule.get("goodsTypeList"))):
        if isinstance(value, Mapping):
            normalized = dict(value)
            source_id = (
                normalized.get("goodsType")
                or normalized.get("id")
                or "goods-type-{0}".format(index)
            )
            normalized.setdefault("refPid", source_id)
            normalized.setdefault(
                "name",
                normalized.get("goodsTypeName")
                or normalized.get("typeName")
                or normalized.get("label")
                or str(source_id),
            )
        else:
            normalized = {
                "refPid": "goods-type-{0}".format(index),
                "name": str(value),
            }
        result.append(normalized)

    if result:
        return tuple(result)
    return _nested_rule_items(
        rule,
        ("services", "serviceRules", "rules", "properties"),
    )


def _rule_has_content(rule: Any, property_rule: bool = False) -> bool:
    if isinstance(rule, (tuple, list)):
        return bool(rule)
    if not isinstance(rule, Mapping) or not rule:
        return False
    if property_rule:
        return bool(_items(rule.get("properties")))
    for value in rule.values():
        if isinstance(value, Mapping) and value:
            return True
        if isinstance(value, (tuple, list)) and value:
            return True
        if value not in (None, "", (), [], {}):
            return True
    return False


_CONSTRAINT_LABELS = {
    "maxSpecNum": "最大销售规格数",
    "minSpecNum": "最小销售规格数",
    "minPrice": "最低价格限制",
    "maxPrice": "最高价格限制",
    "required": "必填约束",
    "enabled": "是否启用",
    "minimum": "最低件数",
}


def _constraint_fields(
    rule: Any,
    *,
    section: str,
    prefix: str,
    api_path: str,
) -> Tuple[FieldSchema, ...]:
    if isinstance(rule, (tuple, list)):
        return _property_fields(
            rule,
            section=section,
            prefix=prefix,
            api_path=api_path,
        )
    if not isinstance(rule, Mapping):
        return ()
    fields = []
    for key, value in rule.items():
        if key in ("properties", "services", "serviceRules", "rules"):
            continue
        if isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                if isinstance(nested_value, (Mapping, tuple, list)):
                    continue
                source_id = "{0}.{1}".format(key, nested_key)
                fields.append(
                    FieldSchema(
                        schema_key="{0}:{1}".format(prefix, source_id),
                        source_id=source_id,
                        label=_CONSTRAINT_LABELS.get(nested_key, str(nested_key)),
                        section=section,
                        control_type=(
                            "checkbox"
                            if isinstance(nested_value, bool)
                            else "number"
                            if isinstance(nested_value, (int, float))
                            else "text"
                        ),
                        value_type=(
                            "boolean"
                            if isinstance(nested_value, bool)
                            else "number"
                            if isinstance(nested_value, (int, float))
                            else "string"
                        ),
                        api_paths=(api_path,),
                    )
                )
            continue
        if isinstance(value, (tuple, list)):
            continue
        fields.append(
            FieldSchema(
                schema_key="{0}:{1}".format(prefix, key),
                source_id=str(key),
                label=_CONSTRAINT_LABELS.get(str(key), str(key)),
                section=section,
                control_type=(
                    "checkbox"
                    if isinstance(value, bool)
                    else "number"
                    if isinstance(value, (int, float))
                    else "text"
                ),
                value_type=(
                    "boolean"
                    if isinstance(value, bool)
                    else "number"
                    if isinstance(value, (int, float))
                    else "string"
                ),
                api_paths=(api_path,),
            )
        )
    return tuple(fields)


def _prediction_descriptors(payload: Any) -> Tuple[Mapping[str, Any], ...]:
    payload = _mapping(payload)
    direct = _items(payload.get("properties"))
    if direct:
        return tuple(value for value in direct if isinstance(value, Mapping))

    encoded = payload.get("platformCatProp")
    if isinstance(encoded, str):
        try:
            encoded = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            encoded = ()
    descriptors = []
    if isinstance(encoded, Mapping):
        encoded_values = _items(encoded.get("properties"))
        if encoded_values:
            encoded = encoded_values
        else:
            mapped = []
            for source_id, value in encoded.items():
                if isinstance(value, Mapping):
                    normalized = dict(value)
                    normalized.setdefault("refPid", source_id)
                    mapped.append(normalized)
            encoded = tuple(mapped)
    for value in _items(encoded):
        if not isinstance(value, Mapping):
            continue
        descriptors.append(
            {
                key: value.get(key)
                for key in (
                    "refPid",
                    "id",
                    "name",
                    "label",
                    "type",
                    "inputType",
                    "required",
                    "values",
                    "options",
                    "chooseMaxNum",
                    "canNote",
                )
                if key in value
            }
        )

    value_map = _mapping(payload.get("platformCatPropVoMap"))
    by_id = {
        str(value.get("refPid") or value.get("id")): value
        for value in descriptors
        if value.get("refPid") not in (None, "") or value.get("id") not in (None, "")
    }
    for source_id, value in value_map.items():
        value = _mapping(value)
        descriptor = by_id.get(str(source_id))
        if descriptor is None:
            descriptor = {
                "refPid": str(source_id),
                "name": value.get("name") or value.get("label") or str(source_id),
            }
            descriptors.append(descriptor)
            by_id[str(source_id)] = descriptor
        options = _items(value.get("values")) or _items(value.get("options"))
        if options:
            descriptor["values"] = options
    return tuple(descriptors)


async def _panel_call(panel: Any, name: str) -> Any:
    method = getattr(panel, name, None) if panel is not None else None
    if not callable(method):
        return None
    result = method()
    return await result if inspect.isawaitable(result) else result


async def _activate(panel: Any, candidate: CategoryCandidate) -> None:
    if panel is None:
        raise PlatformDiscoveryError("category panel is unavailable")
    method = getattr(panel, "activate_category", None)
    if callable(method):
        result = method(candidate)
        if inspect.isawaitable(result):
            await result
        return
    raise PlatformDiscoveryError("panel does not support category activation")


class PddListing:
    spec = get_platform_spec("pdd")
    endpoint_catalog = (
        _DETAIL,
        _ITEM_PICTURE_QUERY,
        _GROUP_SPEC_NAMES,
        _PREDICTION,
        _SPECS,
        _BRAND_RULE,
        _PROPERTIES,
        _PREDICTED_PROPERTIES,
    )
    label_aliases = {"商品名称": ("商品标题",), "服务承诺": ("商品服务",)}

    def __init__(self, transport: Any = None, panel: Any = None) -> None:
        self.panel = panel
        self.generation_tracker = GenerationTracker()
        self._api = ApiClient(
            page=panel,
            endpoint_catalog=self.endpoint_catalog,
            transport=transport,
        )
        self._detail: Mapping[str, Any] = {}
        self._resolution_fragment = SchemaFragment()
        self._candidate_payloads = {}

    @property
    def resolution_fragment(self) -> SchemaFragment:
        return self._resolution_fragment

    def _panel(self, panel: Any) -> Any:
        return panel if panel is not None else self.panel

    def _client(self, api: Optional[ApiClient]) -> ApiClient:
        return api if api is not None else self._api

    def _shop_id(self, panel: Any) -> str:
        panel = self._panel(panel)
        for key in ("shop_id", "shopId"):
            value = getattr(panel, key, None) if panel is not None else None
            if value not in (None, ""):
                return str(value)
        for key in ("shopId", "shop_id"):
            value = self._detail.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    async def capture_fixed(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: Optional[ApiClient],
    ) -> SchemaFragment:
        detail, observation = await self._client(api).request(
            _DETAIL,
            {
                "api_name": "pdd_detail",
                "baseItemId": context.base_item_id or "",
            },
        )
        self._detail = _mapping(detail)
        fields = _descriptor_fields(_items(self._detail.get("fieldDescriptorList")))
        return SchemaFragment(
            sections=(SectionSchema(key="fixed", label="固定字段", fields=fields),),
            endpoints=(observation,),
        )

    async def _existing(self, panel: Any) -> Optional[CategoryCandidate]:
        value = await _panel_call(self._panel(panel), "get_existing_category")
        if isinstance(value, CategoryCandidate):
            return value
        if isinstance(value, Mapping):
            return _candidate(value)
        if value not in (None, ""):
            return CategoryCandidate(
                leaf_id=str(value),
                path=(str(value),),
                validation_status="validated",
            )
        return _candidate(self._detail) if self._detail else None

    async def _probe_candidate(
        self,
        api: ApiClient,
        panel: Any,
        candidate: CategoryCandidate,
        observations: list,
    ) -> bool:
        leaf_id = candidate.leaf_id
        properties, property_observation = await api.request(
            _PROPERTIES,
            {
                "api_name": "pdd_getCategoryProperties",
                "leafCategoryId": leaf_id,
                "shopId": self._shop_id(panel),
            },
        )
        observations.append(property_observation)
        if property_observation.status != "ok" or not isinstance(properties, Mapping):
            self._candidate_payloads[leaf_id] = {
                "properties": None,
                "observations": (property_observation,),
            }
            return False
        critical_rules_complete = (
            _rule_has_content(properties.get("goodsPropertiesRule"), property_rule=True)
            and _rule_has_content(properties.get("goodsServiceRule"))
            and _rule_has_content(properties.get("goodsSkuRule"))
        )
        if not critical_rules_complete:
            self._candidate_payloads[leaf_id] = {
                "properties": None,
                "observations": (property_observation,),
            }
            return False

        specs, spec_observation = await api.request(
            _SPECS,
            {
                "api_name": "pdd_getSpecList",
                "leafCategoryId": leaf_id,
                "shopId": self._shop_id(panel),
            },
        )
        brands, brand_observation = await api.request(
            _BRAND_RULE,
            {
                "api_name": "pdd_getBrandRequireRule",
                "leafCategoryId": leaf_id,
                "shopIds": self._shop_id(panel),
            },
        )
        observations.extend((spec_observation, brand_observation))
        viable = (
            spec_observation.status == "ok"
            and brand_observation.status == "ok"
            and _rule_has_content(specs)
            and _rule_has_content(brands)
        )
        self._candidate_payloads[leaf_id] = {
            "properties": properties,
            "specs": specs,
            "brands": brands,
            "observations": (
                property_observation,
                spec_observation,
                brand_observation,
            ),
        }
        return viable

    async def resolve_category(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: Optional[ApiClient],
    ) -> CategoryResolution:
        existing = await self._existing(panel)
        if existing is not None:
            self._resolution_fragment = SchemaFragment()
            return CategoryResolution(
                status="resolved",
                source="existing",
                selected=existing,
                candidates=(existing,),
            )

        api = self._client(api)
        prediction, prediction_observation = await api.request(
            _PREDICTION,
            {
                "api_name": "publish_fast_prediction_cat",
                "baseItemId": context.base_item_id or "",
                "platformType": "pdd",
                "title": context.title,
            },
        )
        candidates = tuple(
            item
            for item in (
                _candidate(value, rank=index)
                for index, value in enumerate(
                    _items(_mapping(prediction).get("catList")),
                    1,
                )
                if isinstance(value, Mapping)
            )
            if item is not None
        )
        observations = [prediction_observation]
        issues = []
        hints = _hint_leaves(context)
        first_viable = False
        if candidates:
            first_viable = await self._probe_candidate(
                api,
                panel,
                candidates[0],
                observations,
            )
            if not first_viable:
                issues.append("candidate_unavailable:{0}".format(candidates[0].leaf_id))
            if not hints or normalize_label(candidates[0].path[-1]) not in hints:
                issues.append("candidate_hint_mismatch:{0}".format(candidates[0].leaf_id))

        exact = tuple(
            item
            for item in candidates
            if hints and normalize_label(item.path[-1]) in hints
        )
        if len(exact) > 1:
            self._resolution_fragment = SchemaFragment(
                endpoints=tuple(observations),
                issues=tuple(issues),
            )
            return CategoryResolution(
                status="review_required",
                source="recommendation",
                candidates=exact,
                reason="ambiguous_exact_leaf",
            )

        tree_candidates = ()
        selected_source = "recommendation"
        if len(exact) == 1:
            selected = exact[0]
        else:
            tree = await _panel_call(self._panel(panel), "list_category_tree")
            tree_candidates = tuple(
                item
                for item in (
                    value
                    if isinstance(value, CategoryCandidate)
                    else _candidate(value)
                    for value in _items(tree)
                    if isinstance(value, (CategoryCandidate, Mapping))
                )
                if item is not None
            )
            tree_exact = tuple(
                item
                for item in tree_candidates
                if hints and normalize_label(item.path[-1]) in hints
            )
            if len(tree_exact) != 1:
                self._resolution_fragment = SchemaFragment(
                    endpoints=tuple(observations),
                    issues=tuple(issues),
                )
                return CategoryResolution(
                    status="review_required",
                    source="tree" if tree_candidates else "recommendation",
                    candidates=tree_exact or tree_candidates or candidates,
                    reason=(
                        "ambiguous_exact_leaf"
                        if len(tree_exact) > 1
                        else "no_exact_leaf"
                    ),
                )
            selected = tree_exact[0]
            selected_source = "tree"

        if candidates and selected.leaf_id == candidates[0].leaf_id:
            selected_viable = first_viable
        else:
            selected_viable = await self._probe_candidate(
                api,
                panel,
                selected,
                observations,
            )
        if not selected_viable:
            unavailable_issue = "candidate_unavailable:{0}".format(selected.leaf_id)
            if unavailable_issue not in issues:
                issues.append(unavailable_issue)
            if selected_source == "recommendation":
                tree = await _panel_call(self._panel(panel), "list_category_tree")
                tree_candidates = tuple(
                    item
                    for item in (
                        value
                        if isinstance(value, CategoryCandidate)
                        else _candidate(value)
                        for value in _items(tree)
                        if isinstance(value, (CategoryCandidate, Mapping))
                    )
                    if item is not None
                )
                alternate_exact = tuple(
                    item
                    for item in tree_candidates
                    if hints
                    and normalize_label(item.path[-1]) in hints
                    and item.leaf_id != selected.leaf_id
                )
                if len(alternate_exact) == 1:
                    alternate_viable = await self._probe_candidate(
                        api,
                        panel,
                        alternate_exact[0],
                        observations,
                    )
                    if alternate_viable:
                        selected = alternate_exact[0]
                        selected_source = "tree"
                        selected_viable = True
            if selected_viable:
                selected = replace(selected, validation_status="validated")
                self._resolution_fragment = SchemaFragment(
                    endpoints=tuple(observations),
                    issues=tuple(issues),
                )
                return CategoryResolution(
                    status="resolved",
                    source=selected_source,
                    selected=selected,
                    candidates=candidates + tree_candidates,
                )
            self._resolution_fragment = SchemaFragment(
                endpoints=tuple(observations),
                issues=tuple(issues),
            )
            return CategoryResolution(
                status="review_required",
                source="tree" if tree_candidates else selected_source,
                candidates=tree_candidates or (selected,),
                reason="candidate_not_viable",
            )

        selected = replace(selected, validation_status="validated")
        self._resolution_fragment = SchemaFragment(
            endpoints=tuple(observations),
            issues=tuple(issues),
        )
        return CategoryResolution(
            status="resolved",
            source=selected_source,
            selected=selected,
            candidates=candidates + tree_candidates,
        )

    async def activate_category(
        self,
        context: DiscoveryContext,
        panel: Any,
        candidate: CategoryCandidate,
    ) -> None:
        await _activate(self._panel(panel), candidate)

    async def capture_dynamic(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: Optional[ApiClient],
        category: CategoryResolution,
    ) -> SchemaFragment:
        if category.selected is None:
            raise PlatformDiscoveryError("dynamic discovery requires a resolved category")
        generation = self.generation_tracker.current_generation or None
        api = self._client(api)
        leaf_id = category.selected.leaf_id
        observations = []
        cached = self._candidate_payloads.get(leaf_id)
        if not cached or cached.get("properties") is None:
            candidate = category.selected
            await self._probe_candidate(api, panel, candidate, observations)
            cached = self._candidate_payloads.get(leaf_id, {})
        properties = _mapping(cached.get("properties"))
        specs = cached.get("specs")
        brands = cached.get("brands")
        observations.extend(cached.get("observations") or ())

        prediction, prediction_observation = await api.request(
            _PREDICTED_PROPERTIES,
            {
                "api_name": "publish_fast_prediction_cat_prop",
                "baseItemId": context.base_item_id or "",
                "catId": leaf_id,
                "platformType": "pdd",
                "shopId": self._shop_id(panel),
            },
        )
        observations.append(prediction_observation)

        spec_fields = tuple(
            FieldSchema(
                schema_key="pdd:spec:{0}".format(
                    value.get("parentSpecId") or index
                ),
                source_id=str(value.get("parentSpecId") or index),
                label=str(value.get("parentSpecName") or value.get("name") or "规格"),
                section="specifications",
                control_type="select_many",
                multiple=True,
                api_paths=(_SPECS.path,),
            )
            for index, value in enumerate(_items(specs))
            if isinstance(value, Mapping)
        )
        brand_required = any(
            value is True for value in _mapping(brands).values()
        )
        brand_field = FieldSchema(
            schema_key="pdd:brand-requirement",
            source_id="brand",
            label="品牌",
            section="brand_requirement",
            control_type="select_one",
            required=brand_required,
            api_paths=(_BRAND_RULE.path,),
        )
        property_values = _nested_rule_items(
            properties.get("goodsPropertiesRule"),
            ("properties",),
        )
        property_fields = _property_fields(
            property_values,
            section="category_properties",
            prefix="pdd:property",
            api_path=_PROPERTIES.path,
        )
        service_values = _service_rule_items(properties.get("goodsServiceRule"))
        service_fields = _property_fields(
            service_values,
            section="service_rules",
            prefix="pdd:service",
            api_path=_PROPERTIES.path,
        )
        sku_rule = properties.get("goodsSkuRule")
        sku_fields = _constraint_fields(
            sku_rule,
            section="sku_rules",
            prefix="pdd:sku-rule",
            api_path=_PROPERTIES.path,
        )
        spu_rule = properties.get("spuRule")
        spu_fields = _property_fields(
            _nested_rule_items(spu_rule, ("properties",)),
            section="spu_rules",
            prefix="pdd:spu-property",
            api_path=_PROPERTIES.path,
        ) + _constraint_fields(
            spu_rule,
            section="spu_rules",
            prefix="pdd:spu-rule",
            api_path=_PROPERTIES.path,
        )
        discount_fields = _constraint_fields(
            properties.get("twoPiecesDiscountRule"),
            section="two_pieces_discount",
            prefix="pdd:two-pieces-discount",
            api_path=_PROPERTIES.path,
        )
        predicted_fields = _property_fields(
            _prediction_descriptors(prediction),
            section="prediction_properties",
            prefix="pdd:prediction",
            api_path=_PREDICTED_PROPERTIES.path,
            source="prediction",
        )
        return SchemaFragment(
            sections=(
                SectionSchema(key="specifications", label="销售规格", order=1, fields=spec_fields),
                SectionSchema(key="brand_requirement", label="品牌要求", order=2, fields=(brand_field,)),
                SectionSchema(key="category_properties", label="类目属性", order=3, fields=property_fields),
                SectionSchema(key="service_rules", label="商品服务规则", order=4, fields=service_fields),
                SectionSchema(key="sku_rules", label="SKU规则", order=5, fields=sku_fields),
                SectionSchema(key="spu_rules", label="SPU规则", order=6, fields=spu_fields),
                SectionSchema(key="two_pieces_discount", label="两件折扣规则", order=7, fields=discount_fields),
                SectionSchema(key="prediction_properties", label="预测属性证据", order=8, fields=predicted_fields),
            ),
            endpoints=tuple(observations),
            issues=self._resolution_fragment.issues,
            generation=generation,
        )


__all__ = ["PddListing", "parse_pdd_attribute_fields"]
