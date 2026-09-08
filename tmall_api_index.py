"""天猫只读接口 JSON 索引。

页面接口负责提供字段 ID 和候选值，Playwright DOM 只执行最终点击、输入
与可见值回读。响应原文和业务值不会写盘或写入日志。
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit


TMALL_READ_ONLY_ENDPOINTS: Mapping[str, str] = {
    "/tm/matchProductSchema.json": "POST",
    "/tm/matchProductSchema": "POST",
    "/tm/detail.json": "GET",
    "/tm/detailByOtherShop.json": "GET",
    "/publish/fast/prediction/cat.json": "POST",
    "/dsb/queryCategoryConfigInfo.json": "GET",
    "/tm/getProductMatchSchema.json": "GET",
    "/tm/getBrandList.json": "GET",
}

_CONTAINER_KEYS = frozenset(
    (
        "fieldDescriptorList",
        "itemSkuFieldDescriptorList",
        "attrList",
        "fields",
        "properties",
        "children",
        "childFields",
        "childrenUI",
    )
)
_OPTION_KEYS = (
    "options",
    "values",
    "valueList",
    "optionList",
    "dataSource",
)
_ID_KEYS = ("propId", "fieldId", "id", "key", "name")
_LABEL_KEYS = ("label", "propName", "displayName", "title", "name")


def normalize_api_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    text = text.replace("重要", "")
    return re.sub(r"[\s*:：]+", "", text).casefold()


def _text(mapping: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _unwrap_payload(value: Any) -> Any:
    current = value
    for _index in range(3):
        if not isinstance(current, Mapping):
            break
        if "data" not in current:
            break
        if any(key in current for key in ("result", "success", "code", "message", "msg")):
            current = current.get("data")
            continue
        break
    return current


def _option_names(value: Any) -> Tuple[str, ...]:
    if isinstance(value, Mapping):
        nested = value.get("options")
        if isinstance(nested, (tuple, list)):
            value = nested
        else:
            value = tuple(value.values())
    if not isinstance(value, (tuple, list)):
        return ()
    names = []
    for option in value:
        if isinstance(option, Mapping):
            name = _text(
                option,
                ("displayName", "label", "name", "text", "valueName"),
            )
        elif isinstance(option, (str, int, float)):
            name = str(option).strip()
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _descriptor_options(descriptor: Mapping[str, Any]) -> Tuple[str, ...]:
    for key in _OPTION_KEYS:
        if key not in descriptor:
            continue
        names = _option_names(descriptor.get(key))
        if names:
            return names
    component = descriptor.get("component")
    if isinstance(component, Mapping):
        props = component.get("props")
        if isinstance(props, Mapping):
            for key in _OPTION_KEYS:
                names = _option_names(props.get(key))
                if names:
                    return names
    return ()


def _required(descriptor: Mapping[str, Any]) -> Optional[bool]:
    value = descriptor.get("required")
    if isinstance(value, bool):
        return value
    rules = descriptor.get("rules")
    if isinstance(rules, (tuple, list)):
        for rule in rules:
            if isinstance(rule, Mapping) and isinstance(rule.get("required"), bool):
                return bool(rule.get("required"))
    return None


@dataclass(frozen=True)
class TmallApiField:
    endpoint_path: str
    source_id: str
    label: str
    required: Optional[bool]
    options: Tuple[str, ...]


def extract_api_fields(payload: Any, endpoint_path: str) -> Tuple[TmallApiField, ...]:
    """只提取 schema 元数据，不保留响应中的商品或店铺业务值。"""
    root = _unwrap_payload(payload)
    fields: List[TmallApiField] = []
    seen_objects: Set[int] = set()
    seen_fields = set()

    def add_descriptor(descriptor: Mapping[str, Any], fallback_id: str = "") -> None:
        label = _text(descriptor, _LABEL_KEYS)
        source_id = _text(descriptor, _ID_KEYS) or fallback_id
        if not label or not source_id:
            return
        identity = (endpoint_path, source_id, normalize_api_label(label))
        if identity in seen_fields:
            return
        seen_fields.add(identity)
        fields.append(
            TmallApiField(
                endpoint_path=endpoint_path,
                source_id=source_id,
                label=label,
                required=_required(descriptor),
                options=_descriptor_options(descriptor),
            )
        )

    def visit(value: Any, *, descriptor_context: bool = False, depth: int = 0) -> None:
        if depth > 12 or not isinstance(value, (Mapping, tuple, list)):
            return
        identity = id(value)
        if identity in seen_objects:
            return
        seen_objects.add(identity)
        if isinstance(value, (tuple, list)):
            for nested in value:
                visit(nested, descriptor_context=descriptor_context, depth=depth + 1)
            return

        if descriptor_context:
            add_descriptor(value)
        for key, nested in value.items():
            if key not in _CONTAINER_KEYS:
                continue
            if isinstance(nested, Mapping):
                for fallback_id, descriptor in nested.items():
                    if isinstance(descriptor, Mapping):
                        add_descriptor(descriptor, str(fallback_id))
                        visit(descriptor, descriptor_context=False, depth=depth + 1)
            else:
                visit(nested, descriptor_context=True, depth=depth + 1)

    visit(root)
    # 商品匹配接口也可能直接以 propId -> descriptor 作为主体。
    if endpoint_path == "/tm/getProductMatchSchema.json" and isinstance(root, Mapping):
        for fallback_id, descriptor in root.items():
            if isinstance(descriptor, Mapping):
                add_descriptor(descriptor, str(fallback_id))
                visit(descriptor, descriptor_context=False, depth=1)
    return tuple(fields)


class TmallApiJsonIndex:
    """被动读取页面自然产生的已登记接口响应，并建立内存索引。"""

    def __init__(self, page: Any, logger: Any = None) -> None:
        self.page = page
        self.logger = logger
        self._installed = False
        self._tasks: Set[Any] = set()
        self._fields: List[TmallApiField] = []
        self._endpoint_states: Dict[str, Mapping[str, Any]] = {}
        self.matched_existing_product = False

    def install(self) -> None:
        if self._installed:
            return
        register = getattr(self.page, "on", None)
        if not callable(register):
            return
        register("response", self._on_response)
        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return
        remove = getattr(self.page, "remove_listener", None)
        if callable(remove):
            remove("response", self._on_response)
        self._installed = False

    def _on_response(self, response: Any) -> None:
        try:
            path = urlsplit(str(response.url)).path
            expected_method = TMALL_READ_ONLY_ENDPOINTS.get(path)
            method = str(response.request.method).upper()
        except Exception:
            return
        if expected_method is None or method != expected_method:
            return
        task = asyncio.create_task(self._consume(response, path, method))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: Any) -> None:
        self._tasks.discard(task)
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def _consume(self, response: Any, path: str, method: str) -> None:
        http_status = None
        try:
            http_status = int(response.status)
        except (TypeError, ValueError):
            pass
        try:
            payload = await asyncio.wait_for(response.json(), timeout=2.5)
        except Exception:
            self._endpoint_states[path] = {
                "path": path,
                "method": method,
                "http_status": http_status,
                "json": False,
                "field_count": 0,
            }
            return

        if path in {"/tm/matchProductSchema", "/tm/matchProductSchema.json"}:
            data = _unwrap_payload(payload)
            self.matched_existing_product = bool(
                isinstance(payload, dict) and payload.get("result") == 1
                and isinstance(data, list)
                and any(isinstance(row, dict) and row.get("productId") for row in data)
            )
            if self.logger is not None:
                self.logger.info("天猫产品匹配接口：已匹配既有产品=%s", self.matched_existing_product)

        fields = extract_api_fields(payload, path)
        # payload 在这里离开作用域；只保留字段 schema，不保留原始响应。
        existing = {
            (field.endpoint_path, field.source_id, normalize_api_label(field.label))
            for field in self._fields
        }
        for field in fields:
            identity = (
                field.endpoint_path,
                field.source_id,
                normalize_api_label(field.label),
            )
            if identity not in existing:
                existing.add(identity)
                self._fields.append(field)
        self._endpoint_states[path] = {
            "path": path,
            "method": method,
            "http_status": http_status,
            "json": True,
            "field_count": len(fields),
        }

    async def settle(self, timeout_seconds: float = 0.75) -> None:
        if not self._tasks:
            return
        tasks = tuple(self._tasks)
        done, _pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
        for task in done:
            try:
                task.result()
            except Exception:
                pass

    def fields_for_label(self, label: object) -> Tuple[TmallApiField, ...]:
        normalized = normalize_api_label(label)
        return tuple(
            field
            for field in self._fields
            if normalize_api_label(field.label) == normalized
        )

    def source_ids(self, label: object) -> Tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                field.source_id for field in self.fields_for_label(label) if field.source_id
            )
        )

    def resolve_option(
        self,
        label: object,
        candidates: Iterable[object],
    ) -> Optional[str]:
        """按 Excel 候选顺序返回 API 中唯一的精确展示值。"""
        options = tuple(
            dict.fromkeys(
                option
                for field in self.fields_for_label(label)
                for option in field.options
                if option
            )
        )
        for candidate in candidates:
            normalized = normalize_api_label(candidate)
            matches = tuple(
                option for option in options if normalize_api_label(option) == normalized
            )
            if len(matches) == 1:
                return matches[0]
        return None

    async def safe_summary(self, panel: Any = None) -> Mapping[str, Any]:
        await self.settle()
        dom_labels = []
        if panel is not None:
            try:
                values = await panel.locator(".el-form-item__label").all_inner_texts()
            except Exception:
                values = ()
            dom_labels = [normalize_api_label(value) for value in values if value]
        api_labels = {
            normalize_api_label(field.label) for field in self._fields if field.label
        }
        dom_label_set = {value for value in dom_labels if value}
        intersection = api_labels.intersection(dom_label_set)
        json_count = sum(
            1 for state in self._endpoint_states.values() if state.get("json")
        )
        status = (
            "confirmed"
            if intersection
            else "json_captured"
            if json_count
            else "not_observed"
        )
        return {
            "status": status,
            "captured_endpoint_count": len(self._endpoint_states),
            "json_endpoint_count": json_count,
            "api_field_count": len(api_labels),
            "dom_field_count": len(dom_label_set),
            "matched_field_count": len(intersection),
            "endpoints": tuple(
                self._endpoint_states[path] for path in sorted(self._endpoint_states)
            ),
        }


__all__ = [
    "TMALL_READ_ONLY_ENDPOINTS",
    "TmallApiField",
    "TmallApiJsonIndex",
    "extract_api_fields",
    "normalize_api_label",
]
