from __future__ import annotations

import inspect
import re
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

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
    "/tm/detail.json",
    ("baseItemId", "shopIds", "api_name"),
    required_for="fixed",
    read_only=True,
)
_OTHER_SHOP_DETAIL = EndpointSpec(
    "GET",
    "/tm/detailByOtherShop.json",
    ("baseItemId", "shopId", "api_name"),
    required_for="fixed",
    read_only=True,
)
_PREDICTION = EndpointSpec(
    "POST",
    "/publish/fast/prediction/cat.json",
    ("api_name", "baseItemId", "platformType", "shopId", "title"),
    required_for="category",
    read_only=True,
)
_CATEGORY_CONFIG = EndpointSpec(
    "GET",
    "/dsb/queryCategoryConfigInfo.json",
    ("shopType", "leafCategoryId", "api_name"),
    required_for="dynamic",
    read_only=True,
)
_PRODUCT_MATCH = EndpointSpec(
    "GET",
    "/tm/getProductMatchSchema.json",
    ("shopId", "categoryId", "api_name"),
    required_for="dynamic",
    read_only=True,
)
_BRANDS = EndpointSpec(
    "GET",
    "/tm/getBrandList.json",
    ("shopId", "api_name"),
    required_for="dynamic",
    read_only=True,
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Tuple[Any, ...]:
    if isinstance(value, (tuple, list)):
        return tuple(value)
    return ()


def _first_shop_identity(value: Any) -> str:
    if isinstance(value, Mapping):
        for name in ("shopId", "shop_id"):
            identity = value.get(name)
            if identity not in (None, ""):
                return str(identity)
        for nested in value.values():
            identity = _first_shop_identity(nested)
            if identity:
                return identity
    elif isinstance(value, (tuple, list)):
        for nested in value:
            identity = _first_shop_identity(nested)
            if identity:
                return identity
    return ""


def _field_required(descriptor: Mapping[str, Any]) -> Optional[bool]:
    if isinstance(descriptor.get("required"), bool):
        return descriptor.get("required")
    for rule in _sequence(descriptor.get("rules")):
        if isinstance(rule, Mapping) and isinstance(rule.get("required"), bool):
            return rule.get("required")
    return None


def _control_type(descriptor: Mapping[str, Any]) -> str:
    field_type = str(
        descriptor.get("type")
        or descriptor.get("inputType")
        or descriptor.get("component")
        or ""
    ).lower()
    if "multi" in field_type:
        return "select_many"
    if "select" in field_type or "cascader" in field_type:
        return "select_one"
    if "radio" in field_type:
        return "radio"
    if "checkbox" in field_type:
        return "checkbox"
    if "upload" in field_type or "image" in field_type:
        return "upload_file"
    if "textarea" in field_type:
        return "textarea"
    return "text"


def _descriptor_fields(
    descriptors: Iterable[Any],
    *,
    section: str,
    prefix: str,
    api_path: str,
) -> Tuple[FieldSchema, ...]:
    result = []
    seen = set()

    def visit(descriptor: Any) -> None:
        if not isinstance(descriptor, Mapping):
            return
        source_id = descriptor.get("id")
        if source_id is None:
            source_id = descriptor.get("propId") or descriptor.get("name")
        label = descriptor.get("label") or descriptor.get("name") or ""
        identity = (str(source_id or ""), str(label))
        if label and identity not in seen:
            seen.add(identity)
            options = descriptor.get("options")
            if not isinstance(options, (tuple, list)):
                options = descriptor.get("values")
            if not isinstance(options, (tuple, list)):
                options = ()
            control_type = _control_type(descriptor)
            result.append(
                FieldSchema(
                    schema_key="{0}:{1}".format(prefix, source_id or len(result)),
                    source_id=None if source_id in (None, "") else str(source_id),
                    label=str(label),
                    section=section,
                    control_type=control_type,
                    required=_field_required(descriptor),
                    multiple=(
                        True
                        if control_type == "select_many"
                        else descriptor.get("multiple")
                        if isinstance(descriptor.get("multiple"), bool)
                        else None
                    ),
                    custom_allowed=(
                        descriptor.get("customizable")
                        if isinstance(descriptor.get("customizable"), bool)
                        else descriptor.get("canInputCustom")
                        if isinstance(descriptor.get("canInputCustom"), bool)
                        else None
                    ),
                    option_summary=(
                        option_summary(options, source="api") if options else None
                    ),
                    option_values=field_options(options, source="api"),
                    api_paths=(api_path,),
                )
            )
        for child in _sequence(descriptor.get("children")):
            visit(child)

    for descriptor in descriptors:
        visit(descriptor)
    return tuple(result)


def _path_parts(value: Any) -> Tuple[str, ...]:
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
        parts = _path_parts(hint)
        for part in parts or (str(hint).strip(),):
            normalized = normalize_label(part)
            if normalized and normalized not in values:
                values.append(normalized)
    return tuple(values)


def _candidate_from_mapping(
    value: Mapping[str, Any],
    *,
    rank: Optional[int] = None,
    recommended: bool = False,
) -> Optional[CategoryCandidate]:
    leaf_id = (
        value.get("leftCid")
        or value.get("leaf_id")
        or value.get("leafId")
        or value.get("categoryId")
        or value.get("cid")
    )
    leaf_name = (
        value.get("leftName")
        or value.get("leafName")
        or value.get("name")
        or value.get("categoryName")
    )
    path = _path_parts(
        value.get("cidNames")
        or value.get("path")
        or value.get("categoryPath")
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
        recommended=recommended,
        validation_status="validated",
    )


async def _panel_result(panel: Any, method_name: str) -> Any:
    method = getattr(panel, method_name, None) if panel is not None else None
    if not callable(method):
        return None
    value = method()
    return await value if inspect.isawaitable(value) else value


async def _activate_panel(panel: Any, candidate: CategoryCandidate) -> None:
    if panel is None:
        raise PlatformDiscoveryError("category panel is unavailable")
    activate = getattr(panel, "activate_category", None)
    if callable(activate):
        result = activate(candidate)
        if inspect.isawaitable(result):
            await result
        return
    raise PlatformDiscoveryError("panel does not support category activation")


class TmallListing:
    spec = get_platform_spec("tmall")
    endpoint_catalog = (
        _DETAIL,
        _OTHER_SHOP_DETAIL,
        _PREDICTION,
        _CATEGORY_CONFIG,
        _PRODUCT_MATCH,
        _BRANDS,
    )
    label_aliases = {"品牌": ("品牌名称",), "商品标题": ("标题",)}

    def __init__(self, transport: Any = None, panel: Any = None) -> None:
        self.panel = panel
        self.generation_tracker = GenerationTracker()
        self._api = ApiClient(
            page=panel,
            endpoint_catalog=self.endpoint_catalog,
            transport=transport,
        )
        self._detail: Mapping[str, Any] = {}
        self._other_detail: Mapping[str, Any] = {}
        self._resolution_fragment = SchemaFragment()

    @property
    def resolution_fragment(self) -> SchemaFragment:
        return self._resolution_fragment

    def _effective_panel(self, panel: Any) -> Any:
        return panel if panel is not None else self.panel

    def _effective_api(self, api: Optional[ApiClient]) -> ApiClient:
        return api if api is not None else self._api

    def _shop_id(self, panel: Any) -> str:
        panel = self._effective_panel(panel)
        for name in ("shop_id", "shopId"):
            value = getattr(panel, name, None) if panel is not None else None
            if value not in (None, ""):
                return str(value)
        for source in (self._detail, self._other_detail):
            value = _first_shop_identity(source)
            if value:
                return value
        return ""

    async def capture_fixed(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: Optional[ApiClient],
    ) -> SchemaFragment:
        api = self._effective_api(api)
        shop_id = self._shop_id(panel)
        detail, detail_observation = await api.request(
            _DETAIL,
            {
                "api_name": "tm_detail",
                "baseItemId": context.base_item_id or "",
                "shopIds": shop_id,
            },
        )
        self._detail = _mapping(detail)
        shop_id = self._shop_id(panel)
        other, other_observation = await api.request(
            _OTHER_SHOP_DETAIL,
            {
                "api_name": "tm_detailByOtherShop",
                "baseItemId": context.base_item_id or "",
                "shopId": shop_id,
            },
        )
        other = _mapping(other)
        self._other_detail = other
        descriptors = (
            _sequence(self._detail.get("fieldDescriptorList"))
            + _sequence(self._detail.get("itemSkuFieldDescriptorList"))
            + _sequence(other.get("fieldDescriptorList"))
            + _sequence(other.get("itemSkuFieldDescriptorList"))
        )
        fields = _descriptor_fields(
            descriptors,
            section="fixed",
            prefix="tm:fixed",
            api_path=_OTHER_SHOP_DETAIL.path,
        )
        return SchemaFragment(
            sections=(
                SectionSchema(key="fixed", label="固定字段", order=0, fields=fields),
            ),
            endpoints=(detail_observation, other_observation),
        )

    async def _existing_category(self, panel: Any) -> Optional[CategoryCandidate]:
        existing = await _panel_result(self._effective_panel(panel), "get_existing_category")
        if isinstance(existing, CategoryCandidate):
            return existing
        if isinstance(existing, Mapping):
            return _candidate_from_mapping(existing)
        if existing not in (None, ""):
            return CategoryCandidate(
                leaf_id=str(existing),
                path=(str(existing),),
                validation_status="validated",
            )
        return _candidate_from_mapping(self._detail) if self._detail else None

    async def resolve_category(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: Optional[ApiClient],
    ) -> CategoryResolution:
        existing = await self._existing_category(panel)
        if existing is not None:
            self._resolution_fragment = SchemaFragment()
            return CategoryResolution(
                status="resolved",
                source="existing",
                selected=existing,
                candidates=(existing,),
            )

        api = self._effective_api(api)
        payload, observation = await api.request(
            _PREDICTION,
            {
                "api_name": "publish_fast_prediction_cat",
                "baseItemId": context.base_item_id or "",
                "platformType": "tm",
                "shopId": self._shop_id(panel),
                "title": context.title,
            },
        )
        values = _mapping(payload).get("catList")
        candidates = tuple(
            candidate
            for candidate in (
                _candidate_from_mapping(value, rank=index, recommended=True)
                for index, value in enumerate(_sequence(values), 1)
                if isinstance(value, Mapping)
            )
            if candidate is not None
        )
        hints = _hint_leaves(context)
        exact = tuple(
            candidate
            for candidate in candidates
            if hints and normalize_label(candidate.path[-1]) in hints
        )
        self._resolution_fragment = SchemaFragment(endpoints=(observation,))
        if len(exact) == 1:
            return CategoryResolution(
                status="resolved",
                source="recommendation",
                selected=exact[0],
                candidates=candidates,
            )
        if len(exact) > 1:
            return CategoryResolution(
                status="review_required",
                source="recommendation",
                candidates=exact,
                reason="ambiguous_exact_leaf",
            )

        tree = await _panel_result(self._effective_panel(panel), "list_category_tree")
        tree_candidates = tuple(
            candidate
            for candidate in (
                _candidate_from_mapping(value)
                for value in _sequence(tree)
                if isinstance(value, Mapping)
            )
            if candidate is not None
        )
        exact_tree = tuple(
            candidate
            for candidate in tree_candidates
            if hints and normalize_label(candidate.path[-1]) in hints
        )
        if len(exact_tree) == 1:
            return CategoryResolution(
                status="resolved",
                source="tree",
                selected=exact_tree[0],
                candidates=tree_candidates,
            )
        return CategoryResolution(
            status="review_required",
            source="tree" if tree_candidates else "recommendation",
            candidates=exact_tree or tree_candidates or candidates,
            reason="ambiguous_exact_leaf" if len(exact_tree) > 1 else "no_exact_leaf",
        )

    async def activate_category(
        self,
        context: DiscoveryContext,
        panel: Any,
        candidate: CategoryCandidate,
    ) -> None:
        await _activate_panel(self._effective_panel(panel), candidate)

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
        api = self._effective_api(api)
        leaf_id = category.selected.leaf_id
        shop_id = self._shop_id(panel)
        config, config_observation = await api.request(
            _CATEGORY_CONFIG,
            {
                "api_name": "dsb_queryCategoryConfigInfo",
                "leafCategoryId": leaf_id,
                "shopType": "tm",
            },
        )
        product, product_observation = await api.request(
            _PRODUCT_MATCH,
            {
                "api_name": "tm_getProductMatchSchema",
                "categoryId": leaf_id,
                "shopId": shop_id,
            },
        )
        brands, brand_observation = await api.request(
            _BRANDS,
            {"api_name": "tm_getBrandList", "shopId": shop_id},
        )

        config_fields = _descriptor_fields(
            _sequence(_mapping(config).get("attrList"))
            or _sequence(_mapping(config).get("fields")),
            section="category_configuration",
            prefix="tm:category",
            api_path=_CATEGORY_CONFIG.path,
        )
        product_mapping = _mapping(product)
        product_descriptors = (
            _sequence(product_mapping.get("properties"))
            or _sequence(product_mapping.get("fields"))
        )
        if not product_descriptors:
            normalized_descriptors = []
            for source_id, descriptor in product_mapping.items():
                if not isinstance(descriptor, Mapping):
                    continue
                normalized = dict(descriptor)
                normalized.setdefault("id", source_id)
                normalized_descriptors.append(normalized)
            product_descriptors = tuple(normalized_descriptors)
        product_fields = _descriptor_fields(
            product_descriptors,
            section="product_match",
            prefix="tm:product",
            api_path=_PRODUCT_MATCH.path,
        )
        brand_values = _sequence(_mapping(brands).get("brandList")) or _sequence(brands)
        brand_field = FieldSchema(
            schema_key="tm:brand",
            source_id="brand",
            label="品牌",
            section="brand",
            control_type="select_one",
            option_summary=option_summary(brand_values, source="api"),
            option_values=field_options(brand_values, source="api"),
            api_paths=(_BRANDS.path,),
        )
        return SchemaFragment(
            sections=(
                SectionSchema(
                    key="category_configuration",
                    label="类目配置",
                    order=1,
                    visible_when=("category_resolved",),
                    fields=config_fields,
                ),
                SectionSchema(
                    key="product_match",
                    label="商品匹配",
                    order=2,
                    visible_when=("category_resolved",),
                    fields=product_fields,
                ),
                SectionSchema(
                    key="brand",
                    label="品牌结构",
                    order=3,
                    visible_when=("category_resolved",),
                    fields=(brand_field,),
                ),
            ),
            endpoints=(config_observation, product_observation, brand_observation),
            generation=generation,
        )


__all__ = ["TmallListing"]
