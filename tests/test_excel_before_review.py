import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from learning_models import CandidateValue
from attribute_runtime import AttributeRuntime, AttributeRequest
from taobao_listing import TaobaoListing, TaobaoListingError


class ExcelBeforeReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_lining_history_prevents_excel_trial_across_platforms(self):
        from dataclasses import replace
        for platform in ('xhs', 'tb', 'tm', 'pdd', 'wxsph', 'yz'):
            runtime = object.__new__(AttributeRuntime)
            runtime.product_version = 'same-product'
            runtime.store = MagicMock()
            runtime.store.load_review_resolution.return_value = None
            from platform_registry import canonical_platform_name
            runtime.verified_history = {(canonical_platform_name(platform), '有无内胆'): ('无内胆',)}
            listing = object.__new__(TaobaoListing)
            listing.attribute_runtime = runtime
            listing.logger = None
            candidates = (CandidateValue('0', '无内胆'), CandidateValue('1', '有内胆'))
            request = AttributeRequest(platform_id=platform, category_leaf_id='jacket',
                field_id='lining', field_label='有无内胆', candidates=candidates,
                excel_value='否/无', evidence={}, custom_allowed=False, schema_version='s1')
            with patch.object(TaobaoListing, '_select_values', new_callable=AsyncMock) as writer:
                actual, value = await listing._excel_before_review(MagicMock(), candidates,
                    ('否', '无'), label='有无内胆', preflight_request=request)
            writer.assert_not_awaited()
            self.assertEqual(actual, candidates)
            self.assertEqual(value, '否/无')
            self.assertEqual(runtime.reusable_choice(request).label, '无内胆')
            compatible = replace(request, excel_value='无内胆')
            self.assertEqual(
                runtime.reusable_choice(compatible).source,
                'verified_history',
            )
            self.assertIsNone(runtime.reusable_choice(replace(request, excel_value='有内胆')))
            self.assertIsNone(runtime.reusable_choice(replace(request, field_label='其他字段')))
            self.assertIsNone(runtime.reusable_choice(replace(request, candidates=(candidates[1],))))

    async def test_ordered_or_does_not_write(self):
        listing = object.__new__(TaobaoListing)
        candidates = (CandidateValue('b', '自然腰'), CandidateValue('a', '中腰'))
        with patch.object(TaobaoListing, '_select_values', new_callable=AsyncMock) as writer:
            actual, value = await listing._excel_before_review(
                MagicMock(), candidates, ('中腰', '自然腰'), label='腰型')
        self.assertEqual(value, '中腰')
        self.assertEqual(actual, candidates)
        writer.assert_not_awaited()

    async def test_missing_candidate_attempts_input_and_records_verified_value(self):
        listing = object.__new__(TaobaoListing)
        select = MagicMock()
        select.locator.return_value.count = AsyncMock(return_value=0)
        with patch.object(TaobaoListing, '_select_values', new_callable=AsyncMock,
                          return_value=('棉',)) as writer:
            candidates, value = await listing._excel_before_review(
                select, (), ('棉',), label='材质')
        writer.assert_awaited_once()
        self.assertEqual(value, '棉')
        self.assertEqual(candidates, (CandidateValue('棉', '棉'),))

    async def test_failed_input_preserves_missing_value_for_review(self):
        listing = object.__new__(TaobaoListing)
        select = MagicMock()
        select.locator.return_value.count = AsyncMock(return_value=0)
        with patch.object(TaobaoListing, '_select_values', new_callable=AsyncMock,
                          side_effect=TaobaoListingError('readback failed')):
            candidates, value = await listing._excel_before_review(
                select, (), ('棉', '棉布'), label='材质')
        self.assertEqual(candidates, ())
        self.assertEqual(value, '棉/棉布')
