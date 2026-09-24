import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from attribute_runtime import (
    AttributeRequest,
    AttributeRuntime,
    ReviewBatchRequired,
    ReviewRequired,
)
from learning_client import PermanentCloudError
from learning_models import CandidateSnapshot, CandidateValue, canonical_sha256
from learning_store import LearningStore


class FakeClient:
    def __init__(self, response=None):
        self.response = response or {
            "status": "auto_fill_ready",
            "value_id": "long",
            "source": "mature_rule",
            "evidence_kinds": ["visual"],
            "mature_rule": True,
            "support_count": 3,
            "calibrated_acceptance_rate": 1.0,
        }
        self.events = []
        self.decide_calls = []

    def post_event(self, key, event_type, payload):
        self.events.append((key, event_type, payload))
        return {"event_id": key}

    def decide(self, request):
        self.decide_calls.append(request)
        return {**self.response, "snapshot_version": request["snapshot_version"]}


def make_length_request(**overrides):
    values = {
        "platform_id": "wxsph",
        "category_leaf_id": "pants",
        "field_id": "length",
        "field_label": "裤长",
        "candidates": (
            CandidateValue("short", "短裤"),
            CandidateValue("long", "长裤"),
        ),
        "excel_value": "",
        "evidence": {"visual": ["asset-1"]},
        "custom_allowed": False,
        "schema_version": "schema-1",
    }
    values.update(overrides)
    return AttributeRequest(**values)


class AttributeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_names_with_different_ids_choose_first_without_review(self):
        client = FakeClient()
        runtime = AttributeRuntime(self.store, client, 'run', 'product')
        result = await runtime.resolve(make_length_request(excel_value='口袋',
            candidates=(CandidateValue('first','口袋'), CandidateValue('second','口袋'))))
        self.assertEqual((result.value_id,result.label), ('first','口袋'))
        self.assertEqual(client.decide_calls, [])
        await runtime._flush()

    def remember_cross_product(self, request, value, product='old-product'):
        snapshot = CandidateSnapshot(request.platform_id, request.category_leaf_id,
            request.field_id, request.field_label, request.candidates,
            request.schema_version, request.custom_allowed)
        self.store.save_candidate_snapshot(snapshot)
        review_id = product + request.field_id
        self.store.enqueue(review_id, 'review.created', {
            'id': review_id, 'evidence_json': {'reuse_context': {
                'excel_candidates': request.excel_value.split('/'),
                'control_type': request.control_type,
            }},
        })
        self.store.save_review_resolution(product_version=product,
            platform_id=request.platform_id, snapshot_version=snapshot.snapshot_version,
            review_id=review_id, final_value_id=value)

    async def test_new_product_without_excel_match_uses_confidence_gated_learning(self):
        old = make_length_request(excel_value='候选甲/候选乙')
        self.remember_cross_product(old, 'long')
        new = replace(
            old,
            candidates=(
                CandidateValue('new-short', '短裤'),
                CandidateValue('new-long', '长裤'),
            ),
            schema_version='new-schema',
        )
        client = FakeClient({
            "status": "auto_fill_ready",
            "value_id": "new-long",
            "source": "mature_rule",
            "evidence_kinds": ["visual"],
            "mature_rule": True,
            "support_count": 3,
            "calibrated_acceptance_rate": 0.95,
        })
        runtime = AttributeRuntime(self.store, client, 'new-run', 'new-product')

        self.assertIsNone(runtime.reusable_choice(new))
        result = await runtime.resolve(new)

        self.assertEqual((result.value_id, result.source),
                         ('new-long', 'mature_rule'))
        self.assertEqual(len(client.decide_calls), 1)
        await runtime._flush()

    async def test_cross_product_reuse_supports_confirmed_multi_choice(self):
        old = make_length_request(excel_value='原文甲/原文乙')
        self.remember_cross_product(old, 'short,long')
        new = replace(
            old,
            candidates=(
                CandidateValue('new-short', '短裤'),
                CandidateValue('new-long', '长裤'),
            ),
            schema_version='new-schema',
        )
        runtime = AttributeRuntime(self.store, FakeClient(), 'new-run', 'old-product')

        reusable = runtime.reusable_choice(new)
        self.assertEqual(
            (reusable.value_id, reusable.label, reusable.source),
            ('new-short,new-long', '短裤,长裤', 'cross_product_human'),
        )
        resolved = await runtime.resolve(new)
        self.assertEqual(
            (resolved.value_id, resolved.label, resolved.source),
            ('new-short,new-long', '短裤,长裤', 'cross_product_human'),
        )
        await runtime.drain()

    async def test_legacy_select_metadata_reuses_proven_multi_choice_review(self):
        old = make_length_request(excel_value="通用/成人", control_type="select")
        self.remember_cross_product(old, "short,long")
        new = replace(
            old,
            candidates=(
                CandidateValue("new-short", "短裤"),
                CandidateValue("new-long", "长裤"),
            ),
            excel_value="通用/男女通用",
            schema_version="new-schema",
            control_type="multi_select",
        )
        runtime = AttributeRuntime(
            self.store, FakeClient(), "new-run", "new-product"
        )

        # A legacy cross-product approval is training evidence only. The new
        # product must ask the decision service, where the confidence gate is
        # applied, instead of reusing this one historical row locally.
        self.assertIsNone(runtime.reusable_choice(new))

    async def test_other_product_review_never_bypasses_excel_or_confidence_gate(self):
        old = make_length_request(excel_value='原文甲/原文乙')
        self.remember_cross_product(old, 'long')
        client = FakeClient()
        runtime = AttributeRuntime(self.store, client, 'new-run', 'new-product')
        for changed in [replace(old, platform_id='jd'),
                        replace(old, control_type='text'),
                        replace(old, candidates=(CandidateValue('short', '短裤'),))]:
            self.assertIsNone(runtime.reusable_choice(changed))
        self.assertIsNone(runtime.reusable_choice(
            replace(old, excel_value='短裤')
        ))
        self.assertIsNone(runtime.reusable_choice(
            replace(old, excel_value='新原文')
        ))
        forced = runtime.reusable_choice(
            replace(old, excel_value='短裤', evidence={'force_review': True})
        )
        self.assertIsNone(forced)
        self.assertIsNone(runtime.reusable_choice(
            replace(old, evidence={'force_review': True, 'selection_only': True})
        ))
        result = await runtime.resolve(replace(old, excel_value='短裤'))
        self.assertEqual((result.label, result.source),
                         ('短裤', 'explicit_text'))
        await runtime._flush()
        uploaded_snapshots = {
            event[2]['snapshot_version']
            for event in runtime.client.events
            if event[1] == 'snapshot.created'
        }
        self.assertIn(result.snapshot_version, uploaded_snapshots)

    async def test_same_product_review_precedes_excel_across_category_snapshots(self):
        old = make_length_request(excel_value='原文甲/原文乙')
        self.remember_cross_product(old, 'long', product='same-product')
        request = replace(
            old,
            category_leaf_id='new-category',
            schema_version='new-schema',
            excel_value='短裤',
        )
        client = FakeClient()
        runtime = AttributeRuntime(
            self.store, client, 'new-run', 'same-product'
        )

        result = await runtime.resolve(request)
        await runtime.drain()

        self.assertEqual(
            (result.label, result.source),
            ('长裤', 'cross_product_human'),
        )
        uploaded_snapshots = {
            event[2]['snapshot_version']
            for event in client.events
            if event[1] == 'snapshot.created'
        }
        self.assertIn(result.snapshot_version, uploaded_snapshots)

    async def test_same_product_latest_correction_wins_conflicting_history(self):
        request = make_length_request(excel_value='原文甲/原文乙')
        self.remember_cross_product(request, 'long', 'old-1')
        self.remember_cross_product(request, 'short', 'old-2')
        runtime = AttributeRuntime(self.store, FakeClient({'status': 'review_required'}),
                                   'new-run', 'old-2')
        # The second confirmation is the operator's latest correction and is
        # reused even though the older category recorded the other value.
        self.assertEqual(runtime.reusable_choice(request).label, '短裤')
        result = await runtime.resolve(request)
        self.assertEqual((result.label, result.source), ('短裤', 'human_override'))
        await runtime.drain()

    async def test_legacy_text_approval_requires_provable_excel_provenance(self):
        values = (CandidateValue('2026', '2026'), CandidateValue('动态选择当天', '动态选择当天'))
        schema = canonical_sha256({'platform_id': 'douyin', 'category_leaf_id': 'pants',
            'field_id': '680', 'control_type': 'text',
            'options': [{'value_id': v.value_id, 'label': v.label} for v in values]})
        request = make_length_request(platform_id='douyin', field_id='680',
            field_label='上市时间', candidates=values, excel_value='2026/动态选择当天',
            control_type='text', custom_allowed=True, schema_version=schema)
        snapshot = CandidateSnapshot('douyin', 'pants', '680', '上市时间', values, schema, True)
        self.store.save_candidate_snapshot(snapshot)
        self.store.save_review_resolution(product_version='old', platform_id='douyin',
            snapshot_version=snapshot.snapshot_version, review_id='legacy', final_value_id='2026')
        runtime = AttributeRuntime(self.store, FakeClient(), 'new-run', 'new-product')
        self.assertIsNone(runtime.reusable_choice(request))
        self.store.enqueue('legacy', 'review.created', {'id': 'legacy',
            'evidence_json': {'excel': True, 'text': True}})
        self.assertIsNone(runtime.reusable_choice(request))
        self.assertIsNone(runtime.reusable_choice(replace(request, excel_value='2027/动态选择当天')))

    async def test_human_full_snapshot_overrides_exact_excel(self):
        request = make_length_request(excel_value="短裤")
        snapshot = CandidateSnapshot(request.platform_id, request.category_leaf_id,
            request.field_id, request.field_label, request.candidates,
            request.schema_version, request.custom_allowed)
        self.store.save_candidate_snapshot(snapshot)
        self.store.save_review_resolution(product_version="product-1",
            platform_id="wxsph", snapshot_version=snapshot.snapshot_version,
            review_id="approved", final_value_id="long")
        runtime = AttributeRuntime(self.store, FakeClient(), "run", "product-1")
        self.assertEqual(runtime.confirmed_choice(request).label, "长裤")
        result = await runtime.resolve(request)
        self.assertEqual((result.label, result.source), ("长裤", "human_override"))

    async def test_review_confirmation_supports_multi_choice_values(self):
        # 审核确认兼容多选：final_value_id 用逗号分隔多个 valueId 或
        # label（如“short,long”或“短裤,长裤”），全部唯一命中候选后按
        # Excel 同款逗号语法组合返回，消费端 selection_value_groups 拆组全选。
        request = make_length_request(excel_value="短裤,长裤", custom_allowed=False)
        snapshot = CandidateSnapshot(request.platform_id, request.category_leaf_id,
            request.field_id, request.field_label, request.candidates,
            request.schema_version, request.custom_allowed)
        self.store.save_candidate_snapshot(snapshot)
        self.store.save_review_resolution(product_version="product-1",
            platform_id="wxsph", snapshot_version=snapshot.snapshot_version,
            review_id="approved", final_value_id="short,long")
        runtime = AttributeRuntime(self.store, FakeClient(), "run", "product-1")
        result = await runtime.resolve(request)
        self.assertEqual((result.value_id, result.label, result.source),
                         ("short,long", "短裤,长裤", "human_override"))
        # label 组合与 Excel 多选语法一致，selection_value_groups 拆成两组。
        from taobao_listing import selection_value_groups
        self.assertEqual(
            selection_value_groups("裤长", result.label),
            (("短裤",), ("长裤",)),
        )

    async def test_verified_history_reuses_multi_choice_group(self):
        # 多选字段的保存回读是一组值（如 适用季节=秋季,冬季）：组内每个
        # label 都唯一命中当前候选且都在 Excel 意图内时组合复用。
        from historical_readbacks import normalize_history_field_label
        from platform_registry import canonical_platform_name
        history_key = (
            canonical_platform_name("wxsph"),
            normalize_history_field_label("裤长"),
        )
        runtime = AttributeRuntime(
            self.store, FakeClient(), "run", "product-1",
            verified_history={history_key: ("短裤", "长裤")},
        )
        request = make_length_request(excel_value="短裤,长裤", custom_allowed=False)
        reusable = runtime.reusable_choice(request)
        self.assertEqual(
            (reusable.value_id, reusable.label, reusable.source),
            ("short,long", "短裤,长裤", "verified_history"),
        )
        result = await runtime.resolve(request)
        self.assertEqual((result.label, result.source),
                         ("短裤,长裤", "verified_history"))
        # Excel 意图收窄后（只要短裤）不再整组复用。
        narrowed = make_length_request(excel_value="短裤", custom_allowed=False)
        self.assertIsNone(runtime.reusable_choice(narrowed))
        narrowed_result = await runtime.resolve(narrowed)
        self.assertNotEqual(
            (narrowed_result.label, narrowed_result.source),
            ("短裤,长裤", "verified_history"),
        )
        await runtime.drain()

    async def test_write_failure_defers_review_even_with_exact_excel(self):
        runtime = AttributeRuntime(self.store, FakeClient(), "run", "product-1")
        runtime.begin_review_collection("wxsph")
        result = await runtime.resolve(make_length_request(excel_value="短裤",
            evidence={"force_review": True, "selection_only": True,
                      "summary": "无法手填，请选择页面候选"}))
        self.assertIsNone(result)
        self.assertEqual(len(runtime.deferred_reviews), 1)
        await runtime._flush()
        if runtime._background_flush is not None:
            await runtime._background_flush

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "learning.sqlite3")
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_resolves_only_a_live_candidate_after_snapshot_delivery(self):
        client = FakeClient()
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        result = await runtime.resolve(make_length_request())

        self.assertEqual((result.value_id, result.label), ("long", "长裤"))
        self.assertEqual(client.events[0][1], "snapshot.created")
        self.assertEqual(client.events[0][2]["options"][1]["value_id"], "long")
        self.assertEqual(self.store.pending_outbox(), ())

    async def test_review_response_is_persisted_and_raises_before_write(self):
        client = FakeClient(
            {"status": "review_required", "reason_code": "insufficient_evidence"}
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        with self.assertRaises(ReviewRequired) as caught:
            await runtime.resolve(make_length_request())

        self.assertEqual(caught.exception.reason_code, "insufficient_evidence")
        self.assertEqual(client.events[-1][1], "review.created")
        self.assertEqual(client.events[-1][2]["id"], caught.exception.review_id)

    async def test_platform_collection_defers_and_batches_all_review_fields(self):
        client = FakeClient(
            {"status": "review_required", "reason_code": "insufficient_evidence"}
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        runtime.begin_review_collection("wxsph")

        first = await runtime.resolve(make_length_request())
        second = await runtime.resolve(
            make_length_request(
                field_id="style",
                field_label="风格",
                schema_version="schema-2",
            )
        )

        self.assertIsNone(first)
        self.assertIsNone(second)
        with self.assertRaises(ReviewBatchRequired) as caught:
            runtime.raise_deferred_reviews()
        self.assertEqual(
            [review.request.field_label for review in caught.exception.reviews],
            ["裤长", "风格"],
        )
        await runtime.drain()
        self.assertEqual(
            [event[1] for event in client.events].count("review.created"),
            2,
        )

    async def test_platform_collection_deduplicates_same_snapshot(self):
        client = FakeClient(
            {"status": "review_required", "reason_code": "insufficient_evidence"}
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        runtime.begin_review_collection("wxsph")

        self.assertIsNone(await runtime.resolve(make_length_request()))
        self.assertIsNone(await runtime.resolve(make_length_request()))

        self.assertEqual(len(runtime.deferred_reviews), 1)
        await runtime.drain()
        self.assertEqual(
            [event[1] for event in client.events].count("review.created"),
            1,
        )

    async def test_changed_or_invented_cloud_candidate_requires_review(self):
        client = FakeClient(
            {
                "status": "auto_fill_ready",
                "value_id": "invented",
                "source": "mature_rule",
                "evidence_kinds": ["visual"],
                "mature_rule": True,
                "support_count": 3,
                "calibrated_acceptance_rate": 1.0,
            }
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        with self.assertRaises(ReviewRequired) as caught:
            await runtime.resolve(make_length_request())
        self.assertEqual(caught.exception.reason_code, "candidate_missing")

    async def test_excel_exact_match_is_sent_as_text_evidence(self):
        client = FakeClient(
            {
                "status": "auto_fill_ready",
                "value_id": "long",
                "source": "explicit_text",
                "evidence_kinds": ["text", "visual"],
                "mature_rule": False,
                "support_count": 0,
                "calibrated_acceptance_rate": 0,
            }
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        await runtime.resolve(make_length_request(excel_value="长裤"))
        await runtime.drain()
        text_event = next(event for event in client.events if event[1] == "text_facts.created")
        self.assertEqual(
            text_event[2]["payload_json"]["values"]["pants_length"]["value_id"],
            "long",
        )
        self.assertEqual(client.decide_calls, [])

    async def test_multi_select_prefers_first_complete_excel_variant(self):
        client = FakeClient()
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        result = await runtime.resolve(
            make_length_request(
                platform_id="jd",
                field_id="season",
                field_label="适用季节",
                candidates=(
                    CandidateValue("spring", "春季"),
                    CandidateValue("summer", "夏季"),
                    CandidateValue("autumn", "秋季"),
                    CandidateValue("winter", "冬季"),
                ),
                excel_value="秋季，冬季/春秋冬/春秋",
                control_type="multi_select",
            )
        )
        await runtime.drain()

        self.assertEqual(
            (result.value_id, result.label, result.source),
            ("autumn,winter", "秋季,冬季", "explicit_text"),
        )
        self.assertEqual(client.decide_calls, [])

    async def test_single_select_keeps_comma_inside_candidate_label(self):
        client = FakeClient()
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        result = await runtime.resolve(
            make_length_request(
                candidates=(
                    CandidateValue("combined", "防风，保暖"),
                    CandidateValue("wind", "防风"),
                    CandidateValue("warm", "保暖"),
                ),
                excel_value="防风，保暖",
                control_type="select",
            )
        )
        await runtime.drain()

        self.assertEqual(
            (result.value_id, result.label, result.source),
            ("combined", "防风，保暖", "explicit_text"),
        )
        self.assertEqual(client.decide_calls, [])

    async def test_same_product_snapshot_and_value_are_not_enqueued_again_on_rerun(self):
        first_client = FakeClient()
        first_runtime = AttributeRuntime(
            self.store, first_client, "run-1", "product-1", device_id="device-1"
        )
        await first_runtime.resolve(make_length_request(excel_value="长裤"))
        await first_runtime.drain()

        second_client = FakeClient()
        second_runtime = AttributeRuntime(
            self.store, second_client, "run-2", "product-1", device_id="device-1"
        )
        await second_runtime.resolve(make_length_request(excel_value="长裤"))
        await second_runtime.drain()

        self.assertEqual(
            [event[1] for event in first_client.events],
            ["snapshot.created", "text_facts.created"],
        )
        self.assertEqual(second_client.events, [])

    async def test_exact_excel_match_projects_a_large_platform_list_to_one_option(self):
        client = FakeClient()
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )
        candidates = tuple(
            CandidateValue(f"brand-{index}", f"品牌{index}")
            for index in range(600)
        ) + (CandidateValue("neighbor", "NEIGBORL"),)

        result = await runtime.resolve(
            make_length_request(
                field_id="brand",
                field_label="品牌",
                candidates=candidates,
                excel_value="NEIGBORL",
            )
        )
        await runtime.drain()

        snapshot_event = next(
            event for event in client.events if event[1] == "snapshot.created"
        )
        self.assertEqual(result.label, "NEIGBORL")
        self.assertEqual(
            snapshot_event[2]["options"],
            [{"value_id": "neighbor", "label": "NEIGBORL", "position": 0}],
        )

    async def test_exact_excel_candidate_never_requires_field_mapping_review(self):
        client = FakeClient(
            {"status": "review_required", "reason_code": "field_mapping_required"}
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )

        result = await runtime.resolve(
            make_length_request(
                platform_id="douyin",
                field_id="lining-material",
                field_label="里料材质",
                candidates=(
                    CandidateValue("cotton", "棉"),
                    CandidateValue("linen", "亚麻"),
                ),
                excel_value="棉",
            )
        )
        await runtime.drain()

        self.assertEqual((result.value_id, result.label), ("cotton", "棉"))
        self.assertEqual(result.source, "explicit_text")
        self.assertEqual(client.decide_calls, [])
        text_event = next(event for event in client.events if event[1] == "text_facts.created")
        self.assertEqual(text_event[2]["payload_json"]["values"], {})
        platform_fact = next(
            iter(text_event[2]["payload_json"]["platform_values"].values())
        )
        self.assertEqual(platform_fact["field_label"], "里料材质")
        self.assertEqual(platform_fact["value_label"], "棉")

    async def test_verified_same_product_history_avoids_repeat_review(self):
        client = FakeClient(
            {"status": "review_required", "reason_code": "field_mapping_required"}
        )
        runtime = AttributeRuntime(
            self.store,
            client,
            "run-1",
            "product-1",
            device_id="device-1",
            verified_history={("douyin", "风格"): ("时尚都市",)},
        )

        result = await runtime.resolve(
            make_length_request(
                platform_id="fxg",
                field_id="style",
                field_label="风格",
                candidates=(
                    CandidateValue("casual", "休闲"),
                    CandidateValue("urban", "时尚都市"),
                ),
                excel_value="",
            )
        )
        await runtime.drain()

        self.assertEqual((result.value_id, result.label), ("urban", "时尚都市"))
        self.assertEqual(result.source, "verified_history")
        self.assertEqual(client.decide_calls, [])

    async def test_compatible_verified_history_precedes_same_excel_candidate(self):
        client = FakeClient()
        runtime = AttributeRuntime(
            self.store,
            client,
            "run-1",
            "product-1",
            device_id="device-1",
            verified_history={("wxsph", "裤长"): ("长裤",)},
        )

        result = await runtime.resolve(
            make_length_request(excel_value="长裤")
        )
        await runtime.drain()

        self.assertEqual((result.value_id, result.label), ("long", "长裤"))
        self.assertEqual(result.source, "verified_history")
        fact = next(
            payload
            for _key, event_type, payload in client.events
            if event_type == "text_facts.created"
        )
        self.assertEqual(fact["source"], "verified_readback")

    async def test_save_readback_queues_field_level_learning_event(self):
        client = FakeClient()
        runtime = AttributeRuntime(
            self.store,
            client,
            "run-1",
            "product-1",
            device_id="device-1",
        )
        result = await runtime.resolve(
            make_length_request(excel_value="长裤")
        )

        recorded = runtime.record_verified_readbacks(
            "wxsph", {"裤长": ["长裤"]}
        )
        await runtime.drain()
        readback = next(
            payload
            for _key, event_type, payload in client.events
            if event_type == "readback.recorded"
        )

        self.assertEqual(recorded, 1)
        self.assertEqual(readback["field_id"], "length")
        self.assertEqual(readback["actual_value_id"], "long")
        self.assertEqual(readback["actual_label"], "长裤")
        self.assertEqual(readback["snapshot_version"], result.snapshot_version)
        self.assertTrue(readback["verified"])

    async def test_save_readback_queues_multi_choice_learning_event(self):
        client = FakeClient()
        runtime = AttributeRuntime(
            self.store,
            client,
            "run-1",
            "product-1",
            device_id="device-1",
        )
        result = await runtime.resolve(
            make_length_request(
                excel_value="短裤，长裤",
                control_type="multi_select",
            )
        )

        recorded = runtime.record_verified_readbacks(
            "wxsph", {"裤长": ["短裤", "长裤"]}
        )
        await runtime.drain()
        readback = next(
            payload
            for _key, event_type, payload in client.events
            if event_type == "readback.recorded"
        )

        self.assertEqual(recorded, 1)
        self.assertEqual(readback["actual_value_id"], "short,long")
        self.assertEqual(readback["actual_label"], "短裤,长裤")
        self.assertEqual(readback["snapshot_version"], result.snapshot_version)

    async def test_confirmed_review_is_reused_for_unmapped_field_after_retry(self):
        client = FakeClient(
            {"status": "review_required", "reason_code": "field_mapping_required"}
        )
        request = make_length_request(
            platform_id="douyin",
            category_leaf_id="jacket",
            field_id="lining-percent",
            field_label="里料材质成分含量",
            candidates=(
                CandidateValue("95%及以上", "95%及以上"),
                CandidateValue("95", "95"),
            ),
            excel_value="95%及以上/95",
            custom_allowed=True,
            control_type="text",
        )
        snapshot = CandidateSnapshot(
            request.platform_id,
            request.category_leaf_id,
            request.field_id,
            request.field_label,
            request.candidates,
            request.schema_version,
            request.custom_allowed,
        )
        self.store.save_review_resolution(
            product_version="product-1",
            platform_id="douyin",
            snapshot_version=snapshot.snapshot_version,
            review_id="review-1",
            final_value_id="95",
        )
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )

        result = await runtime.resolve(request)

        self.assertEqual((result.value_id, result.label), ("95", "95"))
        self.assertEqual(result.source, "human_override")
        self.assertEqual(client.decide_calls, [])

    async def test_exact_excel_delivery_runs_in_background_and_drains(self):
        class SlowClient(FakeClient):
            def post_event(self, key, event_type, payload):
                time.sleep(0.3)
                return super().post_event(key, event_type, payload)

        client = SlowClient()
        runtime = AttributeRuntime(
            self.store, client, "run-1", "product-1", device_id="device-1"
        )

        started = time.monotonic()
        result = await runtime.resolve(make_length_request(excel_value="长裤"))
        elapsed = time.monotonic() - started

        self.assertEqual(result.label, "长裤")
        self.assertLess(elapsed, 0.2)
        await runtime.drain()
        self.assertEqual(self.store.pending_outbox(), ())

    async def test_completed_background_delivery_failure_is_not_silently_replaced(self):
        class FailingClient(FakeClient):
            def post_event(self, key, event_type, payload):
                raise PermanentCloudError("cloud status 400")

        runtime = AttributeRuntime(
            self.store,
            FailingClient(),
            "run-1",
            "product-1",
            device_id="device-1",
        )
        await runtime.resolve(make_length_request(excel_value="长裤"))
        while not runtime._background_flush.done():
            await asyncio.sleep(0)

        with self.assertRaisesRegex(PermanentCloudError, "cloud status 400"):
            await runtime.resolve(make_length_request(excel_value="长裤"))

    async def test_operational_selector_is_rejected(self):
        runtime = AttributeRuntime(
            self.store, FakeClient(), "run-1", "product-1", device_id="device-1"
        )
        with self.assertRaisesRegex(ValueError, "operational selector"):
            await runtime.resolve(make_length_request(control_type="freight"))


if __name__ == "__main__":
    unittest.main()
