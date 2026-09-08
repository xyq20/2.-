from __future__ import annotations

import inspect
import html
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

from platform_registry import PLATFORM_SPECS, PlatformSpec
from platform_schema import (
    CategoryCandidate,
    CategoryResolution,
    DomLocatorHint,
    EndpointObservation,
    FieldSchema,
    PlatformSchema,
    SectionSchema,
    sanitize_message,
    to_dict,
)


class PlatformDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class EndpointSpec:
    method: str
    path: str
    parameter_names: Tuple[str, ...] = ()
    required_for: str = "auxiliary"
    read_only: bool = False


@dataclass(frozen=True)
class DiscoveryContext:
    style_code: str
    title: str
    category_hints: Tuple[str, ...]
    base_item_id: Optional[str]

    def __post_init__(self) -> None:
        if self.base_item_id is not None:
            object.__setattr__(self, "base_item_id", str(self.base_item_id))


@dataclass(frozen=True)
class SchemaFragment:
    sections: Tuple[SectionSchema, ...] = ()
    endpoints: Tuple[EndpointObservation, ...] = ()
    issues: Tuple[str, ...] = ()
    generation: Optional[int] = None


@dataclass(frozen=True)
class DomFieldObservation:
    section: str
    label: str
    control_type: str
    locator_hint: Optional[DomLocatorHint] = None
    source_id: Optional[str] = None
    required: Optional[bool] = None
    multiple: Optional[bool] = None
    value_type: str = "string"
    custom_allowed: Optional[bool] = None
    occurrence: int = 1
    sensitive: bool = False

    def __post_init__(self) -> None:
        if self.source_id is not None:
            object.__setattr__(self, "source_id", str(self.source_id))

    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)

    @property
    def dom_locator_hint(self) -> Optional[DomLocatorHint]:
        return self.locator_hint


class PlatformSchemaAdapter(Protocol):
    spec: PlatformSpec
    endpoint_catalog: Tuple[EndpointSpec, ...]
    label_aliases: Mapping[str, Tuple[str, ...]]

    async def capture_fixed(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: "ApiClient",
    ) -> SchemaFragment:
        ...

    async def resolve_category(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: "ApiClient",
    ) -> CategoryResolution:
        ...

    async def activate_category(
        self,
        context: DiscoveryContext,
        panel: Any,
        candidate: CategoryCandidate,
    ) -> None:
        ...

    async def capture_dynamic(
        self,
        context: DiscoveryContext,
        panel: Any,
        api: "ApiClient",
        category: CategoryResolution,
    ) -> SchemaFragment:
        ...


_IGNORED_LABEL_CHARACTERS = re.compile(r"[\s*:：＊]+")


def _normalize_label_text(label: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(label))
    normalized = _IGNORED_LABEL_CHARACTERS.sub("", normalized)
    return normalized.replace("重要", "")


def normalize_label(
    label: str,
    aliases: Optional[Mapping[str, Tuple[str, ...]]] = None,
) -> str:
    normalized = _normalize_label_text(label)
    for canonical, variants in (aliases or {}).items():
        canonical_normalized = _normalize_label_text(canonical)
        if normalized == canonical_normalized:
            return canonical_normalized
        if isinstance(variants, str):
            variants = (variants,)
        if any(normalized == _normalize_label_text(variant) for variant in variants):
            return canonical_normalized
    return normalized


def _merge_field(api_field: FieldSchema, dom_field: DomFieldObservation) -> FieldSchema:
    return replace(
        api_field,
        label=api_field.label or dom_field.label,
        section=api_field.section or dom_field.section,
        control_type=(
            dom_field.control_type
            if api_field.control_type in ("", "unknown")
            else api_field.control_type
        ),
        value_type=(dom_field.value_type if api_field.value_type in ("", "unknown") else api_field.value_type),
        required=api_field.required if api_field.required is not None else dom_field.required,
        multiple=api_field.multiple if api_field.multiple is not None else dom_field.multiple,
        custom_allowed=(
            api_field.custom_allowed
            if api_field.custom_allowed is not None
            else dom_field.custom_allowed
        ),
        dom_locator_hint=dom_field.locator_hint or api_field.dom_locator_hint,
        presence="api_dom",
    )


def merge_api_dom_fields(
    api_fields: Sequence[FieldSchema],
    dom_observations: Sequence[DomFieldObservation],
    label_aliases: Optional[Mapping[str, Tuple[str, ...]]] = None,
) -> Tuple[Tuple[FieldSchema, ...], Tuple[str, ...]]:
    api_fields = tuple(api_fields)
    dom_observations = tuple(dom_observations)
    matches: Dict[int, int] = {}
    used_dom = set()
    blocked_api = set()
    blocked_dom = set()
    issues = []

    api_by_id: Dict[str, list] = {}
    dom_by_id: Dict[str, list] = {}
    for index, field in enumerate(api_fields):
        source_id = None if field.source_id in (None, "") else str(field.source_id)
        if source_id is not None:
            api_by_id.setdefault(source_id, []).append(index)
    for index, observation in enumerate(dom_observations):
        source_id = (
            None if observation.source_id in (None, "") else str(observation.source_id)
        )
        if source_id is not None:
            dom_by_id.setdefault(source_id, []).append(index)

    for source_id in sorted(set(api_by_id).union(dom_by_id)):
        api_indexes = api_by_id.get(source_id, [])
        dom_indexes = dom_by_id.get(source_id, [])
        if len(api_indexes) > 1 or len(dom_indexes) > 1:
            blocked_api.update(api_indexes)
            blocked_dom.update(dom_indexes)
            issues.append("ambiguous_source_id:{0}".format(source_id))
            continue
        if len(api_indexes) == 1 and len(dom_indexes) == 1:
            matches[api_indexes[0]] = dom_indexes[0]
            used_dom.add(dom_indexes[0])

    remaining_api_by_label: Dict[Tuple[str, str], list] = {}
    remaining_dom_by_label: Dict[Tuple[str, str], list] = {}
    for index, field in enumerate(api_fields):
        if index in matches or index in blocked_api:
            continue
        key = (field.section, normalize_label(field.label, label_aliases))
        remaining_api_by_label.setdefault(key, []).append(index)
    for index, observation in enumerate(dom_observations):
        if index in used_dom or index in blocked_dom:
            continue
        key = (observation.section, normalize_label(observation.label, label_aliases))
        remaining_dom_by_label.setdefault(key, []).append(index)

    all_label_keys = sorted(set(remaining_api_by_label).union(remaining_dom_by_label))
    for key in all_label_keys:
        api_indexes = remaining_api_by_label.get(key, [])
        dom_indexes = remaining_dom_by_label.get(key, [])
        if len(api_indexes) == 1 and len(dom_indexes) == 1:
            api_field = api_fields[api_indexes[0]]
            dom_field = dom_observations[dom_indexes[0]]
            api_source_id = (
                None if api_field.source_id in (None, "") else str(api_field.source_id)
            )
            dom_source_id = (
                None if dom_field.source_id in (None, "") else str(dom_field.source_id)
            )
            if (
                api_source_id is not None
                and dom_source_id is not None
                and api_source_id != dom_source_id
            ):
                issues.append(
                    "source_id_conflict:{0}:{1}:{2}:{3}".format(
                        key[0],
                        key[1],
                        api_source_id,
                        dom_source_id,
                    )
                )
                continue
            matches[api_indexes[0]] = dom_indexes[0]
            used_dom.add(dom_indexes[0])
            continue
        if len(api_indexes) > 1 or len(dom_indexes) > 1:
            issues.append("ambiguous_label:{0}:{1}".format(key[0], key[1]))

    merged = []
    for index, field in enumerate(api_fields):
        dom_index = matches.get(index)
        if dom_index is None:
            merged.append(replace(field, presence="api_only"))
        else:
            merged.append(_merge_field(field, dom_observations[dom_index]))

    dom_occurrences: Dict[Tuple[str, str], int] = {}
    for index, observation in enumerate(dom_observations):
        if index in used_dom:
            continue
        normalized = normalize_label(observation.label, label_aliases)
        occurrence_key = (observation.section, normalized)
        dom_occurrences[occurrence_key] = dom_occurrences.get(occurrence_key, 0) + 1
        occurrence = max(observation.occurrence, dom_occurrences[occurrence_key])
        merged.append(
            FieldSchema(
                schema_key="dom:{0}:{1}:{2}".format(
                    observation.section,
                    normalized,
                    occurrence,
                ),
                source_id=observation.source_id,
                label=observation.label,
                section=observation.section,
                control_type=observation.control_type,
                value_type=observation.value_type,
                required=observation.required,
                multiple=observation.multiple,
                custom_allowed=observation.custom_allowed,
                dom_locator_hint=observation.locator_hint,
                presence="dom_only",
            )
        )

    return (tuple(merged), tuple(issues))


def _contains_unsafe_url_characters(value: str) -> bool:
    return "\\" in value or any(
        unicodedata.category(character) in ("Cc", "Cf") for character in value
    )


_MAX_PATH_DECODE_ROUNDS = 5
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _fully_decode_url_component(value: str, structural_path: bool) -> Optional[str]:
    current = str(value)
    if _INVALID_PERCENT_ESCAPE_RE.search(current):
        return None

    for _attempt in range(_MAX_PATH_DECODE_ROUNDS + 1):
        normalized = unicodedata.normalize("NFKC", current)
        if structural_path and normalized.count("/") != current.count("/"):
            return None
        if _contains_unsafe_url_characters(normalized):
            return None
        if structural_path:
            if (
                not normalized.startswith("/")
                or normalized.startswith("//")
                or "//" in normalized
                or "?" in normalized
                or "#" in normalized
                or ";" in normalized
                or any(
                    segment in (".", "..") for segment in normalized.split("/")
                )
            ):
                return None

        try:
            decoded = unicodedata.normalize(
                "NFKC",
                unquote(normalized, errors="strict"),
            )
        except UnicodeDecodeError:
            return None
        if structural_path and decoded.count("/") != normalized.count("/"):
            return None
        if decoded == normalized:
            return normalized
        current = decoded
    return None


def _safe_relative_path(path: str, allow_query: bool) -> Optional[str]:
    path = str(path)
    if _contains_unsafe_url_characters(path):
        return None

    try:
        parsed = urlsplit(path)
    except ValueError:
        return None
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        return None

    decoded_path = _fully_decode_url_component(parsed.path, structural_path=True)
    if decoded_path is None:
        return None
    if parsed.query and _fully_decode_url_component(
        parsed.query,
        structural_path=False,
    ) is None:
        return None
    return decoded_path


def _is_relative_path(path: str) -> bool:
    return _safe_relative_path(path, allow_query=False) is not None


def _relative_request_path(path: str) -> Optional[str]:
    return _safe_relative_path(path, allow_query=True)


_STATIC_RESOURCE_TYPES = frozenset(
    ("stylesheet", "image", "font", "script", "media")
)


def _configured_paths(values: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(values, str):
        return (values,)
    return tuple(str(value) for value in values)


def _matches_navigation_path(path: str, navigation_paths: Iterable[str]) -> bool:
    return any(
        _is_relative_path(allowed_path) and path == allowed_path
        for allowed_path in _configured_paths(navigation_paths)
    )


def _matches_static_prefix(path: str, safe_static_prefixes: Iterable[str]) -> bool:
    for prefix in _configured_paths(safe_static_prefixes):
        if not _is_relative_path(prefix) or prefix == "/":
            continue
        if path == prefix:
            return True
        boundary = prefix if prefix.endswith("/") else "{0}/".format(prefix)
        if path.startswith(boundary):
            return True
    return False


def _urlencoded_parameter_names(value: str, strict: bool) -> Optional[frozenset]:
    if _INVALID_PERCENT_ESCAPE_RE.search(value):
        return None
    try:
        pairs = parse_qsl(
            value,
            keep_blank_values=True,
            strict_parsing=strict,
            errors="strict",
        )
    except (TypeError, UnicodeDecodeError, ValueError):
        return None
    return frozenset(str(name) for name, _value in pairs)


def _query_parameter_names(path: str) -> Optional[frozenset]:
    try:
        query = urlsplit(str(path)).query
    except ValueError:
        return None
    return _urlencoded_parameter_names(query, strict=False)


def _post_parameter_names(post_data: Any) -> Optional[frozenset]:
    if isinstance(post_data, bytes):
        try:
            text = post_data.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    elif isinstance(post_data, str):
        text = post_data
    else:
        return None
    if not text:
        return None

    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, Mapping):
            return None
        return frozenset(str(name) for name in payload)
    return _urlencoded_parameter_names(text, strict=True)


def _explicit_parameter_names(
    parameter_names: Optional[Iterable[str]],
) -> Optional[frozenset]:
    if parameter_names is None:
        return frozenset()
    try:
        if isinstance(parameter_names, str):
            return frozenset((parameter_names,))
        return frozenset(str(name) for name in parameter_names)
    except TypeError:
        return None


def is_request_allowed(
    method: str,
    path: str,
    endpoint_catalog: Iterable[EndpointSpec] = (),
    resource_type: Optional[str] = None,
    parameter_names: Optional[Iterable[str]] = None,
    post_data: Optional[Any] = None,
    safe_static_prefixes: Iterable[str] = (),
    navigation_paths: Iterable[str] = (),
) -> bool:
    method = str(method).upper()
    request_path = _relative_request_path(str(path))
    if request_path is None:
        return False

    normalized_resource_type = str(resource_type or "").strip().lower()
    if normalized_resource_type == "document":
        return method in ("GET", "HEAD") and _matches_navigation_path(
            request_path,
            navigation_paths,
        )
    if normalized_resource_type in _STATIC_RESOURCE_TYPES:
        return method in ("GET", "HEAD") and _matches_static_prefix(
            request_path,
            safe_static_prefixes,
        )

    query_names = _query_parameter_names(path)
    explicit_names = _explicit_parameter_names(parameter_names)
    if query_names is None or explicit_names is None or method not in ("GET", "POST"):
        return False
    requested_names = query_names.union(explicit_names)
    if method == "POST":
        post_names = _post_parameter_names(post_data)
        if post_names is None:
            return False
        requested_names = requested_names.union(post_names)

    for spec in endpoint_catalog:
        spec_method = str(spec.method).upper()
        if spec_method != method or spec.path != request_path:
            continue
        if not _is_relative_path(spec.path):
            continue
        if method == "POST" and not spec.read_only:
            continue
        declared_names = frozenset(str(name) for name in spec.parameter_names)
        if requested_names.issubset(declared_names):
            return True
    return False


class MutationGuard:
    def __init__(
        self,
        endpoint_catalog: Iterable[EndpointSpec] = (),
        *,
        safe_static_prefixes: Iterable[str] = (),
        navigation_paths: Iterable[str] = (),
    ) -> None:
        self.endpoint_catalog = tuple(endpoint_catalog)
        self.safe_static_prefixes = _configured_paths(safe_static_prefixes)
        self.navigation_paths = _configured_paths(navigation_paths)

    def allows(
        self,
        method: str,
        path: str,
        resource_type: Optional[str] = None,
        parameter_names: Optional[Iterable[str]] = None,
        post_data: Optional[Any] = None,
    ) -> bool:
        return is_request_allowed(
            method,
            path,
            self.endpoint_catalog,
            resource_type=resource_type,
            parameter_names=parameter_names,
            post_data=post_data,
            safe_static_prefixes=self.safe_static_prefixes,
            navigation_paths=self.navigation_paths,
        )

    def ensure_allowed(
        self,
        method: str,
        path: str,
        resource_type: Optional[str] = None,
        parameter_names: Optional[Iterable[str]] = None,
        post_data: Optional[Any] = None,
    ) -> None:
        if not self.allows(
            method,
            path,
            resource_type=resource_type,
            parameter_names=parameter_names,
            post_data=post_data,
        ):
            try:
                display_path = urlsplit(str(path)).path or "[invalid-path]"
            except ValueError:
                display_path = "[invalid-path]"
            raise PlatformDiscoveryError(
                "request blocked by inspect-only mutation guard: {0} {1}".format(
                    str(method).upper(),
                    display_path,
                )
            )

    check = allows
    assert_allowed = ensure_allowed


class GenerationTracker:
    def __init__(self) -> None:
        self._current = 0

    def begin_generation(self) -> int:
        self._current += 1
        return self._current

    def accept(self, generation: int) -> bool:
        return generation > 0 and generation == self._current

    @property
    def current_generation(self) -> int:
        return self._current


Transport = Callable[[EndpointSpec, Mapping[str, Any]], Any]
_ENDPOINT_STATUS_VALUES = frozenset(
    ("ok", "empty", "http_error", "business_error", "timeout", "schema_error")
)

_IDENTITY_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")
_IDENTITY_HEX_ESCAPE_RE = re.compile(r"\\x([0-9A-Fa-f]{2})")
_MEDIA_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:data|media|image|video|preview|src|url)\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s]+)"
)


def _decode_identity_escape(match: "re.Match[str]") -> str:
    digits = match.group(1) or match.group(2)
    codepoint = int(digits, 16)
    if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
        return "\ufffd"
    return chr(codepoint)


def _identity_detection_view(value: Any) -> str:
    current = "" if value is None else str(value)
    for _attempt in range(5):
        previous = current
        current = html.unescape(current)
        current = unquote_plus(current)
        current = _IDENTITY_UNICODE_ESCAPE_RE.sub(_decode_identity_escape, current)
        current = _IDENTITY_HEX_ESCAPE_RE.sub(
            lambda match: chr(int(match.group(1), 16)),
            current,
        )
        current = unicodedata.normalize("NFKC", current)
        if current == previous:
            break
    return current


def _contains_encoded_identity(value: Any, identities: Iterable[str]) -> bool:
    detection_view = _identity_detection_view(value)
    return any(
        _identity_detection_view(identity) in detection_view
        for identity in identities
        if str(identity)
    )


def _sanitize_discovery_text(value: Any) -> str:
    raw_value = "" if value is None else str(value)
    without_media = _MEDIA_ASSIGNMENT_RE.sub(
        "[redacted-media-assignment]",
        raw_value,
    )
    # A single message can contain both a literal media value and another
    # encoded copy.  Always inspect what remains after direct substitutions.
    if _MEDIA_ASSIGNMENT_RE.search(_identity_detection_view(without_media)):
        return "[redacted-media-message]"
    return sanitize_message(without_media)


def _parameter_value_strings(value: Any) -> Tuple[str, ...]:
    if isinstance(value, Mapping):
        values = []
        for nested in value.values():
            values.extend(_parameter_value_strings(nested))
        return tuple(values)
    if isinstance(value, (tuple, list, set)):
        values = []
        for nested in value:
            values.extend(_parameter_value_strings(nested))
        return tuple(values)
    if value is None:
        return ()
    return (str(value),)


def _redact_decoded_values(
    value: Any,
    sensitive_values: Iterable[str],
    placeholder: str,
) -> str:
    current = "" if value is None else str(value)
    identities = tuple(
        sorted(
            set(str(identity) for identity in sensitive_values if str(identity)),
            key=len,
            reverse=True,
        )
    )
    if not identities:
        return current

    directly_redacted = current
    for identity in identities:
        directly_redacted = directly_redacted.replace(identity, placeholder)
    current = directly_redacted

    # If another identity copy appears after HTML/percent/escape decoding,
    # retain no surrounding text: mixed encodings can otherwise leave a
    # recoverable copy after a direct match was already replaced.
    if _contains_encoded_identity(current, identities):
        return placeholder

    for attempt in range(4):
        for identity in identities:
            current = current.replace(identity, placeholder)
        if attempt == 3:
            break
        decoded = unquote_plus(current)
        if decoded == current:
            break
        current = decoded
    return current


def _sanitize_endpoint_message(message: Any, parameters: Mapping[str, Any]) -> str:
    scrubbed_message = _scrub_report_value(message)
    if isinstance(scrubbed_message, (Mapping, tuple, list)):
        redacted = json.dumps(scrubbed_message, ensure_ascii=False, sort_keys=True)
    else:
        redacted = "" if scrubbed_message is None else str(scrubbed_message)
    parameter_values = []
    for value in parameters.values():
        parameter_values.extend(_parameter_value_strings(value))
    if _contains_encoded_identity(redacted, parameter_values):
        return "[redacted-identity-message]"
    redacted = _redact_decoded_values(
        redacted,
        parameter_values,
        "[redacted-parameter]",
    )
    return _sanitize_discovery_text(redacted)


class ApiClient:
    def __init__(
        self,
        page: Any = None,
        endpoint_catalog: Iterable[EndpointSpec] = (),
        transport: Optional[Transport] = None,
        request_timeout_ms: int = 15000,
    ) -> None:
        self._page = page
        self.endpoint_catalog = tuple(endpoint_catalog)
        self._transport = transport
        self.request_timeout_ms = max(1, int(request_timeout_ms))

    def _validate(
        self,
        endpoint: EndpointSpec,
        parameters: Mapping[str, Any],
    ) -> None:
        if endpoint not in self.endpoint_catalog:
            raise PlatformDiscoveryError("endpoint is not in the adapter whitelist")
        if not _is_relative_path(endpoint.path):
            raise PlatformDiscoveryError("endpoint path must be a same-origin relative path")

        method = endpoint.method.upper()
        if method == "GET":
            pass
        elif method == "POST" and endpoint.read_only:
            pass
        else:
            raise PlatformDiscoveryError("endpoint method is not read-only")

        undeclared = sorted(set(str(name) for name in parameters).difference(endpoint.parameter_names))
        if undeclared:
            raise PlatformDiscoveryError(
                "undeclared endpoint parameter names: {0}".format(", ".join(undeclared))
            )

    async def _page_transport(
        self,
        endpoint: EndpointSpec,
        parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self._page is None:
            raise PlatformDiscoveryError("ApiClient requires a page or injected transport")
        return await self._page.evaluate(
            """
            async ({method, path, parameters, timeout_ms}) => {
              const form = new URLSearchParams();
              Object.entries(parameters).forEach(([name, value]) => {
                if (Array.isArray(value)) {
                  value.forEach(item => form.append(name, String(item)));
                } else if (value !== null && value !== undefined) {
                  form.append(name, String(value));
                }
              });
              let url = path;
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), timeout_ms);
              const init = {method, credentials: 'include', signal: controller.signal};
              if (method === 'GET' && form.toString()) {
                url += '?' + form.toString();
              } else if (method === 'POST') {
                init.headers = {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'};
                init.body = form.toString();
              }
              try {
                const response = await fetch(url, init);
                const text = await response.text();
                let payload = null;
                if (text) {
                  try { payload = JSON.parse(text); } catch (_error) { payload = null; }
                }
                return {
                  payload,
                  http_status: response.status,
                  ok: response.ok,
                  message: payload && (payload.message || payload.msg) || ''
                };
              } catch (error) {
                if (error && error.name === 'AbortError') {
                  return {
                    payload: null,
                    http_status: null,
                    ok: false,
                    status: 'timeout',
                    message: 'request_timeout'
                  };
                }
                return {
                  payload: null,
                  http_status: null,
                  ok: false,
                  status: 'schema_error',
                  message: 'fetch_error'
                };
              } finally {
                clearTimeout(timer);
              }
            }
            """,
            {
                "method": endpoint.method.upper(),
                "path": endpoint.path,
                "parameters": dict(parameters),
                "timeout_ms": self.request_timeout_ms,
            },
        )

    async def request(
        self,
        endpoint: EndpointSpec,
        parameters: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Any, EndpointObservation]:
        parameters = dict(parameters or {})
        self._validate(endpoint, parameters)

        try:
            result = (
                self._transport(endpoint, parameters)
                if self._transport is not None
                else self._page_transport(endpoint, parameters)
            )
            if inspect.isawaitable(result):
                result = await result
        except PlatformDiscoveryError:
            raise
        except Exception as error:
            observation = EndpointObservation(
                method=endpoint.method.upper(),
                path=endpoint.path,
                parameter_names=tuple(sorted(str(name) for name in parameters)),
                status="schema_error",
                http_status=None,
                sanitized_message="transport_error:{0}".format(type(error).__name__),
                required_for=endpoint.required_for,
            )
            return (None, observation)

        http_status = None
        message = ""
        explicit_status = None
        payload = result
        payload_from_wrapper = False
        ok = True
        business_error = False
        if isinstance(result, Mapping):
            business_markers = any(
                marker in result for marker in ("result", "success")
            )
            if "payload" in result:
                payload = result.get("payload")
                payload_from_wrapper = True
            elif "data" in result and not business_markers:
                payload = result.get("data")
            http_status = result.get("http_status")
            message = result.get("message") or result.get("msg") or ""
            explicit_status = result.get("status")
            if "ok" in result:
                ok = bool(result.get("ok"))

        if isinstance(payload, Mapping):
            envelope_message = payload.get("message") or payload.get("msg") or ""
            if "result" in payload and (
                "data" in payload or "message" in payload or "msg" in payload
            ):
                result_value = payload.get("result")
                result_success = result_value is True or str(result_value) == "1"
                message = envelope_message or message
                if result_success:
                    payload = payload.get("data")
                else:
                    payload = None
                    business_error = True
            elif isinstance(payload.get("success"), bool) and any(
                marker in payload for marker in ("data", "message", "msg", "code")
            ):
                success_value = payload.get("success")
                message = envelope_message or message
                if success_value:
                    payload = payload.get("data") if "data" in payload else payload
                else:
                    payload = None
                    business_error = True
            elif (
                "code" in payload
                and ("message" in payload or "msg" in payload)
                and (
                    isinstance(payload.get("code"), (int, float))
                    or "data" in payload
                )
            ):
                code = payload.get("code")
                code_success = str(code).upper() in ("0", "1", "200", "OK", "SUCCESS")
                message = envelope_message or message
                if code_success:
                    payload = payload.get("data") if "data" in payload else payload
                else:
                    payload = None
                    business_error = True
            elif (
                payload_from_wrapper
                and "data" in payload
                and set(str(key) for key in payload).issubset(
                    {"data", "message", "msg"}
                )
            ):
                message = envelope_message or message
                payload = payload.get("data")
        if http_status is not None:
            try:
                http_status = int(http_status)
            except (TypeError, ValueError):
                http_status = None

        trusted_explicit_status = (
            str(explicit_status)
            if explicit_status is not None
            and str(explicit_status) in _ENDPOINT_STATUS_VALUES
            else None
        )
        if trusted_explicit_status in (
            "timeout",
            "business_error",
            "schema_error",
        ):
            status = trusted_explicit_status
        elif not ok or (http_status is not None and not 200 <= http_status < 300):
            status = "http_error"
        elif business_error:
            status = "business_error"
        elif explicit_status is not None:
            candidate_status = str(explicit_status)
            status = (
                candidate_status
                if candidate_status in _ENDPOINT_STATUS_VALUES
                else "schema_error"
            )
        elif payload is None or payload == {} or payload == [] or payload == "":
            status = "empty"
        else:
            status = "ok"

        observation = EndpointObservation(
            method=endpoint.method.upper(),
            path=endpoint.path,
            parameter_names=tuple(sorted(str(name) for name in parameters)),
            status=status,
            http_status=http_status,
            sanitized_message=_sanitize_endpoint_message(message, parameters),
            required_for=endpoint.required_for,
        )
        return (payload, observation)

    async def fetch(
        self,
        endpoint: EndpointSpec,
        parameters: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Any, EndpointObservation]:
        return await self.request(endpoint, parameters)


def _attribute_is_true(attributes: Mapping[str, Any], name: str) -> bool:
    if name not in attributes:
        return False
    value = attributes.get(name)
    return value not in (False, None, "false", "False", "0")


_VIDEO_FILE_EXTENSIONS = frozenset(
    (".3gp", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm", ".wmv")
)
_IMAGE_FILE_EXTENSIONS = frozenset(
    (".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".svg", ".webp")
)


def _classify_file_accept(accept: Any) -> str:
    tokens = tuple(
        token.strip().lower()
        for token in str(accept or "").split(",")
        if token.strip()
    )
    if not tokens:
        return "unknown"

    kinds = set()
    for token in tokens:
        if token.startswith("video/") or token in _VIDEO_FILE_EXTENSIONS:
            kinds.add("video")
        elif token.startswith("image/") or token in _IMAGE_FILE_EXTENSIONS:
            kinds.add("image")
        else:
            kinds.add("file")
    if kinds == {"video"}:
        return "upload_video"
    if kinds == {"image"}:
        return "upload_image"
    return "upload_file"


def classify_control_type(
    tag_name: str,
    attributes: Optional[Mapping[str, Any]] = None,
) -> str:
    attributes = attributes or {}
    tag_name = str(tag_name).lower()
    role = str(attributes.get("role", "")).lower()
    input_type = str(attributes.get("type", "text")).lower()

    if role == "combobox":
        return "select_many" if _attribute_is_true(attributes, "aria-multiselectable") else "select_one"
    if role in ("radio", "checkbox"):
        return role
    if tag_name == "textarea":
        return "textarea"
    if tag_name == "select":
        return "select_many" if _attribute_is_true(attributes, "multiple") else "select_one"
    if tag_name == "input":
        if input_type == "radio":
            return "radio"
        if input_type == "checkbox":
            return "checkbox"
        if input_type in ("number", "range"):
            return "number"
        if input_type == "file":
            return _classify_file_accept(attributes.get("accept", ""))
        return "text"
    if _attribute_is_true(attributes, "contenteditable"):
        return "rich_text"
    return "unknown"


_SENSITIVE_DOM_MARKERS = (
    "shop",
    "logistics",
    "freight",
    "店铺",
    "物流",
    "运费",
)


async def _await_result(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _dom_observation_from_mapping(value: Mapping[str, Any]) -> DomFieldObservation:
    section = str(value.get("section") or "unsectioned")
    label = str(value.get("label") or "")
    attributes = value.get("attributes")
    if not isinstance(attributes, Mapping):
        attributes = {}
    control_type = value.get("control_type")
    if not control_type:
        control_type = classify_control_type(
            str(value.get("tag_name") or ""),
            attributes,
        )

    locator_hint = value.get("locator_hint")
    if isinstance(locator_hint, Mapping):
        locator_hint = DomLocatorHint(
            strategy=str(locator_hint.get("strategy") or "form_label"),
            section_label=str(locator_hint.get("section_label") or section),
            anchor=str(locator_hint.get("anchor") or label),
            occurrence=int(locator_hint.get("occurrence") or 1),
            control_type=str(locator_hint.get("control_type") or control_type),
        )
    elif locator_hint is not None and not isinstance(locator_hint, DomLocatorHint):
        locator_hint = None
    if locator_hint is None and label:
        locator_hint = DomLocatorHint(
            strategy="form_label",
            section_label=section,
            anchor=label,
            occurrence=int(value.get("occurrence") or 1),
            control_type=str(control_type),
        )

    sensitive_text = "{0} {1} {2}".format(
        section,
        label,
        value.get("source_id") or "",
    ).lower()
    sensitive = bool(value.get("sensitive")) or any(
        marker in sensitive_text for marker in _SENSITIVE_DOM_MARKERS
    )
    return DomFieldObservation(
        section=section,
        label=label,
        control_type=str(control_type),
        locator_hint=locator_hint,
        source_id=(
            None
            if value.get("source_id") in (None, "")
            else str(value.get("source_id"))
        ),
        required=(
            value.get("required")
            if isinstance(value.get("required"), bool)
            else None
        ),
        multiple=(
            True
            if str(control_type) == "select_many"
            else value.get("multiple")
            if isinstance(value.get("multiple"), bool)
            else None
        ),
        value_type=str(value.get("value_type") or "string"),
        custom_allowed=(
            value.get("custom_allowed")
            if isinstance(value.get("custom_allowed"), bool)
            else None
        ),
        occurrence=int(value.get("occurrence") or 1),
        sensitive=sensitive,
    )


async def scan_dom_fields(panel: Any) -> Tuple[DomFieldObservation, ...]:
    """Read structural form metadata without reading user-entered field content."""

    if panel is None:
        return ()
    capture = getattr(panel, "capture_dom_fields", None)
    if callable(capture):
        raw_observations = await _await_result(capture())
    else:
        evaluate = getattr(panel, "evaluate", None)
        if not callable(evaluate):
            return ()
        raw_observations = await _await_result(
            evaluate(
                """
                (root) => {
                const scope = root && root.querySelectorAll ? root : document;
                const hiddenByAncestor = control => {
                  for (let node = control.parentElement; node; node = node.parentElement) {
                    const style = getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden') return true;
                    if (node === root) break;
                  }
                  return false;
                };
                return Array.from(scope.querySelectorAll(
                  'input, textarea, select, [role="combobox"], [contenteditable="true"]'
                )).filter(control => {
                  if (hiddenByAncestor(control)) return false;
                  if (control.tagName.toLowerCase() === 'input' &&
                      (control.getAttribute('type') || '').toLowerCase() === 'file') {
                    return true;
                  }
                  const style = getComputedStyle(control);
                  return style.display !== 'none' && style.visibility !== 'hidden' &&
                    control.getClientRects().length > 0;
                }).map(control => {
                  const item = control.closest('.el-form-item, fieldset, [data-section]');
                  const labelNode = item && item.querySelector(
                    'label, legend, .el-form-item__label, [data-field-label]'
                  );
                  const sectionNode = control.closest('fieldset, [data-section], .form-section');
                  const sectionTitle = sectionNode && sectionNode.querySelector(
                    'legend, .section-title, [data-section-title]'
                  );
                  return {
                    section: sectionTitle ? sectionTitle.textContent.trim() : 'unsectioned',
                    label: labelNode ? labelNode.textContent.trim() : '',
                    tag_name: control.tagName.toLowerCase(),
                    attributes: {
                      type: control.getAttribute('type'),
                      role: control.getAttribute('role'),
                      multiple: control.hasAttribute('multiple'),
                      'aria-multiselectable': control.getAttribute('aria-multiselectable'),
                      contenteditable: control.getAttribute('contenteditable'),
                      accept: control.getAttribute('accept')
                    },
                    source_id: control.getAttribute('data-field-id') ||
                      control.getAttribute('name') || control.getAttribute('id'),
                    required: control.hasAttribute('required') ||
                      Boolean(item && item.classList.contains('is-required')),
                    multiple: control.hasAttribute('multiple'),
                    occurrence: 1
                  };
                });
                }
                """
            )
        )

    observations = []
    for value in raw_observations or ():
        if isinstance(value, DomFieldObservation):
            sensitive_text = "{0} {1} {2}".format(
                value.section,
                value.label,
                value.source_id or "",
            ).lower()
            sensitive = value.sensitive or any(
                marker in sensitive_text for marker in _SENSITIVE_DOM_MARKERS
            )
            observations.append(
                replace(value, sensitive=True) if sensitive and not value.sensitive else value
            )
        elif isinstance(value, Mapping):
            observations.append(_dom_observation_from_mapping(value))
    return tuple(observations)


def _unique_strings(values: Iterable[str]) -> Tuple[str, ...]:
    result = []
    seen = set()
    for value in values:
        value = str(value)
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _unique_endpoints(
    values: Iterable[EndpointObservation],
) -> Tuple[EndpointObservation, ...]:
    result = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _merge_schema_sections(
    fixed_sections: Sequence[SectionSchema],
    dynamic_sections: Sequence[SectionSchema],
    observations: Sequence[DomFieldObservation],
    aliases: Mapping[str, Tuple[str, ...]],
    category_ready: bool,
) -> Tuple[Tuple[SectionSchema, ...], Tuple[SectionSchema, ...], Tuple[str, ...]]:
    fixed_sections = tuple(fixed_sections)
    dynamic_sections = tuple(dynamic_sections)
    api_fields = []
    owners = []
    for group, sections in (("fixed", fixed_sections), ("dynamic", dynamic_sections)):
        for section_index, section in enumerate(sections):
            for field in section.fields:
                api_fields.append(field)
                owners.append((group, section_index))

    merged_fields, issues = merge_api_dom_fields(
        api_fields,
        observations,
        label_aliases=aliases,
    )
    fixed_fields = [[] for _section in fixed_sections]
    dynamic_fields = [[] for _section in dynamic_sections]
    for index, field in enumerate(merged_fields[: len(api_fields)]):
        group, section_index = owners[index]
        target = fixed_fields if group == "fixed" else dynamic_fields
        target[section_index].append(field)

    section_targets: Dict[str, list] = {}
    for group, sections in (("fixed", fixed_sections), ("dynamic", dynamic_sections)):
        for section_index, section in enumerate(sections):
            for key in (section.key, section.label):
                section_targets.setdefault(str(key), []).append((group, section_index))

    new_fixed: Dict[str, list] = {}
    new_dynamic: Dict[str, list] = {}
    for field in merged_fields[len(api_fields) :]:
        targets = tuple(set(section_targets.get(field.section, ())))
        if len(targets) == 1:
            group, section_index = targets[0]
            target = fixed_fields if group == "fixed" else dynamic_fields
            target[section_index].append(field)
            continue
        target = new_dynamic if category_ready else new_fixed
        target.setdefault(field.section or "unsectioned", []).append(field)

    merged_fixed = tuple(
        replace(section, fields=tuple(fixed_fields[index]))
        for index, section in enumerate(fixed_sections)
    )
    merged_dynamic = tuple(
        replace(section, fields=tuple(dynamic_fields[index]))
        for index, section in enumerate(dynamic_sections)
    )

    def extra_sections(values: Mapping[str, list], start: int) -> Tuple[SectionSchema, ...]:
        result = []
        for offset, (label, fields) in enumerate(values.items()):
            normalized = normalize_label(label) or "unsectioned"
            result.append(
                SectionSchema(
                    key="dom:{0}".format(normalized),
                    label=label,
                    order=start + offset,
                    fields=tuple(fields),
                )
            )
        return tuple(result)

    merged_fixed += extra_sections(new_fixed, len(merged_fixed))
    merged_dynamic += extra_sections(new_dynamic, len(merged_dynamic))
    return (merged_fixed, merged_dynamic, issues)


async def _resolution_fragment(adapter: PlatformSchemaAdapter) -> SchemaFragment:
    fragment = getattr(adapter, "resolution_fragment", None)
    if callable(fragment):
        fragment = fragment()
    fragment = await _await_result(fragment)
    return fragment if isinstance(fragment, SchemaFragment) else SchemaFragment()


_FAILED_ENDPOINT_STATUSES = frozenset(
    ("http_error", "business_error", "schema_error", "timeout")
)


def _endpoint_failure_issues(
    endpoints: Iterable[EndpointObservation],
) -> Tuple[str, ...]:
    return tuple(
        "endpoint_error:{0}:{1}:{2}".format(
            endpoint.required_for,
            endpoint.status,
            endpoint.path,
        )
        for endpoint in endpoints
        if endpoint.status in _FAILED_ENDPOINT_STATUSES
    )


def _stage_empty_issue(
    stage: str,
    endpoints: Iterable[EndpointObservation],
    sections: Sequence[SectionSchema],
) -> Optional[str]:
    stage_endpoints = tuple(
        endpoint for endpoint in endpoints if endpoint.required_for == stage
    )
    has_empty = any(endpoint.status == "empty" for endpoint in stage_endpoints)
    has_fields = any(section.fields for section in sections)
    # An ``ok`` response is only a real fallback when it produced usable
    # schema fields.  Otherwise an empty required endpoint must keep the run
    # partial (or failed for a wholly unavailable fixed stage).
    if has_empty and not has_fields:
        return "stage_empty:{0}".format(stage)
    return None


def _dom_observation_key(observation: DomFieldObservation) -> Tuple[Any, ...]:
    return (
        observation.section,
        observation.label,
        observation.control_type,
        observation.source_id,
        observation.required,
        observation.multiple,
        observation.value_type,
        observation.custom_allowed,
        observation.occurrence,
        observation.sensitive,
    )


def _unique_dom_observations(
    observations: Iterable[DomFieldObservation],
) -> Tuple[DomFieldObservation, ...]:
    result = []
    seen = set()
    for observation in observations:
        key = _dom_observation_key(observation)
        if key not in seen:
            seen.add(key)
            result.append(observation)
    return tuple(result)


async def discover_platform_schema(
    adapter: PlatformSchemaAdapter,
    context: DiscoveryContext,
    panel: Any,
    api: Optional[ApiClient],
) -> Tuple[PlatformSchema, Tuple[DomFieldObservation, ...]]:
    """Run inspect-only discovery while preserving safe partial observations."""

    fixed = SchemaFragment()
    dynamic = SchemaFragment()
    resolution_details = SchemaFragment()
    category = CategoryResolution(status="pending_category")
    issues = []
    endpoints = []
    partial = False
    fixed_failed = False

    try:
        fixed = await adapter.capture_fixed(context, panel, api)
        if not isinstance(fixed, SchemaFragment):
            raise TypeError("capture_fixed returned a non-fragment")
    except PlatformDiscoveryError:
        raise
    except Exception as error:
        fixed_failed = True
        partial = True
        issues.append("capture_fixed_error:{0}".format(type(error).__name__))
        fixed = SchemaFragment()
    endpoints.extend(fixed.endpoints)
    issues.extend(fixed.issues)

    try:
        fixed_observations = await scan_dom_fields(panel)
    except PlatformDiscoveryError:
        raise
    except Exception as error:
        partial = True
        issues.append("dom_fixed_scan_error:{0}".format(type(error).__name__))
        fixed_observations = ()

    dynamic_observations = ()
    observations = tuple(fixed_observations)

    try:
        category = await adapter.resolve_category(context, panel, api)
        if not isinstance(category, CategoryResolution):
            raise TypeError("resolve_category returned a non-resolution")
        resolution_details = await _resolution_fragment(adapter)
    except PlatformDiscoveryError:
        raise
    except Exception as error:
        partial = True
        issues.append("resolve_category_error:{0}".format(type(error).__name__))
        category = CategoryResolution(
            status="pending_category",
            reason="resolution_error",
        )
        resolution_details = SchemaFragment()
    endpoints.extend(resolution_details.endpoints)
    issues.extend(resolution_details.issues)

    category_ready = category.status == "resolved" and category.selected is not None
    if not category_ready:
        partial = True
    else:
        tracker = getattr(adapter, "generation_tracker", None)
        if not isinstance(tracker, GenerationTracker):
            tracker = GenerationTracker()
            try:
                setattr(adapter, "generation_tracker", tracker)
            except (AttributeError, TypeError):
                pass
        generation = tracker.begin_generation()
        activation_ok = True
        stale_dynamic = False
        if category.source != "existing":
            try:
                await adapter.activate_category(
                    context,
                    panel,
                    category.selected,
                )
            except PlatformDiscoveryError:
                raise
            except Exception as error:
                activation_ok = False
                partial = True
                issues.append("activate_category_error:{0}".format(type(error).__name__))

        if activation_ok:
            dynamic_captured = False
            try:
                dynamic = await adapter.capture_dynamic(
                    context,
                    panel,
                    api,
                    category,
                )
                if not isinstance(dynamic, SchemaFragment):
                    raise TypeError("capture_dynamic returned a non-fragment")
                dynamic_captured = True
            except PlatformDiscoveryError:
                raise
            except Exception as error:
                partial = True
                issues.append("capture_dynamic_error:{0}".format(type(error).__name__))
                dynamic = SchemaFragment()
            if dynamic_captured and (
                dynamic.generation != generation or not tracker.accept(generation)
            ):
                partial = True
                issues.append("stale_generation")
                stale_dynamic = True
                dynamic = SchemaFragment()
            elif dynamic_captured:
                endpoints.extend(dynamic.endpoints)
                issues.extend(dynamic.issues)

            if not stale_dynamic:
                try:
                    post_observations = await scan_dom_fields(panel)
                except PlatformDiscoveryError:
                    raise
                except Exception as error:
                    partial = True
                    issues.append("dom_dynamic_scan_error:{0}".format(type(error).__name__))
                    post_observations = ()
                fixed_keys = {
                    _dom_observation_key(observation)
                    for observation in fixed_observations
                }
                dynamic_observations = tuple(
                    observation
                    for observation in post_observations
                    if _dom_observation_key(observation) not in fixed_keys
                )
                observations = _unique_dom_observations(
                    tuple(fixed_observations) + tuple(post_observations)
                )

    fixed_sections, _unused_dynamic, fixed_merge_issues = _merge_schema_sections(
        fixed.sections,
        (),
        tuple(
            observation
            for observation in fixed_observations
            if not observation.sensitive
        ),
        getattr(adapter, "label_aliases", {}) or {},
        False,
    )
    _unused_fixed, dynamic_sections, dynamic_merge_issues = _merge_schema_sections(
        (),
        dynamic.sections,
        tuple(
            observation
            for observation in dynamic_observations
            if not observation.sensitive
        ),
        getattr(adapter, "label_aliases", {}) or {},
        True,
    )
    merge_issues = fixed_merge_issues + dynamic_merge_issues
    issues.extend(merge_issues)
    if merge_issues:
        partial = True

    endpoint_issues = _endpoint_failure_issues(endpoints)
    if endpoint_issues:
        issues.extend(endpoint_issues)
        partial = True

    for stage, sections in (
        ("fixed", fixed_sections),
        ("dynamic", dynamic_sections),
    ):
        stage_issue = _stage_empty_issue(stage, endpoints, sections)
        if stage_issue is not None:
            issues.append(stage_issue)
            partial = True

    fixed_required_endpoints = tuple(
        endpoint for endpoint in endpoints if endpoint.required_for == "fixed"
    )
    fixed_has_fields = any(section.fields for section in fixed_sections)
    fixed_has_fallback = any(
        endpoint.status == "ok" for endpoint in fixed_required_endpoints
    )
    fixed_stage_unavailable = (
        (fixed_failed or bool(fixed_required_endpoints))
        and not fixed_has_fields
        and not fixed_has_fallback
    )

    if fixed_stage_unavailable:
        capture_status = "failed"
    elif partial:
        capture_status = "partial"
    else:
        capture_status = "complete"
    schema = PlatformSchema(
        platform_id=adapter.spec.platform_id,
        capture_status=capture_status,
        category=category,
        fixed_sections=fixed_sections,
        dynamic_sections=dynamic_sections,
        endpoints=_unique_endpoints(endpoints),
        issues=_unique_strings(issues),
    )
    return (schema, tuple(observations))


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=".{0}.".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


_SENSITIVE_REPORT_KEY_PARTS = (
    "cookie",
    "authorization",
    "token",
    "headers",
    "body",
    "shopid",
    "baseitemid",
    "stylecode",
    "title",
    "media",
    "image",
    "video",
    "url",
    "password",
    "secret",
)
_SENSITIVE_REPORT_EXACT_KEYS = frozenset(
    ("accesskey", "apikey", "key", "privatekey", "secretkey", "sessionid")
)
_SENSITIVE_OPTION_SOURCES = frozenset(("shop", "logistics", "freight"))
_REGISTERED_PLATFORM_IDS = frozenset(spec.platform_id for spec in PLATFORM_SPECS)
_SAFE_PLATFORM_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_STRUCTURAL_REPORT_ID_KEYS = frozenset(("leafid", "sourceid", "schemakey"))
_SAFE_STRUCTURAL_REPORT_ID_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9:._-]{0,199}\Z"
)


def _normalized_report_key(key: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(key)).lower()
    return re.sub(r"[^a-z0-9]", "", normalized)


def _is_sensitive_report_key(key: Any) -> bool:
    normalized = _normalized_report_key(key)
    return normalized in _SENSITIVE_REPORT_EXACT_KEYS or any(
        part in normalized for part in _SENSITIVE_REPORT_KEY_PARTS
    )


def _sanitize_report_path(value: Any, sensitive_values: Iterable[str]) -> str:
    text = str(value)
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc:
        return "[redacted-url]"
    redacted = _redact_decoded_values(
        parsed.path,
        sensitive_values,
        "[redacted-identity]",
    )
    return _sanitize_discovery_text(redacted)


def _sanitize_structural_report_id(
    value: Any,
    sensitive_values: Iterable[str],
) -> str:
    redacted = _redact_decoded_values(
        value,
        sensitive_values,
        "[redacted-identity]",
    )
    if _SAFE_STRUCTURAL_REPORT_ID_RE.fullmatch(redacted) is not None:
        return redacted
    return _sanitize_discovery_text(redacted)


def _scrub_report_value(
    value: Any,
    parent_key: str = "",
    sensitive_values: Iterable[str] = (),
) -> Any:
    if isinstance(value, Mapping):
        scrubbed = {}
        for key, item in value.items():
            if _is_sensitive_report_key(key):
                continue
            safe_key = _sanitize_discovery_text(
                _redact_decoded_values(
                    key,
                    sensitive_values,
                    "[redacted-identity]",
                )
            )
            normalized_key = _normalized_report_key(key)
            if normalized_key == "apipaths" and isinstance(item, (tuple, list)):
                scrubbed[safe_key] = [
                    _sanitize_report_path(path, sensitive_values) for path in item
                ]
            elif normalized_key == "path" and isinstance(item, str):
                scrubbed[safe_key] = _sanitize_report_path(item, sensitive_values)
            else:
                scrubbed[safe_key] = _scrub_report_value(
                    item,
                    normalized_key,
                    sensitive_values,
                )

        source = str(scrubbed.get("source", "")).strip().lower()
        if "sample" in scrubbed and source in _SENSITIVE_OPTION_SOURCES:
            scrubbed["sample"] = []
            if "truncated" in scrubbed:
                scrubbed["truncated"] = bool(scrubbed.get("count"))
        return scrubbed
    if isinstance(value, (tuple, list)):
        return [
            _scrub_report_value(item, parent_key, sensitive_values) for item in value
        ]
    if isinstance(value, str):
        if parent_key == "sha256":
            return value
        if parent_key in _STRUCTURAL_REPORT_ID_KEYS:
            return _sanitize_structural_report_id(value, sensitive_values)
        redacted = _redact_decoded_values(
            value,
            sensitive_values,
            "[redacted-identity]",
        )
        return _sanitize_discovery_text(redacted)
    return value


def _schema_report_payload(
    schema: PlatformSchema,
    sensitive_values: Iterable[str],
) -> Dict[str, Any]:
    return _scrub_report_value(to_dict(schema), sensitive_values=sensitive_values)


def _redact_all_text(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _redact_all_text(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_redact_all_text(item) for item in value]
    if isinstance(value, str):
        return "[redacted]"
    return value


def _dom_observation_report_payload(
    observation: DomFieldObservation,
    sensitive_values: Iterable[str],
) -> Dict[str, Any]:
    payload = to_dict(observation)
    if observation.sensitive:
        payload["section"] = "[redacted]"
        payload["label"] = "[redacted]"
        payload["source_id"] = None
        payload["locator_hint"] = _redact_all_text(payload.get("locator_hint"))
    return _scrub_report_value(payload, sensitive_values=sensitive_values)


def _validated_report_platform_id(value: Any) -> str:
    platform_id = str(value)
    if (
        platform_id not in _REGISTERED_PLATFORM_IDS
        or _SAFE_PLATFORM_ID_RE.fullmatch(platform_id) is None
    ):
        raise PlatformDiscoveryError("report platform_id is not registered")
    return platform_id


def _resolved_report_target(output_dir: Path, filename: str) -> Path:
    target = (output_dir / filename).resolve()
    try:
        target.relative_to(output_dir)
    except ValueError:
        raise PlatformDiscoveryError("report path escapes the output directory")
    return target


def write_schema_reports(
    output_dir: Path,
    schema: PlatformSchema,
    dom_observations: Sequence[DomFieldObservation],
    *,
    context: DiscoveryContext,
    sensitive_values: Sequence[str],
) -> Tuple[Path, Path, Path]:
    output_dir = Path(output_dir).resolve()
    runtime_sensitive_values = (
        context.style_code,
        context.title,
        context.base_item_id,
    ) + tuple(sensitive_values)
    sensitive_values = tuple(
        str(value)
        for value in runtime_sensitive_values
        if value is not None and str(value)
    )
    platform_id = _validated_report_platform_id(schema.platform_id)
    schema_path = _resolved_report_target(
        output_dir,
        "{0}.json".format(platform_id),
    )
    dom_path = _resolved_report_target(
        output_dir,
        "{0}-dom.json".format(platform_id),
    )
    summary_path = _resolved_report_target(output_dir, "summary.json")

    schema_payload = _schema_report_payload(schema, sensitive_values)
    dom_payload = _scrub_report_value(
        {
            "schema_version": schema.schema_version,
            "platform_id": schema.platform_id,
            "observations": [
                _dom_observation_report_payload(observation, sensitive_values)
                for observation in dom_observations
            ],
        },
        sensitive_values=sensitive_values,
    )
    summary_payload = _scrub_report_value(
        {
            "schema_version": schema.schema_version,
            "platform_id": schema.platform_id,
            "capture_status": schema.capture_status,
            "category_status": schema.category.status,
            "fixed_section_count": len(schema.fixed_sections),
            "dynamic_section_count": len(schema.dynamic_sections),
            "endpoint_count": len(schema.endpoints),
            "issue_count": len(schema.issues),
        },
        sensitive_values=sensitive_values,
    )

    _atomic_write_json(schema_path, schema_payload)
    _atomic_write_json(dom_path, dom_payload)
    _atomic_write_json(summary_path, summary_payload)
    return (schema_path, dom_path, summary_path)


__all__ = [
    "ApiClient",
    "DiscoveryContext",
    "DomFieldObservation",
    "EndpointSpec",
    "GenerationTracker",
    "MutationGuard",
    "PlatformDiscoveryError",
    "PlatformSchemaAdapter",
    "SchemaFragment",
    "classify_control_type",
    "discover_platform_schema",
    "is_request_allowed",
    "merge_api_dom_fields",
    "normalize_label",
    "scan_dom_fields",
    "write_schema_reports",
]
