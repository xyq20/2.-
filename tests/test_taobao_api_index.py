import unittest

from taobao_api_index import TaobaoApiJsonIndex


class FakeRequest:
    def __init__(self, method="GET", post_data=""):
        self.method = method
        self.post_data = post_data


class FakeResponse:
    def __init__(
        self,
        url,
        payload,
        *,
        method="GET",
        post_data="",
        status=200,
    ):
        self.url = url
        self.request = FakeRequest(method, post_data)
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


def schema_payload(label="厚薄", field_id="p-thickness"):
    return {
        "result": 1,
        "data": {
            "fieldDescriptorList": [
                {
                    "name": field_id,
                    "label": label,
                    "required": True,
                    "component": {
                        "props": {
                            "dataSource": {
                                "options": [
                                    {"value": "regular", "displayName": "常规"},
                                    {"value": "thick", "displayName": "加厚"},
                                ]
                            }
                        }
                    },
                }
            ]
        },
    }


class TaobaoApiJsonIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_schema_keeps_live_category_field_and_candidate_ids(self):
        index = TaobaoApiJsonIndex(FakePage())
        path = "/tb/getItemPublishSchema"

        await index._consume(
            FakeResponse(
                "https://scm.example/tb/getItemPublishSchema?shopId=private&catId=5001",
                schema_payload(),
            ),
            path,
            "GET",
        )

        fields = index.candidate_fields("厚薄")
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].category_leaf_id, "5001")
        self.assertEqual(fields[0].source_id, "p-thickness")
        self.assertEqual(
            tuple((value.value_id, value.label) for value in fields[0].option_values),
            (("regular", "常规"), ("thick", "加厚")),
        )
        self.assertNotIn("private", repr(await index.safe_summary()))

    async def test_async_property_response_is_correlated_by_cat_and_prop_id(self):
        index = TaobaoApiJsonIndex(FakePage())
        schema_path = "/tb/getItemPublishSchema.json"
        await index._consume(
            FakeResponse(
                "https://scm.example/tb/getItemPublishSchema.json?catId=5001",
                {
                    "result": 1,
                    "data": {
                        "fieldDescriptorList": [
                            {"name": "p-thickness", "label": "厚薄"}
                        ]
                    },
                },
            ),
            schema_path,
            "GET",
        )
        self.assertEqual(index.candidate_fields("厚薄"), ())

        prop_path = "/tb/getItemPublishSchemaProp"
        await index._consume(
            FakeResponse(
                "https://scm.example/tb/getItemPublishSchemaProp?catId=5001&propId=thickness",
                {
                    "result": 1,
                    "data": {
                        "options": [
                            {"value": "regular", "displayName": "常规"},
                            {"value": "thick", "displayName": "加厚"},
                        ]
                    },
                },
            ),
            prop_path,
            "GET",
        )

        fields = index.candidate_fields("厚薄")
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].source_id, "p-thickness")
        self.assertEqual(fields[0].options, ("常规", "加厚"))

    async def test_new_category_drops_old_fields_and_late_old_response(self):
        index = TaobaoApiJsonIndex(FakePage())
        schema_path = "/tb/getItemPublishSchema"
        await index._consume(
            FakeResponse(
                "https://scm.example/tb/getItemPublishSchema?catId=old",
                schema_payload(),
            ),
            schema_path,
            "GET",
        )
        await index._consume(
            FakeResponse(
                "https://scm.example/tb/getItemPublishSchema?catId=new",
                schema_payload(label="裤长", field_id="p-length"),
            ),
            schema_path,
            "GET",
        )
        await index._consume(
            FakeResponse(
                "https://scm.example/tb/getItemPublishSchemaProp?catId=old&propId=thickness",
                {
                    "result": 1,
                    "data": {
                        "options": [
                            {"value": "wrong", "displayName": "旧候选"}
                        ]
                    },
                },
            ),
            "/tb/getItemPublishSchemaProp",
            "GET",
        )

        self.assertEqual(index.candidate_fields("厚薄"), ())
        self.assertEqual(len(index.candidate_fields("裤长")), 1)
        self.assertEqual((await index.safe_summary())["active_category_id"], "new")

    async def test_reopening_same_category_replaces_old_schema(self):
        index = TaobaoApiJsonIndex(FakePage())
        path = "/tb/getItemPublishSchema"
        await index._consume(
            FakeResponse(
                "https://scm.example/tb/getItemPublishSchema?catId=5001",
                schema_payload(),
            ),
            path,
            "GET",
        )
        await index._consume(
            FakeResponse(
                "https://scm.example/tb/getItemPublishSchema?catId=5001",
                schema_payload(label="裤长", field_id="p-length"),
            ),
            path,
            "GET",
        )

        self.assertEqual(index.candidate_fields("厚薄"), ())
        self.assertEqual(len(index.candidate_fields("裤长")), 1)

    async def test_listener_ignores_unknown_endpoint_and_wrong_method(self):
        page = FakePage()
        index = TaobaoApiJsonIndex(page)
        index.install()
        page.emit_response(
            FakeResponse(
                "https://scm.example/tb/getItemPublishSchema?catId=5001",
                schema_payload(),
                method="POST",
            )
        )
        page.emit_response(
            FakeResponse(
                "https://scm.example/tb/saveItem?catId=5001",
                schema_payload(),
                method="POST",
            )
        )
        await index.settle(1)

        self.assertEqual(index.candidate_fields("厚薄"), ())
        index.uninstall()
        self.assertNotIn("response", page.handlers)


if __name__ == "__main__":
    unittest.main()
