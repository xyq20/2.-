import asyncio
import unittest

from tmall_api_index import TmallApiJsonIndex, extract_api_fields


class FakeRequest:
    def __init__(self, method):
        self.method = method


class FakeResponse:
    def __init__(self, url, payload, *, method="GET", status=200):
        self.url = url
        self.request = FakeRequest(method)
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class FakePage:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def remove_listener(self, event, handler):
        if self.handlers.get(event) == handler:
            self.handlers.pop(event)

    def emit_response(self, response):
        self.handlers["response"](response)


class TmallApiExtractionTests(unittest.TestCase):
    def test_extracts_field_ids_options_and_required_without_business_values(self):
        fields = extract_api_fields(
            {
                "success": True,
                "data": {
                    "attrList": [
                        {
                            "propId": 414028073,
                            "label": "裤型",
                            "required": True,
                            "options": [
                                {"value": "secret-id", "displayName": "直筒裤"},
                                {"value": "other-id", "displayName": "工装裤"},
                            ],
                        }
                    ],
                    "shopName": "不得保留的店铺名",
                    "title": "不得保留的商品标题",
                },
            },
            "/dsb/queryCategoryConfigInfo.json",
        )

        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].source_id, "414028073")
        self.assertEqual(fields[0].label, "裤型")
        self.assertTrue(fields[0].required)
        self.assertEqual(fields[0].options, ("直筒裤", "工装裤"))
        self.assertEqual(
            tuple(
                (option.value_id, option.label)
                for option in fields[0].option_values
            ),
            (("secret-id", "直筒裤"), ("other-id", "工装裤")),
        )
        self.assertNotIn("店铺", repr(fields))
        self.assertNotIn("商品标题", repr(fields))

    def test_product_schema_supports_direct_prop_id_mapping(self):
        fields = extract_api_fields(
            {
                "122216347": {
                    "label": "上市年份季节",
                    "options": [{"label": "2026年秋季", "value": "private"}],
                }
            },
            "/tm/getProductMatchSchema.json",
        )

        self.assertEqual(fields[0].source_id, "122216347")
        self.assertEqual(fields[0].options, ("2026年秋季",))

    def test_extracts_json_encoded_edit_schema_fields(self):
        fields = extract_api_fields(
            {
                "success": True,
                "data": [
                    {
                        "editSchemaFieldDescriptor": """
                        {"fields":[{"propId":"344943689","label":"裤型",
                        "options":[{"value":"straight","label":"直筒裤"}]}]}
                        """,
                    }
                ]
            },
            "/tm/detail.json",
        )

        pants_fields = tuple(field for field in fields if field.label == "裤型")
        self.assertEqual(len(pants_fields), 1)
        self.assertEqual(pants_fields[0].source_id, "344943689")
        self.assertEqual(pants_fields[0].options, ("直筒裤",))


class TmallApiIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_matching_existing_product_requires_successful_product_id(self):
        index = TmallApiJsonIndex(FakePage())
        path = "/tm/matchProductSchema.json"
        for payload, expected in [
            ({"result": 1, "data": [{"productId": "123"}]}, True),
            ({"result": 1, "data": [{}]}, False),
            ({"result": 0, "data": [{"productId": "123"}]}, False),
        ]:
            await index._consume(FakeResponse(path, payload, method="POST"), path, "POST")
            self.assertIs(index.matched_existing_product, expected)

    async def test_indexes_only_registered_endpoint_and_exact_method(self):
        page = FakePage()
        index = TmallApiJsonIndex(page)
        index.install()
        page.emit_response(
            FakeResponse(
                "https://scm.example/dsb/queryCategoryConfigInfo.json?shop=private",
                {
                    "data": {
                        "attrList": [
                            {
                                "propId": "414028073",
                                "label": "裤型",
                                "options": [{"displayName": "直筒裤"}],
                            }
                        ]
                    },
                    "success": True,
                },
            )
        )
        page.emit_response(
            FakeResponse(
                "https://scm.example/dsb/queryCategoryConfigInfo.json",
                {"attrList": [{"id": "write", "label": "不应捕获"}]},
                method="POST",
            )
        )
        page.emit_response(
            FakeResponse(
                "https://scm.example/item/base/edit.json",
                {"fields": [{"id": "write", "label": "不应捕获"}]},
                method="POST",
            )
        )

        await index.settle(1)

        self.assertEqual(index.source_ids("裤型"), ("414028073",))
        self.assertEqual(index.resolve_option("裤型", ("直筒裤",)), "直筒裤")
        self.assertEqual(index.fields_for_label("不应捕获"), ())
        summary = await index.safe_summary()
        self.assertEqual(summary["captured_endpoint_count"], 1)
        self.assertEqual(summary["json_endpoint_count"], 1)
        self.assertNotIn("private", repr(summary))
        index.uninstall()
        self.assertNotIn("response", page.handlers)

    async def test_candidate_field_keeps_request_category_and_option_ids(self):
        index = TmallApiJsonIndex(FakePage())
        path = "/dsb/queryCategoryConfigInfo.json"
        await index._consume(
            FakeResponse(
                "https://scm.example/dsb/queryCategoryConfigInfo.json?leafCategoryId=3035",
                {
                    "data": {
                        "attrList": [
                            {
                                "propId": "thickness",
                                "label": "厚薄",
                                "options": [
                                    {"value": "regular", "displayName": "常规"},
                                    {"value": "thick", "displayName": "加厚"},
                                ],
                            }
                        ]
                    },
                    "success": True,
                },
            ),
            path,
            "GET",
        )

        fields = index.candidate_fields("厚薄")

        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].category_leaf_id, "3035")
        self.assertEqual(fields[0].source_id, "thickness")
        self.assertEqual(fields[0].option_values[0].value_id, "regular")

    async def test_candidate_field_uses_category_id_from_detail_payload(self):
        index = TmallApiJsonIndex(FakePage())
        path = "/tm/detail.json"
        await index._consume(
            FakeResponse(
                "https://scm.example/tm/detail.json",
                {
                    "success": True,
                    "data": [
                        {
                            "categoryId": "3035",
                            "editSchemaFieldDescriptor": """
                            {"fields":[{"propId":"344943689","label":"裤型",
                            "options":[{"value":"straight","label":"直筒裤"}]}]}
                            """,
                        }
                    ]
                },
            ),
            path,
            "GET",
        )

        fields = index.candidate_fields("裤型")

        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].category_leaf_id, "3035")

    async def test_resolve_option_follows_candidate_order_and_rejects_duplicates(self):
        page = FakePage()
        index = TmallApiJsonIndex(page)
        index.install()
        page.emit_response(
            FakeResponse(
                "https://scm.example/tm/getProductMatchSchema.json",
                {
                    "one": {
                        "id": "style-a",
                        "label": "风格",
                        "options": [
                            {"label": "时尚都市"},
                            {"label": "休闲风"},
                        ],
                    },
                    "two": {
                        "id": "style-b",
                        "label": "风格",
                        "options": [{"label": "休闲风"}],
                    },
                },
            )
        )
        await index.settle(1)

        self.assertEqual(
            index.resolve_option("风格", ("休闲风", "时尚都市")),
            "休闲风",
        )
        self.assertIsNone(index.resolve_option("风格", ("不存在",)))


if __name__ == "__main__":
    unittest.main()
