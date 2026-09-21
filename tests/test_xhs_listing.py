import inspect
import json
import unittest

from platform_discovery import ApiClient, DiscoveryContext
from platform_registry import get_platform_spec
from platform_schema import to_dict
from xhs_listing import XhsListing


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


class XhsListingContractTests(unittest.TestCase):
    def test_protocol_registry_and_endpoint_catalog_are_read_only_exact_and_have_no_recommendation(self):
        adapter = XhsListing()

        self.assertEqual(type(adapter).__name__, "XhsListing")
        self.assertEqual(adapter.spec, get_platform_spec("xhs"))
        self.assertEqual(adapter.spec.discovery_adapter, "xhs_listing:XhsListing")
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
                    "/xhs/detail.json",
                    ("baseItemId", "api_name"),
                ),
                (
                    "GET",
                    "/xhs/getLogisticsList.json",
                    ("shopId", "api_name"),
                ),
                (
                    "GET",
                    "/xhs/getCarriageTemplateList.json",
                    ("shopId", "api_name"),
                ),
                (
                    "GET",
                    "/xhs/getCategoryTree.json",
                    ("parentId", "endLevel", "api_name"),
                ),
                (
                    "GET",
                    "/xhs/getVariations.json",
                    ("leafCategoryId", "shopId", "api_name"),
                ),
                (
                    "GET",
                    "/xhs/getAttributeList.json",
                    ("leafCategoryId", "shopId", "api_name"),
                ),
                (
                    "GET",
                    "/xhs/getAttributeValues.json",
                    ("shopId", "attributeId", "api_name"),
                ),
                (
                    "GET",
                    "/xhs/getDeliveryRule.json",
                    ("shopId", "categoryId", "logisticsPlanId", "api_name"),
                ),
            ),
        )
        paths = tuple(endpoint.path.lower() for endpoint in adapter.endpoint_catalog)
        self.assertFalse(any("prediction" in path or "recommend" in path for path in paths))
        self.assertTrue(all(endpoint.method == "GET" for endpoint in adapter.endpoint_catalog))
        self.assertFalse(
            any(
                forbidden in path
                for path in paths
                for forbidden in ("save", "publish")
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


class XhsListingFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_shop_identity_is_used_when_detail_has_no_identity(self):
        runtime_shop_id = "PRIVATE-XHS-RUNTIME-SHOP-9"

        def fixture(path, parameters):
            if path == "/xhs/detail.json":
                return {}
            if path in (
                "/xhs/getLogisticsList.json",
                "/xhs/getCarriageTemplateList.json",
            ):
                return []
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = XhsListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        panel = ActivationRecordingPanel()
        panel.runtime_identities = {"shopId": runtime_shop_id}
        panel.shop_id = runtime_shop_id
        panel.shopId = runtime_shop_id

        await adapter.capture_fixed(
            DiscoveryContext(
                style_code="XHS-RUNTIME",
                title="休闲裤",
                category_hints=("休闲裤",),
                base_item_id="runtime-1",
            ),
            panel,
            api,
        )
        calls = {
            path: dict(parameters)
            for _method, path, parameters in transport.calls
        }
        self.assertEqual(
            calls["/xhs/getLogisticsList.json"]["shopId"],
            runtime_shop_id,
        )
        self.assertEqual(
            calls["/xhs/getCarriageTemplateList.json"]["shopId"],
            runtime_shop_id,
        )

    def _fixture_transport(self, duplicate_leaf=False, legacy_delivery=False):
        raw_marker = "DO-NOT-LEAK-RAW-XHS"

        def fixture(path, parameters):
            if path == "/xhs/detail.json":
                return {
                    "shopId": "PRIVATE-XHS-SHOP-8801",
                    "logisticsPlanId": "PRIVATE-LOGISTICS-PLAN-77",
                    "title": raw_marker,
                    "defaultValue": raw_marker,
                    "fieldDescriptorList": [
                        {
                            "id": "title-cn",
                            "name": "中文标题",
                            "type": "text",
                            "value": raw_marker,
                        }
                    ],
                }
            if path == "/xhs/getLogisticsList.json":
                return [
                    {"id": "logistics-private-a", "name": "PRIVATE-LOGISTICS-A"},
                    {"id": "logistics-private-b", "name": "PRIVATE-LOGISTICS-B"},
                ]
            if path == "/xhs/getCarriageTemplateList.json":
                return [
                    {"id": "freight-private-a", "name": "PRIVATE-FREIGHT-A"},
                    {"id": "freight-private-b", "name": "PRIVATE-FREIGHT-B"},
                ]
            if path == "/xhs/getCategoryTree.json":
                parent_id = str(parameters.get("parentId", "0"))
                if parent_id in ("", "0", "None"):
                    roots = [
                        {
                            "cid": "10",
                            "parentCid": "0",
                            "name": "男装",
                            "leaf": 0,
                        }
                    ]
                    if duplicate_leaf:
                        roots.append(
                            {
                                "cid": "20",
                                "parentCid": "0",
                                "name": "服饰",
                                "leaf": 0,
                            }
                        )
                    return roots
                if parent_id == "10":
                    return [
                        {
                            "cid": "11",
                            "parentCid": "10",
                            "name": "裤子",
                            "leaf": 0,
                        }
                    ]
                if parent_id == "11":
                    return [
                        {
                            "cid": "101",
                            "xhsCid": "xhs-101",
                            "parentCid": "11",
                            "name": "工装休闲裤",
                            "leaf": 1,
                        }
                    ]
                if parent_id == "20":
                    return [
                        {
                            "cid": "201",
                            "xhsCid": "xhs-201",
                            "parentCid": "20",
                            "name": "工装休闲裤",
                            "leaf": 1,
                        }
                    ]
                return []
            if path == "/xhs/getAttributeList.json":
                return {
                    "attributeV3s": [
                        {
                            "id": "attribute-fit",
                            "name": "裤型",
                            "isRequired": True,
                            "isMulti": False,
                            "inputType": "select",
                            "dataType": "string",
                            "customizable": False,
                            "value": raw_marker,
                        },
                        {
                            "id": "attribute-scene",
                            "name": "适用场景",
                            "isRequired": False,
                            "isMulti": True,
                            "inputType": "select",
                            "dataType": "string",
                            "customizable": True,
                            "selectedValue": raw_marker,
                        },
                    ]
                }
            if path == "/xhs/getAttributeValues.json":
                if parameters["attributeId"] == "attribute-fit":
                    return {
                        "attributeValueV3s": [
                            {"id": "fit-straight", "name": "直筒"},
                            {"id": "fit-loose", "name": "宽松"},
                        ],
                        "selected": raw_marker,
                    }
                return {
                    "values": [
                        {"id": "scene-daily", "name": "日常"},
                    ],
                    "selected": raw_marker,
                }
            if path == "/xhs/getVariations.json":
                return {
                    "variations": [
                        {"id": "variation-color", "name": "颜色"},
                        {"id": "variation-size", "name": "尺码"},
                    ],
                    "xhsSpecMaxNum": 2,
                    "selected": raw_marker,
                }
            if path == "/xhs/getDeliveryRule.json":
                if legacy_delivery:
                    return {
                        "existingDeliveryRuleList": [
                            {"id": "delivery-existing", "name": "现货"},
                        ],
                        "presaleDeliveryRuleList": [
                            {"id": "delivery-presale", "name": "预售"},
                        ],
                        "selected": raw_marker,
                    }
                return {
                    "existing": [
                        {"timeType": 3, "value": 16, "default": False},
                        {"timeType": 4, "value": 24, "default": False},
                        {"timeType": 4, "value": 48, "default": True},
                    ],
                    "presale": [
                        {"timeType": 4, "value": 0, "min": 72, "max": 360},
                        {"timeType": 5, "value": 0, "min": 72, "max": 360},
                    ],
                    "selected": raw_marker,
                }
            raise AssertionError("unexpected endpoint: {0}".format(path))

        return raw_marker, RecordingTransport(fixture)

    async def test_unique_exact_tree_leaf_activates_and_dynamic_sources_are_normalized(self):
        raw_marker, transport = self._fixture_transport()
        adapter = XhsListing()
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        panel = ActivationRecordingPanel()
        context = DiscoveryContext(
            style_code="XHS-READ-ONLY-001",
            title="男士工装休闲裤",
            category_hints=("男装", "裤子", "工装休闲裤"),
            base_item_id="8800001",
        )

        fixed = await adapter.capture_fixed(context, panel, api)
        resolution = await adapter.resolve_category(context, panel, api)

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.source, "tree")
        self.assertIsNotNone(resolution.selected)
        self.assertEqual(resolution.selected.leaf_id, "101")
        self.assertEqual(
            resolution.selected.path,
            ("男装", "裤子", "工装休闲裤"),
        )

        await adapter.activate_category(context, panel, resolution.selected)
        self.assertEqual(panel.activated, [resolution.selected])

        dynamic = await adapter.capture_dynamic(
            context,
            panel,
            api,
            resolution,
        )
        fields = all_fields(dynamic)
        by_label = {field.label: field for field in fields}

        self.assertEqual(by_label["裤型"].source_id, "attribute-fit")
        self.assertTrue(by_label["裤型"].required)
        self.assertFalse(by_label["裤型"].multiple)
        self.assertFalse(by_label["裤型"].custom_allowed)
        self.assertEqual(by_label["裤型"].option_summary.count, 2)
        self.assertEqual(len(by_label["裤型"].option_values), 2)
        self.assertEqual(by_label["适用场景"].option_summary.count, 1)
        self.assertTrue(by_label["适用场景"].multiple)
        self.assertEqual(by_label["颜色"].source_id, "variation-color")
        self.assertEqual(by_label["尺码"].source_id, "variation-size")
        self.assertEqual(by_label["现货发货规则"].option_summary.count, 3)
        self.assertEqual(by_label["预售发货规则"].option_summary.count, 2)
        self.assertTrue(
            any(
                "/xhs/getDeliveryRule.json" in field.api_paths
                for field in fields
            )
        )

        sensitive_summaries = tuple(
            field.option_summary
            for field in all_fields(fixed)
            if field.option_summary is not None
            and field.option_summary.source in ("logistics", "freight")
        )
        self.assertEqual(
            tuple(summary.source for summary in sensitive_summaries),
            ("logistics", "freight"),
        )
        self.assertTrue(all(summary.count == 2 for summary in sensitive_summaries))
        self.assertTrue(all(summary.sample == () for summary in sensitive_summaries))
        self.assertTrue(all(len(summary.sha256) == 64 for summary in sensitive_summaries))
        self.assertTrue(
            all(
                field.option_values == ()
                for field in all_fields(fixed)
                if field.option_summary is not None
                and field.option_summary.source in ("logistics", "freight")
            )
        )

        report_text = json.dumps(
            {
                "fixed": to_dict(fixed),
                "category": to_dict(resolution),
                "dynamic": to_dict(dynamic),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for secret in (
            raw_marker,
            "PRIVATE-XHS-SHOP-8801",
            "PRIVATE-LOGISTICS-PLAN-77",
            "PRIVATE-LOGISTICS-A",
            "PRIVATE-LOGISTICS-B",
            "PRIVATE-FREIGHT-A",
            "PRIVATE-FREIGHT-B",
        ):
            self.assertNotIn(secret, report_text)

        called_paths = tuple(call[1] for call in transport.calls)
        for expected_path in (
            "/xhs/detail.json",
            "/xhs/getLogisticsList.json",
            "/xhs/getCarriageTemplateList.json",
            "/xhs/getCategoryTree.json",
            "/xhs/getAttributeList.json",
            "/xhs/getAttributeValues.json",
            "/xhs/getVariations.json",
            "/xhs/getDeliveryRule.json",
        ):
            self.assertIn(expected_path, called_paths)
        calls_by_path = {}
        for _method, path, encoded_parameters in transport.calls:
            calls_by_path.setdefault(path, []).append(dict(encoded_parameters))
        expected_api_names = {
            "/xhs/detail.json": "xhs_detail",
            "/xhs/getLogisticsList.json": "xhs_getLogisticsList",
            "/xhs/getCarriageTemplateList.json": "xhs_getCarriageTemplateList",
            "/xhs/getCategoryTree.json": "xhs_getCategoryTree",
            "/xhs/getAttributeList.json": "xhs_getAttributeList",
            "/xhs/getAttributeValues.json": "xhs_getAttributeValues",
            "/xhs/getVariations.json": "xhs_getVariations",
            "/xhs/getDeliveryRule.json": "xhs_getDeliveryRule",
        }
        for path, requests in calls_by_path.items():
            for parameters in requests:
                self.assertEqual(parameters["api_name"], expected_api_names[path], path)
        self.assertEqual(
            calls_by_path["/xhs/getAttributeList.json"][0]["leafCategoryId"],
            "101",
        )
        self.assertEqual(
            calls_by_path["/xhs/getVariations.json"][0]["leafCategoryId"],
            "101",
        )
        self.assertEqual(
            calls_by_path["/xhs/getDeliveryRule.json"][0]["categoryId"],
            "xhs-101",
        )
        self.assertNotIn("/publish/fast/prediction/cat.json", called_paths)
        self.assertFalse(
            any(forbidden in path.lower() for path in called_paths for forbidden in ("save", "publish"))
        )

    async def test_duplicate_exact_leaf_is_review_required_and_never_activates(self):
        _raw_marker, transport = self._fixture_transport(duplicate_leaf=True)
        adapter = XhsListing()
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        panel = ActivationRecordingPanel()
        context = DiscoveryContext(
            style_code="XHS-AMBIGUOUS-001",
            title="工装休闲裤",
            category_hints=("工装休闲裤",),
            base_item_id="8800002",
        )

        await adapter.capture_fixed(context, panel, api)
        resolution = await adapter.resolve_category(context, panel, api)

        self.assertEqual(resolution.status, "review_required")
        self.assertEqual(resolution.source, "tree")
        self.assertIsNone(resolution.selected)
        self.assertEqual(
            {candidate.leaf_id for candidate in resolution.candidates},
            {"101", "201"},
        )
        self.assertEqual(panel.activated, [])
        self.assertNotIn("/publish/fast/prediction/cat.json", tuple(call[1] for call in transport.calls))

    async def test_full_tree_scan_finds_late_duplicate_exact_leaves_without_guessing(self):
        filler = [
            {
                "cid": "filler-{0}".format(index),
                "name": "其他类目{0}".format(index),
                "leaf": 1,
            }
            for index in range(200)
        ]

        def fixture(path, parameters):
            if path == "/xhs/getCategoryTree.json":
                self.assertEqual(parameters["endLevel"], 4)
                return filler + [
                    {
                        "cid": "women",
                        "name": "女装",
                        "leaf": 0,
                        "children": [
                            {"cid": "late-a", "name": "休闲裤", "leaf": 1}
                        ],
                    },
                    {
                        "cid": "men",
                        "name": "男装",
                        "leaf": 0,
                        "children": [
                            {"cid": "late-b", "name": "休闲裤", "leaf": 1}
                        ],
                    },
                ]
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = XhsListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)

        resolution = await adapter.resolve_category(
            DiscoveryContext(
                style_code="XHS-LATE-AMBIGUOUS",
                title="休闲裤",
                category_hints=("休闲裤",),
                base_item_id="late-1",
            ),
            None,
            api,
        )

        self.assertEqual(resolution.status, "review_required")
        self.assertEqual(resolution.reason, "ambiguous_exact_leaf")
        self.assertEqual(
            {candidate.leaf_id for candidate in resolution.candidates},
            {"late-a", "late-b"},
        )

    async def test_legacy_delivery_rule_keys_remain_supported(self):
        _raw_marker, transport = self._fixture_transport(legacy_delivery=True)
        adapter = XhsListing()
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        context = DiscoveryContext(
            style_code="XHS-LEGACY-DELIVERY",
            title="工装休闲裤",
            category_hints=("工装休闲裤",),
            base_item_id="legacy-delivery",
        )

        await adapter.capture_fixed(context, None, api)
        resolution = await adapter.resolve_category(context, None, api)
        self.assertEqual(resolution.status, "resolved")
        dynamic = await adapter.capture_dynamic(context, None, api, resolution)
        by_label = {field.label: field for field in all_fields(dynamic)}

        self.assertEqual(by_label["现货发货规则"].option_summary.count, 1)
        self.assertEqual(by_label["预售发货规则"].option_summary.count, 1)

    async def test_tree_consumes_nested_children_and_uses_numeric_depth(self):
        def fixture(path, parameters):
            if path == "/xhs/detail.json":
                return {}
            if path in (
                "/xhs/getLogisticsList.json",
                "/xhs/getCarriageTemplateList.json",
            ):
                return []
            if path == "/xhs/getCategoryTree.json":
                if str(parameters["parentId"]) == "0":
                    return [
                        {
                            "cid": "10",
                            "name": "男装",
                            "leaf": 0,
                            "children": [
                                {
                                    "cid": "11",
                                    "name": "裤子",
                                    "leaf": 0,
                                    "children": [
                                        {
                                            "cid": "101",
                                            "xhsCid": "xhs-101",
                                            "name": "工装休闲裤",
                                            "leaf": 1,
                                            "children": None,
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                return []
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = XhsListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        context = DiscoveryContext(
            style_code="XHS-NESTED-TREE",
            title="男士工装休闲裤",
            category_hints=("工装休闲裤", "休闲裤"),
            base_item_id="nested-1",
        )

        await adapter.capture_fixed(context, None, api)
        resolution = await adapter.resolve_category(context, None, api)

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.selected.leaf_id, "101")
        self.assertEqual(
            resolution.selected.path,
            ("男装", "裤子", "工装休闲裤"),
        )
        tree_calls = tuple(
            dict(parameters)
            for _method, path, parameters in transport.calls
            if path == "/xhs/getCategoryTree.json"
        )
        self.assertEqual(len(tree_calls), 1)
        self.assertIs(type(tree_calls[0]["endLevel"]), int)
        self.assertEqual(tree_calls[0]["endLevel"], 4)
        self.assertEqual(tree_calls[0]["api_name"], "xhs_getCategoryTree")

    async def test_tree_cycle_is_visited_once_and_requires_review_with_issue(self):
        def fixture(path, parameters):
            if path == "/xhs/getCategoryTree.json":
                return [
                    {
                        "cid": "cycle-a",
                        "xhsCid": "external-cycle-a",
                        "name": "循环类目",
                        "leaf": 0,
                    }
                ]
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = XhsListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            DiscoveryContext(
                style_code="XHS-CYCLE",
                title="循环类目",
                category_hints=("循环类目",),
                base_item_id="cycle-1",
            ),
            None,
            api,
        )

        self.assertEqual(resolution.status, "review_required")
        tree_calls = tuple(
            call for call in transport.calls if call[1] == "/xhs/getCategoryTree.json"
        )
        self.assertEqual(len(tree_calls), 2)
        self.assertTrue(
            any("cycle" in issue for issue in adapter.resolution_fragment.issues),
            adapter.resolution_fragment.issues,
        )

    async def test_tree_node_budget_is_capped_at_128_and_requires_review(self):
        def fixture(path, parameters):
            if path == "/xhs/getCategoryTree.json":
                if str(parameters["parentId"]) != "0":
                    return []
                return [
                    {
                        "cid": "leaf-{0}".format(index),
                        "xhsCid": "external-leaf-{0}".format(index),
                        "name": "预算叶子",
                        "leaf": 1,
                    }
                    for index in range(129)
                ]
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = XhsListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            DiscoveryContext(
                style_code="XHS-NODE-BUDGET",
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
            if path == "/xhs/getCategoryTree.json":
                if str(parameters["parentId"]) == "0":
                    return [
                        {
                            "cid": "branch-{0}".format(index),
                            "name": "分支{0}".format(index),
                            "leaf": 0,
                        }
                        for index in range(128)
                    ]
                return []
            raise AssertionError("unexpected endpoint: {0}".format(path))

        adapter = XhsListing()
        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            DiscoveryContext(
                style_code="XHS-REQUEST-BUDGET",
                title="不存在",
                category_hints=("不存在",),
                base_item_id="request-budget-1",
            ),
            None,
            api,
        )

        self.assertEqual(resolution.status, "review_required")
        tree_calls = tuple(
            call for call in transport.calls if call[1] == "/xhs/getCategoryTree.json"
        )
        self.assertLessEqual(len(tree_calls), 128)
        self.assertTrue(
            any("budget" in issue for issue in adapter.resolution_fragment.issues),
            adapter.resolution_fragment.issues,
        )

    async def test_dynamic_fragment_keeps_generation_from_request_start(self):
        adapter = XhsListing()
        advanced = False

        def fixture(path, parameters):
            nonlocal advanced
            if path == "/xhs/detail.json":
                return {
                    "shopId": "shop-1",
                    "logisticsPlanId": "plan-1",
                }
            if path in (
                "/xhs/getLogisticsList.json",
                "/xhs/getCarriageTemplateList.json",
            ):
                return []
            if path == "/xhs/getCategoryTree.json":
                if str(parameters["parentId"]) != "0":
                    return []
                return [
                    {
                        "cid": "101",
                        "xhsCid": "xhs-101",
                        "name": "工装休闲裤",
                        "leaf": 1,
                    }
                ]
            if path == "/xhs/getAttributeList.json":
                if not advanced:
                    advanced = True
                    adapter.generation_tracker.begin_generation()
                return {"attributeV3s": []}
            if path == "/xhs/getVariations.json":
                return {"variations": []}
            if path == "/xhs/getDeliveryRule.json":
                return {"existing": [], "presale": []}
            raise AssertionError("unexpected endpoint: {0}".format(path))

        transport = RecordingTransport(fixture)
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        context = DiscoveryContext(
            style_code="XHS-GENERATION",
            title="工装休闲裤",
            category_hints=("工装休闲裤",),
            base_item_id="generation-1",
        )
        await adapter.capture_fixed(context, None, api)
        category = await adapter.resolve_category(context, None, api)
        self.assertEqual(category.status, "resolved")
        start_generation = adapter.generation_tracker.begin_generation()

        fragment = await adapter.capture_dynamic(context, None, api, category)

        self.assertGreater(adapter.generation_tracker.current_generation, start_generation)
        self.assertEqual(fragment.generation, start_generation)


if __name__ == "__main__":
    unittest.main()
