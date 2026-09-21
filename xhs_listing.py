from __future__ import annotations

import inspect
import re
from typing import Any, Dict, Mapping, Optional, Tuple

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
    "/xhs/detail.json",
    ("baseItemId", "api_name"),
    required_for="fixed",
    read_only=True,
)
_LOGISTICS = EndpointSpec(
    "GET",
    "/xhs/getLogisticsList.json",
    ("shopId", "api_name"),
    required_for="fixed",
    read_only=True,
)
_FREIGHT = EndpointSpec(
    "GET",
    "/xhs/getCarriageTemplateList.json",
    ("shopId", "api_name"),
    required_for="fixed",
    read_only=True,
)
_TREE = EndpointSpec(
    "GET",
    "/xhs/getCategoryTree.json",
    ("parentId", "endLevel", "api_name"),
    required_for="category",
    read_only=True,
)
_VARIATIONS = EndpointSpec(
    "GET",
    "/xhs/getVariations.json",
    ("leafCategoryId", "shopId", "api_name"),
    required_for="dynamic",
    read_only=True,
)
_ATTRIBUTES = EndpointSpec(
    "GET",
    "/xhs/getAttributeList.json",
    ("leafCategoryId", "shopId", "api_name"),
    required_for="dynamic",
    read_only=True,
)
_ATTRIBUTE_VALUES = EndpointSpec(
    "GET",
    "/xhs/getAttributeValues.json",
    ("shopId", "attributeId", "api_name"),
    required_for="dynamic",
    read_only=True,
)
_DELIVERY = EndpointSpec(
    "GET",
    "/xhs/getDeliveryRule.json",
    ("shopId", "categoryId", "logisticsPlanId", "api_name"),
    required_for="dynamic",
    read_only=True,
)

_TREE_REQUEST_BUDGET = 128
_TREE_NODE_SCAN_BUDGET = 25000
_TREE_REPORT_BUDGET = 128
_TREE_END_LEVEL = 4


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
        item.strip()
        for item in re.split(r"[/／>＞,，]+", str(value))
        if item.strip()
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


def _candidate(
    value: Mapping[str, Any],
    path: Tuple[str, ...] = (),
) -> Optional[CategoryCandidate]:
    leaf_id = (
        value.get("leaf_id")
        or value.get("categoryId")
        or value.get("cid")
        or value.get("id")
    )
    explicit_path = _parts(value.get("path") or value.get("categoryPath"))
    name = value.get("name") or value.get("categoryName")
    candidate_path = explicit_path or path
    if name and (
        not candidate_path
        or normalize_label(candidate_path[-1]) != normalize_label(name)
    ):
        candidate_path += (str(name),)
    if leaf_id in (None, ""):
        return None
    if not candidate_path:
        candidate_path = (str(leaf_id),)
    return CategoryCandidate(
        leaf_id=str(leaf_id),
        path=candidate_path,
        validation_status="validated",
    )


def _is_leaf(value: Mapping[str, Any]) -> bool:
    marker = value.get("leaf")
    if marker is None:
        marker = value.get("isLeaf")
    if isinstance(marker, bool):
        return marker
    if isinstance(marker, int):
        return marker == 1
    if isinstance(marker, str):
        return marker.strip().lower() in ("1", "true")
    return False


def _descriptor_fields(values: Any) -> Tuple[FieldSchema, ...]:
    fields = []
    for index, value in enumerate(_items(values)):
        if not isinstance(value, Mapping):
            continue
        source_id = value.get("id") or value.get("attributeId") or value.get("name")
        label = value.get("name") or value.get("label")
        if not label:
            continue
        type_name = str(value.get("type") or value.get("inputType") or "").lower()
        fields.append(
            FieldSchema(
                schema_key="xhs:fixed:{0}".format(source_id or index),
                source_id=None if source_id in (None, "") else str(source_id),
                label=str(label),
                section="fixed",
                control_type="select_one" if "select" in type_name else "text",
                required=(
                    value.get("required")
                    if isinstance(value.get("required"), bool)
                    else None
                ),
                api_paths=(_DETAIL.path,),
            )
        )
    return tuple(fields)


def normalize_xhs_attribute_options(values: Any) -> Tuple[Any, ...]:
    """Normalize legacy id/name and current valueId/valueName candidates."""
    normalized = []
    for value in _items(values):
        if not isinstance(value, Mapping):
            normalized.append(value)
            continue
        item = dict(value)
        if item.get("id") in (None, "") and item.get("valueId") not in (None, ""):
            item["id"] = item.get("valueId")
        if item.get("name") in (None, "") and item.get("valueName") not in (None, ""):
            item["name"] = item.get("valueName")
        normalized.append(item)
    return tuple(normalized)


def _delivery_rule_options(values: Tuple[Any, ...]) -> Tuple[Any, ...]:
    normalized = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            normalized.append(value)
            continue
        identity_parts = tuple(
            "{0}={1}".format(key, value[key])
            for key in ("id", "timeType", "value", "min", "max")
            if value.get(key) not in (None, "")
        )
        identity = "|".join(identity_parts) or "rule-{0}".format(index)
        normalized.append(
            {
                "id": identity,
                "name": str(value.get("name") or identity),
            }
        )
    return tuple(normalized)


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


async def _existing_from_panel(
    panel: Any,
) -> Tuple[Optional[CategoryCandidate], Optional[str]]:
    method = getattr(panel, "get_existing_category", None) if panel is not None else None
    if not callable(method):
        return None, None
    value = method()
    value = await value if inspect.isawaitable(value) else value
    if isinstance(value, CategoryCandidate):
        return value, None
    if isinstance(value, Mapping):
        xhs_id = value.get("xhsCid")
        return (
            _candidate(value),
            None if xhs_id in (None, "") else str(xhs_id),
        )
    if value not in (None, ""):
        return (
            CategoryCandidate(
                leaf_id=str(value),
                path=(str(value),),
                validation_status="validated",
            ),
            None,
        )
    return None, None


class XhsListing:
    spec = get_platform_spec("xhs")
    endpoint_catalog = (
        _DETAIL,
        _LOGISTICS,
        _FREIGHT,
        _TREE,
        _VARIATIONS,
        _ATTRIBUTES,
        _ATTRIBUTE_VALUES,
        _DELIVERY,
    )
    label_aliases = {"运费模板": ("运费模版",), "发货规则": ("配送规则",)}

    def __init__(self, transport: Any = None, panel: Any = None) -> None:
        self.panel = panel
        self.generation_tracker = GenerationTracker()
        self._api = ApiClient(
            page=panel,
            endpoint_catalog=self.endpoint_catalog,
            transport=transport,
        )
        self._detail: Mapping[str, Any] = {}
        self._delivery_category_ids: Dict[str, str] = {}
        self._resolution_fragment = SchemaFragment()

    @property
    def resolution_fragment(self) -> SchemaFragment:
        return self._resolution_fragment

    def _client(self, api: Optional[ApiClient]) -> ApiClient:
        return api if api is not None else self._api

    def _panel(self, panel: Any) -> Any:
        return panel if panel is not None else self.panel

    def _identity(self, key: str, panel: Any = None) -> str:
        value = self._detail.get(key)
        if value not in (None, ""):
            return str(value)
        panel = self._panel(panel)
        identities = getattr(panel, "runtime_identities", {}) if panel is not None else {}
        if isinstance(identities, Mapping):
            value = identities.get(key)
            if value not in (None, ""):
                return str(value)
        attribute_names = {
            "shopId": ("shop_id", "shopId"),
        }.get(key, (key,))
        for attribute_name in attribute_names:
            value = getattr(panel, attribute_name, None) if panel is not None else None
            if value not in (None, ""):
                return str(value)
        return ""

    async def capture_fixed(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: Optional[ApiClient],
    ) -> SchemaFragment:
        api = self._client(api)
        detail, detail_observation = await api.request(
            _DETAIL,
            {"baseItemId": context.base_item_id or "", "api_name": "xhs_detail"},
        )
        self._detail = _mapping(detail)
        self._delivery_category_ids = {}
        detail_candidate = _candidate(self._detail)
        detail_xhs_id = self._detail.get("xhsCid")
        if detail_candidate is not None and detail_xhs_id not in (None, ""):
            self._delivery_category_ids[detail_candidate.leaf_id] = str(detail_xhs_id)
        shop_id = self._identity("shopId", panel)
        logistics, logistics_observation = await api.request(
            _LOGISTICS,
            {"shopId": shop_id, "api_name": "xhs_getLogisticsList"},
        )
        freight, freight_observation = await api.request(
            _FREIGHT,
            {"shopId": shop_id, "api_name": "xhs_getCarriageTemplateList"},
        )
        fields = _descriptor_fields(self._detail.get("fieldDescriptorList"))
        fields += (
            FieldSchema(
                schema_key="xhs:logistics",
                source_id="logistics-plan",
                label="物流方案",
                section="fixed",
                control_type="select_one",
                option_summary=option_summary(_items(logistics), source="logistics"),
                option_values=field_options(_items(logistics), source="logistics"),
                api_paths=(_LOGISTICS.path,),
            ),
            FieldSchema(
                schema_key="xhs:freight",
                source_id="freight-template",
                label="运费模板",
                section="fixed",
                control_type="select_one",
                option_summary=option_summary(_items(freight), source="freight"),
                option_values=field_options(_items(freight), source="freight"),
                api_paths=(_FREIGHT.path,),
            ),
        )
        return SchemaFragment(
            sections=(SectionSchema(key="fixed", label="固定字段", fields=fields),),
            endpoints=(detail_observation, logistics_observation, freight_observation),
        )

    async def _walk_tree(
        self,
        api: ApiClient,
        parent_id: str,
        path: Tuple[str, ...],
        observations: list,
        state: Mapping[str, Any],
        depth: int = 0,
    ) -> Tuple[CategoryCandidate, ...]:
        if state["request_count"] >= _TREE_REQUEST_BUDGET:
            self._tree_issue(state, "category_tree_request_budget_exceeded")
            return ()
        state["request_count"] += 1
        payload, observation = await api.request(
            _TREE,
            {
                "parentId": parent_id,
                "endLevel": _TREE_END_LEVEL,
                "api_name": "xhs_getCategoryTree",
            },
        )
        observations.append(observation)
        values = _items(payload) or _items(_mapping(payload).get("list"))
        return await self._consume_tree_values(
            api,
            values,
            path,
            observations,
            state,
            depth,
        )

    @staticmethod
    def _tree_issue(state: Mapping[str, Any], issue: str) -> None:
        if issue not in state["issues"]:
            state["issues"].append(issue)

    def _remember_delivery_id(
        self,
        value: Mapping[str, Any],
        candidate: CategoryCandidate,
    ) -> None:
        xhs_id = value.get("xhsCid")
        if xhs_id not in (None, ""):
            self._delivery_category_ids[candidate.leaf_id] = str(xhs_id)

    async def _consume_tree_values(
        self,
        api: ApiClient,
        values: Tuple[Any, ...],
        path: Tuple[str, ...],
        observations: list,
        state: Mapping[str, Any],
        depth: int,
    ) -> Tuple[CategoryCandidate, ...]:
        candidates = []
        for value in values:
            if not isinstance(value, Mapping):
                continue
            if state["node_count"] >= _TREE_NODE_SCAN_BUDGET:
                self._tree_issue(state, "category_tree_node_budget_exceeded")
                break
            state["node_count"] += 1
            name = str(value.get("name") or value.get("categoryName") or "").strip()
            child_path = path + ((name,) if name else ())
            child_id = value.get("cid") or value.get("categoryId") or value.get("id")
            if _is_leaf(value):
                candidate = _candidate(value, child_path)
                if candidate is not None:
                    self._remember_delivery_id(value, candidate)
                    candidates.append(candidate)
                continue
            if child_id in (None, ""):
                continue
            child_key = str(child_id)
            if child_key in state["visited"]:
                self._tree_issue(state, "category_tree_cycle_detected")
                continue
            state["visited"].add(child_key)
            nested_children = _items(value.get("children"))
            if nested_children:
                candidates.extend(
                    await self._consume_tree_values(
                        api,
                        nested_children,
                        child_path,
                        observations,
                        state,
                        depth + 1,
                    )
                )
            else:
                candidates.extend(
                    await self._walk_tree(
                        api,
                        child_key,
                        child_path,
                        observations,
                        state,
                        depth + 1,
                    )
                )
        return tuple(candidates)

    async def resolve_category(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: Optional[ApiClient],
    ) -> CategoryResolution:
        existing, existing_xhs_id = await _existing_from_panel(self._panel(panel))
        if existing is None and self._detail.get("categoryId"):
            existing = _candidate(self._detail)
            detail_xhs_id = self._detail.get("xhsCid")
            existing_xhs_id = (
                None if detail_xhs_id in (None, "") else str(detail_xhs_id)
            )
        if existing is not None:
            if existing_xhs_id is not None:
                self._delivery_category_ids[existing.leaf_id] = existing_xhs_id
            self._resolution_fragment = SchemaFragment()
            return CategoryResolution(
                status="resolved",
                source="existing",
                selected=existing,
                candidates=(existing,),
            )
        observations = []
        state = {
            "visited": set(),
            "request_count": 0,
            "node_count": 0,
            "issues": [],
        }
        candidates = await self._walk_tree(
            self._client(api),
            "0",
            (),
            observations,
            state,
        )
        hints = _hint_leaves(context)
        exact = tuple(
            candidate
            for candidate in candidates
            if hints and normalize_label(candidate.path[-1]) in hints
        )
        blocking_issues = tuple(
            issue
            for issue in state["issues"]
            if issue != "category_tree_report_budget_exceeded"
        )
        if len(exact) > _TREE_REPORT_BUDGET:
            self._tree_issue(state, "category_tree_report_budget_exceeded")
            exact = exact[:_TREE_REPORT_BUDGET]
        elif not exact and len(candidates) > _TREE_REPORT_BUDGET:
            self._tree_issue(state, "category_tree_report_budget_exceeded")
        reported_candidates = exact or candidates[:_TREE_REPORT_BUDGET]
        self._resolution_fragment = SchemaFragment(
            endpoints=tuple(observations),
            issues=tuple(state["issues"]),
        )
        if len(exact) == 1 and not blocking_issues:
            return CategoryResolution(
                status="resolved",
                source="tree",
                selected=exact[0],
                candidates=reported_candidates,
            )
        return CategoryResolution(
            status="review_required",
            source="tree",
            candidates=reported_candidates,
            reason="ambiguous_exact_leaf" if len(exact) > 1 else "no_exact_leaf",
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
        shop_id = self._identity("shopId", panel)
        leaf_id = category.selected.leaf_id
        attributes, attributes_observation = await api.request(
            _ATTRIBUTES,
            {
                "leafCategoryId": leaf_id,
                "shopId": shop_id,
                "api_name": "xhs_getAttributeList",
            },
        )
        observations = [attributes_observation]
        attribute_fields = []
        for index, value in enumerate(_items(_mapping(attributes).get("attributeV3s"))):
            if not isinstance(value, Mapping):
                continue
            attribute_id = value.get("id")
            if attribute_id in (None, ""):
                continue
            values, values_observation = await api.request(
                _ATTRIBUTE_VALUES,
                {
                    "shopId": shop_id,
                    "attributeId": str(attribute_id),
                    "api_name": "xhs_getAttributeValues",
                },
            )
            observations.append(values_observation)
            values_mapping = _mapping(values)
            options = (
                _items(values_mapping.get("values"))
                or _items(values_mapping.get("attributeValueV3s"))
                or _items(values)
            )
            multiple = value.get("isMulti")
            attribute_fields.append(
                FieldSchema(
                    schema_key="xhs:attribute:{0}".format(attribute_id or index),
                    source_id=str(attribute_id),
                    label=str(value.get("name") or value.get("label") or "属性"),
                    section="attributes",
                    control_type="select_many" if multiple is True else "select_one",
                    value_type=str(value.get("dataType") or "string"),
                    required=(
                        value.get("isRequired")
                        if isinstance(value.get("isRequired"), bool)
                        else None
                    ),
                    multiple=multiple if isinstance(multiple, bool) else None,
                    custom_allowed=(
                        value.get("customizable")
                        if isinstance(value.get("customizable"), bool)
                        else None
                    ),
                    option_summary=option_summary(
                        normalize_xhs_attribute_options(options), source="api"
                    ),
                    option_values=field_options(
                        normalize_xhs_attribute_options(options), source="api"
                    ),
                    api_paths=(_ATTRIBUTES.path, _ATTRIBUTE_VALUES.path),
                )
            )

        variations, variations_observation = await api.request(
            _VARIATIONS,
            {
                "leafCategoryId": leaf_id,
                "shopId": shop_id,
                "api_name": "xhs_getVariations",
            },
        )
        observations.append(variations_observation)
        variation_fields = tuple(
            FieldSchema(
                schema_key="xhs:variation:{0}".format(value.get("id") or index),
                source_id=str(value.get("id") or index),
                label=str(value.get("name") or "销售规格"),
                section="variations",
                control_type="select_many",
                multiple=True,
                api_paths=(_VARIATIONS.path,),
            )
            for index, value in enumerate(_items(_mapping(variations).get("variations")))
            if isinstance(value, Mapping)
        )
        delivery_category_id = self._delivery_category_ids.get(leaf_id, leaf_id)
        delivery, delivery_observation = await api.request(
            _DELIVERY,
            {
                "shopId": shop_id,
                "categoryId": delivery_category_id,
                "logisticsPlanId": self._identity("logisticsPlanId", panel),
                "api_name": "xhs_getDeliveryRule",
            },
        )
        observations.append(delivery_observation)
        delivery = _mapping(delivery)
        existing_options = _delivery_rule_options(
            _items(delivery.get("existing")) or _items(
                delivery.get("existingDeliveryRuleList")
            )
        )
        presale_options = _delivery_rule_options(
            _items(delivery.get("presale")) or _items(
                delivery.get("presaleDeliveryRuleList")
            )
        )
        delivery_fields = (
            FieldSchema(
                schema_key="xhs:delivery:existing",
                source_id="delivery-existing",
                label="现货发货规则",
                section="delivery",
                control_type="select_one",
                option_summary=option_summary(existing_options, source="api"),
                option_values=field_options(existing_options, source="api"),
                api_paths=(_DELIVERY.path,),
            ),
            FieldSchema(
                schema_key="xhs:delivery:presale",
                source_id="delivery-presale",
                label="预售发货规则",
                section="delivery",
                control_type="select_one",
                option_summary=option_summary(presale_options, source="api"),
                option_values=field_options(presale_options, source="api"),
                api_paths=(_DELIVERY.path,),
            ),
        )
        return SchemaFragment(
            sections=(
                SectionSchema(key="attributes", label="商品属性", order=1, fields=tuple(attribute_fields)),
                SectionSchema(key="variations", label="销售规格", order=2, fields=variation_fields),
                SectionSchema(key="delivery", label="发货规则", order=3, fields=delivery_fields),
            ),
            endpoints=tuple(observations),
            generation=generation,
        )


__all__ = ["XhsListing", "normalize_xhs_attribute_options"]
