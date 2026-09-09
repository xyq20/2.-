"""淘宝只读接口 JSON 索引。

每次淘宝资料页打开或下拉框展开时，被动读取页面自然产生的接口响应。
接口 JSON 提供类目、字段和候选 ID；DOM 仅用于点击及完整候选回读校验。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlsplit

from platform_schema import FieldOption
from tmall_api_index import TmallApiField, extract_api_fields, normalize_api_label


TAOBAO_READ_ONLY_ENDPOINTS: Mapping[str, str] = {
    "/tb/getItemPublishSchema": "GET",
    "/tb/getItemPublishSchema.json": "GET",
    "/tb/getItemPublishSchemaProp": "GET",
    "/tb/getItemPublishSchemaProp.json": "GET",
    "/tb/getItemPublishSchemaPropV2": "POST",
    "/tb/getItemPublishSchemaPropV2.json": "POST",
}

_SCHEMA_PATHS = frozenset(
    {"/tb/getItemPublishSchema", "/tb/getItemPublishSchema.json"}
)
_PROP_PATHS = frozenset(
    {"/tb/getItemPublishSchemaProp", "/tb/getItemPublishSchemaProp.json"}
)


def _scalar(mapping: Mapping[str, Any], names: Sequence[str]) -> str:
    for name in names:
        value = mapping.get(name)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _request_parameters(response: Any) -> Mapping[str, str]:
    result: Dict[str, str] = {}
    try:
        query = parse_qs(
            urlsplit(str(response.url)).query,
            keep_blank_values=True,
        )
        for key, values in query.items():
            nonempty = [str(value).strip() for value in values if str(value).strip()]
            if len(set(nonempty)) == 1:
                result[key] = nonempty[0]
    except Exception:
        pass

    request = getattr(response, "request", None)
    post_data = getattr(request, "post_data", "") if request is not None else ""
    if isinstance(post_data, str) and post_data.strip():
        payload: Any = None
        try:
            payload = json.loads(post_data)
        except (TypeError, ValueError):
            try:
                payload = {
                    key: values[-1]
                    for key, values in parse_qs(
                        post_data, keep_blank_values=True
                    ).items()
                    if values
                }
            except Exception:
                payload = None
        if isinstance(payload, Mapping):
            for key, value in payload.items():
                if isinstance(value, (str, int, float)) and str(value).strip():
                    result[str(key)] = str(value).strip()
    return result


def _parameter(parameters: Mapping[str, str], names: Sequence[str]) -> str:
    values = tuple(
        dict.fromkeys(
            str(parameters.get(name) or "").strip()
            for name in names
            if str(parameters.get(name) or "").strip()
        )
    )
    return values[0] if len(values) == 1 else ""


def _prop_identity(value: object) -> str:
    text = str(value or "").strip()
    return text[2:] if text.startswith("p-") else text


def _unwrap(value: Any) -> Any:
    current = value
    for _index in range(4):
        if not isinstance(current, Mapping) or "data" not in current:
            break
        if any(
            key in current
            for key in ("result", "success", "code", "message", "msg")
        ):
            current = current.get("data")
            continue
        break
    return current


def _option_values(value: Any) -> Tuple[FieldOption, ...]:
    root = _unwrap(value)
    if isinstance(root, Mapping):
        for key in ("options", "values", "valueList", "optionList", "dataSource"):
            if key in root:
                nested = root.get(key)
                if isinstance(nested, Mapping) and isinstance(nested.get("options"), list):
                    root = nested.get("options")
                else:
                    root = nested
                break
    if not isinstance(root, (tuple, list)):
        return ()

    result = []
    for position, option in enumerate(root):
        if isinstance(option, Mapping):
            value_id = _scalar(option, ("value", "valueId", "vid", "id", "key"))
            label = _scalar(
                option,
                ("displayName", "label", "name", "text", "valueName"),
            )
        elif isinstance(option, (str, int, float)):
            value_id = label = str(option).strip()
        else:
            continue
        if value_id and label:
            result.append(FieldOption(value_id, label, position))
    return tuple(result)


class TaobaoApiJsonIndex:
    """只保留当前类目的淘宝字段 schema，不保存原始业务响应。"""

    def __init__(self, page: Any, logger: Any = None) -> None:
        self.page = page
        self.logger = logger
        self._installed = False
        self._tasks: Set[Any] = set()
        self._active_category_id = ""
        self._fields: Dict[Tuple[str, str], TmallApiField] = {}
        self._endpoint_states: Dict[str, Mapping[str, Any]] = {}

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
            expected_method = TAOBAO_READ_ONLY_ENDPOINTS.get(path)
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

    def _replace_field(self, field: TmallApiField) -> None:
        identity = (_prop_identity(field.source_id), normalize_api_label(field.label))
        self._fields[identity] = field

    async def _consume(self, response: Any, path: str, method: str) -> None:
        parameters = _request_parameters(response)
        category_id = _parameter(
            parameters,
            ("catId", "categoryId", "leafCategoryId"),
        )
        prop_id = _parameter(parameters, ("propId", "fieldId"))
        status: Optional[int]
        try:
            status = int(response.status)
        except (TypeError, ValueError):
            status = None
        try:
            payload = await asyncio.wait_for(response.json(), timeout=2.5)
        except Exception:
            self._endpoint_states[path] = {
                "path": path,
                "method": method,
                "http_status": status,
                "json": False,
                "field_count": 0,
            }
            return

        if not category_id:
            self._endpoint_states[path] = {
                "path": path,
                "method": method,
                "http_status": status,
                "json": True,
                "field_count": 0,
                "category_bound": False,
            }
            return

        if path in _SCHEMA_PATHS:
            # 每一个初始 schema 响应都代表一次新的页面代际。即使类目 ID
            # 没变，也必须清空旧字段，防止同类目页面升级后沿用旧候选。
            self._active_category_id = category_id
            self._fields.clear()
            fields = extract_api_fields(payload, path)
            for field in fields:
                enriched = replace(field, category_leaf_id=category_id)
                self._replace_field(enriched)
        elif category_id == self._active_category_id:
            fields = extract_api_fields(payload, path)
            for field in fields:
                self._replace_field(replace(field, category_leaf_id=category_id))

            options = _option_values(payload) if path in _PROP_PATHS else ()
            normalized_prop_id = _prop_identity(prop_id)
            if options and normalized_prop_id:
                matches = [
                    key
                    for key in self._fields
                    if key[0] == normalized_prop_id
                ]
                if len(matches) == 1:
                    current = self._fields[matches[0]]
                    self._fields[matches[0]] = replace(
                        current,
                        endpoint_path=path,
                        options=tuple(option.label for option in options),
                        option_values=options,
                    )

        self._endpoint_states[path] = {
            "path": path,
            "method": method,
            "http_status": status,
            "json": True,
            "field_count": len(self._fields),
            "category_bound": category_id == self._active_category_id,
        }

    async def settle(self, timeout_seconds: float = 0.75) -> None:
        if not self._tasks:
            await asyncio.sleep(0)
            return
        tasks = tuple(self._tasks)
        done, _pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, timeout_seconds),
        )
        for task in done:
            try:
                task.result()
            except Exception:
                pass

    def fields_for_label(self, label: object) -> Tuple[TmallApiField, ...]:
        normalized = normalize_api_label(label)
        return tuple(
            field
            for field in self._fields.values()
            if field.category_leaf_id == self._active_category_id
            and normalize_api_label(field.label) == normalized
        )

    def candidate_fields(self, label: object) -> Tuple[TmallApiField, ...]:
        return tuple(
            field
            for field in self.fields_for_label(label)
            if field.source_id and field.option_values
        )

    async def wait_for_candidate_field(
        self,
        label: object,
        *,
        timeout_seconds: float = 3.0,
    ) -> Tuple[TmallApiField, ...]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await self.settle(timeout_seconds=0.25)
            fields = self.candidate_fields(label)
            if fields:
                return fields
            await asyncio.sleep(0.05)
        return self.candidate_fields(label)

    async def safe_summary(self) -> Mapping[str, Any]:
        await self.settle()
        return {
            "active_category_id": self._active_category_id,
            "captured_endpoint_count": len(self._endpoint_states),
            "json_endpoint_count": sum(
                1 for state in self._endpoint_states.values() if state.get("json")
            ),
            "field_count": len(self._fields),
            "candidate_field_count": sum(
                1 for field in self._fields.values() if field.option_values
            ),
        }


__all__ = ["TAOBAO_READ_ONLY_ENDPOINTS", "TaobaoApiJsonIndex"]
