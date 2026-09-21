import unittest
from unittest.mock import AsyncMock, patch

from attribute_runtime import ResolvedAttribute
from store_freight import sync_store_freight
from taobao_listing import TaobaoListing, TaobaoListingError
from wxsph_form_listing import WxsphFormListing
from xhs_form_listing import XhsFormListing


class StoreFreightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        self.scope = patch.dict('store_freight.FREIGHT_TEMPLATE_SHOPS',
                                {'wxsph': ('甲店', '乙店'), 'xhs': ('甲店', '乙店')})
        self.scope.start()
        self.addCleanup(self.scope.stop)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def listing(self, platform="wxsph", second_options=("裤子模板", "外套模板")):
        def row(shop, options):
            return '<div class="set-ship"><span class="shop-title">' + shop + '</span><div class="el-select"><input class="el-input__inner" readonly value="裤子模板" onclick="this.nextElementSibling.style.display=\'block\'"><div class="el-select-dropdown" style="display:none"><ul>' + ''.join('<li class="el-select-dropdown__item" onclick="choose(this)">' + option + '</li>' for option in options) + '</ul></div></div></div>'
        await self.page.set_content('<div class="el-form-item"><label class="el-form-item__label">运费模板：</label><div class="el-form-item__content">' + row("甲店", ("裤子模板", "外套模板")) + row("乙店", second_options) + '</div></div><div class="el-form-item"><label class="el-form-item__label">物流公司</label>' + row("甲店", ("物流公司",)) + '</div><script>window.clicks=0; function choose(el){clicks++; const s=el.closest(".el-select");s.querySelector("input").value=el.innerText;el.closest(".el-select-dropdown").style.display="none";}document.addEventListener("keydown",e=>{if(e.key==="Escape")document.querySelectorAll(".el-select-dropdown").forEach(d=>d.style.display="none")});</script>')
        adapter = WxsphFormListing if platform == "wxsph" else XhsFormListing
        if platform == "xhs":
            await self.page.evaluate("""() => {
                document.querySelectorAll('.set-ship').forEach(e => e.className='row-item');
                document.querySelector('.el-form-item__label').textContent='运费模版：';
                document.querySelectorAll('.el-form-item__label')[1].textContent='物流模版：';
            }""")
        listing = adapter(self.page, self.page.locator("body"), None)
        listing.panel = self.page.locator("body")
        return listing

    async def test_two_platforms_share_store_scoped_idempotent_selection(self):
        for platform in ("wxsph", "xhs"):
            listing = await self.listing(platform)
            fields = {"运费设置": "不存在/外套模板"}
            result = await sync_store_freight(listing, fields)
            self.assertEqual(set(result["stores"]), {"甲店", "乙店"})
            self.assertTrue(all(r["value"] == "外套模板" for r in result["stores"].values()))
            self.assertEqual(await self.page.evaluate("clicks"), 2)
            await sync_store_freight(listing, fields)
            await sync_store_freight(listing, fields, read_only=True)
            self.assertEqual(await self.page.evaluate("clicks"), 2)

    async def test_mismatch_reviews_only_affected_shop_and_continues(self):
        listing = await self.listing(second_options=("其他模板",))
        class Runtime:
            confirmed_choice = staticmethod(lambda request: None)
            resolve = AsyncMock(return_value=None)
        listing.attribute_runtime = Runtime()
        result = await sync_store_freight(listing, {"运费设置": "外套模板"})
        self.assertEqual(result["stores"]["甲店"]["value"], "外套模板")
        self.assertEqual(result["stores"]["乙店"]["status"], "review_required")
        request = listing.attribute_runtime.resolve.call_args.args[0]
        self.assertIn("乙店", request.field_label)
        self.assertFalse(request.custom_allowed)
        self.assertTrue(request.evidence["selection_only"])
        self.assertIn("无法手填", request.evidence["reason"])

    async def test_human_approval_has_priority_and_store_scope(self):
        listing = await self.listing()
        class Runtime:
            @staticmethod
            def confirmed_choice(request):
                if request.evidence["shop_name"] != "乙店":
                    return None
                candidate = next(v for v in request.candidates if v.label == "裤子模板")
                return ResolvedAttribute(candidate.value_id, candidate.label, "human_override", "v1")
        listing.attribute_runtime = Runtime()
        result = await sync_store_freight(listing, {"运费设置": "外套模板"})
        self.assertEqual(result["stores"]["甲店"]["value"], "外套模板")
        self.assertEqual(result["stores"]["乙店"]["value"], "裤子模板")
        await sync_store_freight(listing, {"运费设置": "外套模板"}, read_only=True)
        # The reopened adapter has no runtime; presave evidence must still
        # take precedence over Excel rather than rejecting human approval.
        listing.attribute_runtime = None
        await sync_store_freight(listing, {"运费设置": "外套模板"}, read_only=True, expected=result)

    async def test_readback_never_repairs_wrong_value(self):
        listing = await self.listing()
        with self.assertRaisesRegex(TaobaoListingError, "保存回读不同"):
            await sync_store_freight(listing, {"运费设置": "外套模板"}, read_only=True)
        self.assertEqual(await self.page.evaluate("clicks"), 0)

    async def test_duplicate_template_is_not_arbitrarily_clicked(self):
        listing = await self.listing(second_options=("外套模板", "外套模板"))
        with self.assertRaisesRegex(TaobaoListingError, "唯一"):
            await sync_store_freight(listing, {"运费设置": "外套模板"})
        self.assertEqual(await self.page.evaluate("clicks"), 1)

    async def test_missing_source_does_not_touch_page(self):
        listing = await self.listing()
        self.assertEqual((await sync_store_freight(listing, {}))["status"], "no_excel_source")
        self.assertEqual(await self.page.evaluate("clicks"), 0)

    async def test_only_configured_shop_is_touched_or_reviewed(self):
        listing = await self.listing(second_options=('其他模板',))
        class Runtime:
            confirmed_choice = staticmethod(lambda request: None)
            resolve = AsyncMock(return_value=None)
        listing.attribute_runtime = Runtime()
        with patch.dict('store_freight.FREIGHT_TEMPLATE_SHOPS', {'wxsph': ('甲店',)}):
            result = await sync_store_freight(listing, {'运费设置': '外套模板'})
            self.assertEqual(set(result['stores']), {'甲店'})
            self.assertEqual(result['status'], 'verified')
            listing.attribute_runtime.resolve.assert_not_awaited()
            await sync_store_freight(listing, {'运费设置': '外套模板'}, read_only=True, expected=result)
        self.assertEqual(await self.page.evaluate('clicks'), 1)
        self.assertEqual(await self.page.locator('.el-form-item').first.locator('input').nth(1).input_value(), '裤子模板')

    async def test_missing_target_never_falls_back_to_visible_shop(self):
        listing = await self.listing()
        with patch.dict('store_freight.FREIGHT_TEMPLATE_SHOPS', {'wxsph': ('不存在的店',)}):
            with self.assertRaisesRegex(TaobaoListingError, '未找到指定运费店铺'):
                await sync_store_freight(listing, {'运费设置': '外套模板'})
        self.assertEqual(await self.page.evaluate('clicks'), 0)

    async def enable_native_entry(self, *, reject=False, accepted=None):
        await self.page.locator('.el-form-item').first.locator('.el-select').evaluate_all('''(selects, settings) => {
            for (const select of selects) {
                const input = select.querySelector('input');
                input.removeAttribute('readonly');
                select.__vue__ = {value:'old-id', selected:{label:'裤子模板'}};
                input.onkeydown = event => {
                    if (event.key !== 'Enter' || settings.reject) return;
                    if (settings.accepted && input.value !== settings.accepted) return;
                    select.__vue__.value = input.value;
                    select.__vue__.selected = {label:input.value};
                };
            }
        }''', {"reject": reject, "accepted": accepted})

    async def test_missing_candidates_native_entry_and_saved_readback(self):
        listing = await self.listing(second_options=())
        await self.enable_native_entry()
        fields = {"运费设置": "新疆，西藏不包邮-鞋子，皮衣，外套/新疆西藏不包邮鞋子皮衣外套"}
        result = await sync_store_freight(listing, fields)
        self.assertTrue(all(v['value'] == fields['运费设置'].split('/')[0] for v in result['stores'].values()))
        await sync_store_freight(listing, fields, read_only=True, expected=result)

    async def test_second_or_custom_name_is_tried_without_removing_commas(self):
        listing = await self.listing()
        await self.enable_native_entry(accepted='新疆西藏不包邮鞋子皮衣外套')
        result = await sync_store_freight(listing, {"运费设置": "新疆，西藏不包邮-鞋子，皮衣，外套/新疆西藏不包邮鞋子皮衣外套"})
        self.assertTrue(all(v['value'] == '新疆西藏不包邮鞋子皮衣外套' for v in result['stores'].values()))

    async def test_search_text_without_committed_value_is_reviewed_after_attempt(self):
        listing = await self.listing()
        await self.enable_native_entry(reject=True)
        class Runtime:
            confirmed_choice = staticmethod(lambda request: None)
            resolve = AsyncMock(return_value=None)
        listing.attribute_runtime = Runtime()
        result = await sync_store_freight(listing, {"运费设置": "鞋子，外套/鞋子外套"})
        self.assertEqual(result['status'], 'review_required')
        self.assertEqual(listing.attribute_runtime.resolve.call_count, 2)
        for call in listing.attribute_runtime.resolve.call_args_list:
            evidence = call.args[0].evidence
            self.assertTrue(evidence['custom_input_attempted'])
            self.assertEqual(len(evidence['custom_input_attempts']), 2)
            self.assertTrue(evidence['selection_only'])
