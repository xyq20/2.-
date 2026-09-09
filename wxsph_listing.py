from __future__ import annotations

import inspect
import re
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
    "/wxsph/detail.json",
    ("baseItemId", "api_name"),
    required_for="fixed",
    read_only=True,
)
_PREDICTION = EndpointSpec(
    "POST",
    "/publish/fast/prediction/cat.json",
    ("api_name", "baseItemId", "platformType", "title"),
    required_for="category",
    read_only=True,
)
_PROPERTIES = EndpointSpec(
    "GET",
    "/wxsph/getCategoryProperties.json",
    ("shopId", "categoryId", "api_name"),
    required_for="dynamic",
    read_only=True,
)
_TREE = EndpointSpec(
    "GET",
    "/wxsph/getCategoryTree.json",
    ("parentId", "endLevel", "api_name"),
    required_for="category",
    read_only=True,
)
_TEMPLATES = EndpointSpec(
    "GET",
    "/wxsph/getTemplateList.json",
    ("offset", "limit", "searchUsed", "userId", "api_name"),
    required_for="fixed",
    read_only=True,
)
_DISTRIBUTION = EndpointSpec(
    "GET",
    "/dsb/queryDistributionConfig.json",
    ("shopType", "api_name"),
    required_for="fixed",
    read_only=True,
)

_TREE_BUDGET = 128


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


def _category(value: Mapping[str, Any], path: Tuple[str, ...] = ()) -> Optional[CategoryCandidate]:
    leaf_id = (
        value.get("leaf_id")
        or value.get("categoryId")
        or value.get("cid")
        or value.get("id")
    )
    name = value.get("name") or value.get("categoryName")
    explicit_path = _parts(value.get("path") or value.get("categoryPath"))
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


def _recommended_category(
    value: Mapping[str, Any],
    rank: int,
) -> Optional[CategoryCandidate]:
    leaf_id = (
        value.get("leftCid")
        or value.get("leaf_id")
        or value.get("categoryId")
        or value.get("cid")
    )
    leaf_name = value.get("leftName") or value.get("leafName") or value.get("name")
    path = _parts(
        value.get("cidNames") or value.get("path") or value.get("categoryPath")
    )
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
        recommended=True,
        validation_status="unverified",
    )


def _control(value: Mapping[str, Any]) -> str:
    kind = str(value.get("inputType") or value.get("type") or "").lower()
    if value.get("multiple") is True or "multi" in kind:
        return "select_many"
    if value.get("values") or "select" in kind:
        return "select_one"
    if "bool" in kind or "check" in kind:
        return "checkbox"
    if "upload" in kind or "image" in kind:
        return "upload_file"
    return "text"


def _bool_value(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1"):
            return True
        if normalized in ("false", "0"):
            return False
    return None


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


def _fields(
    values: Iterable[Any],
    *,
    section: str,
    prefix: str,
    path: str,
) -> Tuple[FieldSchema, ...]:
    result = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            continue
        source_id = value.get("id") or value.get("refPid") or value.get("name")
        label = value.get("name") or value.get("label")
        if not label:
            continue
        options = _items(value.get("values")) or _items(value.get("options"))
        control_type = _control(value)
        required = value.get("required")
        if required is None:
            required = value.get("isRequired")
        required = _bool_value(required)
        result.append(
            FieldSchema(
                schema_key="{0}:{1}".format(prefix, source_id or index),
                source_id=None if source_id in (None, "") else str(source_id),
                label=str(label),
                section=section,
                control_type=control_type,
                required=required,
                multiple=(
                    value.get("multiple")
                    if isinstance(value.get("multiple"), bool)
                    else True if control_type == "select_many" else False if control_type == "select_one" else None
                ),
                option_summary=option_summary(options, source="api") if options else None,
                option_values=field_options(options, source="api"),
                api_paths=(path,),
            )
        )
    return tuple(result)


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


async def _existing_from_panel(panel: Any) -> Optional[CategoryCandidate]:
    method = getattr(panel, "get_existing_category", None) if panel is not None else None
    if not callable(method):
        return None
    value = method()
    value = await value if inspect.isawaitable(value) else value
    if isinstance(value, CategoryCandidate):
        return value
    if isinstance(value, Mapping):
        return _category(value)
    if value not in (None, ""):
        return CategoryCandidate(
            leaf_id=str(value),
            path=(str(value),),
            validation_status="validated",
        )
    return None


class WxsphListing:
    spec = get_platform_spec("wxsph")
    endpoint_catalog = (
        _DETAIL,
        _PREDICTION,
        _PROPERTIES,
        _TREE,
        _TEMPLATES,
        _DISTRIBUTION,
    )
    label_aliases = {"商品条码": ("条码",), "尺码表": ("尺码模板",)}

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

    @property
    def resolution_fragment(self) -> SchemaFragment:
        return self._resolution_fragment

    def _client(self, api: Optional[ApiClient]) -> ApiClient:
        return api if api is not None else self._api

    def _panel(self, panel: Any) -> Any:
        return panel if panel is not None else self.panel

    def _identity(self, name: str, panel: Any = None) -> str:
        value = self._detail.get(name)
        if value not in (None, ""):
            return str(value)
        panel = self._panel(panel)
        identities = getattr(panel, "runtime_identities", {}) if panel is not None else {}
        if isinstance(identities, Mapping):
            value = identities.get(name)
            if value not in (None, ""):
                return str(value)
        attribute_names = {
            "shopId": ("shop_id", "shopId"),
            "userId": ("user_id", "userId"),
        }.get(name, (name,))
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
            {"baseItemId": context.base_item_id or "", "api_name": "wxsph_detail"},
        )
        self._detail = _mapping(detail)
        templates, template_observation = await api.request(
            _TEMPLATES,
            {
                "offset": 0,
                "limit": 100,
                "searchUsed": False,
                "userId": self._identity("userId", panel),
                "api_name": "wxsph_getTemplateList",
            },
        )
        distribution, distribution_observation = await api.request(
            _DISTRIBUTION,
            {"shopType": "wxsph", "api_name": "dsb_queryDistributionConfig"},
        )
        template_values = _items(_mapping(templates).get("list")) or _items(templates)
        template_field = FieldSchema(
            schema_key="wxsph:shop-template",
            source_id="shop-template",
            label="店铺模板",
            section="fixed",
            control_type="select_one",
            option_summary=option_summary(template_values, source="shop"),
            option_values=field_options(template_values, source="shop"),
            api_paths=(_TEMPLATES.path,),
        )
        fixed_fields = _fields(
            _items(self._detail.get("fieldDescriptorList")),
            section="fixed",
            prefix="wxsph:fixed",
            path=_DETAIL.path,
        )
        if isinstance(_mapping(distribution).get("enabled"), bool):
            fixed_fields += (
                FieldSchema(
                    schema_key="wxsph:distribution",
                    source_id="distribution-enabled",
                    label="分销配置",
                    section="fixed",
                    control_type="checkbox",
                    value_type="boolean",
                    api_paths=(_DISTRIBUTION.path,),
                ),
            )
        return SchemaFragment(
            sections=(
                SectionSchema(
                    key="fixed",
                    label="固定字段",
                    fields=fixed_fields + (template_field,),
                ),
            ),
            endpoints=(detail_observation, template_observation, distribution_observation),
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
        if state["request_count"] >= _TREE_BUDGET:
            self._tree_issue(state, "category_tree_request_budget_exceeded")
            return ()
        state["request_count"] += 1
        payload, observation = await api.request(
            _TREE,
            {
                "parentId": parent_id,
                "endLevel": depth + 1,
                "api_name": "wxsph_getCategoryTree",
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
            if state["node_count"] >= _TREE_BUDGET:
                self._tree_issue(state, "category_tree_node_budget_exceeded")
                break
            state["node_count"] += 1
            name = str(value.get("name") or value.get("categoryName") or "").strip()
            child_path = path + ((name,) if name else ())
            child_id = value.get("cid") or value.get("categoryId") or value.get("id")
            if _is_leaf(value):
                candidate = _category(value, child_path)
                if candidate is not None:
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
        existing = await _existing_from_panel(self._panel(panel))
        if existing is None:
            existing = _category(self._detail) if self._detail else None
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
                "platformType": "wxsph",
                "title": context.title,
            },
        )
        prediction_candidates = tuple(
            candidate
            for candidate in (
                _recommended_category(value, index)
                for index, value in enumerate(
                    _items(_mapping(prediction).get("catList")),
                    1,
                )
                if isinstance(value, Mapping)
            )
            if candidate is not None
        )
        hints = _hint_leaves(context)
        recommendation_exact = tuple(
            candidate
            for candidate in prediction_candidates
            if hints and normalize_label(candidate.path[-1]) in hints
        )
        if len(recommendation_exact) == 1:
            self._resolution_fragment = SchemaFragment(
                endpoints=(prediction_observation,),
            )
            return CategoryResolution(
                status="resolved",
                source="recommendation",
                selected=recommendation_exact[0],
                candidates=prediction_candidates,
            )
        if len(recommendation_exact) > 1:
            self._resolution_fragment = SchemaFragment(
                endpoints=(prediction_observation,),
            )
            return CategoryResolution(
                status="review_required",
                source="recommendation",
                candidates=recommendation_exact,
                reason="ambiguous_exact_leaf",
            )

        observations = [prediction_observation]
        state = {
            "visited": set(),
            "request_count": 0,
            "node_count": 0,
            "issues": [],
        }
        candidates = await self._walk_tree(
            api,
            "0",
            (),
            observations,
            state,
        )
        exact = tuple(
            candidate
            for candidate in candidates
            if hints and normalize_label(candidate.path[-1]) in hints
        )
        self._resolution_fragment = SchemaFragment(
            endpoints=tuple(observations),
            issues=tuple(state["issues"]),
        )
        if len(exact) == 1 and not state["issues"]:
            return CategoryResolution(
                status="resolved",
                source="tree",
                selected=exact[0],
                candidates=candidates,
            )
        return CategoryResolution(
            status="review_required",
            source="tree",
            candidates=exact or candidates or prediction_candidates,
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
        payload, observation = await self._client(api).request(
            _PROPERTIES,
            {
                "shopId": self._identity("shopId", panel),
                "categoryId": category.selected.leaf_id,
                "api_name": "wxsph_getCategoryProperties",
            },
        )
        payload = _mapping(payload)
        attributes = _fields(
            _items(payload.get("attr")),
            section="attributes",
            prefix="wxsph:attribute",
            path=_PROPERTIES.path,
        )
        services = _fields(
            _items(payload.get("extraServiceList")),
            section="services",
            prefix="wxsph:service",
            path=_PROPERTIES.path,
        )
        barcode = ()
        if isinstance(payload.get("isNeedBarCode"), bool):
            barcode = (
                FieldSchema(
                    schema_key="wxsph:barcode",
                    source_id="barcode",
                    label="商品条码",
                    section="barcode",
                    control_type="text",
                    required=payload.get("isNeedBarCode"),
                    api_paths=(_PROPERTIES.path,),
                ),
            )
        qualifications = _fields(
            _items(payload.get("productQuaInfo")),
            section="qualifications",
            prefix="wxsph:qualification",
            path=_PROPERTIES.path,
        )
        requirements = _fields(
            _items(payload.get("productRequirement")),
            section="product_requirements",
            prefix="wxsph:requirement",
            path=_PROPERTIES.path,
        )
        size_values = payload.get("sizeChart")
        size_values = (size_values,) if isinstance(size_values, Mapping) else _items(size_values)
        size_chart = _fields(
            size_values,
            section="size_chart",
            prefix="wxsph:size-chart",
            path=_PROPERTIES.path,
        )
        return SchemaFragment(
            sections=(
                SectionSchema(key="attributes", label="商品属性", order=1, fields=attributes),
                SectionSchema(key="services", label="服务", order=2, fields=services),
                SectionSchema(key="barcode", label="条码", order=3, fields=barcode),
                SectionSchema(key="qualifications", label="资质", order=4, fields=qualifications),
                SectionSchema(key="product_requirements", label="商品要求", order=5, fields=requirements),
                SectionSchema(key="size_chart", label="尺码表", order=6, fields=size_chart),
            ),
            endpoints=(observation,),
            generation=generation,
        )


__all__ = ["WxsphListing"]
