import json
import unittest

from platform_discovery import ApiClient, DiscoveryContext, discover_platform_schema
from platform_registry import get_platform_spec
from platform_schema import CategoryCandidate, to_dict


TMALL_FIXTURE = {
    "/tm/detail.json": {"data": {}},
    "/tm/detailByOtherShop.json": {
        "data": {
            "authorizedShops": (
                {"shopId": "SHOP-ID-FROM-OTHER-DETAIL"},
            ),
            "fieldDescriptorList": (
                {
                    "id": "title",
                    "label": "商品标题",
                    "type": "input",
                    "rules": ({"required": True},),
                },
                {
                    "id": "season",
                    "label": "上市季节",
                    "type": "select",
                    "options": (
                        {"id": "spring", "name": "春季"},
                        {"id": "autumn", "name": "秋季"},
                    ),
                },
            ),
            "itemSkuFieldDescriptorList": (
                {"id": "skuCode", "label": "商家编码", "type": "input"},
            ),
            "headers": {"Cookie": "PRIVATE-COOKIE-MUST-NOT-LEAK"},
        }
    },
    "/publish/fast/prediction/cat.json": {
        "data": {
            "catList": (
                {
                    "leftCid": "5001",
                    "leftName": "休闲裤",
                    "cidNames": ("男装", "裤子", "休闲裤"),
                    "score": 0.98,
                },
                {
                    "leftCid": "5002",
                    "leftName": "工装裤",
                    "cidNames": ("男装", "裤子", "工装裤"),
                    "score": 0.91,
                },
            )
        }
    },
    "/dsb/queryCategoryConfigInfo.json": {
        "data": {
            "attrList": (
                {
                    "id": "fit",
                    "name": "版型",
                    "required": True,
                    "options": ({"id": "loose", "name": "宽松"},),
                },
            )
        }
    },
    "/tm/getProductMatchSchema.json": {
        "data": {
            "properties": (
                {"id": "prop_model", "name": "货号", "type": "input"},
            )
        }
    },
    "/tm/getBrandList.json": {
        "data": {
            "brandList": (
                {"id": "brand-a", "name": "品牌甲"},
                {"id": "brand-b", "name": "品牌乙"},
            )
        }
    },
}


class FixtureTransport:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    async def __call__(self, endpoint, parameters):
        self.calls.append((endpoint.path, dict(parameters)))
        return self.fixture[endpoint.path]


class FakePanel:
    def __init__(self):
        self.shop_id = "SHOP-ID-ONLY-IN-MEMORY"
        self.activated = []

    async def get_existing_category(self):
        return None

    async def list_category_tree(self):
        return ()

    async def activate_category(self, candidate):
        self.activated.append(candidate.leaf_id)


class TmallListingFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tm_listing import TmallListing

        self.adapter = TmallListing()
        self.transport = FixtureTransport(TMALL_FIXTURE)
        self.api = ApiClient(
            endpoint_catalog=self.adapter.endpoint_catalog,
            transport=self.transport,
        )
        self.panel = FakePanel()
        self.context = DiscoveryContext(
            style_code="STYLE-PRIVATE",
            title="秋季男士休闲裤",
            category_hints=("休闲裤", "工装休闲裤"),
            base_item_id="BASE-ID-PRIVATE",
        )

    def test_registry_class_protocol_and_endpoint_whitelist_are_exact(self):
        spec = get_platform_spec("tmall")
        self.assertEqual(spec.discovery_adapter, "tm_listing:TmallListing")
        self.assertEqual(self.adapter.spec, spec)
        for method_name in (
            "capture_fixed",
            "resolve_category",
            "activate_category",
            "capture_dynamic",
        ):
            self.assertTrue(callable(getattr(self.adapter, method_name)))
        self.assertFalse(hasattr(self.adapter, "save"))
        self.assertFalse(hasattr(self.adapter, "publish"))

        actual = {
            endpoint.path: (
                endpoint.method,
                endpoint.parameter_names,
                endpoint.read_only,
            )
            for endpoint in self.adapter.endpoint_catalog
        }
        self.assertEqual(
            actual,
            {
                "/tm/detail.json": (
                    "GET",
                    ("baseItemId", "shopIds", "api_name"),
                    True,
                ),
                "/tm/detailByOtherShop.json": (
                    "GET",
                    ("baseItemId", "shopId", "api_name"),
                    True,
                ),
                "/publish/fast/prediction/cat.json": (
                    "POST",
                    ("api_name", "baseItemId", "platformType", "shopId", "title"),
                    True,
                ),
                "/dsb/queryCategoryConfigInfo.json": (
                    "GET",
                    ("shopType", "leafCategoryId", "api_name"),
                    True,
                ),
                "/tm/getProductMatchSchema.json": (
                    "GET",
                    ("shopId", "categoryId", "api_name"),
                    True,
                ),
                "/tm/getBrandList.json": (
                    "GET",
                    ("shopId", "api_name"),
                    True,
                ),
            },
        )

    async def test_detail_empty_still_uses_other_shop_descriptors_for_fixed_schema(self):
        fragment = await self.adapter.capture_fixed(
            self.context,
            self.panel,
            self.api,
        )

        labels = tuple(
            field.label for section in fragment.sections for field in section.fields
        )
        self.assertEqual(labels, ("商品标题", "上市季节", "商家编码"))
        season = next(
            field
            for section in fragment.sections
            for field in section.fields
            if field.source_id == "season"
        )
        self.assertEqual(season.option_summary.count, 2)
        self.assertEqual(season.option_summary.source, "api")
        serialized = json.dumps(to_dict(fragment), ensure_ascii=False)
        self.assertNotIn("PRIVATE-COOKIE-MUST-NOT-LEAK", serialized)
        self.assertNotIn("SHOP-ID-ONLY-IN-MEMORY", serialized)
        self.assertNotIn("SHOP-ID-FROM-OTHER-DETAIL", serialized)

    async def test_other_shop_detail_identity_feeds_dynamic_queries_without_persisting(self):
        self.panel.shop_id = ""

        await self.adapter.capture_fixed(self.context, self.panel, self.api)
        resolution = await self.adapter.resolve_category(
            self.context,
            self.panel,
            self.api,
        )
        await self.adapter.capture_dynamic(
            self.context,
            self.panel,
            self.api,
            resolution,
        )

        dynamic_calls = tuple(
            parameters
            for path, parameters in self.transport.calls
            if path in ("/tm/getProductMatchSchema.json", "/tm/getBrandList.json")
        )
        self.assertTrue(dynamic_calls)
        self.assertTrue(
            all(
                parameters["shopId"] == "SHOP-ID-FROM-OTHER-DETAIL"
                for parameters in dynamic_calls
            )
        )

    async def test_unique_exact_hint_selects_recommendation_and_dynamic_sources_merge(self):
        resolution = await self.adapter.resolve_category(
            self.context,
            self.panel,
            self.api,
        )

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.source, "recommendation")
        self.assertEqual(resolution.selected.leaf_id, "5001")
        self.assertEqual(resolution.selected.path[-1], "休闲裤")

        await self.adapter.activate_category(
            self.context,
            self.panel,
            resolution.selected,
        )
        self.assertEqual(self.panel.activated, ["5001"])

        fragment = await self.adapter.capture_dynamic(
            self.context,
            self.panel,
            self.api,
            resolution,
        )
        section_keys = tuple(section.key for section in fragment.sections)
        self.assertEqual(
            section_keys,
            ("category_configuration", "product_match", "brand"),
        )
        brand = fragment.sections[-1].fields[0]
        self.assertEqual(brand.label, "品牌")
        self.assertEqual(brand.option_summary.count, 2)

    async def test_duplicate_exact_recommendations_require_review(self):
        duplicate_fixture = dict(TMALL_FIXTURE)
        duplicate_fixture["/publish/fast/prediction/cat.json"] = {
            "data": {
                "catList": (
                    {"leftCid": "one", "leftName": "休闲裤", "cidNames": ("男装", "休闲裤")},
                    {"leftCid": "two", "leftName": "休闲裤", "cidNames": ("服饰", "休闲裤")},
                )
            }
        }
        transport = FixtureTransport(duplicate_fixture)
        api = ApiClient(
            endpoint_catalog=self.adapter.endpoint_catalog,
            transport=transport,
        )

        resolution = await self.adapter.resolve_category(
            self.context,
            self.panel,
            api,
        )

        self.assertEqual(resolution.status, "review_required")
        self.assertIsNone(resolution.selected)
        self.assertEqual(resolution.reason, "ambiguous_exact_leaf")
        self.assertEqual(self.panel.activated, [])

    async def test_product_match_mapping_keys_are_normalized_as_fields(self):
        mapping_fixture = dict(TMALL_FIXTURE)
        mapping_fixture["/tm/getProductMatchSchema.json"] = {
            "data": {
                "prop_model": {"name": "货号", "type": "input"},
                "prop_origin": {"name": "产地", "type": "select"},
            }
        }
        transport = FixtureTransport(mapping_fixture)
        api = ApiClient(
            endpoint_catalog=self.adapter.endpoint_catalog,
            transport=transport,
        )
        resolution = await self.adapter.resolve_category(
            self.context,
            self.panel,
            api,
        )

        fragment = await self.adapter.capture_dynamic(
            self.context,
            self.panel,
            api,
            resolution,
        )

        product_fields = fragment.sections[1].fields
        self.assertEqual(
            tuple(field.source_id for field in product_fields),
            ("prop_model", "prop_origin"),
        )
        self.assertEqual(
            tuple(field.label for field in product_fields),
            ("货号", "产地"),
        )

    async def test_real_api_name_casing_and_dynamic_generation_are_frozen_at_start(self):
        adapter = self.adapter

        class BumpingTransport(FixtureTransport):
            def __init__(_self):
                super().__init__(TMALL_FIXTURE)
                _self.bumped = False

            async def __call__(_self, endpoint, parameters):
                if endpoint.path == "/dsb/queryCategoryConfigInfo.json" and not _self.bumped:
                    _self.bumped = True
                    adapter.generation_tracker.begin_generation()
                return await super().__call__(endpoint, parameters)

        transport = BumpingTransport()
        api = ApiClient(
            endpoint_catalog=adapter.endpoint_catalog,
            transport=transport,
        )
        await adapter.capture_fixed(self.context, self.panel, api)
        resolution = await adapter.resolve_category(
            self.context,
            self.panel,
            api,
        )
        generation_at_start = adapter.generation_tracker.begin_generation()
        fragment = await adapter.capture_dynamic(
            self.context,
            self.panel,
            api,
            resolution,
        )

        self.assertEqual(fragment.generation, generation_at_start)
        api_names = {
            path: parameters["api_name"]
            for path, parameters in transport.calls
        }
        self.assertEqual(api_names["/tm/detail.json"], "tm_detail")
        self.assertEqual(
            api_names["/tm/detailByOtherShop.json"],
            "tm_detailByOtherShop",
        )
        self.assertEqual(
            api_names["/publish/fast/prediction/cat.json"],
            "publish_fast_prediction_cat",
        )
        self.assertEqual(
            api_names["/dsb/queryCategoryConfigInfo.json"],
            "dsb_queryCategoryConfigInfo",
        )
        self.assertEqual(
            api_names["/tm/getProductMatchSchema.json"],
            "tm_getProductMatchSchema",
        )
        self.assertEqual(
            api_names["/tm/getBrandList.json"],
            "tm_getBrandList",
        )

    async def test_real_page_pure_data_envelope_runs_complete_with_empty_detail_fallback(self):
        from tm_listing import TmallListing

        responses = {
            "/tm/detail.json": {},
            "/tm/detailByOtherShop.json": {
                "fieldDescriptorList": [
                    {"id": "title", "label": "商品标题", "type": "input"},
                ],
            },
            "/dsb/queryCategoryConfigInfo.json": {
                "attrList": [
                    {"id": "fit", "name": "版型", "type": "select"},
                ],
            },
            "/tm/getProductMatchSchema.json": {
                "properties": [
                    {"id": "model", "name": "货号", "type": "input"},
                ],
            },
            "/tm/getBrandList.json": {
                "brandList": [{"id": "brand", "name": "品牌"}],
            },
        }

        class Page:
            def __init__(_self):
                _self.calls = []

            async def evaluate(_self, script, arguments):
                path = arguments["path"]
                _self.calls.append(path)
                return {
                    "payload": {"data": responses[path]},
                    "http_status": 200,
                    "ok": True,
                }

        class ExistingPanel:
            shop_id = "SHOP-ID-ONLY-IN-MEMORY"

            async def get_existing_category(_self):
                return CategoryCandidate(
                    leaf_id="5001",
                    path=("男装", "休闲裤"),
                    validation_status="validated",
                )

            async def capture_dom_fields(_self):
                return ()

        page = Page()
        adapter = TmallListing()
        schema, _ = await discover_platform_schema(
            adapter,
            self.context,
            ExistingPanel(),
            ApiClient(page=page, endpoint_catalog=adapter.endpoint_catalog),
        )

        self.assertEqual(schema.capture_status, "complete")
        self.assertIn(
            "商品标题",
            tuple(
                field.label
                for section in schema.fixed_sections
                for field in section.fields
            ),
        )
        statuses = {endpoint.path: endpoint.status for endpoint in schema.endpoints}
        self.assertEqual(statuses["/tm/detail.json"], "empty")
        self.assertEqual(statuses["/tm/detailByOtherShop.json"], "ok")


if __name__ == "__main__":
    unittest.main()
