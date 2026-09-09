import json
import unittest

from platform_discovery import ApiClient, DiscoveryContext
from platform_registry import get_platform_spec
from platform_schema import to_dict


PREDICTION_PAYLOAD = {
    "catList": (
        {
            "leftCid": "20970",
            "leftName": "工装裤",
            "cidNames": ("童装", "裤子", "工装裤"),
            "score": 0.99,
        },
        {
            "leftCid": "7307",
            "leftName": "休闲裤",
            "cidNames": ("男装", "裤子", "休闲裤"),
            "score": 0.95,
        },
    )
}


class PddFixtureTransport:
    def __init__(self):
        self.calls = []

    async def __call__(self, endpoint, parameters):
        parameters = dict(parameters)
        self.calls.append((endpoint.path, parameters))
        if endpoint.path == "/pdd/detail.json":
            return {
                "data": {
                    "fieldDescriptorList": (
                        {"id": "goodsName", "label": "商品名称", "type": "input"},
                    ),
                    "cookie": "PRIVATE-COOKIE-MUST-NOT-LEAK",
                }
            }
        if endpoint.path == "/publish/fast/prediction/cat.json":
            return {"data": PREDICTION_PAYLOAD}
        if endpoint.path == "/pdd/getCategoryProperties.json":
            if str(parameters["leafCategoryId"]) == "20970":
                return {
                    "data": None,
                    "status": "business_error",
                    "message": "类目尚未开放",
                }
            return {
                "data": {
                    "goodsPropertiesRule": (
                        {
                            "refPid": "fit",
                            "name": "版型",
                            "required": True,
                            "propertyValueType": "select",
                            "chooseMaxNum": 1,
                            "canNote": False,
                            "values": (
                                {"vid": "loose", "value": "宽松"},
                                {"vid": "straight", "value": "直筒"},
                            ),
                        },
                    ),
                    "goodsServiceRule": (
                        {"refPid": "service", "name": "服务承诺", "required": False},
                    ),
                    "goodsSkuRule": (
                        {"refPid": "sku-color", "name": "颜色", "chooseMaxNum": 8},
                    ),
                    "spuRule": {"required": False},
                    "twoPiecesDiscountRule": {"enabled": True},
                }
            }
        if endpoint.path == "/pdd/getSpecList.json":
            return {
                "data": (
                    {"parentSpecId": "color", "parentSpecName": "颜色"},
                    {"parentSpecId": "size", "parentSpecName": "尺码"},
                )
            }
        if endpoint.path == "/pdd/getBrandRequireRule.json":
            return {"data": {"SHOP-ID-ONLY-IN-MEMORY": True}}
        if endpoint.path == "/publish/fast/prediction/cat/prop.json":
            return {
                "data": {
                    "properties": (
                        {
                            "refPid": "predicted-style",
                            "name": "风格",
                            "predictedValue": "PRIVATE-PREDICTED-DEFAULT",
                            "values": (
                                {"vid": "casual", "value": "休闲"},
                                {"vid": "workwear", "value": "工装"},
                            ),
                        },
                    )
                }
            }
        raise AssertionError("unexpected endpoint: {0}".format(endpoint.path))


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


class PddListingFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from pdd_listing import PddListing

        self.adapter = PddListing()
        self.transport = PddFixtureTransport()
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
        spec = get_platform_spec("pdd")
        self.assertEqual(spec.discovery_adapter, "pdd_listing:PddListing")
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
                "/pdd/detail.json": (
                    "GET",
                    ("baseItemId", "api_name"),
                    True,
                ),
                "/item/picture/query.json": (
                    "POST",
                    ("api_name", "baseItemId"),
                    True,
                ),
                "/pdd/getGroupSpecNames.json": (
                    "POST",
                    ("api_name",),
                    True,
                ),
                "/publish/fast/prediction/cat.json": (
                    "POST",
                    ("api_name", "baseItemId", "platformType", "title"),
                    True,
                ),
                "/pdd/getSpecList.json": (
                    "GET",
                    ("leafCategoryId", "shopId", "api_name"),
                    True,
                ),
                "/pdd/getBrandRequireRule.json": (
                    "GET",
                    ("leafCategoryId", "shopIds", "api_name"),
                    True,
                ),
                "/pdd/getCategoryProperties.json": (
                    "GET",
                    ("leafCategoryId", "shopId", "api_name"),
                    True,
                ),
                "/publish/fast/prediction/cat/prop.json": (
                    "GET",
                    ("baseItemId", "platformType", "catId", "shopId", "api_name"),
                    True,
                ),
            },
        )

    async def test_first_unavailable_recommendation_is_probed_then_safe_exact_candidate_selected(self):
        resolution = await self.adapter.resolve_category(
            self.context,
            self.panel,
            self.api,
        )

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.source, "recommendation")
        self.assertEqual(resolution.selected.leaf_id, "7307")
        self.assertEqual(resolution.selected.validation_status, "validated")
        property_probe_ids = [
            str(parameters["leafCategoryId"])
            for path, parameters in self.transport.calls
            if path == "/pdd/getCategoryProperties.json"
        ]
        self.assertEqual(property_probe_ids, ["20970", "7307"])
        selected_paths = {
            path
            for path, parameters in self.transport.calls
            if str(parameters.get("leafCategoryId")) == "7307"
        }
        self.assertEqual(
            selected_paths,
            {
                "/pdd/getCategoryProperties.json",
                "/pdd/getSpecList.json",
                "/pdd/getBrandRequireRule.json",
            },
        )

        await self.adapter.activate_category(
            self.context,
            self.panel,
            resolution.selected,
        )
        self.assertEqual(self.panel.activated, ["7307"])

    async def test_prediction_properties_are_source_evidence_never_defaults(self):
        fixed = await self.adapter.capture_fixed(self.context, self.panel, self.api)
        self.assertEqual(fixed.sections[0].fields[0].label, "商品名称")

        resolution = await self.adapter.resolve_category(
            self.context,
            self.panel,
            self.api,
        )
        dynamic = await self.adapter.capture_dynamic(
            self.context,
            self.panel,
            self.api,
            resolution,
        )

        section_keys = tuple(section.key for section in dynamic.sections)
        self.assertEqual(
            section_keys,
            (
                "specifications",
                "brand_requirement",
                "category_properties",
                "service_rules",
                "sku_rules",
                "spu_rules",
                "two_pieces_discount",
                "prediction_properties",
            ),
        )
        prediction_field = dynamic.sections[-1].fields[0]
        self.assertEqual(prediction_field.option_summary.source, "prediction")
        self.assertFalse(hasattr(prediction_field, "default"))
        serialized = json.dumps(to_dict(dynamic), ensure_ascii=False)
        self.assertNotIn("PRIVATE-PREDICTED-DEFAULT", serialized)
        self.assertNotIn("SHOP-ID-ONLY-IN-MEMORY", serialized)
        self.assertIn("candidate_unavailable:20970", dynamic.issues)

    async def test_real_nested_rules_prediction_json_api_names_and_frozen_generation(self):
        adapter = self.adapter
        raw_prediction_default = "PRIVATE-PREDICTION-DEFAULT"

        class RealShapeTransport:
            def __init__(_self):
                _self.calls = []
                _self.bumped = False

            async def __call__(_self, endpoint, parameters):
                parameters = dict(parameters)
                _self.calls.append((endpoint.path, parameters))
                path = endpoint.path
                if path == "/pdd/detail.json":
                    return {"data": {}}
                if path == "/publish/fast/prediction/cat.json":
                    return {"data": PREDICTION_PAYLOAD}
                if path == "/pdd/getCategoryProperties.json":
                    if str(parameters["leafCategoryId"]) == "20970":
                        return {
                            "data": None,
                            "status": "business_error",
                            "message": "类目尚未开放",
                        }
                    return {
                        "data": {
                            "goodsPropertiesRule": {
                                "properties": [
                                    {
                                        "refPid": "fit",
                                        "name": "版型",
                                        "required": True,
                                        "values": [{"vid": "loose", "value": "宽松"}],
                                    }
                                ]
                            },
                            "goodsServiceRule": {
                                "services": [
                                    {
                                        "refPid": "freight-insurance",
                                        "name": "运费险",
                                        "required": False,
                                    }
                                ]
                            },
                            "goodsSkuRule": {
                                "maxSpecNum": 2,
                                "minPrice": 1,
                            },
                            "spuRule": {
                                "properties": [
                                    {
                                        "refPid": "spu-model",
                                        "name": "商品型属性",
                                    }
                                ]
                            },
                            "twoPiecesDiscountRule": {
                                "enabled": True,
                                "minimum": 2,
                            },
                        }
                    }
                if path == "/pdd/getSpecList.json":
                    return {
                        "data": [
                            {"parentSpecId": "color", "parentSpecName": "颜色"},
                        ]
                    }
                if path == "/pdd/getBrandRequireRule.json":
                    return {"data": {"SHOP-ID-ONLY-IN-MEMORY": True}}
                if path == "/publish/fast/prediction/cat/prop.json":
                    if not _self.bumped:
                        _self.bumped = True
                        adapter.generation_tracker.begin_generation()
                    return {
                        "data": {
                            "platformCatProp": json.dumps(
                                [
                                    {
                                        "refPid": "predicted-style",
                                        "name": "风格",
                                        "predictedValue": raw_prediction_default,
                                    }
                                ],
                                ensure_ascii=False,
                            ),
                            "platformCatPropVoMap": {
                                "predicted-style": {
                                    "values": [
                                        {"vid": "casual", "value": "休闲"},
                                        {"vid": "workwear", "value": "工装"},
                                    ]
                                }
                            },
                        }
                    }
                raise AssertionError(path)

        transport = RealShapeTransport()
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            self.context,
            self.panel,
            api,
        )
        generation_at_start = adapter.generation_tracker.begin_generation()
        dynamic = await adapter.capture_dynamic(
            self.context,
            self.panel,
            api,
            resolution,
        )

        self.assertEqual(dynamic.generation, generation_at_start)
        by_section = {section.key: section for section in dynamic.sections}
        self.assertEqual(by_section["category_properties"].fields[0].label, "版型")
        self.assertEqual(
            tuple(
                (value.value_id, value.label)
                for value in by_section["category_properties"].fields[0].option_values
            ),
            (("loose", "宽松"),),
        )
        self.assertEqual(by_section["service_rules"].fields[0].label, "运费险")
        self.assertTrue(by_section["sku_rules"].fields)
        self.assertEqual(by_section["spu_rules"].fields[0].label, "商品型属性")
        self.assertTrue(by_section["two_pieces_discount"].fields)
        prediction = by_section["prediction_properties"].fields[0]
        self.assertEqual(prediction.option_summary.source, "prediction")
        self.assertEqual(prediction.option_summary.count, 2)
        self.assertEqual(len(prediction.option_values), 2)
        self.assertEqual(
            tuple((value.value_id, value.label) for value in prediction.option_values),
            (("casual", "休闲"), ("workwear", "工装")),
        )
        self.assertNotIn(
            raw_prediction_default,
            json.dumps(to_dict(dynamic), ensure_ascii=False),
        )

        api_names = {path: parameters["api_name"] for path, parameters in transport.calls}
        self.assertEqual(
            api_names["/publish/fast/prediction/cat.json"],
            "publish_fast_prediction_cat",
        )
        self.assertEqual(
            api_names["/pdd/getCategoryProperties.json"],
            "pdd_getCategoryProperties",
        )
        self.assertEqual(api_names["/pdd/getSpecList.json"], "pdd_getSpecList")
        self.assertEqual(
            api_names["/pdd/getBrandRequireRule.json"],
            "pdd_getBrandRequireRule",
        )
        self.assertEqual(
            api_names["/publish/fast/prediction/cat/prop.json"],
            "publish_fast_prediction_cat_prop",
        )

    async def test_probe_rejects_candidate_with_empty_required_rule_structures(self):
        adapter = self.adapter

        class EmptyRuleTransport(PddFixtureTransport):
            async def __call__(_self, endpoint, parameters):
                if (
                    endpoint.path == "/pdd/getCategoryProperties.json"
                    and str(parameters["leafCategoryId"]) == "7307"
                ):
                    _self.calls.append((endpoint.path, dict(parameters)))
                    return {
                        "data": {
                            "goodsPropertiesRule": {"properties": []},
                            "goodsServiceRule": {},
                            "goodsSkuRule": {},
                            "spuRule": {},
                            "twoPiecesDiscountRule": {},
                        }
                    }
                return await super().__call__(endpoint, parameters)

        transport = EmptyRuleTransport()
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            self.context,
            self.panel,
            api,
        )

        self.assertEqual(resolution.status, "review_required")
        self.assertIsNone(resolution.selected)
        self.assertIn("candidate_unavailable:7307", adapter.resolution_fragment.issues)

    async def test_real_service_rule_map_and_optional_rules_remain_viable(self):
        adapter = self.adapter

        class RealOptionalRuleTransport(PddFixtureTransport):
            async def __call__(_self, endpoint, parameters):
                if (
                    endpoint.path == "/pdd/getCategoryProperties.json"
                    and str(parameters["leafCategoryId"]) == "7307"
                ):
                    _self.calls.append((endpoint.path, dict(parameters)))
                    return {
                        "data": {
                            "goodsPropertiesRule": {
                                "properties": [
                                    {"refPid": "fit", "name": "版型"},
                                ]
                            },
                            "goodsServiceRule": {
                                "goodsServiceRuleMap": {
                                    "freight-insurance": {
                                        "serviceName": "运费险",
                                        "required": False,
                                    }
                                },
                                "goodsTypeList": [
                                    {
                                        "goodsType": "normal",
                                        "goodsTypeName": "普通商品",
                                    }
                                ],
                            },
                            "goodsSkuRule": {"maxSpecNum": 2},
                            "spuRule": None,
                            "twoPiecesDiscountRule": None,
                        }
                    }
                return await super().__call__(endpoint, parameters)

        transport = RealOptionalRuleTransport()
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            self.context,
            self.panel,
            api,
        )

        self.assertEqual(resolution.status, "resolved")
        dynamic = await adapter.capture_dynamic(
            self.context,
            self.panel,
            api,
            resolution,
        )
        sections = {section.key: section for section in dynamic.sections}
        service_labels = tuple(
            field.label for field in sections["service_rules"].fields
        )
        self.assertIn("运费险", service_labels)
        self.assertIn("普通商品", service_labels)
        self.assertEqual(sections["spu_rules"].fields, ())
        self.assertEqual(sections["two_pieces_discount"].fields, ())

    async def test_probe_rejects_nonempty_wrappers_with_empty_spec_or_brand_content(self):
        adapter = self.adapter

        class EmptySpecWrapperTransport(PddFixtureTransport):
            async def __call__(_self, endpoint, parameters):
                if (
                    endpoint.path == "/pdd/getSpecList.json"
                    and str(parameters["leafCategoryId"]) == "7307"
                ):
                    _self.calls.append((endpoint.path, dict(parameters)))
                    return {"data": {"list": []}}
                return await super().__call__(endpoint, parameters)

        transport = EmptySpecWrapperTransport()
        api = ApiClient(endpoint_catalog=adapter.endpoint_catalog, transport=transport)
        resolution = await adapter.resolve_category(
            self.context,
            self.panel,
            api,
        )

        self.assertEqual(resolution.status, "review_required")
        self.assertIsNone(resolution.selected)
        self.assertIn("candidate_unavailable:7307", adapter.resolution_fragment.issues)


if __name__ == "__main__":
    unittest.main()
