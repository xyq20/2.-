"""Safe inspect-only orchestration for scaffold platform pages."""

from __future__ import annotations

import html
import importlib
import json
import logging
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote, urlsplit

from platform_discovery import (
    ApiClient,
    DiscoveryContext,
    DomFieldObservation,
    EndpointSpec,
    MutationGuard,
    PlatformDiscoveryError,
    discover_platform_schema,
    normalize_label,
    scan_dom_fields,
    write_schema_reports,
)
from platform_registry import PlatformSpec, get_platform_spec
from platform_schema import CategoryCandidate, CategoryResolution, PlatformSchema


_URL_RE = re.compile(r"(?i)\b(?:https?|smb|file)://[^\s<>\"']+")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:])/(?:[^\s<>\"']+/)*[^\s<>\"']+")
_MEDIA_RE = re.compile(
    r"(?i)(?:[^\s<>\"']*[\\/])?[^\s<>\"']+\."
    r"(?:avif|bmp|gif|heic|heif|jpe?g|png|svg|webp|3gp|avi|m4v|mkv|mov|mp4|mpeg|mpg|webm|wmv)"
    r"(?:\?[^\s<>\"']*)?"
)
_CREDENTIAL_KEY_PATTERN = (
    r"(?:authorization|cookie(?:digest)?|token|access[\s_-]*token|"
    r"refresh[\s_-]*token|session[\s_-]*(?:token|cookie)|id[\s_-]*token|"
    r"csrf[\s_-]*token|base[\s_-]*item[\s_-]*id|password|secret|"
    r"api[\s_-]*key|access[\s_-]*key)"
)
_COOKIE_CREDENTIAL_RE = re.compile(
    r"(?i)(?:\"|')?cookie(?:digest)?(?:\"|')?\s*(?::|=)\s*[^\r\n|]+"
)
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:\"|')?" + _CREDENTIAL_KEY_PATTERN + r"(?:\"|')?\s*(?::|=|\s)\s*"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"[^\s,;}\]]+(?:\s+[^\s,;}\]]+)?)"
)
_CATEGORY_ID_ATTRIBUTES = (
    "data-category-id",
    "data-leaf-category-id",
    "data-cid",
    "data-leaf-id",
)
_CATEGORY_NODE_SELECTOR = ", ".join("[{0}]".format(name) for name in _CATEGORY_ID_ATTRIBUTES)
_SAFE_STATIC_PREFIXES = (
    "/static/",
    "/assets/",
    "/css/",
    "/js/",
    "/fonts/",
    "/images/",
)
_SAFE_NAVIGATION_PATHS = ("/supplier/prod/center",)
_COMMON_READ_ONLY_ENDPOINTS = (
    EndpointSpec(
        "POST",
        "/fxg/getProductSuggestionResult.json",
        (),
        required_for="category",
        read_only=True,
    ),
    EndpointSpec(
        "POST",
        "/item/picture/query.json",
        ("api_name", "baseItemId"),
        required_for="fixed",
        read_only=True,
    ),
)


def _runtime_identity_text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.casefold() in ("none", "null", "true", "false", "undefined"):
        return None
    if re.sub(r"\s+", "", text) in ("[]", "{}", "()"):
        return None
    numeric = text.lstrip("+-")
    if numeric.isdigit() and len(numeric) < 4:
        return None
    return text


class SensitiveLogRedactor:
    def __init__(self, sensitive_values: Iterable[Any] = ()) -> None:
        self._lock = threading.RLock()
        self._variants = set()
        self.add_sensitive_values(*tuple(sensitive_values))

    @staticmethod
    def _encoded_variants(value: str) -> Tuple[str, ...]:
        percent = quote(value, safe="")
        unicode_escaped = value.encode("unicode_escape").decode("ascii")
        json_escaped = json.dumps(value, ensure_ascii=True)[1:-1]
        numeric_html = "".join("&#x{0:x};".format(ord(character)) for character in value)
        decimal_html = "".join("&#{0};".format(ord(character)) for character in value)
        return (
            value,
            percent,
            percent.lower(),
            quote(percent, safe=""),
            html.escape(value, quote=True),
            unicode_escaped,
            json_escaped,
            numeric_html,
            decimal_html,
        )

    def add_sensitive_values(self, *values: Any) -> None:
        with self._lock:
            for value in values:
                if value is None:
                    continue
                text = str(value)
                if not text:
                    continue
                self._variants.update(
                    variant for variant in self._encoded_variants(text) if variant
                )

    @property
    def sensitive_values(self) -> Tuple[str, ...]:
        with self._lock:
            raw = tuple(
                value
                for value in self._variants
                if "%" not in value and "\\u" not in value and "&#" not in value
            )
        return tuple(sorted(raw, key=len, reverse=True))

    def redact(self, value: Any) -> str:
        text = "" if value is None else str(value)
        with self._lock:
            variants = tuple(sorted(self._variants, key=len, reverse=True))
        for variant in variants:
            text = text.replace(variant, "[redacted-identity]")
        text = _URL_RE.sub("[redacted-url]", text)
        text = _COOKIE_CREDENTIAL_RE.sub("[redacted-credential]", text)
        text = _CREDENTIAL_RE.sub("[redacted-credential]", text)
        text = _MEDIA_RE.sub("[redacted-media]", text)
        text = _ABSOLUTE_PATH_RE.sub("[redacted-path]", text)
        return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: SensitiveLogRedactor, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.redact(super().format(record))


def load_platform_adapter(spec: PlatformSpec) -> Any:
    reference = spec.discovery_adapter or ""
    module_name, separator, class_name = reference.partition(":")
    if not separator or not module_name or not class_name:
        raise PlatformDiscoveryError(
            "inspect-only platform does not declare a discovery adapter"
        )
    try:
        module = importlib.import_module(module_name)
        adapter_class = getattr(module, class_name)
        adapter = adapter_class()
    except Exception as error:
        raise PlatformDiscoveryError("discovery adapter could not be loaded") from error

    if getattr(getattr(adapter, "spec", None), "platform_id", None) != spec.platform_id:
        raise PlatformDiscoveryError("discovery adapter platform does not match registry")
    catalog = tuple(getattr(adapter, "endpoint_catalog", ()))
    if any(not _endpoint_is_read_only(endpoint) for endpoint in catalog):
        raise PlatformDiscoveryError("discovery adapter contains a write endpoint")
    return adapter


def _endpoint_is_read_only(endpoint: Any) -> bool:
    if not isinstance(endpoint, EndpointSpec):
        return False
    method = str(endpoint.method).upper()
    return method == "GET" or (method == "POST" and endpoint.read_only)


async def _visible_items(locator: Any) -> Tuple[Any, ...]:
    values = []
    try:
        count = await locator.count()
    except Exception:
        return ()
    for index in range(count):
        item = locator.nth(index)
        try:
            if await item.is_visible():
                values.append(item)
        except Exception:
            continue
    return tuple(values)


class InspectionPanel:
    def __init__(self, page: Any, locator: Any, spec: PlatformSpec) -> None:
        self.page = page
        self.locator = locator
        self.spec = spec
        self.shop_id = ""
        self.shopId = ""
        self.user_id = ""
        self.userId = ""
        self.runtime_identities = {}
        self.runtime_sensitive_values: Tuple[str, ...] = ()
        self.timeout_seconds = 60

    @classmethod
    async def open(
        cls,
        page: Any,
        drawer: Any,
        spec: PlatformSpec,
        timeout_seconds: int,
    ) -> "InspectionPanel":
        timeout_ms = timeout_seconds * 1000
        tab = drawer.get_by_role("tab", name=spec.tab_label, exact=True)
        tabs = await _visible_items(tab)
        if len(tabs) != 1:
            raise PlatformDiscoveryError("inspect-only platform tab is not uniquely visible")
        selected = (await tabs[0].get_attribute("aria-selected") or "").lower()
        if selected != "true":
            await tabs[0].click()

        panel_locator = drawer.get_by_role(
            "tabpanel",
            name=spec.tab_label,
            exact=True,
        )
        try:
            await panel_locator.first.wait_for(state="visible", timeout=timeout_ms)
        except Exception as error:
            raise PlatformDiscoveryError("inspect-only platform panel did not render") from error
        panels = await _visible_items(panel_locator)
        if len(panels) != 1:
            raise PlatformDiscoveryError("inspect-only platform panel is not unique")
        panel = cls(page, panels[0], spec)
        panel.timeout_seconds = timeout_seconds
        await panel._wait_for_loading_to_finish()
        await panel._read_explicit_runtime_identities()
        return panel

    async def _wait_for_loading_to_finish(self) -> None:
        deadline = time.monotonic() + max(1, int(self.timeout_seconds))
        loading = self.locator.locator(
            ".el-loading-mask:visible, .el-loading-spinner:visible, "
            "[aria-busy='true']:visible"
        )
        while await _visible_items(loading):
            if time.monotonic() >= deadline:
                raise PlatformDiscoveryError(
                    "inspect-only platform panel did not finish loading"
                )
            wait_for_timeout = getattr(self.page, "wait_for_timeout", None)
            if not callable(wait_for_timeout):
                raise PlatformDiscoveryError(
                    "inspect-only platform panel loading state cannot be observed"
                )
            await wait_for_timeout(200)

    async def _settle_after_category_activation(self) -> None:
        wait_for_timeout = getattr(self.page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            await wait_for_timeout(150)
        await self._wait_for_loading_to_finish()

    async def _read_explicit_runtime_identities(self) -> None:
        try:
            values = await self.locator.evaluate(
                """
                root => {
                  const names = [
                    'data-shop-id', 'data-shopid', 'data-store-id', 'data-storeid',
                    'data-merchant-id', 'data-merchantid'
                  ];
                  const result = [];
                  for (const element of [root, ...root.querySelectorAll(
                    '[data-shop-id], [data-shopid], [data-store-id], [data-storeid], '
                    + '[data-merchant-id], [data-merchantid]'
                  )]) {
                    for (const name of names) {
                      const value = element.getAttribute && element.getAttribute(name);
                      if (value) result.push(value);
                    }
                  }
                  for (const tab of root.querySelectorAll(
                    '[role="tab"][aria-selected="true"], .el-tabs__item.is-active'
                  )) {
                    for (const name of ['id', 'aria-controls']) {
                      const value = tab.getAttribute && tab.getAttribute(name);
                      if (!value) continue;
                      const identity = value.replace(/^(?:tab|pane)-/, '');
                      if (/^[A-Za-z0-9_-]{4,}$/.test(identity)) result.push(identity);
                    }
                  }
                  const identityKeys = new Set([
                    'shopid', 'shopids', 'storeid', 'storeids',
                    'merchantid', 'merchantids', 'sellerid', 'sellerids'
                  ]);
                  const seen = new WeakSet();
                  const remember = value => {
                    const values = Array.isArray(value) ? value : [value];
                    for (const item of values) {
                      if ((typeof item === 'string' || typeof item === 'number') && item !== '') {
                        result.push(String(item));
                      }
                    }
                  };
                  const visit = (value, depth) => {
                    if (!value || typeof value !== 'object' || depth > 4 || seen.has(value)) return;
                    seen.add(value);
                    let keys = [];
                    try { keys = Object.keys(value); } catch (_error) { return; }
                    for (const key of keys) {
                      if (result.length >= 32) return;
                      let nested;
                      try { nested = value[key]; } catch (_error) { continue; }
                      const normalized = String(key).toLowerCase().replace(/[^a-z0-9]/g, '');
                      if (identityKeys.has(normalized)) remember(nested);
                      else if (!String(key).startsWith('$')) visit(nested, depth + 1);
                    }
                  };
                  let inspectedComponents = 0;
                  for (const element of [root, ...root.querySelectorAll('*')]) {
                    if (inspectedComponents >= 200 || result.length >= 32) break;
                    if (!element) continue;
                    for (const runtime of [element.__vue__, element.__vueParentComponent]) {
                      if (!runtime) continue;
                      inspectedComponents += 1;
                      visit(runtime, 0);
                      if (inspectedComponents >= 200 || result.length >= 32) break;
                    }
                  }
                  return [...new Set(result)];
                }
                """
            )
        except Exception:
            values = ()
        values = tuple(
            text
            for text in (_runtime_identity_text(value) for value in values or ())
            if text is not None
        )
        self.runtime_sensitive_values = values
        numeric_values = tuple(value for value in values if value.isdigit())
        selected_identity = (
            numeric_values[0]
            if numeric_values
            else values[0]
            if len(values) == 1
            else ""
        )
        if selected_identity:
            self.shop_id = selected_identity
            self.shopId = selected_identity
            self.runtime_identities["shopId"] = selected_identity

    async def capture_dom_fields(self) -> Tuple[DomFieldObservation, ...]:
        return await scan_dom_fields(self.locator)

    async def _explicit_categories(self, selected_only: bool) -> Tuple[Mapping[str, Any], ...]:
        try:
            values = await self.locator.evaluate(
                r"""
                (root, selectedOnly) => {
                  const selectors = [
                    '[data-category-id]', '[data-leaf-category-id]',
                    '[data-cid]', '[data-leaf-id]'
                  ];
                  const idNames = [
                    'data-category-id', 'data-leaf-category-id',
                    'data-cid', 'data-leaf-id'
                  ];
                  const nodes = Array.from(root.querySelectorAll(selectors.join(',')));
                  const result = [];
                  for (const node of nodes) {
                    const selected = node.matches(
                      '[aria-selected="true"], [data-selected="true"], .is-selected, .selected'
                    ) || Boolean(node.closest(
                      '[aria-selected="true"], [data-selected="true"], .is-selected, .selected'
                    ));
                    if (selectedOnly && !selected) continue;
                    const leafId = idNames.map(name => node.getAttribute(name)).find(Boolean);
                    if (!leafId) continue;
                    const pathValue = node.getAttribute('data-category-path') ||
                      node.getAttribute('data-path') || '';
                    const path = pathValue.split(/[\/／>＞]/).map(value => value.trim()).filter(Boolean);
                    result.push({leaf_id: leafId, path: path.length ? path : [leafId]});
                  }
                  return result;
                }
                """,
                selected_only,
            )
        except Exception:
            return ()
        result = []
        seen = set()
        for value in values or ():
            if not isinstance(value, Mapping):
                continue
            leaf_id = str(value.get("leaf_id") or "").strip()
            if not leaf_id or leaf_id in seen:
                continue
            path_value = value.get("path")
            path = (
                tuple(str(item).strip() for item in path_value if str(item).strip())
                if isinstance(path_value, (tuple, list))
                else (leaf_id,)
            )
            result.append({"leaf_id": leaf_id, "path": path or (leaf_id,)})
            seen.add(leaf_id)
        return tuple(result)

    async def get_existing_category(self) -> Optional[Mapping[str, Any]]:
        selected = await self._explicit_categories(True)
        if len(selected) == 1:
            return selected[0]
        return None

    async def list_category_tree(self) -> Tuple[Mapping[str, Any], ...]:
        return await self._explicit_categories(False)

    async def _node_category_id(self, node: Any) -> str:
        for name in _CATEGORY_ID_ATTRIBUTES:
            try:
                value = await node.get_attribute(name)
            except Exception:
                value = None
            if value not in (None, ""):
                return str(value)
        return ""

    async def activate_category(self, candidate: CategoryCandidate) -> None:
        if not candidate.leaf_id or not candidate.path:
            raise PlatformDiscoveryError("category candidate lacks an explicit id or path")

        nodes = await _visible_items(self.locator.locator(_CATEGORY_NODE_SELECTOR))
        matching_nodes = []
        for node in nodes:
            if await self._node_category_id(node) == str(candidate.leaf_id):
                matching_nodes.append(node)

        actions = []
        action_name = re.compile(r"^\s*(?:点击使用|使用)\s*$")
        for node in matching_nodes:
            candidates = await _visible_items(
                node.get_by_role("button", name=action_name, exact=False)
            )
            if not candidates:
                candidates = await _visible_items(node.get_by_text("点击使用", exact=True))
            if not candidates:
                candidates = await _visible_items(node.get_by_text("使用", exact=True))
            actions.extend(candidates)
        if len(actions) == 1:
            await actions[0].click()
            await self._settle_after_category_activation()
            return
        if len(actions) > 1:
            raise PlatformDiscoveryError("category candidate has ambiguous scoped actions")

        if await self._activate_recommendation_path(candidate):
            return
        if await self._activate_exact_path_text(candidate):
            return
        if candidate.recommended:
            deadline = time.monotonic() + max(1, int(self.timeout_seconds))
            wait_for_timeout = getattr(self.page, "wait_for_timeout", None)
            while time.monotonic() < deadline:
                if not callable(wait_for_timeout):
                    break
                await wait_for_timeout(200)
                if await self._activate_recommendation_path(candidate):
                    return
                if await self._activate_exact_path_text(candidate):
                    return
            raise PlatformDiscoveryError(
                "category recommendation did not render an exact scoped action"
            )
        await self._activate_category_path(candidate)

    async def _activate_recommendation_path(
        self,
        candidate: CategoryCandidate,
    ) -> bool:
        expected = tuple(
            normalized
            for normalized in (normalize_label(part) for part in candidate.path)
            if normalized
        )
        if not expected:
            return False
        rows = await _visible_items(
            self.locator.locator(
                ".prediction-item, [data-category-recommendation]"
            )
        )
        matches = []
        for row in rows:
            path_nodes = await _visible_items(
                row.locator(".category-path, [data-category-path]")
            )
            if len(path_nodes) != 1:
                continue
            try:
                text = await path_nodes[0].inner_text()
            except Exception:
                continue
            actual = tuple(
                normalized
                for normalized in (
                    normalize_label(part)
                    for part in re.split(r"[>＞]+", str(text))
                )
                if normalized
            )
            if len(actual) >= len(expected) and actual[-len(expected) :] == expected:
                matches.append(row)
        if not matches:
            return False
        if len(matches) != 1:
            raise PlatformDiscoveryError(
                "category recommendation path is not uniquely visible"
            )

        action_name = re.compile(r"^\s*(?:点击使用|使用)\s*$")
        actions = await _visible_items(
            matches[0].get_by_role("button", name=action_name, exact=False)
        )
        if not actions:
            actions = await _visible_items(
                matches[0].get_by_text("点击使用", exact=True)
            )
        if not actions:
            actions = await _visible_items(
                matches[0].get_by_text("使用", exact=True)
            )
        if len(actions) != 1:
            raise PlatformDiscoveryError(
                "category recommendation has no unique scoped action"
            )
        await actions[0].click()
        await self._settle_after_category_activation()
        return True

    async def _activate_exact_path_text(
        self,
        candidate: CategoryCandidate,
    ) -> bool:
        parts = tuple(str(part).strip() for part in candidate.path if str(part).strip())
        if not parts:
            return False
        pattern = re.compile(
            r"^\s*"
            + r"\s*[>＞]\s*".join(re.escape(part) for part in parts)
            + r"\s*$"
        )
        path_nodes = await _visible_items(self.locator.get_by_text(pattern))
        if not path_nodes:
            return False
        if len(path_nodes) != 1:
            raise PlatformDiscoveryError(
                "category recommendation path is not uniquely visible"
            )

        scope = path_nodes[0]
        for _depth in range(3):
            scope = scope.locator("xpath=..")
            actions = await _visible_items(
                scope.get_by_role(
                    "button",
                    name=re.compile(r"^\s*(?:点击使用|使用)\s*$"),
                    exact=False,
                )
            )
            if len(actions) == 1:
                await actions[0].click()
                await self._settle_after_category_activation()
                return True
            if len(actions) > 1:
                raise PlatformDiscoveryError(
                    "category recommendation has ambiguous scoped actions"
                )
        raise PlatformDiscoveryError(
            "category recommendation has no unique scoped action"
        )

    async def _activate_category_path(self, candidate: CategoryCandidate) -> None:
        controls = await _visible_items(
            self.locator.locator(
                "[data-category-cascader], [data-category-tree], .el-cascader, [role='tree']"
            )
        )
        if len(controls) != 1:
            raise PlatformDiscoveryError("no unique explicit category tree control is visible")
        try:
            await controls[0].click()
        except Exception:
            pass

        final_target = None
        for segment in candidate.path:
            scopes = await _visible_items(
                self.page.locator(
                    ".el-cascader-panel:visible, [data-category-popup]:visible, "
                    "[data-category-tree]:visible, [role='tree']:visible"
                )
            )
            matches = []
            for scope in scopes:
                matches.extend(await _visible_items(scope.get_by_text(segment, exact=True)))
            if len(matches) != 1:
                raise PlatformDiscoveryError("category path segment is not uniquely visible")
            final_target = matches[0]
            await final_target.click()
            wait_for_timeout = getattr(self.page, "wait_for_timeout", None)
            if callable(wait_for_timeout):
                await wait_for_timeout(100)

        if final_target is None:
            raise PlatformDiscoveryError("category path is empty")
        try:
            final_id = await final_target.evaluate(
                """
                element => {
                  const node = element.closest(
                    '[data-category-id], [data-leaf-category-id], [data-cid], [data-leaf-id]'
                  );
                  if (!node) return '';
                  return node.getAttribute('data-category-id') ||
                    node.getAttribute('data-leaf-category-id') ||
                    node.getAttribute('data-cid') || node.getAttribute('data-leaf-id') || '';
                }
                """
            )
        except Exception:
            final_id = ""
        if str(final_id) != str(candidate.leaf_id):
            raise PlatformDiscoveryError("category path did not end at the explicit candidate id")
        await self._settle_after_category_activation()


def _origin(value: str) -> Optional[Tuple[str, str, int]]:
    try:
        parsed = urlsplit(str(value))
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if not scheme or not hostname:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else -1
    return (scheme, hostname, port)


def _safe_blocked_path(value: Any) -> str:
    try:
        path = urlsplit(str(value)).path or "/"
    except (TypeError, ValueError):
        return "[invalid-path]"
    safe_segments = []
    for segment in path.split("/"):
        if not segment:
            continue
        if (
            re.fullmatch(r"[A-Za-z0-9._~-]+", segment) is None
            or len(segment) > 80
            or (len(segment) >= 12 and "." not in segment and "-" in segment)
        ):
            safe_segments.append("[redacted-segment]")
            continue
        safe_segments.append(re.sub(r"\d{4,}", "[redacted-id]", segment))
    return "/" + "/".join(safe_segments) if safe_segments else "/"


class InspectionRequestGuard:
    def __init__(self, page: Any, endpoint_catalog: Sequence[EndpointSpec]) -> None:
        self.page = page
        self.endpoint_catalog = tuple(endpoint_catalog) + _COMMON_READ_ONLY_ENDPOINTS
        if any(not _endpoint_is_read_only(endpoint) for endpoint in self.endpoint_catalog):
            raise PlatformDiscoveryError("inspect-only endpoint catalog contains a write endpoint")
        self.guard = MutationGuard(
            self.endpoint_catalog,
            safe_static_prefixes=_SAFE_STATIC_PREFIXES,
            navigation_paths=_SAFE_NAVIGATION_PATHS,
        )
        self.page_origin = _origin(page.url)
        self.blocked_requests = []
        self._runtime_identity_candidates = {"shopId": [], "userId": []}
        required_identity_names = []
        for endpoint in self.endpoint_catalog:
            for parameter_name in endpoint.parameter_names:
                normalized = re.sub(
                    r"[^a-z0-9]",
                    "",
                    str(parameter_name).casefold(),
                )
                if normalized in (
                    "shopid",
                    "shopids",
                    "storeid",
                    "storeids",
                    "merchantid",
                    "merchantids",
                    "sellerid",
                    "sellerids",
                ):
                    identity_name = "shopId"
                elif normalized in ("userid", "userids"):
                    identity_name = "userId"
                else:
                    continue
                if identity_name not in required_identity_names:
                    required_identity_names.append(identity_name)
        self._required_runtime_identity_names = tuple(required_identity_names)
        self.handler = self._handle
        self._installed = False

    @property
    def shop_id_candidates(self) -> Tuple[str, ...]:
        return tuple(self._runtime_identity_candidates["shopId"])

    @property
    def runtime_identity_values(self) -> Tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value
                for values in self._runtime_identity_candidates.values()
                for value in values
            )
        )

    def _remember_runtime_identity(self, name: str, value: Any) -> None:
        candidates = self._runtime_identity_candidates.setdefault(name, [])
        values = value if isinstance(value, (tuple, list)) else (value,)
        for item in values:
            for part in str(item).split(","):
                text = _runtime_identity_text(part)
                if text is not None and text not in candidates:
                    candidates.append(text)

    def _remember_shop_id(self, value: Any) -> None:
        self._remember_runtime_identity("shopId", value)

    def _observe_runtime_identities(self, request: Any) -> None:
        items = []
        try:
            items.extend(parse_qsl(urlsplit(str(request.url)).query, keep_blank_values=False))
        except (TypeError, ValueError):
            pass
        post_data = getattr(request, "post_data", None)
        if isinstance(post_data, str) and post_data:
            stripped = post_data.lstrip()
            if stripped.startswith("{"):
                try:
                    payload = json.loads(post_data)
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, Mapping):
                    items.extend(payload.items())
            else:
                items.extend(parse_qsl(post_data, keep_blank_values=False))
        for key, value in items:
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in (
                "shopid",
                "shopids",
                "storeid",
                "storeids",
                "merchantid",
                "merchantids",
                "sellerid",
                "sellerids",
            ):
                self._remember_shop_id(value)
            elif normalized in ("userid", "userids"):
                self._remember_runtime_identity("userId", value)

    def apply_runtime_identity(self, panel: Any) -> None:
        existing = tuple(getattr(panel, "runtime_sensitive_values", ()) or ())
        panel.runtime_sensitive_values = tuple(
            dict.fromkeys(existing + self.runtime_identity_values)
        )
        identities = dict(getattr(panel, "runtime_identities", {}) or {})
        for name, candidates in self._runtime_identity_candidates.items():
            if candidates and not identities.get(name):
                identities[name] = candidates[0]
        panel.runtime_identities = identities
        if not getattr(panel, "shop_id", "") and identities.get("shopId"):
            panel.shop_id = identities["shopId"]
            panel.shopId = identities["shopId"]
        if not getattr(panel, "user_id", "") and identities.get("userId"):
            panel.user_id = identities["userId"]
            panel.userId = identities["userId"]

    def _has_required_runtime_identity(self, panel: Any) -> bool:
        identities = dict(getattr(panel, "runtime_identities", {}) or {})
        if getattr(panel, "shop_id", ""):
            identities.setdefault("shopId", getattr(panel, "shop_id"))
        if getattr(panel, "user_id", ""):
            identities.setdefault("userId", getattr(panel, "user_id"))
        if not self._required_runtime_identity_names:
            return bool(self.runtime_identity_values or identities)
        return any(
            bool(self._runtime_identity_candidates.get(name) or identities.get(name))
            for name in self._required_runtime_identity_names
        )

    async def wait_for_runtime_identity(
        self,
        panel: Any,
        timeout_seconds: float = 5.0,
    ) -> bool:
        self.apply_runtime_identity(panel)
        if self._has_required_runtime_identity(panel):
            return True
        wait_for_timeout = getattr(self.page, "wait_for_timeout", None)
        if not callable(wait_for_timeout):
            return False
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            await wait_for_timeout(100)
            self.apply_runtime_identity(panel)
            if self._has_required_runtime_identity(panel):
                return True
        refresh = getattr(panel, "_read_explicit_runtime_identities", None)
        if callable(refresh):
            await refresh()
            self.apply_runtime_identity(panel)
        return self._has_required_runtime_identity(panel)

    def _record_block(self, request: Any, reason: str) -> None:
        self.blocked_requests.append(
            {
                "method": str(getattr(request, "method", "")).upper(),
                "path": _safe_blocked_path(getattr(request, "url", "")),
                "resource_type": str(getattr(request, "resource_type", "")),
                "reason": reason,
            }
        )

    async def _handle(self, route: Any, request: Any) -> None:
        method = str(getattr(request, "method", "")).upper()
        request_origin = _origin(getattr(request, "url", ""))
        if method == "GET":
            if self.page_origin is not None and request_origin == self.page_origin:
                self._observe_runtime_identities(request)
            await route.continue_()
            return
        if self.page_origin is None or request_origin != self.page_origin:
            self._record_block(request, "cross_origin_write")
            await route.abort()
            return
        parsed = urlsplit(str(request.url))
        relative_path = parsed.path or "/"
        if parsed.query:
            relative_path += "?" + parsed.query
        if method == "POST" and any(
            str(endpoint.method).upper() == "POST"
            and endpoint.path == (parsed.path or "/")
            and endpoint.read_only
            for endpoint in self.endpoint_catalog
        ):
            self._observe_runtime_identities(request)
            await route.continue_()
            return
        allowed = self.guard.allows(
            method,
            relative_path,
            resource_type=str(request.resource_type),
            post_data=getattr(request, "post_data", None),
        )
        if not allowed:
            registered_shape = any(
                str(endpoint.method).upper() == method
                and endpoint.path == (parsed.path or "/")
                and endpoint.read_only
                for endpoint in self.endpoint_catalog
            )
            self._record_block(
                request,
                (
                    "registered_request_shape_mismatch"
                    if registered_shape
                    else "unregistered_write"
                ),
            )
            await route.abort()
            return
        await route.continue_()

    async def install(self) -> None:
        if not self._installed:
            await self.page.route("**/*", self.handler)
            self._installed = True

    async def uninstall(self) -> None:
        if self._installed:
            await self.page.unroute("**/*", self.handler)
            self._installed = False


def _sensitive_values_from(value: Any) -> Tuple[str, ...]:
    markers = (
        "shop",
        "store",
        "merchant",
        "seller",
        "tenant",
        "account",
        "supplier",
        "baseitem",
        "style",
        "title",
        "media",
        "image",
        "video",
        "token",
        "cookie",
    )
    result = []
    seen_objects = set()

    def collect_scalars(item: Any) -> None:
        if isinstance(item, Mapping):
            for nested in item.values():
                collect_scalars(nested)
        elif isinstance(item, (tuple, list, set)):
            for nested in item:
                collect_scalars(nested)
        elif isinstance(item, (str, int)):
            text = _runtime_identity_text(item)
            if text is not None:
                result.append(text)

    def visit(item: Any) -> None:
        identity = id(item)
        if identity in seen_objects:
            return
        if isinstance(item, (Mapping, tuple, list, set)) or hasattr(item, "__dict__"):
            seen_objects.add(identity)
        if isinstance(item, Mapping):
            for key, nested in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if any(marker in normalized for marker in markers):
                    collect_scalars(nested)
                else:
                    visit(nested)
        elif isinstance(item, (tuple, list, set)):
            for nested in item:
                visit(nested)
        elif hasattr(item, "__dict__"):
            visit(vars(item))

    visit(value)
    return tuple(dict.fromkeys(result))


def _base_item_id(record: Any) -> Optional[str]:
    if not isinstance(record, Mapping):
        return None
    value = record.get("baseItemId")
    if value in (None, ""):
        value = record.get("base_item_id")
    if value in (None, ""):
        value = record.get("id")
    return None if value in (None, "") else str(value)


async def _safe_screenshot(page: Any, path: Path) -> None:
    try:
        await page.screenshot(path=str(path), full_page=False)
    except Exception:
        pass


async def _safe_close_drawer(page: Any, drawer: Any) -> None:
    try:
        close_buttons = await _visible_items(
            drawer.locator(
                "button.el-drawer__close-btn, button.el-dialog__headerbtn, "
                ".el-drawer__close-btn, .el-dialog__headerbtn"
            )
        )
        if len(close_buttons) != 1:
            return
        await close_buttons[0].click()
    except Exception:
        return

    try:
        dialogs = await _visible_items(
            page.locator("[role='dialog']:visible, .el-message-box:visible")
        )
        safe_actions = []
        for dialog in dialogs:
            for label in ("不保存", "放弃", "离开"):
                safe_actions.extend(
                    await _visible_items(dialog.get_by_role("button", name=label, exact=True))
                )
        if len(safe_actions) == 1:
            await safe_actions[0].click()
    except Exception:
        return


def _failed_schema(spec: PlatformSpec, issue: str) -> PlatformSchema:
    return PlatformSchema(
        platform_id=spec.platform_id,
        capture_status="failed",
        category=CategoryResolution(
            status="review_required",
            reason="inspection_failed",
        ),
        issues=(issue,),
    )


async def run_platform_inspection(
    page: Any,
    drawer: Any,
    args: Any,
    product: Any,
    record: Any,
    artifact_dir: Path,
    logger: logging.Logger,
    redactor: SensitiveLogRedactor,
) -> None:
    spec = get_platform_spec(args.platform)
    context = DiscoveryContext(
        style_code=product.style_code,
        title=product.title,
        category_hints=tuple(product.category_hints),
        base_item_id=_base_item_id(record),
    )
    redactor.add_sensitive_values(context.style_code, context.title, context.base_item_id)
    runtime_sensitive_values = list(_sensitive_values_from(record))
    redactor.add_sensitive_values(*runtime_sensitive_values)

    adapter = None
    panel = None
    request_guard = None
    schema = None
    observations: Tuple[DomFieldObservation, ...] = ()
    failure: Optional[Exception] = None

    try:
        service_workers = tuple(getattr(getattr(page, "context", None), "service_workers", ()))
        if service_workers:
            raise PlatformDiscoveryError(
                "inspect-only cannot run while the browser context has active service workers"
            )
        adapter = load_platform_adapter(spec)
        request_guard = InspectionRequestGuard(page, adapter.endpoint_catalog)
        await request_guard.install()
        panel = await InspectionPanel.open(
            page,
            drawer,
            spec,
            timeout_seconds=args.timeout,
        )
        await request_guard.wait_for_runtime_identity(
            panel,
            timeout_seconds=min(5.0, max(0.0, float(args.timeout))),
        )
        adapter.panel = panel
        runtime_sensitive_values.extend(panel.runtime_sensitive_values)
        redactor.add_sensitive_values(*panel.runtime_sensitive_values)
        api = ApiClient(page=page, endpoint_catalog=adapter.endpoint_catalog)
        schema, observations = await discover_platform_schema(
            adapter,
            context,
            panel,
            api,
        )
        adapter_sensitive = _sensitive_values_from(adapter)
        runtime_sensitive_values.extend(adapter_sensitive)
        redactor.add_sensitive_values(*adapter_sensitive)
    except Exception as error:
        failure = error
        schema = _failed_schema(spec, "inspection_error:{0}".format(type(error).__name__))
        if adapter is not None:
            adapter_sensitive = _sensitive_values_from(adapter)
            runtime_sensitive_values.extend(adapter_sensitive)
            redactor.add_sensitive_values(*adapter_sensitive)

    try:
        if schema is None:
            schema = _failed_schema(spec, "inspection_error:unknown")
        await _safe_screenshot(
            page,
            Path(artifact_dir) / "{0}-inspect.png".format(spec.platform_id),
        )
        await _safe_close_drawer(page, drawer)
        if request_guard is not None:
            late_runtime_values = request_guard.runtime_identity_values
            runtime_sensitive_values.extend(late_runtime_values)
            redactor.add_sensitive_values(*late_runtime_values)
        if request_guard is not None and request_guard.blocked_requests:
            issues = tuple(schema.issues)
            if "mutation_guard_blocked" not in issues:
                issues += ("mutation_guard_blocked",)
            for reason in sorted(
                {
                    str(item.get("reason") or "unknown")
                    for item in request_guard.blocked_requests
                }
            ):
                detail_issue = "mutation_guard_blocked:{0}".format(reason)
                if detail_issue not in issues:
                    issues += (detail_issue,)
            for reason, path in sorted(
                {
                    (
                        str(item.get("reason") or "unknown"),
                        str(item.get("path") or "[invalid-path]"),
                    )
                    for item in request_guard.blocked_requests
                }
            ):
                path_issue = "mutation_guard_blocked:{0}:{1}".format(reason, path)
                if path_issue not in issues:
                    issues += (path_issue,)
            schema = replace(
                schema,
                capture_status="failed",
                issues=issues,
            )
            if failure is None:
                failure = PlatformDiscoveryError(
                    "inspect-only request guard blocked an unapproved request"
                )
        write_schema_reports(
            Path(artifact_dir),
            schema,
            observations,
            context=context,
            sensitive_values=tuple(dict.fromkeys(runtime_sensitive_values)),
        )
        logger.info(
            "%s字段发现报告已生成：状态=%s",
            spec.display_name,
            schema.capture_status,
        )
        if failure is not None:
            raise PlatformDiscoveryError("平台字段发现失败，已输出脱敏报告") from failure
        if schema.capture_status in ("partial", "failed"):
            raise PlatformDiscoveryError(
                "平台字段发现未完整，已输出脱敏报告供复核"
            )
    finally:
        if request_guard is not None:
            await request_guard.uninstall()


__all__ = [
    "InspectionPanel",
    "InspectionRequestGuard",
    "RedactingFormatter",
    "SensitiveLogRedactor",
    "load_platform_adapter",
    "run_platform_inspection",
]
