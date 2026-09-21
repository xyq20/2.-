import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from field_policies import without_color_attributes
from taobao_listing import TaobaoListing
from douyin_listing import DouyinListing
from jd_form_listing import JdFormListing
from pdd_form_listing import PddFormListing
from xhs_form_listing import XhsFormListing
from youzan_form_listing import YouzanFormListing
from wxsph_form_listing import WxsphFormListing
from tmall_form_listing import TmallFormListing


class ColorExclusionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_assignment_builders_ignore_conflicting_color_sources(self):
        fields = {'颜色': '黑色', '商品颜色/颜色': '白色', '裤长': '短裤'}
        items = {'颜色': ('颜色', None), '裤长': ('裤长', None)}
        for cls in (TaobaoListing, JdFormListing, PddFormListing,
                    XhsFormListing, YouzanFormListing, WxsphFormListing):
            with self.subTest(platform=cls.__name__):
                adapter = object.__new__(cls)
                method = adapter._build_assignments if cls is TaobaoListing else adapter._attribute_assignments
                result = await method(fields, items)
                self.assertNotIn('颜色', result)
                self.assertEqual(result['裤长'][1], '短裤')

    async def test_douyin_does_not_write_color(self):
        adapter = object.__new__(DouyinListing)
        adapter.logger = None
        adapter.apply_first_recommended_category = AsyncMock(return_value='裤装')
        adapter._attribute_items = AsyncMock(return_value={'颜色': ('颜色', None)})
        adapter.fill_short_title = AsyncMock(return_value='标题')
        adapter.fill_attribute = AsyncMock()
        await adapter.apply_category_and_fields(SimpleNamespace(attributes={'颜色': '黑色'}, short_title='标题'))
        adapter.fill_attribute.assert_not_awaited()

    async def test_tmall_required_color_does_not_request_review(self):
        adapter = object.__new__(TmallFormListing)
        adapter._attribute_items = AsyncMock(return_value={'颜色': ('颜色', None)})
        adapter.logger = None
        adapter._item_is_required = AsyncMock(side_effect=AssertionError('excluded field inspected'))
        await adapter.fill_attributes(SimpleNamespace(fields={'颜色': '黑色'}))
        adapter._item_is_required.assert_not_awaited()

    def test_label_filter_preserves_non_color_fields(self):
        self.assertEqual(without_color_attributes({'a': ('* 颜色：', None), 'b': ('颜色分类', None), 'c': ('裤长', None)}), {'c': ('裤长', None)})
