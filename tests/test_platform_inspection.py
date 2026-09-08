import html
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

from platform_discovery import EndpointSpec, PlatformDiscoveryError
from platform_registry import get_platform_spec
from platform_schema import CategoryCandidate, CategoryResolution, PlatformSchema


class _Collection:
    def __init__(self, values=()):
        self.values = list(values)

    @property
    def first(self):
        return self.values[0] if self.values else _MissingLocator()

    async def count(self):
        return len(self.values)

    def nth(self, index):
        return self.values[index]


class _MissingLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 0

    async def is_visible(self):
        return False


class _Action:
    def __init__(self):
        self.clicked = False

    async def is_visible(self):
        return True

    async def click(self):
        self.clicked = True


class _CategoryNode:
    def __init__(self, category_id, action=None):
        self.category_id = category_id
        self.action = action

    async def is_visible(self):
        return True

    async def get_attribute(self, name):
        if name in (
            "data-category-id",
            "data-leaf-category-id",
            "data-cid",
            "data-leaf-id",
        ):
            return self.category_id if name == "data-category-id" else None
        return None

    def get_by_role(self, role, name=None, exact=None):
        if role == "button" and self.action is not None:
            return _Collection((self.action,))
        return _Collection()

    def get_by_text(self, text, exact=None):
        return _Collection()


class _RecommendationPath:
    def __init__(self, text):
        self.text = text

    async def is_visible(self):
        return True

    async def inner_text(self):
        return self.text


class _RecommendationRow:
    def __init__(self, path, action):
        self.path = _RecommendationPath(path)
        self.action = action

    async def is_visible(self):
        return True

    def locator(self, selector):
        if "category-path" in selector:
            return _Collection((self.path,))
        return _Collection()

    def get_by_role(self, role, name=None, exact=None):
        if role == "button":
            return _Collection((self.action,))
        return _Collection()

    def get_by_text(self, text, exact=None):
        return _Collection()


class _PanelLocator:
    def __init__(self, nodes=(), evaluate_result=(), recommendation_rows=()):
        self.nodes = tuple(nodes)
        self.evaluate_result = evaluate_result
        self.recommendation_rows = tuple(recommendation_rows)
        self.global_action = _Action()

    def locator(self, selector):
        if "data-category-id" in selector:
            return _Collection(self.nodes)
        if "prediction-item" in selector:
            return _Collection(self.recommendation_rows)
        return _Collection()

    def get_by_text(self, text, exact=None):
        return _Collection((self.global_action,))

    async def evaluate(self, _script, _argument=None):
        return self.evaluate_result


class _Page:
    def __init__(self):
        self.url = "https://scm.superboss.cc/supplier/prod/center"
        self.context = SimpleNamespace(service_workers=[])
        self.route_calls = []
        self.unroute_calls = []
        self.screenshot_calls = []

    async def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))

    async def unroute(self, pattern, handler):
        self.unroute_calls.append((pattern, handler))

    async def screenshot(self, **kwargs):
        self.screenshot_calls.append(kwargs)

    def locator(self, _selector):
        return _Collection()


class _Drawer:
    def locator(self, _selector):
        return _Collection()


class SensitiveLogRedactorTests(unittest.TestCase):
    def test_nested_identity_collection_ignores_boolean_and_short_numeric_sentinels(self):
        from platform_inspection import SensitiveLogRedactor, _sensitive_values_from

        values = _sensitive_values_from(
            {
                "shop": {
                    "shopId": "987654321",
                    "status": 0,
                    "enabled": True,
                    "shopNames": "[]",
                    "storeIds": "{}",
                }
            }
        )
        redactor = SensitiveLogRedactor(values)

        result = redactor.redact("2026-08-30 21:10:30 shop=987654321")

        self.assertEqual(values, ("987654321",))
        self.assertIn("2026-08-30 21:10:30", result)
        self.assertNotIn("987654321", result)

    def test_redacts_raw_and_encoded_identities_urls_credentials_and_media(self):
        from platform_inspection import SensitiveLogRedactor

        identity = "私密<标题>/42"
        redactor = SensitiveLogRedactor((identity,))
        encoded = (
            identity,
            quote(identity, safe=""),
            html.escape(identity, quote=True),
            identity.encode("unicode_escape").decode("ascii"),
        )
        message = " | ".join(encoded) + (
            " https://private.example/item?token=abc "
            "Authorization: Bearer secret-value /tmp/private-cover.jpg "
            '{"token":"json-secret","cookie":"session-secret"}'
        )

        result = redactor.redact(message)

        for value in encoded:
            self.assertNotIn(value, result)
        self.assertNotIn("private.example", result)
        self.assertNotIn("secret-value", result)
        self.assertNotIn("json-secret", result)
        self.assertNotIn("session-secret", result)
        self.assertNotIn("private-cover.jpg", result)

    def test_setup_logging_redacts_the_written_run_log(self):
        import kuaimai_erp
        from platform_inspection import SensitiveLogRedactor

        identity = "私密<标题>/42"
        with tempfile.TemporaryDirectory() as directory:
            redactor = SensitiveLogRedactor((identity,))
            logger = kuaimai_erp.setup_logging(Path(directory), redactor=redactor)
            logger.error(
                "%s | %s | token=secret | https://private.example/a | /tmp/private.jpg",
                identity,
                quote(identity, safe=""),
            )
            for handler in logger.handlers:
                handler.flush()
            text = (Path(directory) / "run.log").read_text(encoding="utf-8")
            for handler in logger.handlers:
                handler.close()

        self.assertNotIn(identity, text)
        self.assertNotIn(quote(identity, safe=""), text)
        self.assertNotIn("secret", text)
        self.assertNotIn("private.example", text)
        self.assertNotIn("private.jpg", text)


class AdapterLoadingTests(unittest.TestCase):
    def test_accepts_get_and_readonly_post_but_rejects_write_post(self):
        from platform_inspection import load_platform_adapter

        spec = get_platform_spec("tmall")

        class SafeAdapter:
            endpoint_catalog = (
                EndpointSpec("GET", "/read.json", (), read_only=False),
                EndpointSpec("POST", "/predict.json", (), read_only=True),
            )

            def __init__(self):
                self.spec = spec

        with patch(
            "platform_inspection.importlib.import_module",
            return_value=SimpleNamespace(TmallListing=SafeAdapter),
        ):
            self.assertIsInstance(load_platform_adapter(spec), SafeAdapter)

        SafeAdapter.endpoint_catalog += (
            EndpointSpec("POST", "/save.json", (), read_only=False),
        )
        with patch(
            "platform_inspection.importlib.import_module",
            return_value=SimpleNamespace(TmallListing=SafeAdapter),
        ):
            with self.assertRaises(PlatformDiscoveryError):
                load_platform_adapter(spec)


class InspectionRequestGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_late_readonly_request_identity_before_applying_it(self):
        from platform_inspection import InspectionRequestGuard

        page = _Page()
        guard = InspectionRequestGuard(
            page,
            (
                EndpointSpec(
                    "GET",
                    "/tm/getBrandList.json",
                    ("shopId", "api_name"),
                    read_only=True,
                ),
            ),
        )
        waits = []

        async def wait_for_timeout(milliseconds):
            waits.append(milliseconds)
            guard._remember_shop_id("PRIVATE-LATE-SHOP-9")

        page.wait_for_timeout = wait_for_timeout
        panel = SimpleNamespace(
            shop_id="",
            shopId="",
            runtime_sensitive_values=(),
        )

        found = await guard.wait_for_runtime_identity(panel, timeout_seconds=1)

        self.assertTrue(found)
        self.assertEqual(waits, [100])
        self.assertEqual(panel.shop_id, "PRIVATE-LATE-SHOP-9")
        self.assertEqual(
            panel.runtime_sensitive_values,
            ("PRIVATE-LATE-SHOP-9",),
        )

    async def test_observes_user_identity_without_mislabeling_it_as_shop_identity(self):
        from platform_inspection import InspectionRequestGuard

        page = _Page()
        guard = InspectionRequestGuard(
            page,
            (
                EndpointSpec(
                    "GET",
                    "/wxsph/getTemplateList.json",
                    ("userId", "api_name"),
                    read_only=True,
                ),
            ),
        )
        request = SimpleNamespace(
            url=(
                "https://scm.superboss.cc/wxsph/getTemplateList.json"
                "?userId=PRIVATE-WX-USER-9&api_name=wxsph_getTemplateList"
            ),
            method="GET",
            resource_type="xhr",
            post_data=None,
        )
        route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        panel = SimpleNamespace(
            shop_id="",
            shopId="",
            runtime_sensitive_values=(),
        )

        await guard._handle(route, request)
        guard.apply_runtime_identity(panel)

        route.continue_.assert_awaited_once()
        self.assertEqual(panel.shop_id, "")
        self.assertEqual(panel.user_id, "PRIVATE-WX-USER-9")
        self.assertEqual(panel.userId, "PRIVATE-WX-USER-9")
        self.assertEqual(panel.runtime_identities["userId"], "PRIVATE-WX-USER-9")
        self.assertIn("PRIVATE-WX-USER-9", panel.runtime_sensitive_values)

    async def test_wait_ignores_unneeded_user_identity_until_required_shop_identity_arrives(self):
        from platform_inspection import InspectionRequestGuard

        page = _Page()
        guard = InspectionRequestGuard(
            page,
            (
                EndpointSpec(
                    "GET",
                    "/xhs/getLogisticsList.json",
                    ("shopId", "api_name"),
                    read_only=True,
                ),
            ),
        )
        guard._remember_runtime_identity("userId", "PRIVATE-UNRELATED-USER-9")
        waits = []

        async def wait_for_timeout(milliseconds):
            waits.append(milliseconds)
            guard._remember_shop_id("PRIVATE-XHS-SHOP-9")

        page.wait_for_timeout = wait_for_timeout
        panel = SimpleNamespace(
            shop_id="",
            shopId="",
            runtime_sensitive_values=(),
        )

        found = await guard.wait_for_runtime_identity(panel, timeout_seconds=1)

        self.assertTrue(found)
        self.assertEqual(waits, [100])
        self.assertEqual(panel.shop_id, "PRIVATE-XHS-SHOP-9")
        self.assertIn("PRIVATE-UNRELATED-USER-9", panel.runtime_sensitive_values)
        self.assertIn("PRIVATE-XHS-SHOP-9", panel.runtime_sensitive_values)

    async def test_allows_only_same_origin_catalog_requests_and_unroutes_same_handler(self):
        from platform_inspection import InspectionRequestGuard

        page = _Page()
        catalog = (
            EndpointSpec(
                "GET",
                "/tm/detail.json",
                ("baseItemId", "api_name"),
                read_only=True,
            ),
            EndpointSpec(
                "POST",
                "/publish/fast/prediction/cat.json",
                ("title", "api_name"),
                read_only=True,
            ),
        )
        guard = InspectionRequestGuard(page, catalog)
        await guard.install()
        handler = page.route_calls[0][1]

        allowed_get = SimpleNamespace(
            url="https://scm.superboss.cc/tm/detail.json?baseItemId=1&api_name=tm_detail",
            method="GET",
            resource_type="xhr",
            post_data=None,
        )
        allowed_post = SimpleNamespace(
            url="https://scm.superboss.cc/publish/fast/prediction/cat.json",
            method="POST",
            resource_type="fetch",
            post_data=(
                "title=trousers&api_name=predict&uiRuntimeFlag=1"
                "&shopId=PRIVATE-RUNTIME-SHOP-9"
            ),
        )
        common_suggestion = SimpleNamespace(
            url="https://scm.superboss.cc/fxg/getProductSuggestionResult.json",
            method="POST",
            resource_type="fetch",
            post_data="runtimeShape=opaque&shopId=PRIVATE-RUNTIME-SHOP-9",
        )
        common_picture_query = SimpleNamespace(
            url="https://scm.superboss.cc/item/picture/query.json",
            method="POST",
            resource_type="xhr",
            post_data="api_name=item_picture_query&baseItemId=PRIVATE-BASE-9",
        )
        unknown_write = SimpleNamespace(
            url="https://scm.superboss.cc/PRIVATE-STYLE-42/save.json?title=private",
            method="POST",
            resource_type="xhr",
            post_data="title=private",
        )
        unknown_get = SimpleNamespace(
            url="https://scm.superboss.cc/tm/unknown.json?baseItemId=PRIVATE-BASE-9",
            method="GET",
            resource_type="xhr",
            post_data=None,
        )
        external = SimpleNamespace(
            url="https://outside.example/tm/detail.json?baseItemId=1&api_name=tm_detail",
            method="GET",
            resource_type="xhr",
            post_data=None,
        )
        external_post = SimpleNamespace(
            url="https://outside.example/tm/detail.json",
            method="POST",
            resource_type="xhr",
            post_data="action=write",
        )

        allowed_get_route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        allowed_post_route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        common_suggestion_route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        common_picture_route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        unknown_route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        unknown_get_route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        external_route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        external_post_route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
        await handler(allowed_get_route, allowed_get)
        await handler(allowed_post_route, allowed_post)
        await handler(common_suggestion_route, common_suggestion)
        await handler(common_picture_route, common_picture_query)
        await handler(unknown_route, unknown_write)
        await handler(unknown_get_route, unknown_get)
        await handler(external_route, external)
        await handler(external_post_route, external_post)
        await guard.uninstall()

        allowed_get_route.continue_.assert_awaited_once()
        allowed_post_route.continue_.assert_awaited_once()
        common_suggestion_route.continue_.assert_awaited_once()
        common_picture_route.continue_.assert_awaited_once()
        unknown_route.abort.assert_awaited_once()
        unknown_get_route.continue_.assert_awaited_once()
        unknown_get_route.abort.assert_not_awaited()
        external_route.continue_.assert_awaited_once()
        external_route.abort.assert_not_awaited()
        external_post_route.abort.assert_awaited_once()
        self.assertEqual(len(guard.blocked_requests), 2)
        self.assertEqual(
            guard.shop_id_candidates,
            ("PRIVATE-RUNTIME-SHOP-9",),
        )
        panel = SimpleNamespace(
            shop_id="",
            shopId="",
            runtime_sensitive_values=(),
        )
        guard.apply_runtime_identity(panel)
        self.assertEqual(panel.shop_id, "PRIVATE-RUNTIME-SHOP-9")
        self.assertEqual(panel.shopId, "PRIVATE-RUNTIME-SHOP-9")
        self.assertEqual(
            panel.runtime_sensitive_values,
            ("PRIVATE-RUNTIME-SHOP-9",),
        )
        self.assertEqual(
            {item["reason"] for item in guard.blocked_requests},
            {"unregistered_write", "cross_origin_write"},
        )
        self.assertEqual(
            {item["path"] for item in guard.blocked_requests},
            {"/[redacted-segment]/save.json", "/tm/detail.json"},
        )
        self.assertIs(page.unroute_calls[0][1], handler)
        combined = json.dumps(guard.blocked_requests)
        self.assertNotIn("private", combined)
        self.assertNotIn("baseItemId", combined)
        self.assertNotIn("PRIVATE-STYLE-42", combined)
        self.assertNotIn("PRIVATE-BASE-9", combined)
        self.assertNotIn("PRIVATE-RUNTIME-SHOP-9", combined)


class InspectionPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_nested_store_tab_exposes_runtime_identity_only_in_memory(self):
        from playwright.async_api import async_playwright

        from platform_inspection import InspectionPanel

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <section id="platform-panel">
                  <div role="tab" id="tab-900596991"
                    aria-controls="pane-900596991" aria-selected="true">授权店铺</div>
                  <div role="tab" id="tab-900671180"
                    aria-controls="pane-900671180" aria-selected="false">其他店铺</div>
                  <div role="tab" id="tab-brand-settings"
                    aria-controls="pane-brand-settings" aria-selected="true">子区域</div>
                </section>
                """
            )
            panel = InspectionPanel(
                page,
                page.locator("#platform-panel"),
                get_platform_spec("tmall"),
            )

            await panel._read_explicit_runtime_identities()

            self.assertEqual(panel.shop_id, "900596991")
            self.assertEqual(
                panel.runtime_sensitive_values,
                ("900596991", "brand-settings"),
            )
            await browser.close()

    async def test_vue_runtime_shop_identity_is_used_when_dom_has_no_explicit_id(self):
        from playwright.async_api import async_playwright

        from platform_inspection import InspectionPanel

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content('<section id="platform-panel"></section>')
            await page.evaluate(
                """
                document.querySelector('#platform-panel').__vue__ = {
                  selectedShop: {shopId: '900596991'}
                }
                """
            )
            panel = InspectionPanel(
                page,
                page.locator("#platform-panel"),
                get_platform_spec("tmall"),
            )

            await panel._read_explicit_runtime_identities()

            self.assertEqual(panel.shop_id, "900596991")
            self.assertEqual(panel.runtime_sensitive_values, ("900596991",))
            await browser.close()

    async def test_vue3_runtime_shop_identity_is_used_when_dom_has_no_explicit_id(self):
        from playwright.async_api import async_playwright

        from platform_inspection import InspectionPanel

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content('<section id="platform-panel"></section>')
            await page.evaluate(
                """
                document.querySelector('#platform-panel').__vueParentComponent = {
                  setupState: {selectedShop: {shopId: '900596991'}}
                }
                """
            )
            panel = InspectionPanel(
                page,
                page.locator("#platform-panel"),
                get_platform_spec("tmall"),
            )

            await panel._read_explicit_runtime_identities()

            self.assertEqual(panel.shop_id, "900596991")
            self.assertEqual(panel.runtime_sensitive_values, ("900596991",))
            await browser.close()

    async def test_dom_capture_ignores_controls_in_hidden_store_panes(self):
        from playwright.async_api import async_playwright

        from platform_inspection import InspectionPanel

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <section id="platform-panel">
                  <div class="el-form-item">
                    <label class="el-form-item__label">商品标题</label>
                    <input id="title-field">
                  </div>
                  <div style="display:none">
                    <div class="el-form-item">
                      <label class="el-form-item__label">商品标题</label>
                      <input id="title-field">
                    </div>
                  </div>
                </section>
                """
            )
            panel = InspectionPanel(
                page,
                page.locator("#platform-panel"),
                get_platform_spec("tmall"),
            )

            observations = await panel.capture_dom_fields()

            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].label, "商品标题")
            await browser.close()

    async def test_single_unselected_candidate_is_not_existing_category(self):
        from platform_inspection import InspectionPanel

        panel = InspectionPanel(_Page(), _PanelLocator(), get_platform_spec("tmall"))
        panel._explicit_categories = AsyncMock(
            return_value=()
        )

        existing = await panel.get_existing_category()

        self.assertIsNone(existing)
        panel._explicit_categories.assert_awaited_once_with(True)

    async def test_existing_and_tree_categories_require_explicit_ids(self):
        from platform_inspection import InspectionPanel

        raw = _PanelLocator(
            evaluate_result=(
                {"leaf_id": "123", "path": ("男装", "休闲裤")},
                {"leaf_id": "", "path": ("只有文本",)},
            )
        )
        panel = InspectionPanel(_Page(), raw, get_platform_spec("tmall"))

        existing = await panel.get_existing_category()
        tree = await panel.list_category_tree()

        self.assertEqual(existing["leaf_id"], "123")
        self.assertEqual(tuple(item["leaf_id"] for item in tree), ("123",))

    async def test_activation_clicks_only_exact_candidate_scoped_action(self):
        from platform_inspection import InspectionPanel

        exact_action = _Action()
        raw = _PanelLocator(
            nodes=(
                _CategoryNode("wrong", _Action()),
                _CategoryNode("wanted", exact_action),
            )
        )
        panel = InspectionPanel(_Page(), raw, get_platform_spec("tmall"))

        await panel.activate_category(
            CategoryCandidate(leaf_id="wanted", path=("男装", "休闲裤"))
        )

        self.assertTrue(exact_action.clicked)
        self.assertFalse(raw.global_action.clicked)

    async def test_activation_uses_one_exact_recommendation_path_without_dom_id(self):
        from platform_inspection import InspectionPanel

        exact_action = _Action()
        wrong_action = _Action()
        raw = _PanelLocator(
            recommendation_rows=(
                _RecommendationRow("服装 > 男装 > 牛仔裤", wrong_action),
                _RecommendationRow("服装 > 男装 > 休闲裤", exact_action),
            )
        )
        panel = InspectionPanel(_Page(), raw, get_platform_spec("tmall"))
        panel._settle_after_category_activation = AsyncMock()

        await panel.activate_category(
            CategoryCandidate(leaf_id="5001", path=("男装", "休闲裤"))
        )

        self.assertTrue(exact_action.clicked)
        self.assertFalse(wrong_action.clicked)
        self.assertFalse(raw.global_action.clicked)
        panel._settle_after_category_activation.assert_awaited_once()

    async def test_activation_scopes_exact_visible_path_when_row_has_no_stable_class(self):
        from playwright.async_api import async_playwright

        from platform_inspection import InspectionPanel

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <section id="platform-panel">
                  <div class="runtime-generated-class">
                    <span>男装&gt;牛仔裤</span>
                    <button onclick="window.wrongClicked = true">点击使用</button>
                  </div>
                  <div class="another-runtime-class">
                    <span>男装&gt;休闲裤</span>
                    <button onclick="window.exactClicked = true">点击使用</button>
                  </div>
                </section>
                """
            )
            panel = InspectionPanel(
                page,
                page.locator("#platform-panel"),
                get_platform_spec("tmall"),
            )

            await panel.activate_category(
                CategoryCandidate(leaf_id="5001", path=("男装", "休闲裤"))
            )

            self.assertTrue(await page.evaluate("Boolean(window.exactClicked)"))
            self.assertFalse(await page.evaluate("Boolean(window.wrongClicked)"))
            await browser.close()

    async def test_recommended_category_waits_for_delayed_exact_path(self):
        from playwright.async_api import async_playwright

        from platform_inspection import InspectionPanel

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content('<section id="platform-panel"></section>')
            await page.evaluate(
                """
                setTimeout(() => {
                  document.querySelector('#platform-panel').innerHTML = `
                    <div><span>男装&gt;休闲裤</span>
                    <button onclick="window.delayedClicked = true">点击使用</button></div>`;
                }, 250)
                """
            )
            panel = InspectionPanel(
                page,
                page.locator("#platform-panel"),
                get_platform_spec("tmall"),
            )
            panel.timeout_seconds = 2

            await panel.activate_category(
                CategoryCandidate(
                    leaf_id="5001",
                    path=("男装", "休闲裤"),
                    recommended=True,
                )
            )

            self.assertTrue(await page.evaluate("Boolean(window.delayedClicked)"))
            await browser.close()


class InspectionRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_attempt_triggered_while_closing_is_reported_as_failed(self):
        from platform_inspection import (
            InspectionPanel,
            SensitiveLogRedactor,
            run_platform_inspection,
        )

        spec = get_platform_spec("tmall")
        adapter = SimpleNamespace(spec=spec, endpoint_catalog=(), label_aliases={})
        schema = PlatformSchema(
            platform_id="tm",
            capture_status="complete",
            category=CategoryResolution(status="resolved"),
        )
        page = _Page()
        drawer = _Drawer()
        args = SimpleNamespace(platform="tmall", timeout=1)
        product = SimpleNamespace(
            style_code="PRIVATE-STYLE-42",
            title="PRIVATE PRODUCT TITLE",
            category_hints=("男装",),
        )
        logger = logging.getLogger("inspection-close-write-test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())

        async def close_with_write(_page, _drawer):
            request = SimpleNamespace(
                url="https://scm.superboss.cc/tm/save.json",
                method="POST",
                resource_type="xhr",
                post_data="action=save",
            )
            route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
            await page.route_calls[0][1](route, request)
            route.abort.assert_awaited_once()

        with tempfile.TemporaryDirectory() as directory, patch(
            "platform_inspection.load_platform_adapter", return_value=adapter
        ), patch.object(
            InspectionPanel,
            "open",
            new=AsyncMock(return_value=InspectionPanel(page, _PanelLocator(), spec)),
        ), patch(
            "platform_inspection.discover_platform_schema",
            new=AsyncMock(return_value=(schema, ())),
        ), patch(
            "platform_inspection._safe_close_drawer",
            new=close_with_write,
        ):
            with self.assertRaises(PlatformDiscoveryError):
                await run_platform_inspection(
                    page,
                    drawer,
                    args,
                    product,
                    {"baseItemId": "PRIVATE-BASE-9"},
                    Path(directory),
                    logger,
                    SensitiveLogRedactor(),
                )

            summary = json.loads(
                (Path(directory) / "summary.json").read_text(encoding="utf-8")
            )
            report = json.loads(
                (Path(directory) / "tm.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["capture_status"], "failed")
            self.assertIn("mutation_guard_blocked", report["issues"])

    async def test_partial_run_writes_redacted_reports_before_raising_and_unroutes(self):
        from platform_inspection import (
            InspectionPanel,
            SensitiveLogRedactor,
            run_platform_inspection,
        )

        private_title = "PRIVATE PRODUCT TITLE"
        private_style = "PRIVATE-STYLE-42"
        private_base = "PRIVATE-BASE-9"
        private_late_user = "PRIVATE-LATE-USER-9009"
        spec = get_platform_spec("tmall")
        adapter = SimpleNamespace(spec=spec, endpoint_catalog=(), label_aliases={})
        schema = PlatformSchema(
            platform_id="tm",
            capture_status="partial",
            category=CategoryResolution(status="review_required"),
            issues=(private_title, private_late_user),
        )
        page = _Page()
        drawer = _Drawer()
        args = SimpleNamespace(platform="tmall", timeout=1)
        product = SimpleNamespace(
            style_code=private_style,
            title=private_title,
            category_hints=("男装",),
        )
        logger = logging.getLogger("inspection-partial-test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        redactor = SensitiveLogRedactor((private_style, private_title))

        async def discover_with_late_runtime_identity(*_args, **_kwargs):
            request = SimpleNamespace(
                url=(
                    "https://scm.superboss.cc/runtime/read.json"
                    "?userId={0}".format(private_late_user)
                ),
                method="GET",
                resource_type="xhr",
                post_data=None,
            )
            route = SimpleNamespace(continue_=AsyncMock(), abort=AsyncMock())
            await page.route_calls[0][1](route, request)
            route.continue_.assert_awaited_once()
            return (schema, ())

        with tempfile.TemporaryDirectory() as directory, patch(
            "platform_inspection.load_platform_adapter", return_value=adapter
        ), patch.object(
            InspectionPanel,
            "open",
            new=AsyncMock(return_value=InspectionPanel(page, _PanelLocator(), spec)),
        ), patch(
            "platform_inspection.discover_platform_schema",
            new=AsyncMock(side_effect=discover_with_late_runtime_identity),
        ):
            with self.assertRaises(PlatformDiscoveryError):
                await run_platform_inspection(
                    page,
                    drawer,
                    args,
                    product,
                    {"baseItemId": private_base},
                    Path(directory),
                    logger,
                    redactor,
                )

            report_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(Path(directory).glob("*.json"))
            )
            self.assertTrue((Path(directory) / "tm.json").is_file())
            self.assertTrue((Path(directory) / "tm-dom.json").is_file())
            self.assertTrue((Path(directory) / "summary.json").is_file())
            self.assertNotIn(private_title, report_text)
            self.assertNotIn(private_style, report_text)
            self.assertNotIn(private_base, report_text)
            self.assertNotIn(private_late_user, report_text)
            self.assertEqual(page.screenshot_calls[0]["path"], str(Path(directory) / "tm-inspect.png"))
            self.assertIs(page.route_calls[0][1], page.unroute_calls[0][1])


if __name__ == "__main__":
    unittest.main()
