import inspect
import json
import unittest

from platform_discovery import ApiClient, DiscoveryContext
from platform_registry import get_platform_spec
from platform_schema import CategoryCandidate, CategoryResolution, to_dict
from wxsph_listing import WxsphListing


class RecordingTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def __call__(self, endpoint, parameters):
        parameters = dict(parameters)
        self.calls.append(
            (endpoint.method, endpoint.path, tuple(sorted(parameters.items())))
        )
        return {
            "data": self.handler(endpoint.path, parameters),
            "http_status": 200,
            "ok": True,
        }


class ActivationRecordingPanel:
    def __init__(self):
        self.activated = []

    async def activate_category(self, candidate):
        self.activated.append(candidate)


def all_fields(fragment):
    return tuple(
        field
        for section in fragment.sections
        for field in section.fields
    )


class WxsphListingContractTests(unittest.TestCase):
    def test_protocol_registry_and_endpoint_catalog_are_read_only_and_exact(self):
        adapter = WxsphListing()

        self.assertEqual(type(adapter).__name__, "WxsphListing")
        self.assertEqual(adapter.spec, get_platform_spec("wxsph"))
        self.assertEqual(
            adapter.spec.discovery_adapter,
            "wxsph_listing:WxsphListing",
        )
        self.assertTrue(adapter.spec.enabled_in_all)
        self.assertTrue(adapter.spec.publish_allowed)
        for method_name in (
            "capture_fixed",
            "resolve_category",
            "activate_category",
            "capture_dynamic",
        ):
            self.assertTrue(
                inspect.iscoroutinefunction(getattr(type(adapter), method_name)),
                method_name,
            )

        actual_catalog = tuple(
            (endpoint.method, endpoint.path, endpoint.parameter_names)
            for endpoint in adapter.endpoint_catalog
        )
        self.assertEqual(
            actual_catalog,
            (
                (
                    "GET",
                    "/wxsph/detail.json",
                    ("baseItemId", "api_name"),
                ),
                (
                    "POST",
                    "/publish/fast/prediction/cat.json",
                    ("api_name", "baseItemId", "platformType", "title"),
                ),
                (
                    "GET",
                    "/wxsph/getCategoryProperties.json",
                    ("shopId", "categoryId", "api_name"),
                ),
                (
                    "GET",
                    "/wxsph/getCategoryTree.json",
                    ("parentId", "endLevel", "api_name"),
                ),
                (
                    "GET",
                    "/wxsph/getTemplateList.json",
                    ("offset", "limit", "searchUsed", "userId", "api_name"),
                ),
                (
                    "GET",
                    "/dsb/queryDistributionConfig.json",
                    ("shopType", "api_name"),
                ),
            ),
        )
        self.assertTrue(
            all(
                endpoint.method == "GET"
                or (endpoint.method == "POST" and endpoint.read_only)
                for endpoint in adapter.endpoint_catalog
            )
        )
        self.assertFalse(
            any(
                "save" in endpoint.path.lower()
                for endpoint in adapter.endpoint_catalog
            )
        )
        self.assertTrue(
            all(
                endpoint.read_only
                for endpoint in adapter.endpoint_catalog
                if "publish" in endpoint.path.lower()
            )
        )
        self.assertEqual(adapter.spec.save_policy, "allowed")
        self.assertTrue(adapter.spec.publish_allowed)
        self.assertFalse(
            any(
                forbidden in name.lower()
                for name in dir(adapter)
                for forbidden in ("save", "publish")
            )
        )


class WxsphListingFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_unique_exact_recommendation_precedes_budgeted_tree_search(self):
        def fixture(path, parameters):
            if path == "/wxsph/detail.json":
                return {}
            if path == "/wxsph/getTemplateList.json":
                return {"list": []}
            if path == "/dsb/queryDistributionConfig.json":
                return {}
            if path == "/publish/fast/prediction/cat.json":
                self.assertEqual(parameters["platformType"], "wxsph")
                return {
                    "catList": [
                        {
                            "leftCid": "545735",
                            "leftName": "工装裤",
                            "cidNames": ["服饰内衣", "男装", "裤装", "工装裤"],
                            "score": 100,
                        },
                        {
                            "leftCid": "545733",
                            "leftName": "休闲裤",
                            "cidNames": ["服饰内衣", "男装", "裤装", "休闲裤"],
                            "score": 98,
                        },
                    ]
                }
            if path == "/wxsph/getCategoryTree.json":
                raise AssertionError("unique recommendation must skip the tree")
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = WxsphListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        panel = ActivationRecordingPanel()
        context = DiscoveryContext(
            style_code="WX-RECOMMENDATION",
            title="男士休闲裤",
            category_hints=("休闲裤",),
            base_item_id="recommendation-1",
        )

        await adapter.capture_fixed(context, panel, api)
        resolution = await adapter.resolve_category(context, panel, api)

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.source, "recommendation")
        self.assertEqual(resolution.selected.leaf_id, "545733")
        self.assertEqual(resolution.selected.path[-1], "休闲裤")
        self.assertTrue(resolution.selected.recommended)
        called_paths = tuple(path for _method, path, _parameters in transport.calls)
        self.assertNotIn("/wxsph/getCategoryTree.json", called_paths)

    async def test_runtime_user_identity_is_used_when_detail_has_no_identity(self):
        runtime_user_id = "PRIVATE-WX-RUNTIME-USER-9"

        def fixture(path, parameters):
            if path == "/wxsph/detail.json":
                return {}
            if path == "/wxsph/getTemplateList.json":
                self.assertEqual(parameters["userId"], runtime_user_id)
                return {"list": []}
            if path == "/dsb/queryDistributionConfig.json":
                return {}
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = WxsphListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        panel = ActivationRecordingPanel()
        panel.runtime_identities = {"userId": runtime_user_id}
        panel.user_id = runtime_user_id
        panel.userId = runtime_user_id

        await adapter.capture_fixed(
            DiscoveryContext(
                style_code="WX-RUNTIME",
                title="休闲裤",
                category_hints=("休闲裤",),
                base_item_id="runtime-1",
            ),
            panel,
            api,
        )
        template_call = next(
            dict(parameters)
            for _method, path, parameters in transport.calls
            if path == "/wxsph/getTemplateList.json"
        )
        self.assertEqual(template_call["userId"], runtime_user_id)

    async def test_existing_leaf_skips_tree_and_activation_and_normalizes_all_dynamic_groups(self):
        raw_marker = "DO-NOT-LEAK-RAW-WXSPH"
        private_template_names = (
            "PRIVATE-WX-TEMPLATE-A",
            "PRIVATE-WX-TEMPLATE-B",
        )

        def fixture(path, parameters):
            if path == "/wxsph/detail.json":
                return {
                    "shopId": "PRIVATE-WX-SHOP-9001",
                    "userId": "PRIVATE-WX-USER-9001",
                    "categoryId": "545735",
                    "categoryIds": "100,200,545735",
                    "categoryPath": "男装 > 裤子 > 休闲裤",
                    "title": raw_marker,
                    "defaultValue": raw_marker,
                    "skuList": [
                        {
                            "skuId": "sku-private-1",
                            "price": raw_marker,
                        }
                    ],
                }
            if path == "/wxsph/getTemplateList.json":
                return {
                    "list": [
                        {"id": "template-private-a", "name": private_template_names[0]},
                        {"id": "template-private-b", "name": private_template_names[1]},
                    ],
                    "total": 2,
                }
            if path == "/dsb/queryDistributionConfig.json":
                return {
                    "enabled": True,
                    "selected": raw_marker,
                }
            if path == "/wxsph/getCategoryTree.json":
                raise AssertionError("an existing leaf category must not query the tree")
            if path == "/wxsph/getCategoryProperties.json":
                self.assertEqual(parameters["categoryId"], "545735")
                return {
                    "attr": [
                        {
                            "id": "attr-pattern",
                            "name": "图案",
                            "isRequired": "true",
                            "multiple": False,
                            "inputType": "select",
                            "values": [
                                {"id": "solid", "name": "纯色"},
                                {"id": "stripe", "name": "条纹"},
                            ],
                            "value": raw_marker,
                        }
                    ],
                    "extraServiceList": [
                        {
                            "id": "service-insurance",
                            "name": "运费险",
                            "isRequired": "false",
                            "selected": raw_marker,
                        }
                    ],
                    "isNeedBarCode": True,
                    "productQuaInfo": [
                        {
                            "id": "qualification-report",
                            "name": "质检报告",
                            "isRequired": True,
                            "privateValue": raw_marker,
                        }
                    ],
                    "productRequirement": [
                        {
                            "id": "requirement-origin",
                            "name": "商品要求",
                            "isRequired": False,
                            "defaultValue": raw_marker,
                        }
                    ],
                    "sizeChart": {
                        "id": "size-chart",
                        "name": "尺码表",
                        "isRequired": "true",
                        "value": raw_marker,
                    },
                    "info": {"selectedValue": raw_marker},
                }
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = WxsphListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(
            endpoint_catalog=adapter.endpoint_catalog,
            transport=transport,
        )
        panel = ActivationRecordingPanel()
        context = DiscoveryContext(
            style_code="WX-READ-ONLY-001",
            title="男士休闲裤",
            category_hints=("男装", "裤子", "休闲裤"),
            base_item_id="9000001",
        )

        fixed = await adapter.capture_fixed(context, panel, api)
        resolution = await adapter.resolve_category(context, panel, api)

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.source, "existing")
        self.assertIsNotNone(resolution.selected)
        self.assertEqual(resolution.selected.leaf_id, "545735")
        self.assertEqual(resolution.selected.path[-1], "休闲裤")
        self.assertNotIn(
            "/wxsph/getCategoryTree.json",
            tuple(call[1] for call in transport.calls),
        )
        self.assertEqual(panel.activated, [])

        dynamic = await adapter.capture_dynamic(
            context,
            panel,
            api,
            resolution,
        )
        fields = all_fields(dynamic)
        by_label = {field.label: field for field in fields}

        self.assertEqual(by_label["图案"].source_id, "attr-pattern")
        self.assertTrue(by_label["图案"].required)
        self.assertFalse(by_label["图案"].multiple)
        self.assertEqual(by_label["图案"].option_summary.count, 2)
        self.assertEqual(by_label["运费险"].source_id, "service-insurance")
        self.assertFalse(by_label["运费险"].required)
        self.assertIn("商品条码", by_label)
        self.assertTrue(by_label["商品条码"].required)
        self.assertEqual(by_label["质检报告"].source_id, "qualification-report")
        self.assertTrue(by_label["质检报告"].required)
        self.assertEqual(by_label["商品要求"].source_id, "requirement-origin")
        self.assertFalse(by_label["商品要求"].required)
        self.assertEqual(by_label["尺码表"].source_id, "size-chart")
        self.assertTrue(by_label["尺码表"].required)

        shop_summaries = tuple(
            field.option_summary
            for field in all_fields(fixed)
            if field.option_summary is not None
            and field.option_summary.source == "shop"
        )
        self.assertEqual(len(shop_summaries), 1)
        self.assertEqual(shop_summaries[0].count, 2)
        self.assertEqual(shop_summaries[0].sample, ())
        self.assertEqual(len(shop_summaries[0].sha256), 64)

        report_text = json.dumps(
            {
                "fixed": to_dict(fixed),
                "category": to_dict(resolution),
                "dynamic": to_dict(dynamic),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertNotIn(raw_marker, report_text)
        self.assertNotIn("PRIVATE-WX-SHOP-9001", report_text)
        self.assertNotIn("PRIVATE-WX-USER-9001", report_text)
        for private_name in private_template_names:
            self.assertNotIn(private_name, report_text)

        called_paths = tuple(call[1] for call in transport.calls)
        self.assertEqual(called_paths.count("/wxsph/detail.json"), 1)
        self.assertIn("/wxsph/getTemplateList.json", called_paths)
        self.assertIn("/dsb/queryDistributionConfig.json", called_paths)
        self.assertIn("/wxsph/getCategoryProperties.json", called_paths)
        expected_api_names = {
            "/wxsph/detail.json": "wxsph_detail",
            "/wxsph/getTemplateList.json": "wxsph_getTemplateList",
            "/dsb/queryDistributionConfig.json": "dsb_queryDistributionConfig",
            "/wxsph/getCategoryProperties.json": "wxsph_getCategoryProperties",
        }
        for _method, path, encoded_parameters in transport.calls:
            self.assertEqual(
                dict(encoded_parameters)["api_name"],
                expected_api_names[path],
                path,
            )
        self.assertFalse(
            any(forbidden in path.lower() for path in called_paths for forbidden in ("save", "publish"))
        )

    async def test_tree_consumes_nested_children_and_uses_numeric_depth(self):
        def fixture(path, parameters):
            if path == "/wxsph/detail.json":
                return {}
            if path == "/wxsph/getTemplateList.json":
                return {"list": []}
            if path == "/dsb/queryDistributionConfig.json":
                return {}
            if path == "/wxsph/getCategoryTree.json":
                if str(parameters["parentId"]) == "0":
                    return [
                        {
                            "cid": "100",
                            "name": "男装",
                            "leaf": False,
                            "children": [
                                {
                                    "cid": "110",
                                    "name": "裤子",
                                    "leaf": False,
                                    "children": [
                                        {
                                            "cid": "111",
                                            "name": "休闲裤",
                                            "leaf": True,
                                            "children": None,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                return []
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = WxsphListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        context = DiscoveryContext(
            style_code="WX-NESTED-TREE",
            title="男士休闲裤",
            category_hints=("休闲裤", "工装休闲裤"),
            base_item_id="nested-1",
        )

        await adapter.capture_fixed(context, None, api)
        resolution = await adapter.resolve_category(context, None, api)

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.selected.leaf_id, "111")
        self.assertEqual(resolution.selected.path, ("男装", "裤子", "休闲裤"))
        tree_calls = tuple(
            dict(parameters)
            for _method, path, parameters in transport.calls
            if path == "/wxsph/getCategoryTree.json"
        )
        self.assertEqual(len(tree_calls), 1)
        self.assertIs(type(tree_calls[0]["endLevel"]), int)
        self.assertEqual(tree_calls[0]["endLevel"], 1)
        self.assertEqual(tree_calls[0]["api_name"], "wxsph_getCategoryTree")

    async def test_tree_cycle_is_visited_once_and_requires_review_with_issue(self):
        def fixture(path, parameters):
            if path == "/wxsph/getCategoryTree.json":
                return [
                    {
                        "cid": "cycle-a",
                        "name": "循环类目",
                        "leaf": False,
                    }
                ]
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = WxsphListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            DiscoveryContext(
                style_code="WX-CYCLE",
                title="循环类目",
                category_hints=("循环类目",),
                base_item_id="cycle-1",
            ),
            None,
            api,
        )

        self.assertEqual(resolution.status, "review_required")
        tree_calls = tuple(
            call for call in transport.calls if call[1] == "/wxsph/getCategoryTree.json"
        )
        self.assertEqual(len(tree_calls), 2)
        self.assertTrue(
            any("cycle" in issue for issue in adapter.resolution_fragment.issues),
            adapter.resolution_fragment.issues,
        )

    async def test_tree_node_budget_is_capped_at_128_and_requires_review(self):
        def fixture(path, parameters):
            if path == "/wxsph/getCategoryTree.json":
                return [
                    {
                        "cid": "leaf-{0}".format(index),
                        "name": "预算叶子",
                        "leaf": True,
                    }
                    for index in range(129)
                ]
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = WxsphListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            DiscoveryContext(
                style_code="WX-NODE-BUDGET",
                title="预算叶子",
                category_hints=("预算叶子",),
                base_item_id="node-budget-1",
            ),
            None,
            api,
        )

        self.assertEqual(resolution.status, "review_required")
        self.assertLessEqual(len(resolution.candidates), 128)
        self.assertTrue(
            any("budget" in issue for issue in adapter.resolution_fragment.issues),
            adapter.resolution_fragment.issues,
        )

    async def test_tree_request_budget_is_capped_at_128_and_requires_review(self):
        def fixture(path, parameters):
            if path == "/wxsph/getCategoryTree.json":
                if str(parameters["parentId"]) == "0":
                    return [
                        {
                            "cid": "branch-{0}".format(index),
                            "name": "分支{0}".format(index),
                            "leaf": False,
                        }
                        for index in range(128)
                    ]
                return []
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = WxsphListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            DiscoveryContext(
                style_code="WX-REQUEST-BUDGET",
                title="不存在",
                category_hints=("不存在",),
                base_item_id="request-budget-1",
            ),
            None,
            api,
        )

        self.assertEqual(resolution.status, "review_required")
        tree_calls = tuple(
            call for call in transport.calls if call[1] == "/wxsph/getCategoryTree.json"
        )
        self.assertLessEqual(len(tree_calls), 128)
        self.assertTrue(
            any("budget" in issue for issue in adapter.resolution_fragment.issues),
            adapter.resolution_fragment.issues,
        )

    async def test_dynamic_fragment_keeps_generation_from_request_start(self):
        adapter = WxsphListing()
        start_generation = adapter.generation_tracker.begin_generation()
        advanced = False

        def fixture(path, parameters):
            nonlocal advanced
            if path == "/wxsph/getCategoryProperties.json":
                if not advanced:
                    advanced = True
                    adapter.generation_tracker.begin_generation()
                return {}
            raise AssertionError("unexpected endpoint: {0}".format(path))

        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        candidate = CategoryCandidate(
            leaf_id="545735",
            path=("休闲裤",),
            validation_status="validated",
        )
        category = CategoryResolution(
            status="resolved",
            source="existing",
            selected=candidate,
            candidates=(candidate,),
        )

        fragment = await adapter.capture_dynamic(
            DiscoveryContext(
                style_code="WX-GENERATION",
                title="休闲裤",
                category_hints=("休闲裤",),
                base_item_id="generation-1",
            ),
            None,
            api,
            category,
        )

        self.assertGreater(adapter.generation_tracker.current_generation, start_generation)
        self.assertEqual(fragment.generation, start_generation)


if __name__ == "__main__":
    unittest.main()
