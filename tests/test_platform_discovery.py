import dataclasses
import importlib
import importlib.util
import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote, quote_plus

import platform_schema


def _load_discovery(test_case):
    test_case.assertIsNotNone(
        importlib.util.find_spec("platform_discovery"),
        "platform_discovery must be implemented",
    )
    return importlib.import_module("platform_discovery")


def _safe_context(discovery):
    return discovery.DiscoveryContext(
        style_code="TEST-STYLE-CODE",
        title="Test product title",
        category_hints=(),
        base_item_id="TEST-BASE-ID",
    )


def _api_field(
    schema_key,
    label,
    section,
    source_id=None,
    control_type="text",
):
    return platform_schema.FieldSchema(
        schema_key=schema_key,
        source_id=source_id,
        label=label,
        section=section,
        control_type=control_type,
        value_type="string",
        presence="api_only",
    )


class PlatformDiscoveryTests(unittest.TestCase):
    def test_label_normalization_is_nfkc_exact_and_alias_driven(self):
        discovery = _load_discovery(self)
        aliases = {"运费模板": ("运费模版",)}

        self.assertEqual(discovery.normalize_label("＊ 重 要：　Ａ "), "A")
        self.assertEqual(discovery.normalize_label("运费模版", aliases), "运费模板")
        self.assertEqual(discovery.normalize_label("运费模版A", aliases), "运费模版A")

    def test_merge_prefers_source_id_then_unique_exact_label(self):
        discovery = _load_discovery(self)
        api_fields = (
            _api_field("api:color", "颜色", "attributes", source_id="color", control_type="select_one"),
            _api_field("api:weight", "重量：重要", "logistics"),
            _api_field("api:note", "备注", "basic", source_id="note"),
        )
        dom_fields = (
            discovery.DomFieldObservation(
                section="attributes",
                label="页面颜色",
                control_type="select_one",
                source_id="color",
                locator_hint=platform_schema.DomLocatorHint(
                    strategy="form_label",
                    section_label="属性",
                    anchor="颜色",
                    occurrence=1,
                    control_type="select_one",
                ),
            ),
            discovery.DomFieldObservation(
                section="logistics",
                label=" * 重 量 ",
                control_type="number",
                required=True,
            ),
            discovery.DomFieldObservation(
                section="attributes",
                label="尺码",
                control_type="select_many",
                multiple=True,
            ),
        )

        merged, issues = discovery.merge_api_dom_fields(api_fields, dom_fields)

        self.assertEqual([field.presence for field in merged], ["api_dom", "api_dom", "api_only", "dom_only"])
        self.assertEqual(merged[0].schema_key, "api:color")
        self.assertEqual(merged[0].dom_locator_hint.anchor, "颜色")
        self.assertEqual(merged[1].control_type, "text")
        self.assertTrue(merged[1].required)
        self.assertEqual(merged[3].label, "尺码")
        self.assertEqual(issues, ())

    def test_merge_preserves_ambiguous_duplicate_labels(self):
        discovery = _load_discovery(self)
        api_fields = (
            _api_field("api:retail-price", "价格", "sku", source_id="retail-price"),
            _api_field("api:sale-price", "价格", "sku", source_id="sale-price"),
        )
        dom_fields = (
            discovery.DomFieldObservation(section="sku", label="价格：", control_type="number"),
        )

        merged, issues = discovery.merge_api_dom_fields(api_fields, dom_fields)

        self.assertEqual([field.presence for field in merged], ["api_only", "api_only", "dom_only"])
        self.assertEqual(len(merged), 3)
        self.assertIn("ambiguous_label:sku:价格", issues)

    def test_merge_applies_only_explicit_aliases_within_the_same_section(self):
        discovery = _load_discovery(self)
        api_fields = (_api_field("api:freight", "运费模板", "logistics"),)
        dom_fields = (
            discovery.DomFieldObservation(section="logistics", label="运费模版", control_type="select_one"),
        )

        without_alias, _ = discovery.merge_api_dom_fields(api_fields, dom_fields)
        with_alias, _ = discovery.merge_api_dom_fields(
            api_fields,
            dom_fields,
            label_aliases={"运费模板": ("运费模版",)},
        )

        self.assertEqual([field.presence for field in without_alias], ["api_only", "dom_only"])
        self.assertEqual([field.presence for field in with_alias], ["api_dom"])

    def test_merge_does_not_fallback_when_nonempty_source_ids_conflict(self):
        discovery = _load_discovery(self)
        api_fields = (
            _api_field("api:color-a", "颜色", "attributes", source_id="api-color"),
        )
        dom_fields = (
            discovery.DomFieldObservation(
                section="attributes",
                label="颜色：",
                control_type="select_one",
                source_id="dom-color",
            ),
        )

        merged, issues = discovery.merge_api_dom_fields(api_fields, dom_fields)

        self.assertEqual([field.presence for field in merged], ["api_only", "dom_only"])
        self.assertTrue(any(issue.startswith("source_id_conflict:") for issue in issues))

    def test_merge_blocks_label_fallback_for_duplicate_source_ids_on_either_side(self):
        discovery = _load_discovery(self)
        cases = (
            (
                (
                    _api_field("api:color-1", "颜色", "attributes", source_id="color"),
                    _api_field("api:color-2", "图案", "attributes", source_id="color"),
                ),
                (
                    discovery.DomFieldObservation(
                        section="attributes",
                        label="颜色",
                        control_type="select_one",
                        source_id="color",
                    ),
                ),
            ),
            (
                (_api_field("api:color", "颜色", "attributes", source_id="color"),),
                (
                    discovery.DomFieldObservation(
                        section="attributes",
                        label="颜色",
                        control_type="select_one",
                        source_id="color",
                    ),
                    discovery.DomFieldObservation(
                        section="attributes",
                        label="图案",
                        control_type="select_one",
                        source_id="color",
                    ),
                ),
            ),
        )

        for api_fields, dom_fields in cases:
            with self.subTest(api_count=len(api_fields), dom_count=len(dom_fields)):
                merged, issues = discovery.merge_api_dom_fields(api_fields, dom_fields)
                self.assertFalse(any(field.presence == "api_dom" for field in merged))
                self.assertIn("ambiguous_source_id:color", issues)

    def test_mutation_guard_routes_static_resources_and_whitelists_api_requests(self):
        discovery = _load_discovery(self)
        self.assertIn("resource_type", inspect.signature(discovery.is_request_allowed).parameters)
        self.assertIn("parameter_names", inspect.signature(discovery.is_request_allowed).parameters)
        self.assertIn("post_data", inspect.signature(discovery.is_request_allowed).parameters)
        self.assertIn("safe_static_prefixes", inspect.signature(discovery.is_request_allowed).parameters)
        self.assertIn("navigation_paths", inspect.signature(discovery.is_request_allowed).parameters)
        self.assertIn("resource_type", inspect.signature(discovery.MutationGuard.allows).parameters)
        self.assertIn("parameter_names", inspect.signature(discovery.MutationGuard.allows).parameters)
        self.assertIn("post_data", inspect.signature(discovery.MutationGuard.allows).parameters)
        catalog = (
            discovery.EndpointSpec(
                method="GET",
                path="/detail.json",
                parameter_names=("item",),
            ),
            discovery.EndpointSpec(
                method="POST",
                path="/prediction/category.json",
                parameter_names=("generation", "title"),
                required_for="dynamic",
                read_only=True,
            ),
            discovery.EndpointSpec(method="POST", path="/save.json", read_only=False),
        )

        self.assertTrue(
            discovery.is_request_allowed(
                "GET",
                "/assets/app.js?v=runtime-value",
                catalog,
                resource_type="script",
                safe_static_prefixes=("/assets/",),
            )
        )
        self.assertTrue(
            discovery.is_request_allowed(
                "HEAD",
                "/assets/font.woff2",
                catalog,
                resource_type="font",
                safe_static_prefixes=("/assets/",),
            )
        )
        for resource_type, path in (
            ("document", "/index.html"),
            ("stylesheet", "/assets/site.css"),
            ("image", "/assets/product.webp"),
            ("font", "/assets/font.woff2"),
            ("script", "/assets/app.js"),
            ("media", "/assets/demo.mp4"),
        ):
            with self.subTest(resource_type=resource_type, path=path):
                self.assertTrue(
                    discovery.is_request_allowed(
                        "GET",
                        path,
                        catalog,
                        resource_type=resource_type,
                        safe_static_prefixes=("/assets/",),
                        navigation_paths=("/index.html",),
                    )
                )
        for resource_type in (
            "document",
            "stylesheet",
            "image",
            "font",
            "script",
            "media",
        ):
            with self.subTest(spoofed_resource_type=resource_type):
                self.assertFalse(
                    discovery.is_request_allowed(
                        "GET",
                        "/save-via-get.json",
                        catalog,
                        resource_type=resource_type,
                        safe_static_prefixes=("/assets/",),
                        navigation_paths=("/index.html",),
                    )
                )
        self.assertFalse(
            discovery.is_request_allowed(
                "GET",
                "/loose.js",
                catalog,
                resource_type="script",
            )
        )
        self.assertFalse(
            discovery.is_request_allowed(
                "GET",
                "/index.html",
                catalog,
                resource_type="document",
            )
        )
        self.assertTrue(
            discovery.is_request_allowed(
                "GET",
                "/detail.json?item=runtime-value",
                catalog,
                resource_type="fetch",
            )
        )
        self.assertTrue(
            discovery.is_request_allowed(
                "POST",
                "/prediction/category.json?generation=2",
                catalog,
                resource_type="xhr",
                post_data="title=PRIVATE+PRODUCT+TITLE",
            )
        )
        self.assertTrue(
            discovery.is_request_allowed(
                "POST",
                "/prediction/category.json",
                catalog,
                resource_type="fetch",
                post_data='{"title":"PRIVATE PRODUCT TITLE"}',
            )
        )
        self.assertFalse(
            discovery.is_request_allowed(
                "POST",
                "/prediction/category.json",
                catalog,
                resource_type="xhr",
                parameter_names=("title",),
            )
        )
        self.assertFalse(
            discovery.is_request_allowed(
                "POST",
                "/prediction/category.json",
                catalog,
                resource_type="xhr",
                post_data="title=PRIVATE+TITLE&unknown=PRIVATE+VALUE",
            )
        )
        for post_data in ("", "{not-json", "title=%ZZ"):
            with self.subTest(post_data=post_data):
                self.assertFalse(
                    discovery.is_request_allowed(
                        "POST",
                        "/prediction/category.json",
                        catalog,
                        resource_type="xhr",
                        post_data=post_data,
                    )
                )
        for method, path, resource_type, parameter_names in (
            ("GET", "/save.json", "fetch", ()),
            ("GET", "/unlisted-read.json", "xhr", ()),
            ("GET", "/detail.json?item=1&extra=2", "fetch", ()),
            ("GET", "/detail.json", "fetch", ("extra",)),
            ("POST", "/prediction/category.json", "xhr", ("title", "unknown")),
            ("POST", "/save.json", "xhr", ()),
            ("POST", "/unknown.json", "xhr", ()),
            ("PUT", "/detail.json", "fetch", ()),
            ("PATCH", "/detail.json", "fetch", ()),
            ("DELETE", "/detail.json", "fetch", ()),
            ("GET", "https://outside.example/detail.json", "script", ()),
        ):
            self.assertFalse(
                discovery.is_request_allowed(
                    method,
                    path,
                    catalog,
                    resource_type=resource_type,
                    parameter_names=parameter_names,
                )
            )

        guard = discovery.MutationGuard(
            catalog,
            safe_static_prefixes=("/assets/",),
            navigation_paths=("/index.html",),
        )
        guard.ensure_allowed("GET", "/assets/app.js", resource_type="script")
        guard.ensure_allowed(
            "GET",
            "/detail.json?item=runtime-value",
            resource_type="fetch",
        )
        with self.assertRaises(discovery.PlatformDiscoveryError):
            guard.ensure_allowed("GET", "/anything.json")
        with self.assertRaises(discovery.PlatformDiscoveryError) as blocked:
            guard.ensure_allowed(
                "POST",
                "/prediction/category.json?token=PRIVATE-QUERY-VALUE",
                resource_type="xhr",
                post_data="title=PRIVATE-BODY-VALUE",
            )
        self.assertNotIn("PRIVATE-QUERY-VALUE", str(blocked.exception))
        with self.assertRaises(discovery.PlatformDiscoveryError) as blocked_body:
            guard.ensure_allowed(
                "POST",
                "/prediction/category.json",
                resource_type="xhr",
                post_data="title=SAFE&unknown=PRIVATE-BODY-VALUE",
            )
        self.assertNotIn("PRIVATE-BODY-VALUE", str(blocked_body.exception))

    def test_request_paths_reject_backslashes_controls_and_encoded_cross_origin_shapes(self):
        discovery = _load_discovery(self)
        unsafe_paths = (
            "/\\outside.example/path",
            "/\t/outside",
            "/\u0085/outside",
            "/\u202e/outside",
            "/%5Coutside.example/path",
            "/%09/outside",
            "/%2F%2Foutside.example/path",
            "/assets/%252e%252e/save-via-get.json",
            "/assets/.%252e/save-via-get.json",
            "/assets/%252e%252e%252fsave-via-get.json",
            "/assets/..;/save-via-get.json",
            "/assets/%2e%2e%3b/save-via-get.json",
            "/assets/%252e%252e%253b/save-via-get.json",
        )

        for path in unsafe_paths:
            with self.subTest(path=repr(path)):
                self.assertFalse(discovery._is_relative_path(path))
                self.assertFalse(discovery.is_request_allowed("GET", path))
                self.assertFalse(
                    discovery.is_request_allowed(
                        "GET",
                        path,
                        resource_type="image",
                        safe_static_prefixes=("/assets/",),
                    )
                )

        catalog = (
            discovery.EndpointSpec(
                method="GET",
                path="/detail.json",
                parameter_names=("item",),
            ),
        )
        self.assertTrue(
            discovery.is_request_allowed(
                "GET",
                "/detail.json?item=runtime-value",
                catalog,
            )
        )
        self.assertFalse(discovery.is_request_allowed("GET", "/detail.json?item=runtime-value"))
        self.assertFalse(discovery._is_relative_path("/detail.json?item=runtime-value"))

    def test_malformed_url_is_blocked_without_leaking_url_parser_errors(self):
        discovery = _load_discovery(self)
        guard = discovery.MutationGuard(())

        try:
            allowed = discovery.is_request_allowed("GET", "http://[")
        except ValueError as error:
            self.fail("request guard must convert malformed URL errors to a rejection: {0}".format(error))
        self.assertFalse(allowed)

        try:
            guard.ensure_allowed("GET", "http://[")
        except discovery.PlatformDiscoveryError:
            pass
        except ValueError as error:
            self.fail("ensure_allowed must not expose URL parser errors: {0}".format(error))
        else:
            self.fail("ensure_allowed must block malformed URLs")

    def test_generation_tracker_rejects_late_responses(self):
        discovery = _load_discovery(self)
        tracker = discovery.GenerationTracker()

        self.assertFalse(tracker.accept(0))
        first = tracker.begin_generation()
        second = tracker.begin_generation()

        self.assertEqual((first, second), (1, 2))
        self.assertFalse(tracker.accept(first))
        self.assertTrue(tracker.accept(second))

    def test_control_classification_uses_explicit_dom_shape(self):
        discovery = _load_discovery(self)

        self.assertEqual(discovery.classify_control_type("input", {"type": "number"}), "number")
        self.assertEqual(discovery.classify_control_type("select", {"multiple": True}), "select_many")
        self.assertEqual(discovery.classify_control_type("input", {"type": "file", "accept": "video/*"}), "upload_video")
        self.assertEqual(discovery.classify_control_type("div", {"contenteditable": "true"}), "rich_text")
        self.assertEqual(discovery.classify_control_type("custom-widget", {}), "unknown")

    def test_file_control_classification_uses_mime_and_extension_evidence(self):
        discovery = _load_discovery(self)

        for accept in ("video/mp4", ".mp4", ".MOV,video/*"):
            with self.subTest(accept=accept):
                self.assertEqual(
                    discovery.classify_control_type(
                        "input",
                        {"type": "file", "accept": accept},
                    ),
                    "upload_video",
                )
        for accept in ("image/jpeg", ".jpg", ".PNG,image/*"):
            with self.subTest(accept=accept):
                self.assertEqual(
                    discovery.classify_control_type(
                        "input",
                        {"type": "file", "accept": accept},
                    ),
                    "upload_image",
                )
        self.assertEqual(
            discovery.classify_control_type(
                "input",
                {"type": "file", "accept": "application/pdf,.pdf"},
            ),
            "upload_file",
        )
        self.assertEqual(
            discovery.classify_control_type("input", {"type": "file", "accept": ""}),
            "unknown",
        )

    def test_merge_normalizes_numeric_source_ids_before_matching(self):
        discovery = _load_discovery(self)
        api_field = _api_field(
            "api:color",
            "API Color",
            "attributes",
            source_id="123",
        )
        dom_field = discovery.DomFieldObservation(
            section="attributes",
            label="DOM Color",
            control_type="select_one",
            source_id=123,
        )
        context = discovery.DiscoveryContext(
            style_code="STYLE-A",
            title="Product",
            category_hints=(),
            base_item_id=456,
        )

        try:
            merged, issues = discovery.merge_api_dom_fields((api_field,), (dom_field,))
        except TypeError as error:
            self.fail("numeric and string source ids must merge without TypeError: {0}".format(error))

        self.assertEqual(api_field.source_id, "123")
        self.assertEqual(dom_field.source_id, "123")
        self.assertEqual(context.base_item_id, "456")
        self.assertEqual([field.presence for field in merged], ["api_dom"])
        self.assertEqual(issues, ())

    def test_reports_are_plain_json_and_exclude_runtime_context_identity(self):
        discovery = _load_discovery(self)
        context = discovery.DiscoveryContext(
            style_code="PRIVATE-STYLE-42",
            title="PRIVATE PRODUCT TITLE",
            category_hints=("男装",),
            base_item_id="PRIVATE-BASE-10001",
        )
        self.assertEqual(context.style_code, "PRIVATE-STYLE-42")
        category = platform_schema.CategoryResolution(
            status="pending_category",
            reason=context.title,
        )
        endpoint = platform_schema.EndpointObservation(
            method="GET",
            path="/detail.json",
            parameter_names=("base_item_id",),
            status="ok",
            http_status=200,
            sanitized_message=platform_schema.sanitize_message(
                "contact operator@example.test at 13800138000 or https://private.example/item/12345678"
            ),
            required_for="fixed",
        )
        platform = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=category,
            endpoints=(endpoint,),
            issues=(context.style_code, context.base_item_id),
        )
        dom = (
            discovery.DomFieldObservation(
                section=context.base_item_id,
                label="商品标题 {0}".format(context.title),
                control_type="text",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                platform,
                dom,
                context=context,
                sensitive_values=(),
            )
            self.assertEqual(
                tuple(path.name for path in paths),
                ("tm.json", "tm-dom.json", "summary.json"),
            )
            payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
            combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
            self.assertEqual(payloads[0]["schema_version"], 1)
            self.assertEqual(payloads[1]["platform_id"], "tm")
            self.assertEqual(payloads[2]["capture_status"], "partial")
            self.assertFalse(list(Path(directory).glob("*.tmp")))
            for payload in payloads:
                self.assertFalse(
                    {"style_code", "title", "base_item_id", "cookie", "headers", "body"}.intersection(
                        payload
                    )
                )

        for secret in (
            "PRIVATE-STYLE-42",
            "PRIVATE PRODUCT TITLE",
            "PRIVATE-BASE-10001",
            "operator@example.test",
            "13800138000",
            "https://private.example",
        ):
            self.assertNotIn(secret, combined)

    def test_reports_reject_parent_platform_id_injection(self):
        discovery = _load_discovery(self)
        schema = platform_schema.PlatformSchema(
            platform_id="../tm",
            capture_status="partial",
            category=platform_schema.CategoryResolution(status="pending_category"),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "reports"
            with self.assertRaises(discovery.PlatformDiscoveryError):
                discovery.write_schema_reports(
                    output_dir,
                    schema,
                    (),
                    context=_safe_context(discovery),
                    sensitive_values=(),
                )
            self.assertFalse(list(root.rglob("*.json")))

    def test_reports_reject_absolute_platform_id_injection(self):
        discovery = _load_discovery(self)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = platform_schema.PlatformSchema(
                platform_id=str(root / "absolute-escape"),
                capture_status="partial",
                category=platform_schema.CategoryResolution(status="pending_category"),
            )
            with self.assertRaises(discovery.PlatformDiscoveryError):
                discovery.write_schema_reports(
                    root / "reports",
                    schema,
                    (),
                    context=_safe_context(discovery),
                    sensitive_values=(),
                )
            self.assertFalse(list(root.rglob("*.json")))

    def test_reports_reject_unregistered_platform_id(self):
        discovery = _load_discovery(self)
        schema = platform_schema.PlatformSchema(
            platform_id="unknown",
            capture_status="partial",
            category=platform_schema.CategoryResolution(status="pending_category"),
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(discovery.PlatformDiscoveryError):
                discovery.write_schema_reports(
                    Path(directory),
                    schema,
                    (),
                    context=_safe_context(discovery),
                    sensitive_values=(),
                )

    def test_reports_redact_free_text_credentials_in_every_payload(self):
        discovery = _load_discovery(self)
        secrets = (
            "AuthValue-XyZ",
            "TokenValue-AbC",
            "ApiKeyValue-DeF",
            "KeyValue-GhI",
            "SecretValue-JkL",
            "PasswordValue-MnO",
        )
        schema = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="Authorization: Bearer {0}".format(secrets[0]),
            category=platform_schema.CategoryResolution(
                status="token = {0}".format(secrets[1]),
                reason="API_KEY:{0}; key={1}".format(secrets[2], secrets[3]),
            ),
            issues=("secret : {0}; password={1}".format(secrets[4], secrets[5]),),
        )
        dom = (
            discovery.DomFieldObservation(
                section="Authorization: Bearer {0}".format(secrets[0]),
                label="token={0}; API_KEY={1}; password={2}".format(
                    secrets[1],
                    secrets[2],
                    secrets[5],
                ),
                control_type="text",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                schema,
                dom,
                context=_safe_context(discovery),
                sensitive_values=(),
            )
            reports = tuple(path.read_text(encoding="utf-8") for path in paths)

        for report in reports:
            for secret in secrets:
                self.assertNotIn(secret, report)

    def test_reports_recursively_redact_cookie_digest_and_prefixed_credentials(self):
        discovery = _load_discovery(self)
        secrets = (
            "CookieReport-XyZ",
            "DigestUser-AbC",
            "DigestNonce-DeF",
            "ClientSecret-GhI",
            "CsrfToken-JkL",
            "IdToken-MnO",
            "SetCookie-PqR",
            "SessionToken-StU",
            "BareToken-VwX",
            "EscapedToken-YzA",
            "PrivateKey-BcD",
            "AccessKey-EfG",
            "SecretKey-HiJ",
            "XApiKey-KlM",
            "BareBasic-NpQ",
            "HtmlToken-RsT",
            "ProxyDigest-UvW",
        )
        schema = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=platform_schema.CategoryResolution(
                status="review_required",
                reason="Cookie: sessionid={0}; theme=dark".format(secrets[0]),
            ),
            issues=(
                'Authorization: Digest username="{0}", nonce="{1}"'.format(
                    secrets[1],
                    secrets[2],
                ),
                'credentials {{"client_secret":"{0}","csrf_token":"{1}",'
                '"id_token":"{2}"}}'.format(secrets[3], secrets[4], secrets[5]),
                r'escaped {{\"token\":\"{0}\"}}'.format(secrets[9]),
                "private_key={0}; access_key={1}; secret_key={2}; x-api-key={3}".format(
                    secrets[10],
                    secrets[11],
                    secrets[12],
                    secrets[13],
                ),
                "&#116;&#111;&#107;&#101;&#110;={0}".format(secrets[15]),
                "Proxy-Authorization: Digest response={0}".format(secrets[16]),
            ),
        )
        dom = (
            discovery.DomFieldObservation(
                section="Set-Cookie: session={0}; Path=/".format(secrets[6]),
                label="session_token={0}; Bearer {1}; Basic {2}".format(
                    secrets[7],
                    secrets[8],
                    secrets[14],
                ),
                control_type="text",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                schema,
                dom,
                context=_safe_context(discovery),
                sensitive_values=(),
            )
            reports = tuple(path.read_text(encoding="utf-8") for path in paths)

        for report in reports:
            for secret in secrets:
                self.assertNotIn(secret, report)

    def test_reports_handle_invalid_long_unicode_escapes_fail_closed(self):
        discovery = _load_discovery(self)
        secrets = ("InvalidEscapeToken-XyZ", "InvalidEscapeKey-AbC")
        schema = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=platform_schema.CategoryResolution(
                status="review_required",
                reason=r"bad \UFFFFFFFF token=InvalidEscapeToken-XyZ",
            ),
            issues=(r"bad \U00110000 access_key=InvalidEscapeKey-AbC",),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                schema,
                (),
                context=_safe_context(discovery),
                sensitive_values=(),
            )
            reports = tuple(path.read_text(encoding="utf-8") for path in paths)

        for report in reports:
            for secret in secrets:
                self.assertNotIn(secret, report)

    def test_reports_redact_obfuscated_credential_markers(self):
        discovery = _load_discovery(self)
        secrets = ("Opaque-XyZ", "Opaque-AbC")
        schema = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=platform_schema.CategoryResolution(
                status="review_required",
                reason="to\u200bken=Opaque-XyZ",
            ),
            issues=("%74%6f%6b%65%6e=Opaque-AbC",),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                schema,
                (),
                context=_safe_context(discovery),
                sensitive_values=(),
            )
            reports = tuple(path.read_text(encoding="utf-8") for path in paths)

        for report in reports:
            for secret in secrets:
                self.assertNotIn(secret, report)

    def test_reports_sanitize_nested_schema_dom_text_and_sensitive_keys(self):
        discovery = _load_discovery(self)
        sensitive_options = platform_schema.OptionSummary(
            source="shop",
            count=1,
            sample=(("shop-1", "钊叔制"),),
            truncated=False,
            sha256="a" * 64,
        )
        locator = platform_schema.DomLocatorHint(
            strategy="form_label",
            section_label="属性 operator@example.test",
            anchor="https://private.example/field/12345678",
            occurrence=1,
            control_type="text",
        )
        field = platform_schema.FieldSchema(
            schema_key="api:title",
            source_id="product-title",
            label="商品标题 operator@example.test",
            section="basic",
            option_summary=sensitive_options,
            api_paths=("/detail.json?token=PRIVATE-TOKEN-42",),
            dom_locator_hint=locator,
        )
        section = platform_schema.SectionSchema(
            key="basic",
            label="基础资料 138-0013-8000",
            fields=(field,),
        )
        candidate = platform_schema.CategoryCandidate(
            leaf_id="1234567890",
            path=("男装 operator@example.test", "https://private.example/category"),
        )
        platform = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=platform_schema.CategoryResolution(
                status="review_required",
                selected=candidate,
                candidates=(candidate,),
                reason="category at https://private.example/item",
            ),
            fixed_sections=(section,),
            issues=("contact operator@example.test or 139 0013 8000",),
        )
        dom = (
            discovery.DomFieldObservation(
                section="basic",
                label="联系电话 138-0013-8000 operator@example.test",
                control_type="text",
                locator_hint={
                    "strategy": "form_label",
                    "anchor": "https://private.example/dom",
                    "headers": {"Authorization": "Bearer PRIVATE-AUTH-42"},
                    "body": "PRIVATE-BODY-42",
                    "sessionid": "PRIVATE-SESSION-ID-42",
                    "shopId": "PRIVATE-SHOP-ID-42",
                    "imageUrl": "https://private.example/image.jpg",
                },
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                platform,
                dom,
                context=_safe_context(discovery),
                sensitive_values=(),
            )
            payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
            combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

        for secret in (
            "钊叔制",
            "PRIVATE-TOKEN-42",
            "PRIVATE-AUTH-42",
            "PRIVATE-BODY-42",
            "PRIVATE-SESSION-ID-42",
            "PRIVATE-SHOP-ID-42",
            "operator@example.test",
            "138-0013-8000",
            "139 0013 8000",
            "https://private.example",
        ):
            self.assertNotIn(secret, combined)
        for sensitive_key in (
            "headers",
            "authorization",
            "body",
            "sessionid",
            "shopId",
            "imageUrl",
        ):
            self.assertNotIn('"{0}"'.format(sensitive_key), combined)
        self.assertIn("商品标题", payloads[0]["fixed_sections"][0]["fields"][0]["label"])
        self.assertEqual(
            payloads[0]["fixed_sections"][0]["fields"][0]["option_summary"]["sample"],
            [],
        )

    def test_reports_redact_runtime_identities_and_sensitive_dom_observations(self):
        discovery = _load_discovery(self)
        self.assertIn("context", inspect.signature(discovery.write_schema_reports).parameters)
        self.assertIn("sensitive_values", inspect.signature(discovery.write_schema_reports).parameters)
        self.assertEqual(
            inspect.signature(discovery.write_schema_reports).parameters["context"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )
        self.assertEqual(
            inspect.signature(discovery.write_schema_reports).parameters["sensitive_values"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )
        self.assertIs(
            inspect.signature(discovery.write_schema_reports).parameters["context"].default,
            inspect.Parameter.empty,
        )
        self.assertIs(
            inspect.signature(discovery.write_schema_reports).parameters["sensitive_values"].default,
            inspect.Parameter.empty,
        )
        self.assertIn("sensitive", {field.name for field in dataclasses.fields(discovery.DomFieldObservation)})

        product_title = "秋季工装休闲裤"
        style_code = "KD-FK-A"
        base_item_id = "KM-STYLE-A"
        shop_name = "钊叔制"
        context = discovery.DiscoveryContext(
            style_code=style_code,
            title=product_title,
            category_hints=(),
            base_item_id=base_item_id,
        )
        field = platform_schema.FieldSchema(
            schema_key="api:title",
            label="商品标题 {0}".format(product_title),
            section="basic",
        )
        platform = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=platform_schema.CategoryResolution(
                status="review_required",
                reason="{0} style {1}".format(product_title, style_code),
            ),
            fixed_sections=(
                platform_schema.SectionSchema(
                    key="basic",
                    label="基础资料 {0}".format(base_item_id),
                    fields=(field,),
                ),
            ),
            issues=("inspect {0} {1}".format(style_code, base_item_id),),
        )
        dom = (
            discovery.DomFieldObservation(
                section=shop_name,
                label="店铺 {0}".format(shop_name),
                control_type="select_one",
                locator_hint=platform_schema.DomLocatorHint(
                    strategy="form_label",
                    section_label=shop_name,
                    anchor=shop_name,
                    occurrence=1,
                    control_type="select_one",
                ),
                source_id=shop_name,
                sensitive=True,
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                platform,
                dom,
                context=context,
                sensitive_values=(shop_name,),
            )
            payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
            combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

        for identity in (product_title, style_code, base_item_id, shop_name):
            self.assertNotIn(identity, combined)
        self.assertIn("商品标题", payloads[0]["fixed_sections"][0]["fields"][0]["label"])
        observation = payloads[1]["observations"][0]
        self.assertEqual(observation["section"], "[redacted]")
        self.assertEqual(observation["label"], "[redacted]")
        self.assertEqual(observation["locator_hint"]["anchor"], "[redacted]")
        self.assertEqual(observation["locator_hint"]["section_label"], "[redacted]")

    def test_reports_preserve_structural_ids_but_redact_matching_runtime_identity(self):
        discovery = _load_discovery(self)
        runtime_shop_id = "900596991"
        category_id = "1234567890"
        field_id = "5288208"
        schema = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=platform_schema.CategoryResolution(
                status="resolved",
                selected=platform_schema.CategoryCandidate(
                    leaf_id=category_id,
                    path=("男装", "休闲裤"),
                ),
            ),
            fixed_sections=(
                platform_schema.SectionSchema(
                    key="fixed",
                    label="固定字段",
                    fields=(
                        platform_schema.FieldSchema(
                            schema_key="tm:fixed:{0}".format(field_id),
                            source_id=field_id,
                            label="商品标题",
                            section="fixed",
                        ),
                        platform_schema.FieldSchema(
                            schema_key="tm:fixed:runtime",
                            source_id=runtime_shop_id,
                            label="授权上下文",
                            section="fixed",
                        ),
                    ),
                ),
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                schema,
                (),
                context=_safe_context(discovery),
                sensitive_values=(runtime_shop_id,),
            )
            payload = json.loads(paths[0].read_text(encoding="utf-8"))

        self.assertEqual(payload["category"]["selected"]["leaf_id"], category_id)
        fields = payload["fixed_sections"][0]["fields"]
        self.assertEqual(fields[0]["source_id"], field_id)
        self.assertIn(field_id, fields[0]["schema_key"])
        self.assertNotEqual(fields[1]["source_id"], runtime_shop_id)
        self.assertIn("redacted", fields[1]["source_id"])

    def test_reports_redact_encoded_media_assignments_before_serializing(self):
        discovery = _load_discovery(self)
        markers = (
            "d%61ta%3Aimage/png;base64,PRIVATE-PERCENT-MEDIA",
            "data&#58;image/png;base64,PRIVATE-HTML-MEDIA",
            r"\u0064ata\u003aimage/png;base64,PRIVATE-UNICODE-MEDIA",
            (
                "preview=data:image/png;base64,PRIVATE-DIRECT-MEDIA "
                "encoded=d%61ta%3Aimage/png;base64,PRIVATE-MIXED-MEDIA"
            ),
        )
        schema = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=platform_schema.CategoryResolution(status="pending_category"),
            issues=markers,
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                schema,
                (),
                context=discovery.DiscoveryContext(
                    style_code="",
                    title="",
                    category_hints=(),
                    base_item_id=None,
                ),
                sensitive_values=(),
            )
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in paths
            )

        for secret in (
            "PRIVATE-PERCENT-MEDIA",
            "PRIVATE-HTML-MEDIA",
            "PRIVATE-UNICODE-MEDIA",
            "PRIVATE-DIRECT-MEDIA",
            "PRIVATE-MIXED-MEDIA",
        ):
            self.assertNotIn(secret, combined)

    def test_reports_redact_plain_and_encoded_copies_of_the_same_identity(self):
        discovery = _load_discovery(self)
        title = "敏感标题"
        encoded_title = quote(title)
        context = discovery.DiscoveryContext(
            style_code="SAFE-STYLE",
            title=title,
            category_hints=(),
            base_item_id="SAFE-BASE",
        )
        schema = platform_schema.PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=platform_schema.CategoryResolution(status="pending_category"),
            issues=("plain={0}; encoded={1}".format(title, encoded_title),),
        )

        with tempfile.TemporaryDirectory() as directory:
            paths = discovery.write_schema_reports(
                Path(directory),
                schema,
                (),
                context=context,
                sensitive_values=(),
            )
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in paths
            )

        self.assertNotIn(title, combined)
        self.assertNotIn(encoded_title, combined)


class PlatformDiscoveryRunnerTests(unittest.IsolatedAsyncioTestCase):
    def _fixed_fragment(self):
        return self.discovery.SchemaFragment(
            sections=(
                platform_schema.SectionSchema(
                    key="basic",
                    label="基础信息",
                    fields=(
                        platform_schema.FieldSchema(
                            schema_key="api:title",
                            source_id="title",
                            label="商品标题",
                            section="basic",
                            control_type="text",
                        ),
                    ),
                ),
            ),
            issues=("fixed_fixture",),
        )

    async def asyncSetUp(self):
        self.discovery = _load_discovery(self)
        from platform_registry import get_platform_spec

        self.spec = get_platform_spec("tmall")
        self.context = _safe_context(self.discovery)

    async def test_unresolved_category_retains_fixed_and_skips_activation_and_dynamic(self):
        discovery = self.discovery
        fixed_fragment = self._fixed_fragment()

        class Adapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            def __init__(self):
                self.activated = 0
                self.dynamic = 0

            async def capture_fixed(_self, context, panel, api):
                return fixed_fragment

            async def resolve_category(_self, context, panel, api):
                return platform_schema.CategoryResolution(
                    status="review_required",
                    source="category_tree",
                    reason="ambiguous_exact_leaf",
                )

            async def activate_category(_self, context, panel, candidate):
                _self.activated += 1

            async def capture_dynamic(_self, context, panel, api, category):
                _self.dynamic += 1
                raise AssertionError("dynamic capture must be skipped")

        class Panel:
            async def capture_dom_fields(_self):
                return (
                    discovery.DomFieldObservation(
                        section="basic",
                        label="商品标题",
                        source_id="title",
                        control_type="text",
                    ),
                )

        adapter = Adapter()
        schema, observations = await discovery.discover_platform_schema(
            adapter,
            self.context,
            Panel(),
            None,
        )

        self.assertEqual(schema.capture_status, "partial")
        self.assertEqual(schema.category.status, "review_required")
        self.assertEqual(schema.fixed_sections[0].fields[0].presence, "api_dom")
        self.assertIn("fixed_fixture", schema.issues)
        self.assertEqual(adapter.activated, 0)
        self.assertEqual(adapter.dynamic, 0)
        self.assertEqual(len(observations), 1)

    async def test_stale_dynamic_generation_is_discarded(self):
        discovery = self.discovery
        fixed_fragment = self._fixed_fragment()
        dynamic_section = platform_schema.SectionSchema(
            key="attributes",
            label="属性",
            fields=(
                platform_schema.FieldSchema(
                    schema_key="api:color",
                    source_id="color",
                    label="颜色",
                    section="attributes",
                    control_type="select_one",
                ),
            ),
        )

        class Adapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            def __init__(_self):
                _self.generation_tracker = discovery.GenerationTracker()

            async def capture_fixed(_self, context, panel, api):
                return fixed_fragment

            async def resolve_category(_self, context, panel, api):
                return platform_schema.CategoryResolution(
                    status="resolved",
                    source="recommendation",
                    selected=platform_schema.CategoryCandidate(
                        leaf_id="leaf-1",
                        path=("男装", "休闲裤"),
                        recommended=True,
                    ),
                )

            async def activate_category(_self, context, panel, candidate):
                return None

            async def capture_dynamic(_self, context, panel, api, category):
                stale_generation = _self.generation_tracker.current_generation
                _self.generation_tracker.begin_generation()
                return discovery.SchemaFragment(
                    sections=(dynamic_section,),
                    issues=("must_be_discarded",),
                    generation=stale_generation,
                )

        adapter = Adapter()
        schema, _observations = await discovery.discover_platform_schema(
            adapter,
            self.context,
            None,
            None,
        )

        self.assertEqual(schema.capture_status, "partial")
        self.assertEqual(schema.dynamic_sections, ())
        self.assertIn("stale_generation", schema.issues)
        self.assertNotIn("must_be_discarded", schema.issues)

    async def test_platform_discovery_error_is_not_swallowed(self):
        discovery = self.discovery

        class Adapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            async def capture_fixed(_self, context, panel, api):
                raise discovery.PlatformDiscoveryError("blocked by read-only guard")

            async def resolve_category(_self, context, panel, api):
                raise AssertionError

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                raise AssertionError

        with self.assertRaisesRegex(discovery.PlatformDiscoveryError, "read-only guard"):
            await discovery.discover_platform_schema(
                Adapter(),
                self.context,
                None,
                None,
            )

    async def test_dynamic_exception_is_structured_partial_not_false_stale(self):
        discovery = self.discovery
        fixed_fragment = self._fixed_fragment()

        class Adapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            def __init__(_self):
                _self.generation_tracker = discovery.GenerationTracker()

            async def capture_fixed(_self, context, panel, api):
                return fixed_fragment

            async def resolve_category(_self, context, panel, api):
                candidate = platform_schema.CategoryCandidate(
                    leaf_id="leaf-1",
                    path=("男装", "休闲裤"),
                )
                return platform_schema.CategoryResolution(
                    status="resolved",
                    source="existing",
                    selected=candidate,
                    candidates=(candidate,),
                )

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError("existing category must not activate")

            async def capture_dynamic(_self, context, panel, api, category):
                raise RuntimeError("raw private backend text")

        schema, _observations = await discovery.discover_platform_schema(
            Adapter(),
            self.context,
            None,
            None,
        )

        self.assertEqual(schema.capture_status, "partial")
        self.assertIn("capture_dynamic_error:RuntimeError", schema.issues)
        self.assertNotIn("stale_generation", schema.issues)
        self.assertNotIn("raw private backend text", json.dumps(schema.to_dict()))

    async def test_dom_scanner_uses_structural_metadata_without_values_or_placeholders(self):
        discovery = self.discovery

        class LocatorPanel:
            async def evaluate(_self, script):
                self.assertNotIn(".value", script)
                self.assertNotIn("placeholder", script.lower())
                self.assertNotIn("dataurl", script.lower())
                return (
                    {
                        "section": "物流信息",
                        "label": "运费模板",
                        "tag_name": "select",
                        "attributes": {},
                        "source_id": "freight-template",
                    },
                )

        observations = await discovery.scan_dom_fields(LocatorPanel())

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].control_type, "select_one")
        self.assertTrue(observations[0].sensitive)
        self.assertEqual(observations[0].label, "运费模板")

    async def test_sensitive_dom_identity_is_returned_marked_but_not_merged_into_schema(self):
        discovery = self.discovery
        private_label = "PRIVATE-SHOP-NAME 店铺物流"

        class Adapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            async def capture_fixed(_self, context, panel, api):
                return discovery.SchemaFragment()

            async def resolve_category(_self, context, panel, api):
                return platform_schema.CategoryResolution(status="pending_category")

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                raise AssertionError

        class Panel:
            async def capture_dom_fields(_self):
                return (
                    {
                        "section": "物流信息",
                        "label": private_label,
                        "control_type": "select_one",
                        "source_id": "PRIVATE-SHOP-SOURCE-ID",
                    },
                )

        schema, observations = await discovery.discover_platform_schema(
            Adapter(),
            self.context,
            Panel(),
            None,
        )

        self.assertTrue(observations[0].sensitive)
        serialized = json.dumps(schema.to_dict(), ensure_ascii=False)
        self.assertNotIn(private_label, serialized)
        self.assertNotIn("PRIVATE-SHOP-SOURCE-ID", serialized)

    async def test_required_endpoint_failures_make_partial_or_failed_with_structured_issue(self):
        discovery = self.discovery
        failed_endpoint = platform_schema.EndpointObservation(
            method="GET",
            path="/fixed.json",
            status="business_error",
            required_for="fixed",
        )

        class FailedFixedAdapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            async def capture_fixed(_self, context, panel, api):
                return discovery.SchemaFragment(endpoints=(failed_endpoint,))

            async def resolve_category(_self, context, panel, api):
                return platform_schema.CategoryResolution(status="pending_category")

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                raise AssertionError

        failed, _ = await discovery.discover_platform_schema(
            FailedFixedAdapter(),
            self.context,
            None,
            None,
        )
        self.assertEqual(failed.capture_status, "failed")
        self.assertIn(
            "endpoint_error:fixed:business_error:/fixed.json",
            failed.issues,
        )

        dynamic_error = platform_schema.EndpointObservation(
            method="GET",
            path="/dynamic.json",
            status="http_error",
            required_for="dynamic",
        )

        class PartialAdapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            def __init__(_self):
                _self.generation_tracker = discovery.GenerationTracker()

            async def capture_fixed(_self, context, panel, api):
                return self._fixed_fragment()

            async def resolve_category(_self, context, panel, api):
                candidate = platform_schema.CategoryCandidate("leaf", ("叶子",))
                return platform_schema.CategoryResolution(
                    status="resolved",
                    source="existing",
                    selected=candidate,
                )

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                return discovery.SchemaFragment(
                    endpoints=(dynamic_error,),
                    generation=_self.generation_tracker.current_generation,
                )

        partial, _ = await discovery.discover_platform_schema(
            PartialAdapter(),
            self.context,
            None,
            None,
        )
        self.assertEqual(partial.capture_status, "partial")
        self.assertIn(
            "endpoint_error:dynamic:http_error:/dynamic.json",
            partial.issues,
        )

    async def test_empty_fixed_endpoint_is_allowed_when_explicit_fallback_has_fields(self):
        discovery = self.discovery
        empty = platform_schema.EndpointObservation(
            method="GET",
            path="/tm/detail.json",
            status="empty",
            required_for="fixed",
        )
        fallback = platform_schema.EndpointObservation(
            method="GET",
            path="/tm/detailByOtherShop.json",
            status="ok",
            required_for="fixed",
        )

        class Adapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            def __init__(_self):
                _self.generation_tracker = discovery.GenerationTracker()

            async def capture_fixed(_self, context, panel, api):
                fragment = self._fixed_fragment()
                return discovery.SchemaFragment(
                    sections=fragment.sections,
                    endpoints=(empty, fallback),
                )

            async def resolve_category(_self, context, panel, api):
                candidate = platform_schema.CategoryCandidate("leaf", ("叶子",))
                return platform_schema.CategoryResolution(
                    status="resolved",
                    source="existing",
                    selected=candidate,
                )

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                return discovery.SchemaFragment(
                    generation=_self.generation_tracker.current_generation,
                )

        schema, _ = await discovery.discover_platform_schema(
            Adapter(),
            self.context,
            None,
            None,
        )
        self.assertEqual(schema.capture_status, "complete")
        self.assertFalse(any("empty" in issue for issue in schema.issues))

    async def test_empty_required_stage_without_fields_or_ok_fallback_is_incomplete(self):
        discovery = self.discovery
        fixed_empty = platform_schema.EndpointObservation(
            method="GET",
            path="/fixed-empty.json",
            status="empty",
            required_for="fixed",
        )
        dynamic_empty = platform_schema.EndpointObservation(
            method="GET",
            path="/dynamic-empty.json",
            status="empty",
            required_for="dynamic",
        )

        class FixedEmptyAdapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            async def capture_fixed(_self, context, panel, api):
                return discovery.SchemaFragment(endpoints=(fixed_empty,))

            async def resolve_category(_self, context, panel, api):
                return platform_schema.CategoryResolution(status="pending_category")

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                raise AssertionError

        fixed_schema, _ = await discovery.discover_platform_schema(
            FixedEmptyAdapter(),
            self.context,
            None,
            None,
        )
        self.assertEqual(fixed_schema.capture_status, "failed")
        self.assertIn("stage_empty:fixed", fixed_schema.issues)

        class DynamicEmptyAdapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            def __init__(_self):
                _self.generation_tracker = discovery.GenerationTracker()

            async def capture_fixed(_self, context, panel, api):
                return self._fixed_fragment()

            async def resolve_category(_self, context, panel, api):
                candidate = platform_schema.CategoryCandidate("leaf", ("叶子",))
                return platform_schema.CategoryResolution(
                    status="resolved",
                    source="existing",
                    selected=candidate,
                )

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                return discovery.SchemaFragment(
                    endpoints=(dynamic_empty,),
                    generation=_self.generation_tracker.current_generation,
                )

        dynamic_schema, _ = await discovery.discover_platform_schema(
            DynamicEmptyAdapter(),
            self.context,
            None,
            None,
        )
        self.assertEqual(dynamic_schema.capture_status, "partial")
        self.assertIn("stage_empty:dynamic", dynamic_schema.issues)

    async def test_empty_required_stage_with_ok_but_no_fields_is_partial(self):
        discovery = self.discovery
        empty = platform_schema.EndpointObservation(
            method="GET",
            path="/dynamic-empty.json",
            status="empty",
            required_for="dynamic",
        )
        fallback = platform_schema.EndpointObservation(
            method="GET",
            path="/dynamic-fallback.json",
            status="ok",
            required_for="dynamic",
        )

        class Adapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            def __init__(_self):
                _self.generation_tracker = discovery.GenerationTracker()

            async def capture_fixed(_self, context, panel, api):
                return discovery.SchemaFragment(
                    sections=self._fixed_fragment().sections,
                )

            async def resolve_category(_self, context, panel, api):
                candidate = platform_schema.CategoryCandidate("leaf", ("叶子",))
                return platform_schema.CategoryResolution(
                    status="resolved",
                    source="existing",
                    selected=candidate,
                )

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                return discovery.SchemaFragment(
                    endpoints=(empty, fallback),
                    generation=_self.generation_tracker.current_generation,
                )

        schema, _ = await discovery.discover_platform_schema(
            Adapter(),
            self.context,
            None,
            None,
        )
        self.assertEqual(schema.capture_status, "partial")
        self.assertIn("stage_empty:dynamic", schema.issues)

    async def test_empty_required_stage_accepts_fallback_that_produces_fields(self):
        discovery = self.discovery
        empty = platform_schema.EndpointObservation(
            method="GET",
            path="/dynamic-empty.json",
            status="empty",
            required_for="dynamic",
        )
        fallback = platform_schema.EndpointObservation(
            method="GET",
            path="/dynamic-fallback.json",
            status="ok",
            required_for="dynamic",
        )

        class Adapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            def __init__(_self):
                _self.generation_tracker = discovery.GenerationTracker()

            async def capture_fixed(_self, context, panel, api):
                return self._fixed_fragment()

            async def resolve_category(_self, context, panel, api):
                candidate = platform_schema.CategoryCandidate("leaf", ("叶子",))
                return platform_schema.CategoryResolution(
                    status="resolved",
                    source="existing",
                    selected=candidate,
                )

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                field = platform_schema.FieldSchema(
                    schema_key="dynamic:color",
                    source_id="color",
                    label="颜色",
                    section="attributes",
                    control_type="select_one",
                )
                return discovery.SchemaFragment(
                    sections=(
                        platform_schema.SectionSchema(
                            key="attributes",
                            label="属性",
                            fields=(field,),
                        ),
                    ),
                    endpoints=(empty, fallback),
                    generation=_self.generation_tracker.current_generation,
                )

        schema, _ = await discovery.discover_platform_schema(
            Adapter(),
            self.context,
            None,
            None,
        )
        self.assertEqual(schema.capture_status, "complete")
        self.assertNotIn("stage_empty:dynamic", schema.issues)

    async def test_dom_snapshots_keep_initial_only_fields_fixed_and_use_dynamic_delta(self):
        discovery = self.discovery

        class Panel:
            def __init__(_self):
                _self.calls = 0

            async def capture_dom_fields(_self):
                _self.calls += 1
                fixed = discovery.DomFieldObservation(
                    section="basic",
                    label="初始说明",
                    control_type="text",
                    source_id="initial-note",
                )
                if _self.calls == 1:
                    return (fixed,)
                return (
                    fixed,
                    discovery.DomFieldObservation(
                        section="attributes",
                        label="颜色",
                        control_type="select_one",
                        source_id="color",
                    ),
                )

        class Adapter:
            spec = self.spec
            endpoint_catalog = ()
            label_aliases = {}

            def __init__(_self):
                _self.generation_tracker = discovery.GenerationTracker()

            async def capture_fixed(_self, context, panel, api):
                return discovery.SchemaFragment()

            async def resolve_category(_self, context, panel, api):
                candidate = platform_schema.CategoryCandidate("leaf", ("叶子",))
                return platform_schema.CategoryResolution(
                    status="resolved",
                    source="existing",
                    selected=candidate,
                )

            async def activate_category(_self, context, panel, candidate):
                raise AssertionError

            async def capture_dynamic(_self, context, panel, api, category):
                return discovery.SchemaFragment(
                    generation=_self.generation_tracker.current_generation,
                )

        panel = Panel()
        schema, observations = await discovery.discover_platform_schema(
            Adapter(),
            self.context,
            panel,
            None,
        )
        fixed_labels = tuple(
            field.label for section in schema.fixed_sections for field in section.fields
        )
        dynamic_labels = tuple(
            field.label for section in schema.dynamic_sections for field in section.fields
        )
        self.assertEqual(panel.calls, 2)
        self.assertEqual(fixed_labels, ("初始说明",))
        self.assertEqual(dynamic_labels, ("颜色",))
        self.assertEqual(tuple(item.label for item in observations), ("初始说明", "颜色"))

    async def test_activation_requires_explicit_panel_protocol_never_generic_text_click(self):
        discovery = self.discovery
        from pdd_listing import PddListing
        from tm_listing import TmallListing
        from wxsph_listing import WxsphListing
        from xhs_listing import XhsListing

        class TextOnlyPanel:
            def __init__(_self):
                _self.text_calls = 0

            def get_by_text(_self, *args, **kwargs):
                _self.text_calls += 1
                raise AssertionError("generic text lookup must never run")

        candidate = platform_schema.CategoryCandidate("leaf", ("男装", "休闲裤"))
        for adapter_type in (TmallListing, PddListing, WxsphListing, XhsListing):
            with self.subTest(adapter=adapter_type.__name__):
                panel = TextOnlyPanel()
                with self.assertRaises(discovery.PlatformDiscoveryError):
                    await adapter_type().activate_category(
                        self.context,
                        panel,
                        candidate,
                    )
                self.assertEqual(panel.text_calls, 0)


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_transport_unwraps_real_business_envelope_and_failure(self):
        discovery = _load_discovery(self)
        endpoint = discovery.EndpointSpec(
            method="GET",
            path="/detail.json",
            parameter_names=("title",),
            required_for="fixed",
            read_only=True,
        )

        class Page:
            def __init__(_self, envelope):
                _self.envelope = envelope
                _self.scripts = []

            async def evaluate(_self, script, arguments):
                _self.scripts.append((script, arguments))
                return {
                    "payload": _self.envelope,
                    "http_status": 200,
                    "ok": True,
                    "message": _self.envelope.get("message", ""),
                }

        success_page = Page(
            {
                "result": 1,
                "message": "success",
                "data": {"fieldDescriptorList": [{"id": "title"}]},
            }
        )
        client = discovery.ApiClient(page=success_page, endpoint_catalog=(endpoint,))
        payload, observation = await client.request(endpoint, {"title": "safe"})
        self.assertEqual(payload, {"fieldDescriptorList": [{"id": "title"}]})
        self.assertEqual(observation.status, "ok")

        failure_page = Page(
            {
                "result": 0,
                "message": "类目尚未开放 PRIVATE-TITLE",
                "data": None,
            }
        )
        client = discovery.ApiClient(page=failure_page, endpoint_catalog=(endpoint,))
        payload, observation = await client.request(
            endpoint,
            {"title": "PRIVATE-TITLE"},
        )
        self.assertIsNone(payload)
        self.assertEqual(observation.status, "business_error")
        self.assertNotIn("PRIVATE-TITLE", observation.sanitized_message)

    async def test_common_success_code_failure_is_detected_without_misreading_business_object(self):
        discovery = _load_discovery(self)
        endpoint = discovery.EndpointSpec("GET", "/shape.json", read_only=True)
        responses = iter(
            (
                {"payload": {"success": False, "code": 500, "message": "失败", "data": None}},
                {"payload": {"code": "cotton", "name": "棉"}},
            )
        )

        async def transport(_endpoint, _parameters):
            return next(responses)

        client = discovery.ApiClient(endpoint_catalog=(endpoint,), transport=transport)
        failed_payload, failed = await client.request(endpoint)
        business_payload, business = await client.request(endpoint)
        self.assertIsNone(failed_payload)
        self.assertEqual(failed.status, "business_error")
        self.assertEqual(business_payload, {"code": "cotton", "name": "棉"})
        self.assertEqual(business.status, "ok")

    async def test_payload_wrapper_unwraps_pure_data_once_only(self):
        discovery = _load_discovery(self)
        endpoint = discovery.EndpointSpec("GET", "/shape.json", read_only=True)

        class Page:
            async def evaluate(_self, script, arguments):
                return {
                    "payload": {"data": {"fieldDescriptorList": [{"id": "title"}]}},
                    "http_status": 200,
                    "ok": True,
                }

        page_client = discovery.ApiClient(page=Page(), endpoint_catalog=(endpoint,))
        payload, observation = await page_client.request(endpoint)
        self.assertEqual(payload, {"fieldDescriptorList": [{"id": "title"}]})
        self.assertEqual(observation.status, "ok")

        async def transport(_endpoint, _parameters):
            return {
                "data": {
                    "data": {"businessValue": 1},
                    "name": "ordinary business mapping",
                }
            }

        injected_client = discovery.ApiClient(
            endpoint_catalog=(endpoint,),
            transport=transport,
        )
        business_payload, business_observation = await injected_client.request(endpoint)
        self.assertEqual(
            business_payload,
            {
                "data": {"businessValue": 1},
                "name": "ordinary business mapping",
            },
        )
        self.assertEqual(business_observation.status, "ok")

    async def test_page_fetch_has_abort_timeout_and_maps_timeout_status(self):
        discovery = _load_discovery(self)
        endpoint = discovery.EndpointSpec("GET", "/slow.json", read_only=True)

        class Page:
            async def evaluate(_self, script, arguments):
                self.assertIn("AbortController", script)
                self.assertIn("timeout_ms", arguments)
                return {
                    "payload": None,
                    "status": "timeout",
                    "http_status": None,
                    "ok": False,
                    "message": "request_timeout",
                }

        client = discovery.ApiClient(page=Page(), endpoint_catalog=(endpoint,))
        payload, observation = await client.request(endpoint)
        self.assertIsNone(payload)
        self.assertEqual(observation.status, "timeout")

    async def test_encoded_identity_and_media_assignments_are_removed_from_message(self):
        discovery = _load_discovery(self)
        private_title = "敏感标题"
        endpoint = discovery.EndpointSpec(
            "GET",
            "/message.json",
            parameter_names=("title",),
            read_only=True,
        )
        messages = iter(
            (
                "id=\\u654f\\u611f\\u6807\\u9898; html=&#25935;&#24863;&#26631;&#39064;; percent=%E6%95%8F%E6%84%9F%E6%A0%87%E9%A2%98",
                "failure preview=data:image/png;base64,PRIVATE-BLOB src=https://private.example/media.png video=PRIVATE-VIDEO",
                "failure d%61ta%3Aimage/png;base64,PRIVATE-PERCENT-MEDIA",
                "failure data&#58;image/png;base64,PRIVATE-HTML-MEDIA",
                r"failure \u0064ata\u003aimage/png;base64,PRIVATE-UNICODE-MEDIA",
            )
        )

        async def transport(_endpoint, _parameters):
            return {"data": {"safe": True}, "message": next(messages)}

        client = discovery.ApiClient(endpoint_catalog=(endpoint,), transport=transport)
        _payload, identity_observation = await client.request(
            endpoint,
            {"title": private_title},
        )
        self.assertEqual(
            identity_observation.sanitized_message,
            "[redacted-identity-message]",
        )
        _payload, media_observation = await client.request(
            endpoint,
            {"title": private_title},
        )
        self.assertIn("failure", media_observation.sanitized_message)
        for secret in (
            "data:image",
            "PRIVATE-BLOB",
            "private.example",
            "PRIVATE-VIDEO",
        ):
            self.assertNotIn(secret, media_observation.sanitized_message)
        for secret in (
            "PRIVATE-PERCENT-MEDIA",
            "PRIVATE-HTML-MEDIA",
            "PRIVATE-UNICODE-MEDIA",
        ):
            _payload, encoded_media = await client.request(
                endpoint,
                {"title": private_title},
            )
            self.assertNotIn(secret, encoded_media.sanitized_message)

    async def test_aria_multiselect_dom_sets_control_type_and_multiple(self):
        discovery = _load_discovery(self)

        class Panel:
            async def capture_dom_fields(_self):
                return (
                    {
                        "section": "attributes",
                        "label": "适用场景",
                        "tag_name": "div",
                        "attributes": {
                            "role": "combobox",
                            "aria-multiselectable": "true",
                        },
                        # The real DOM scanner emits hasAttribute('multiple')
                        # as False for ARIA-only multi-select widgets.
                        "multiple": False,
                    },
                )

        observations = await discovery.scan_dom_fields(Panel())
        self.assertEqual(observations[0].control_type, "select_many")
        self.assertTrue(observations[0].multiple)

    async def test_client_uses_only_whitelisted_read_only_endpoint_specs(self):
        discovery = _load_discovery(self)
        endpoint = discovery.EndpointSpec(
            method="POST",
            path="/prediction/category.json",
            parameter_names=("title",),
            required_for="dynamic",
            read_only=True,
        )
        calls = []

        async def transport(spec, parameters):
            calls.append((spec, parameters))
            return {
                "data": {"candidates": []},
                "status": "ok",
                "http_status": 200,
                "message": "PRIVATE PRODUCT TITLE read https://private.example/12345678",
            }

        client = discovery.ApiClient(endpoint_catalog=(endpoint,), transport=transport)
        payload, observation = await client.request(endpoint, {"title": "PRIVATE PRODUCT TITLE"})

        self.assertEqual(payload, {"candidates": []})
        self.assertEqual(calls, [(endpoint, {"title": "PRIVATE PRODUCT TITLE"})])
        self.assertEqual(observation.method, "POST")
        self.assertEqual(observation.path, "/prediction/category.json")
        self.assertEqual(observation.parameter_names, ("title",))
        self.assertEqual(observation.required_for, "dynamic")
        self.assertNotIn("PRIVATE PRODUCT TITLE", json.dumps(observation.to_dict()))
        self.assertNotIn("private.example", observation.sanitized_message)

        with self.assertRaises(discovery.PlatformDiscoveryError):
            await client.request(discovery.EndpointSpec(method="GET", path="/unknown.json"))
        with self.assertRaises(discovery.PlatformDiscoveryError):
            await client.request(
                discovery.EndpointSpec(method="GET", path="https://outside.example/detail.json")
            )
        with self.assertRaises(discovery.PlatformDiscoveryError):
            await client.request(endpoint, {"undeclared": "SECRET-VALUE"})

    async def test_client_rejects_unsafe_endpoint_paths_before_transport(self):
        discovery = _load_discovery(self)
        calls = []

        async def transport(spec, parameters):
            calls.append((spec, parameters))
            return {"data": {}}

        for path in (
            "/\\outside.example/path",
            "/\t/outside",
            "/\u0085/outside",
            "/\u202e/outside",
            "/%5Coutside.example/path",
            "/detail.json?item=forbidden-in-spec",
        ):
            with self.subTest(path=repr(path)):
                endpoint = discovery.EndpointSpec(method="GET", path=path)
                client = discovery.ApiClient(endpoint_catalog=(endpoint,), transport=transport)
                with self.assertRaises(discovery.PlatformDiscoveryError):
                    await client.request(endpoint)
        self.assertEqual(calls, [])

    async def test_client_redacts_encoded_parameters_and_rejects_untrusted_status(self):
        discovery = _load_discovery(self)
        private_title = "PRIVATE PRODUCT TITLE"
        endpoint = discovery.EndpointSpec(
            method="POST",
            path="/prediction/category.json",
            parameter_names=("title",),
            read_only=True,
        )

        async def transport(_spec, _parameters):
            return {
                "data": {"candidates": []},
                "status": "PRIVATE-STATUS",
                "http_status": 200,
                "message": "raw={0} quoted={1} plus={2}".format(
                    private_title,
                    quote(private_title, safe=""),
                    quote_plus(private_title, safe=""),
                ),
                "headers": {"Authorization": "Bearer PRIVATE-HEADER-42"},
                "body": "PRIVATE-BODY-42",
            }

        client = discovery.ApiClient(endpoint_catalog=(endpoint,), transport=transport)
        payload, observation = await client.request(endpoint, {"title": private_title})
        serialized = json.dumps(observation.to_dict(), ensure_ascii=False)

        self.assertEqual(payload, {"candidates": []})
        self.assertEqual(observation.status, "schema_error")
        for secret in (
            private_title,
            quote(private_title, safe=""),
            quote_plus(private_title, safe=""),
            "PRIVATE-STATUS",
            "PRIVATE-HEADER-42",
            "PRIVATE-BODY-42",
        ):
            self.assertNotIn(secret, serialized)

    async def test_client_decodes_url_search_params_before_redacting_parameters(self):
        discovery = _load_discovery(self)
        private_title = "PRIVATE~TITLE X"
        chinese_title = "秋季工装休闲裤"

        def form_component(value):
            return quote_plus(value, safe="").replace("~", "%7E")

        form_title = form_component(private_title)
        lower_chinese = re.sub(
            r"%[0-9A-F]{2}",
            lambda match: match.group(0).lower(),
            form_component(chinese_title),
        )
        triple_title = quote_plus(quote_plus(form_title, safe=""), safe="")
        endpoint = discovery.EndpointSpec(
            method="POST",
            path="/prediction/category.json",
            parameter_names=("title", "category"),
            read_only=True,
        )

        async def transport(_spec, _parameters):
            return {
                "data": {"candidates": []},
                "status": "ok",
                "http_status": 200,
                "message": "form={0}; lower={1}; triple={2}".format(
                    form_title,
                    lower_chinese,
                    triple_title,
                ),
            }

        client = discovery.ApiClient(endpoint_catalog=(endpoint,), transport=transport)
        _payload, observation = await client.request(
            endpoint,
            {"title": private_title, "category": chinese_title},
        )

        for secret in (
            private_title,
            chinese_title,
            form_title,
            lower_chinese,
            triple_title,
            "%7ETITLE+X",
        ):
            self.assertNotIn(secret, observation.sanitized_message)

    async def test_client_http_failure_overrides_explicit_success_status(self):
        discovery = _load_discovery(self)
        endpoint = discovery.EndpointSpec(method="GET", path="/detail.json")
        envelopes = (
            {
                "data": {"item": "from-data"},
                "status": "ok",
                "http_status": 500,
                "ok": True,
            },
            {
                "payload": {"item": "from-payload"},
                "status": "ok",
                "http_status": 200,
                "ok": False,
            },
        )

        for envelope in envelopes:
            with self.subTest(envelope=tuple(sorted(envelope))):
                async def transport(_spec, _parameters, result=envelope):
                    return result

                client = discovery.ApiClient(
                    endpoint_catalog=(endpoint,),
                    transport=transport,
                )
                _payload, observation = await client.request(endpoint)

                self.assertEqual(observation.status, "http_error")


if __name__ == "__main__":
    unittest.main()
