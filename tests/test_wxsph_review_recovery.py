import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from attribute_runtime import ResolvedAttribute
from taobao_listing import TaobaoListingError
from wxsph_form_listing import WxsphFormListing


class ReviewRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def listing(self, decision):
        listing = object.__new__(WxsphFormListing)
        listing.logger = None
        listing.attribute_runtime = SimpleNamespace(resolve=AsyncMock(return_value=decision))
        listing._set_select_values_directly = AsyncMock(side_effect=TaobaoListingError("无法手填"))
        listing._open_select = AsyncMock()
        listing._dismiss_select_dropdown = AsyncMock()
        listing._visible_dom_options = AsyncMock(return_value=(None, [{"name": "明线"}, {"name": "口袋"}]))
        listing._captured_api_field = AsyncMock(return_value=(SimpleNamespace(source_id="details"), "jacket"))
        listing._select_values = AsyncMock(return_value=("明线",))
        item = MagicMock()
        item.locator.return_value.first.locator.return_value.count = AsyncMock(return_value=0)
        return listing, item

    async def test_failed_field_is_deferred_without_stopping(self):
        listing, item = self.listing(None)
        result = await listing._review_failed_attribute("流行元素", item, ("做旧",), "回读不一致")
        self.assertIsNone(result)
        request = listing.attribute_runtime.resolve.call_args.args[0]
        self.assertTrue(request.evidence["selection_only"])
        self.assertTrue(request.evidence["force_review"])
        listing._select_values.assert_not_awaited()

    async def test_approved_value_not_excel_is_written_and_verified(self):
        listing, item = self.listing(ResolvedAttribute("明线", "明线", "human_override", "snapshot"))
        result = await listing._review_failed_attribute("流行元素", item, ("做旧",), "回读不一致")
        self.assertEqual(result, ("明线",))
        self.assertEqual(listing._select_values.call_args.args[1], (("明线",),))
