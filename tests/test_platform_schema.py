import dataclasses
import importlib
import importlib.util
import inspect
import json
import unittest


def _load_schema(test_case):
    test_case.assertIsNotNone(
        importlib.util.find_spec("platform_schema"),
        "platform_schema must be implemented",
    )
    return importlib.import_module("platform_schema")


class PlatformSchemaTests(unittest.TestCase):
    def test_field_options_preserve_complete_api_order_and_duplicate_ids(self):
        schema = _load_schema(self)
        options = [
            {"id": "long", "label": "长裤"},
            {"id": "short", "label": "短裤"},
            {"id": "long", "label": "长裤冲突项"},
        ] + [(str(index), "候选 {0}".format(index)) for index in range(25)]

        values = schema.field_options(options, source="api")

        self.assertEqual(len(values), 28)
        self.assertEqual(
            values[:3],
            (
                schema.FieldOption("long", "长裤", 0),
                schema.FieldOption("short", "短裤", 1),
                schema.FieldOption("long", "长裤冲突项", 2),
            ),
        )

    def test_field_options_preserve_pdd_vid_and_value_shape(self):
        schema = _load_schema(self)

        values = schema.field_options(
            (
                {"vid": "loose", "value": "宽松"},
                {"vid": "straight", "value": "直筒"},
            ),
            source="api",
        )

        self.assertEqual(
            values,
            (
                schema.FieldOption("loose", "宽松", 0),
                schema.FieldOption("straight", "直筒", 1),
            ),
        )

    def test_field_options_do_not_invent_missing_api_ids_or_labels(self):
        schema = _load_schema(self)

        values = schema.field_options(
            (
                {"label": "只有名称"},
                {"id": "id-only"},
                {"value": "ambiguous-value"},
            ),
            source="api",
        )

        self.assertEqual(
            values,
            (
                schema.FieldOption("", "只有名称", 0),
                schema.FieldOption("id-only", "", 1),
                schema.FieldOption("", "ambiguous-value", 2),
            ),
        )

    def test_field_options_hide_sensitive_sources_even_on_direct_field_creation(self):
        schema = _load_schema(self)
        sensitive = (schema.FieldOption("shop-1", "Internal Shop", 0),)

        for source in ("shop", "logistics", "freight"):
            self.assertEqual(schema.field_options((("shop-1", "Internal Shop"),), source=source), ())
            field = schema.FieldSchema(
                schema_key="sensitive",
                option_summary=schema.OptionSummary(source, 1, (), True, "a" * 64),
                option_values=sensitive,
            )
            self.assertEqual(field.option_values, ())

    def test_option_summary_is_deterministic_deduplicated_and_limited(self):
        schema = _load_schema(self)
        options = [
            {"id": "2", "label": "Beta"},
            ("1", "Alpha"),
            {"id": "2", "label": "Beta"},
            ("3", "Gamma"),
        ]

        first = schema.option_summary(options, limit=2)
        second = schema.option_summary(list(reversed(options)), limit=2)

        self.assertEqual(first.source, "inline")
        self.assertEqual(first.count, 3)
        self.assertEqual(first.sample, (("1", "Alpha"), ("2", "Beta")))
        self.assertTrue(first.truncated)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(len(first.sha256), 64)
        self.assertEqual(first, second)

    def test_option_summary_can_hide_samples_without_changing_count_or_hash(self):
        schema = _load_schema(self)
        options = [("2", "Beta"), ("1", "Alpha")]

        visible = schema.option_summary(options)
        hidden = schema.option_summary(options, include_samples=False)

        self.assertEqual(hidden.sample, ())
        self.assertEqual(hidden.count, visible.count)
        self.assertEqual(hidden.sha256, visible.sha256)
        self.assertTrue(hidden.truncated)

    def test_option_summary_rejects_negative_limit(self):
        schema = _load_schema(self)

        with self.assertRaises(ValueError):
            schema.option_summary([], limit=-1)

    def test_option_summary_accepts_source_and_hides_sensitive_source_samples(self):
        schema = _load_schema(self)
        self.assertIn("source", inspect.signature(schema.option_summary).parameters)
        options = (("private-id", "PRIVATE-SHOP-42"),)

        for source in ("shop", "logistics", "freight"):
            summary = schema.option_summary(options, source=source)
            self.assertEqual(summary.source, source)
            self.assertEqual(summary.count, 1)
            self.assertEqual(summary.sample, ())
            self.assertTrue(summary.truncated)

    def test_sensitive_option_summary_cannot_be_bypassed_by_direct_construction(self):
        schema = _load_schema(self)

        summary = schema.OptionSummary(
            source="shop",
            count=1,
            sample=(("shop-1", "钊叔制"),),
            truncated=False,
            sha256="a" * 64,
        )

        self.assertEqual(summary.sample, ())
        self.assertTrue(summary.truncated)

    def test_sensitive_option_source_ignores_surrounding_space_and_case(self):
        schema = _load_schema(self)
        options = (("shop-1", "Internal Shop"),)

        direct = schema.OptionSummary(
            source=" SHOP ",
            count=1,
            sample=options,
            truncated=False,
            sha256="a" * 64,
        )
        generated = schema.option_summary(options, source=" freight ")

        self.assertEqual(direct.sample, ())
        self.assertTrue(direct.truncated)
        self.assertEqual(generated.sample, ())
        self.assertTrue(generated.truncated)

    def test_option_summary_never_allows_more_than_twenty_samples(self):
        schema = _load_schema(self)
        options = tuple((str(index), "Option {0}".format(index)) for index in range(25))

        self.assertEqual(len(schema.option_summary(options).sample), 20)
        with self.assertRaises(ValueError):
            schema.option_summary(options, limit=21)

    def test_sanitize_message_removes_common_identity_values_and_truncates(self):
        schema = _load_schema(self)
        raw = (
            "request https://erp.example.test/item?id=9876543210 "
            "email operator@example.test phone +86 13800138000 order 123456789 "
            + "x" * 300
        )

        sanitized = schema.sanitize_message(raw)

        for secret in (
            "https://erp.example.test",
            "operator@example.test",
            "13800138000",
            "123456789",
        ):
            self.assertNotIn(secret, sanitized)
        self.assertLessEqual(len(sanitized), 200)
        self.assertIn("[redacted-url]", sanitized)
        self.assertIn("[redacted-email]", sanitized)
        self.assertIn("[redacted-phone]", sanitized)
        self.assertIn("[redacted-number]", sanitized)

    def test_sanitize_message_removes_grouped_mobile_numbers(self):
        schema = _load_schema(self)
        raw = "phones +86 138-0013-8000 and 139 0013 8000"

        sanitized = schema.sanitize_message(raw)

        self.assertNotIn("138-0013-8000", sanitized)
        self.assertNotIn("139 0013 8000", sanitized)
        self.assertEqual(sanitized.count("[redacted-phone]"), 2)

    def test_sanitize_message_redacts_credential_assignments(self):
        schema = _load_schema(self)
        secrets = (
            "AuthValue-XyZ",
            "TokenValue-AbC",
            "ApiKeyValue-DeF",
            "KeyValue-GhI",
            "SecretValue-JkL",
            "PasswordValue-MnO",
        )
        raw = (
            "Authorization: Bearer {0}; token = {1}; API_KEY:{2}; "
            "key={3}; secret : {4}; password={5}"
        ).format(*secrets)

        sanitized = schema.sanitize_message(raw)

        for secret in secrets:
            self.assertNotIn(secret, sanitized)
        self.assertEqual(sanitized, "[redacted-credential-message]")
        self.assertLessEqual(len(sanitized), 200)

    def test_sanitize_message_redacts_headers_prefixed_credentials_and_bare_bearer(self):
        schema = _load_schema(self)
        cases = (
            (
                "Cookie: sessionid=CookieValue-XyZ; csrf=CookieCsrf-AbC",
                ("CookieValue-XyZ", "CookieCsrf-AbC"),
            ),
            (
                "Set-Cookie = session=SetCookieValue-DeF; Path=/; HttpOnly",
                ("SetCookieValue-DeF",),
            ),
            (
                'Authorization: Digest username="DigestUser-GhI", '
                'realm="DigestRealm-JkL", nonce="DigestNonce-MnO"',
                ("DigestUser-GhI", "DigestRealm-JkL", "DigestNonce-MnO"),
            ),
            (
                'credentials {"token":"JsonToken-PqR","client_secret":"ClientSecret-StU",'
                '"csrf_token":"CsrfToken-VwX","id_token":"IdToken-YzA"}',
                ("JsonToken-PqR", "ClientSecret-StU", "CsrfToken-VwX", "IdToken-YzA"),
            ),
            (
                "sessionid=SessionValue-BcD session_token=SessionToken-EfG "
                "Bearer BareToken-HiJ",
                ("SessionValue-BcD", "SessionToken-EfG", "BareToken-HiJ"),
            ),
            (
                r'escaped {\"token\":\"EscapedToken-KlM\"}',
                ("EscapedToken-KlM",),
            ),
            (
                r"escaped \u0074oken=UnicodeToken-NpQ",
                ("UnicodeToken-NpQ",),
            ),
            (
                "&#116;&#111;&#107;&#101;&#110;=HtmlToken-RsT",
                ("HtmlToken-RsT",),
            ),
            (
                "private_key=PrivateKey-UvW access_key=AccessKey-XyZ "
                "secret_key=SecretKey-AbC x-api-key=XApiKey-DeF",
                ("PrivateKey-UvW", "AccessKey-XyZ", "SecretKey-AbC", "XApiKey-DeF"),
            ),
            (
                "Proxy-Authorization: Digest response=ProxyDigest-GhI",
                ("ProxyDigest-GhI",),
            ),
            (
                "Basic BareBasic-JkL",
                ("BareBasic-JkL",),
            ),
            (
                "to\u200bken=Opaque-MnO co\u2060okie=Opaque-PqR "
                "Ba\ufeffsic Opaque-StU se\u00adcret=Opaque-VwX",
                ("Opaque-MnO", "Opaque-PqR", "Opaque-StU", "Opaque-VwX"),
            ),
            (
                "%74%6f%6b%65%6e=Opaque-YzA",
                ("Opaque-YzA",),
            ),
        )

        for raw, secrets in cases:
            with self.subTest(raw=raw.split(" ", 1)[0]):
                sanitized = schema.sanitize_message(raw)
                for secret in secrets:
                    self.assertNotIn(secret, sanitized)
                self.assertEqual(sanitized, "[redacted-credential-message]")
                self.assertLessEqual(len(sanitized), 200)

    def test_sanitize_message_handles_invalid_long_unicode_escapes_fail_closed(self):
        schema = _load_schema(self)
        cases = (
            (r"bad \UFFFFFFFF token=InvalidEscapeToken-XyZ", "InvalidEscapeToken-XyZ"),
            (r"bad \U00110000 access_key=InvalidEscapeKey-AbC", "InvalidEscapeKey-AbC"),
        )

        for raw, secret in cases:
            with self.subTest(raw=raw):
                sanitized = schema.sanitize_message(raw)
                self.assertEqual(sanitized, "[redacted-credential-message]")
                self.assertNotIn(secret, sanitized)

    def test_external_schema_ids_are_normalized_to_strings(self):
        schema = _load_schema(self)

        field = schema.FieldSchema(schema_key="api:color", source_id=123)
        candidate = schema.CategoryCandidate(leaf_id=456, path=("men",))

        self.assertEqual(field.source_id, "123")
        self.assertIsInstance(field.source_id, str)
        self.assertEqual(candidate.leaf_id, "456")
        self.assertIsInstance(candidate.leaf_id, str)

    def test_schema_dataclasses_are_frozen_and_serialize_to_plain_json(self):
        schema = _load_schema(self)
        option_summary = schema.OptionSummary(
            source="api",
            count=1,
            sample=(("blue", "蓝色"),),
            truncated=False,
            sha256="a" * 64,
        )
        locator = schema.DomLocatorHint(
            strategy="form_label",
            section_label="属性",
            anchor="颜色",
            occurrence=1,
            control_type="select_one",
        )
        field = schema.FieldSchema(
            schema_key="api:color",
            source_id="color",
            label="颜色",
            section="attributes",
            control_type="select_one",
            value_type="string",
            required=True,
            multiple=False,
            custom_allowed=False,
            option_summary=option_summary,
            option_values=(schema.FieldOption("blue", "蓝色", 0),),
            dependencies=("category",),
            api_paths=("/safe/attributes.json",),
            dom_locator_hint=locator,
            presence="api_dom",
        )
        section = schema.SectionSchema(
            key="attributes",
            label="属性",
            order=1,
            visible_when=("category_resolved",),
            fields=(field,),
        )
        candidate = schema.CategoryCandidate(
            leaf_id="leaf-1",
            path=("男装", "休闲裤"),
            rank=1,
            score=0.9,
            recommended=True,
            validation_status="viable",
            issue_code="",
        )
        resolution = schema.CategoryResolution(
            status="resolved",
            source="recommendation",
            selected=candidate,
            candidates=(candidate,),
            reason="",
        )
        endpoint = schema.EndpointObservation(
            method="GET",
            path="/safe/attributes.json",
            parameter_names=("category_id",),
            status="ok",
            http_status=200,
            sanitized_message="",
            required_for="dynamic",
        )
        platform = schema.PlatformSchema(
            platform_id="tm",
            capture_status="complete",
            category=resolution,
            fixed_sections=(),
            dynamic_sections=(section,),
            endpoints=(endpoint,),
            issues=(),
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            platform.capture_status = "failed"

        payload = schema.to_dict(platform)
        self.assertEqual(payload, platform.to_dict())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["category"]["selected"]["path"], ["男装", "休闲裤"])
        self.assertEqual(
            payload["dynamic_sections"][0]["fields"][0]["option_summary"]["sample"],
            [["blue", "蓝色"]],
        )
        self.assertEqual(
            payload["dynamic_sections"][0]["fields"][0]["option_values"],
            [{"value_id": "blue", "label": "蓝色", "position": 0}],
        )
        json.dumps(payload, ensure_ascii=False)

    def test_schema_types_do_not_define_runtime_identity_fields(self):
        schema = _load_schema(self)
        prohibited = {
            "style_code",
            "title",
            "base_item_id",
            "shop_id",
            "cookie",
            "headers",
            "body",
        }

        for model in (
            schema.OptionSummary,
            schema.FieldOption,
            schema.EndpointObservation,
            schema.CategoryCandidate,
            schema.CategoryResolution,
            schema.DomLocatorHint,
            schema.FieldSchema,
            schema.SectionSchema,
            schema.PlatformSchema,
        ):
            self.assertTrue(dataclasses.is_dataclass(model))
            self.assertFalse(prohibited.intersection(field.name for field in dataclasses.fields(model)))

    def test_every_schema_dataclass_is_frozen(self):
        schema = _load_schema(self)
        instances = (
            schema.OptionSummary("inline", 0, (), False, "0" * 64),
            schema.FieldOption("blue", "蓝色", 0),
            schema.EndpointObservation("GET", "/detail.json"),
            schema.CategoryCandidate("leaf", ("男装",)),
            schema.CategoryResolution("pending_category"),
            schema.DomLocatorHint("form_label", "属性", "颜色"),
            schema.FieldSchema("api:color"),
            schema.SectionSchema("attributes", "属性"),
            schema.PlatformSchema(
                "tm",
                "partial",
                schema.CategoryResolution("pending_category"),
            ),
        )

        for instance in instances:
            with self.subTest(model=type(instance).__name__):
                self.assertTrue(instance.__dataclass_params__.frozen)
                field_name = dataclasses.fields(instance)[0].name
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(instance, field_name, "changed")


if __name__ == "__main__":
    unittest.main()
