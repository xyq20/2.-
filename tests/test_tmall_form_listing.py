import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tmall_form_listing import (
    TmallFormListing,
    TmallFormListingError,
    TmallProductWriteRequired,
)


class TmallFormListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            channel="chrome", headless=True
        )
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def _listing(self, body, api_index=None):
        await self.page.set_content(
            f"""
            <button role="tab" aria-selected="false" id="tmall-tab"
              onclick="this.setAttribute('aria-selected','true');
                document.querySelector('[role=tabpanel]').style.display='block'">
              天猫资料
            </button>
            <div role="tabpanel" aria-label="天猫资料" style="display:none">
              {body}
            </div>
            """
        )
        listing = TmallFormListing(
            self.page,
            self.page.locator("body"),
            None,
            api_index=api_index,
        )
        await listing.open()
        return listing

    def _method(self, listing, name):
        method = getattr(listing, name, None)
        self.assertTrue(callable(method), f"TmallFormListing 缺少 {name}")
        return method

    async def test_api_field_id_locates_dom_control_before_label_scan(self):
        api_index = SimpleNamespace(
            settle=AsyncMock(),
            source_ids=Mock(return_value=("13021751",)),
            resolve_option=Mock(return_value=None),
        )
        listing = await self._listing(
            """
            <div class="el-form-item" id="api-located-code">
              <label class="el-form-item__label">页面文案可能变化</label>
              <div class="el-form-item__content">
                <input name="prop_13021751">
              </div>
            </div>
            """,
            api_index=api_index,
        )

        item = await listing._tmall_form_item("货号", scope=listing.panel)

        self.assertEqual(await item.get_attribute("id"), "api-located-code")
        api_index.source_ids.assert_called_with("货号")

    async def test_api_option_locates_value_then_dom_performs_click(self):
        api_index = SimpleNamespace(
            settle=AsyncMock(),
            source_ids=Mock(return_value=()),
            resolve_option=Mock(return_value="时尚都市"),
        )
        listing = await self._listing(
            """
            <div class="el-form-item" id="api-style">
              <label class="el-form-item__label">风格</label>
              <div class="el-form-item__content"><div class="el-select">
                <input class="el-input__inner" readonly onclick="openApiSelect(this)">
                <div class="el-select-dropdown" style="display:none"><ul>
                  <li class="el-select-dropdown__item"
                    onclick="chooseApiOption(this)">休闲风</li>
                  <li class="el-select-dropdown__item"
                    onclick="chooseApiOption(this)">时尚都市</li>
                </ul></div>
              </div></div>
            </div>
            <script>
              function openApiSelect(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }
              function chooseApiOption(option) {
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display='none';
              }
            </script>
            """,
            api_index=api_index,
        )

        actual = await listing._fill_exact_form_item(
            "风格",
            self.page.locator("#api-style"),
            "休闲风/时尚都市",
        )

        self.assertEqual(actual, "时尚都市")
        self.assertEqual(
            await self.page.locator("#api-style input").input_value(),
            "时尚都市",
        )
        api_index.resolve_option.assert_called_once()
        self.assertEqual(api_index.resolve_option.call_args.args[0], "风格")
        self.assertIn("时尚都市", api_index.resolve_option.call_args.args[1])

    async def test_open_refreshes_stale_shop_authorization_once(self):
        await self.page.set_content(
            """
            <button role="tab" aria-selected="false"
              onclick="this.setAttribute('aria-selected','true');
                document.querySelector('[role=tabpanel]').style.display='block'">
              天猫资料
            </button>
            <div role="tabpanel" aria-label="天猫资料" style="display:none">
              <span id="shop-state">绑定天猫店铺</span>
            </div>
            <button id="refresh-shop" onclick="
              window.refreshClicks = (window.refreshClicks || 0) + 1;
              document.querySelector('#shop-state').textContent='已授权店铺';">
              刷新店铺授权状态
            </button>
            """
        )
        listing = TmallFormListing(self.page, self.page.locator("body"), None)

        await listing.open()

        self.assertEqual(await self.page.evaluate("window.refreshClicks"), 1)
        self.assertNotIn("绑定天猫店铺", await listing.panel.inner_text())

    async def test_applies_unique_recommended_category_without_product_publish(self):
        listing = await self._listing(
            """
            <div class="platform-category-input">
              <span class="current"></span>
              <div class="recommendation">
                <span>推荐</span><span>男装&gt;休闲裤</span>
                <button onclick="window.productPublishClicks = 0;
                  document.querySelector('.current').textContent='男装 > 休闲裤';
                  this.closest('.recommendation').remove()">点击使用</button>
              </div>
              <button>修改类目</button>
            </div>
            <button id="product-publish"
              onclick="window.productPublishClicks++">发布</button>
            """
        )

        category = await listing.apply_recommended_category()

        self.assertEqual(category, "男装 > 休闲裤")
        self.assertEqual(
            await self.page.evaluate("window.productPublishClicks || 0"), 0
        )

    async def test_waits_for_delayed_recommendation_when_stale_category_text_matches(self):
        listing = await self._listing(
            """
            <div class="platform-category-input">
              <span class="current">男装 &gt; 休闲裤</span>
              <div id="recommendation-mount"></div>
              <button>修改类目</button>
            </div>
            <script>
              setTimeout(() => {
                const row = document.createElement('div');
                row.className = 'recommendation';
                row.innerHTML = '<span>推荐</span><span>男装&gt;休闲裤</span><button>点击使用</button>';
                row.querySelector('button').onclick = () => {
                  window.recommendationClicks = (window.recommendationClicks || 0) + 1;
                  row.remove();
                };
                document.querySelector('#recommendation-mount').append(row);
              }, 75);
            </script>
            """
        )

        category = await listing.apply_recommended_category()

        self.assertEqual(category, "男装 > 休闲裤")
        self.assertEqual(
            await self.page.evaluate("window.recommendationClicks || 0"),
            1,
        )

    async def test_existing_category_with_expand_enters_product_info_path(self):
        """已有类目且产品信息折叠时，不应继续等待推荐类目。"""
        listing = await self._listing(
            """
            <div class="platform-category-input">
              <span class="current">男装 &gt; 休闲裤</span>
              <button>修改类目</button>
            </div>
            <section class="tm-product-info"><button>展开</button></section>
            """
        )

        category = await asyncio.wait_for(
            listing.apply_recommended_category(), timeout=0.5
        )

        self.assertEqual(category, "男装 > 休闲裤")

    async def test_waits_for_delayed_product_identity_fields_after_category_activation(self):
        listing = await self._listing(
            """
            <section class="tm-product-info" id="product-info"></section>
            <script>
              setTimeout(() => {
                document.querySelector('#product-info').innerHTML = `
                  <div class="el-form-item" id="late-code">
                    <label class="el-form-item__label">货号</label>
                    <div class="el-form-item__content"><input></div>
                  </div>
                  <div class="el-form-item" id="late-brand">
                    <label class="el-form-item__label">品牌</label>
                    <div class="el-form-item__content"><input></div>
                  </div>`;
              }, 75);
            </script>
            """
        )

        items = await listing._wait_for_product_identity_items()

        self.assertEqual(await items["货号"].get_attribute("id"), "late-code")
        self.assertEqual(await items["品牌"].get_attribute("id"), "late-brand")

    async def test_product_image_slots_can_be_outside_identity_form(self):
        listing = await self._listing('''
          <form class="product-form"><input value="NGBL-10588"></form>
          <div class="complex-items product_images">
            <div class="el-form-item pic"><label class="el-form-item__label">产品主图</label></div>
            <div class="el-form-item pic"><label class="el-form-item__label">产品图片2</label></div>
            <div class="el-form-item pic"><label class="el-form-item__label">产品图片3</label></div>
            <div class="el-form-item pic"><label class="el-form-item__label">产品图片4</label></div>
            <div class="el-form-item pic"><label class="el-form-item__label">产品图片5</label></div>
          </div>
        ''')
        slots = await listing._initial_product_image_slots()
        self.assertEqual(len(slots), 5)

    async def test_waits_for_initial_product_images_then_replaces_in_tmall_order(self):
        listing = await self._listing(
            """
            <section class="tm-product-info" id="product-info"></section>
            <script>
              setTimeout(() => {
                const labels = ['产品主图', '产品图片2', '产品图片3', '产品图片4', '产品图片5'];
                document.querySelector('#product-info').innerHTML = labels.map((label, index) => `
                  <div class="el-form-item pic" id="initial-${index + 1}">
                    <label class="el-form-item__label">${label}</label>
                    <div class="el-form-item__content"><div class="sc-upload">
                      <div class="file-img" style="width:80px;height:80px"><img class="originImg" style="width:80px;height:80px" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div>
                      <input type="file">
                    </div></div>
                  </div>`).join('');
              }, 75);
            </script>
            """
        )
        calls = []

        async def uploader(_page, item, paths, label, _timeout, *, force_replace=False):
            calls.append(
                (
                    await item.get_attribute("id"),
                    tuple(path.name for path in paths),
                    label,
                    force_replace,
                )
            )
            return "replaced"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = tuple(root / f"main-{index}.jpg" for index in range(1, 6))
            ready = await listing.wait_for_initial_product_images(
                expected_count=5,
                timeout_seconds=3,
            )
            report = await listing.sync_initial_product_images(
                paths, timeout_seconds=2, uploader=uploader
            )

        self.assertEqual(ready["count"], 5)
        self.assertEqual(
            calls,
            [
                ("initial-1", ("main-3.jpg",), "天猫产品主图", True),
                ("initial-2", ("main-2.jpg",), "天猫产品图片2", True),
                ("initial-3", ("main-1.jpg",), "天猫产品图片3", True),
                ("initial-4", ("main-4.jpg",), "天猫产品图片4", True),
                ("initial-5", ("main-5.jpg",), "天猫产品图片5", True),
            ],
        )
        self.assertEqual(report["count"], 5)

    async def test_initial_product_images_must_keep_same_sources_before_publish(self):
        listing = await self._listing(
            """
            <section class="tm-product-info" id="product-info">
              <div class="el-form-item pic"><label class="el-form-item__label">产品主图</label><div class="sc-upload"><div class="file-img"><img class="originImg" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片2</label><div class="sc-upload"><div class="file-img"><img class="originImg" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片3</label><div class="sc-upload"><div class="file-img"><img class="originImg" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片4</label><div class="sc-upload"><div class="file-img"><img class="originImg" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片5</label><div class="sc-upload"><div class="file-img"><img class="originImg" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div></div></div>
            </section>
            <script>
              setTimeout(() => {
                document.querySelectorAll('#product-info img')[4].src =
                  'data:image/gif;base64,R0lGODlhAQABAIAAAAD/AP///ywAAAAAAQABAAACAUwAOw==';
              }, 550);
            </script>
            """
        )

        ready = await listing.wait_for_initial_product_images(
            expected_count=5,
            timeout_seconds=3,
            initial_delay_seconds=0,
            stable_seconds=0.8,
        )

        self.assertEqual(ready["count"], 5)
        self.assertGreaterEqual(ready["waited_seconds"], 1.3)

    async def test_initial_product_images_use_actual_base_image_count(self):
        listing = await self._listing(
            """
            <section class="tm-product-info">
              <div class="el-form-item pic"><label class="el-form-item__label">产品主图</label><div class="sc-upload"><div class="file-img"><img class="originImg" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片2</label><div class="sc-upload"><div class="file-img"><img class="originImg" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片3</label><div class="sc-upload"><div class="file-img"><img class="originImg" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片4</label><div class="sc-upload"></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片5</label><div class="sc-upload"></div></div>
            </section>
            """
        )

        ready = await listing.wait_for_initial_product_images(
            expected_count=3,
            timeout_seconds=1,
            initial_delay_seconds=0,
            stable_seconds=0.05,
        )

        self.assertEqual(ready["count"], 3)
        self.assertEqual(ready["expected_count"], 3)

    async def test_inspects_incomplete_existing_top_product_images(self):
        listing = await self._listing(
            """
            <section class="tm-product-info product_images">
              <div class="el-form-item pic"><label class="el-form-item__label">产品主图</label><div class="sc-upload"><div class="file-img"><img class="originImg" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></div></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片2</label><div class="sc-upload"></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片3</label><div class="sc-upload"></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片4</label><div class="sc-upload"></div></div>
              <div class="el-form-item pic"><label class="el-form-item__label">产品图片5</label><div class="sc-upload"></div></div>
            </section>
            """
        )

        state = await listing.inspect_initial_product_images(expected_count=5)

        self.assertEqual(state["count"], 1)
        self.assertEqual(state["slot_count"], 5)
        self.assertFalse(state["decoded"])

    async def test_clicks_product_information_publish_and_waits_for_form(self):
        listing = await self._listing(
            """
            <section class="tm-product-info">
              <button id="product-publish"
                onclick="window.productPublishClicks=(window.productPublishClicks||0)+1;this.remove()">
                发布
              </button>
            </section>
            """
        )
        listing.require_full_form = AsyncMock()
        listing.wait_for_initial_product_images = AsyncMock()

        report = await listing.publish_product_information(timeout_seconds=1)

        self.assertEqual(await self.page.evaluate("window.productPublishClicks || 0"), 1)
        listing.require_full_form.assert_awaited()
        self.assertEqual(report["clicked"], True)

    async def test_wait_detects_api_confirmed_product_match_not_image_success(self):
        listing = await self._listing('<section class="tm-product-info">产品信息</section>')
        listing.api_index = SimpleNamespace(matched_existing_product=True)
        listing.require_full_form = AsyncMock()
        result = await listing.wait_for_initial_product_images(
            expected_count=5, timeout_seconds=1,
        )
        self.assertTrue(result['matched_existing_product'])
        self.assertFalse(result['decoded'])

    async def test_publish_refuses_product_match_during_final_image_check(self):
        listing = await self._listing('<button onclick="window.didPublish=true">发布</button>')
        listing.wait_for_initial_product_images = AsyncMock(return_value={
            'matched_existing_product': True, 'decoded': False,
        })
        with self.assertRaisesRegex(TmallFormListingError, '禁止'):
            await listing.publish_product_information(timeout_seconds=1)
        self.assertFalse(await self.page.evaluate('Boolean(window.didPublish)'))

    async def test_image_sync_failure_prevents_publish_click(self):
        listing = await self._listing('''
            <button onclick="window.published=true">发布</button>
        ''')
        listing.wait_for_initial_product_images = AsyncMock(
            side_effect=TmallFormListingError("图片尚未同步完整：1/5")
        )
        with self.assertRaisesRegex(TmallFormListingError, "1/5"):
            await listing.publish_product_information(timeout_seconds=1)
        self.assertFalse(await self.page.evaluate("Boolean(window.published)"))

    async def test_required_attributes_are_preflighted_before_any_field_is_written(self):
        listing = await self._listing(
            """
            <div class="conf"><div class="complex-wrap">
              <div class="complex-item"><div class="el-form-item is-required">
                <label class="el-form-item__label">* 风格</label>
                <div class="el-form-item__content"><input id="style"></div>
              </div></div>
              <div class="complex-item"><div class="el-form-item is-required">
                <label class="el-form-item__label">* 面料</label>
                <div class="el-form-item__content"><input id="fabric"></div>
              </div></div>
            </div></div>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "面料"):
            await listing.fill_attributes(
                SimpleNamespace(fields={"风格": "休闲"})
            )

        self.assertEqual(await self.page.locator("#style").input_value(), "")
        self.assertEqual(await self.page.locator("#fabric").input_value(), "")

    async def test_required_attribute_needs_excel_source_even_when_page_has_old_value(self):
        listing = await self._listing(
            """
            <div class="conf"><div class="complex-wrap">
              <div class="complex-item"><div class="el-form-item is-required">
                <label class="el-form-item__label">* 风格</label>
                <div class="el-form-item__content"><input id="style" value="旧风格"></div>
              </div></div>
            </div></div>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "风格"):
            await listing.fill_attributes({})

        self.assertEqual(await self.page.locator("#style").input_value(), "旧风格")

    async def test_fills_required_attributes_and_optional_hang_tag_price_only(self):
        listing = await self._listing(
            """
            <div class="conf"><div class="complex-wrap">
              <div class="complex-item"><div class="el-form-item is-required">
                <label class="el-form-item__label">* 风格</label>
                <div class="el-form-item__content"><input id="style"></div>
              </div></div>
              <div class="complex-item"><div class="el-form-item">
                <label class="el-form-item__label">吊牌价</label>
                <div class="el-form-item__content"><input id="hang-price"></div>
              </div></div>
              <div class="complex-item"><div class="el-form-item">
                <label class="el-form-item__label">图案</label>
                <div class="el-form-item__content"><input id="pattern"></div>
              </div></div>
            </div></div>
            """
        )

        report = await listing.fill_attributes(
            SimpleNamespace(
                fields={"风格": "休闲", "吊牌价/价格/基本售价": "586", "图案": "纯色"}
            )
        )

        self.assertEqual(await self.page.locator("#style").input_value(), "休闲")
        self.assertEqual(await self.page.locator("#hang-price").input_value(), "586元")
        self.assertEqual(await self.page.locator("#pattern").input_value(), "")
        self.assertEqual(set(report["attributes"]), {"风格", "吊牌价"})

    async def test_fills_optional_waist_shape_from_excel_alias(self):
        listing = await self._listing(
            """
            <div class="conf"><div class="complex-wrap">
              <div class="complex-item"><div class="el-form-item" id="waist-shape">
                <label class="el-form-item__label">腰型</label>
                <div class="el-form-item__content"><div class="el-select">
                  <input class="el-input__inner" readonly onclick="openTmallSelect(this)">
                  <div class="el-select-dropdown" style="display:none"><ul>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">中腰</li>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">高腰</li>
                  </ul></div>
                </div></div>
              </div></div>
              <div class="complex-item"><div class="el-form-item" id="pattern">
                <label class="el-form-item__label">图案</label>
                <div class="el-form-item__content"><input></div>
              </div></div>
            </div></div>
            <script>
              function openTmallSelect(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }
              function chooseTmallOption(option) {
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display='none';
              }
            </script>
            """
        )

        report = await listing.fill_attributes({"腰形": "中腰"})

        self.assertEqual(
            await self.page.locator("#waist-shape input").input_value(), "中腰"
        )
        self.assertEqual(report["attributes"]["腰型"], ("中腰",))
        self.assertEqual(await self.page.locator("#pattern input").input_value(), "")

    async def test_fills_attributes_from_real_tmall_props_items_layout(self):
        listing = await self._listing(
            """
            <form class="step3-form">
              <div class="wrap useCategory-wrap"><div class="props-items">
                <span><div class="el-form-item is-required" id="real-style">
                  <label class="el-form-item__label">风格</label>
                  <div class="el-form-item__content"><input></div>
                </div></span>
                <span><div class="el-form-item" id="real-price">
                  <label class="el-form-item__label">吊牌价</label>
                  <div class="el-form-item__content"><input></div>
                </div></span>
                <span><div class="el-form-item" id="real-pattern">
                  <label class="el-form-item__label">图案</label>
                  <div class="el-form-item__content"><input></div>
                </div></span>
              </div></div>
            </form>
            """
        )

        report = await listing.fill_attributes(
            {"风格": "休闲", "吊牌价": "586", "图案": "纯色"}
        )

        self.assertEqual(await self.page.locator("#real-style input").input_value(), "休闲")
        self.assertEqual(await self.page.locator("#real-price input").input_value(), "586元")
        self.assertEqual(await self.page.locator("#real-pattern input").input_value(), "")
        self.assertEqual(set(report["attributes"]), {"风格", "吊牌价"})

    async def test_required_fabric_uses_material_name_from_percentage_value(self):
        listing = await self._listing(
            """
            <div class="conf"><div class="complex-wrap"><div class="complex-item">
              <div class="el-form-item is-required" id="fabric">
                <label class="el-form-item__label">面料</label>
                <div class="el-form-item__content"><div class="el-select">
                  <input class="el-input__inner" readonly onclick="openTmallSelect(this)">
                  <div class="el-select-dropdown" style="display:none"><ul>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">棉</li>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">亚麻</li>
                  </ul></div>
                </div></div>
              </div>
            </div></div></div>
            <script>
              function openTmallSelect(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }
              function chooseTmallOption(option) {
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display='none';
              }
            </script>
            """
        )

        report = await listing.fill_attributes(
            {
                "面料材质/面料": "棉（100%）",
                "材质成分/材质": "棉（100%）",
            }
        )

        self.assertEqual(report["attributes"]["面料"], ("棉",))
        self.assertEqual(await self.page.locator("#fabric input").input_value(), "棉")

    async def test_attributes_defer_material_component_to_after_sales(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">售后及其他</div>
              <div class="conf"><div class="complex-wrap"><div class="complex-item_multi">
                <div class="el-form-item is-required">
                  <label class="el-form-item__label">*材质成分</label>
                  <div class="el-form-item__content"><div class="multi-complex-items">
                    <button onclick="window.materialAddClicks++">添加</button>
                  </div></div>
                </div>
              </div></div></div>
            </div>
            <script>window.materialAddClicks = 0;</script>
            """
        )
        listing.fill_attribute = AsyncMock()

        report = await listing.fill_attributes({"材质成分/材质": "棉/100%"})

        listing.fill_attribute.assert_not_awaited()
        self.assertEqual(report["deferred"], ("材质成分",))
        self.assertEqual(await self.page.evaluate("window.materialAddClicks"), 0)

    async def test_hidden_full_form_requires_separate_product_write_and_never_clicks_it(self):
        listing = await self._listing(
            """
            <section class="tm-product-info">
              <label>*货号</label><input value="NGBL-10588">
              <button id="product-publish"
                onclick="window.productPublishClicks=(window.productPublishClicks||0)+1">
                发布
              </button>
            </section>
            """
        )

        with self.assertRaises(TmallProductWriteRequired):
            await listing.require_full_form()

        self.assertEqual(
            await self.page.evaluate("window.productPublishClicks || 0"), 0
        )

    async def test_product_info_wrap_label_does_not_count_as_full_form(self):
        listing = await self._listing(
            """
            <div class="wrap-item tm-product-info">
              <div class="wrap-item_label">天猫产品信息</div>
              <label>*货号</label><input value="NGBL-10588">
            </div>
            <button id="product-publish"
              onclick="window.productPublishClicks=(window.productPublishClicks||0)+1">
              发布
            </button>
            """
        )

        with self.assertRaises(TmallProductWriteRequired):
            await listing.require_full_form()

        self.assertEqual(
            await self.page.evaluate("window.productPublishClicks || 0"), 0
        )

    async def test_attribute_section_alone_does_not_count_as_full_form(self):
        listing = await self._listing(
            """
            <div class="conf"><div class="complex-wrap"><div class="complex-item">
              <div class="el-form-item is-required">
                <label class="el-form-item__label">* 风格</label>
                <div class="el-form-item__content"><input></div>
              </div>
            </div></div></div>
            <button id="product-publish"
              onclick="window.productPublishClicks=(window.productPublishClicks||0)+1">
              发布
            </button>
            """
        )

        with self.assertRaises(TmallProductWriteRequired):
            await listing.require_full_form()

        self.assertEqual(
            await self.page.evaluate("window.productPublishClicks || 0"), 0
        )

    async def test_one_empty_later_section_does_not_count_as_full_form(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">商品明细</div></div>
            <button id="product-publish"
              onclick="window.productPublishClicks=(window.productPublishClicks||0)+1">
              发布
            </button>
            """
        )

        with self.assertRaises(TmallProductWriteRequired):
            await listing.require_full_form()

        self.assertEqual(
            await self.page.evaluate("window.productPublishClicks || 0"), 0
        )

    async def test_full_form_requires_every_section_used_by_the_workflow(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">商品明细</div>
              <div class="sku-batch-row"><input></div>
            </div>
            <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
              <table class="tmall-size-table"><tbody><tr><td>S</td></tr></tbody></table>
            </div>
            <div class="wrap-item"><div class="wrap-item_label">物流信息</div><input></div>
            <div class="wrap-item"><div class="wrap-item_label">商品描述</div><input></div>
            <div class="wrap-item"><div class="wrap-item_label">售后及其他</div><input></div>
            """
        )

        await listing.require_full_form()

    async def test_full_form_accepts_real_tmall_section_and_size_table_classes(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">商品明细</div>
              <div class="sku-batch-row"><input></div>
            </div>
            <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
              <div class="block-std-size-extends"><div class="el-table">
                <div class="el-table__header-wrapper"><table>
                  <thead><tr><th>“中国码”尺码</th><th>* 身高（cm） 区间</th></tr></thead>
                </table></div>
                <div class="el-table__body-wrapper"><table>
                  <tbody><tr><td>S</td><td><input></td></tr></tbody>
                </table></div>
              </div></div>
            </div>
            <form class="step3-form">
              <div class="title ft-12">物流信息</div><input>
              <div class="title ft-12">商品描述</div><input>
              <div class="title ft-12">售后及其他</div><input>
            </form>
            """
        )

        await listing.require_full_form()

    async def test_sku_batch_uses_today_and_preserves_platform_codes(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="details">
              <div class="wrap-item_label">商品明细</div>
              <div class="sku-batch-row">
                <div class="sku-batch-item"><span class="sku-batch-item_label">价格</span>
                  <input class="el-input__inner" id="batch-price"></div>
                <div class="sku-batch-item"><span class="sku-batch-item_label">库存</span>
                  <input class="el-input__inner" id="batch-stock"></div>
                <div class="sku-batch-item"><span class="sku-batch-item_label">上市时间</span>
                  <input class="el-input__inner" id="batch-date" placeholder="选择日期"></div>
                <div class="sku-batch-item"><span class="sku-batch-item_label">货号</span>
                  <input class="el-input__inner" id="batch-code"></div>
                <button onclick="applyBatch()">批量设置</button>
              </div>
              <div class="el-table__main-wrapper">
                <div class="el-table__header-wrapper"><table><thead><tr>
                  <th>颜色</th><th>尺码</th><th>价格</th><th>库存</th>
                  <th>上市时间</th><th>货号</th><th>平台规格编码</th>
                </tr></thead></table></div>
                <div class="el-table__body-wrapper"><table><tbody>
                  <tr><td>军绿色</td><td>S</td><td><input data-field="price"></td>
                    <td><input data-field="stock"></td><td><input data-field="date"></td>
                    <td><input data-field="code"></td><td>PLATFORM-S</td></tr>
                  <tr><td>军绿色</td><td>M</td><td><input data-field="price"></td>
                    <td><input data-field="stock"></td><td><input data-field="date"></td>
                    <td><input data-field="code"></td><td>PLATFORM-M</td></tr>
                </tbody></table></div>
              </div>
            </div>
            <script>
              function applyBatch() {
                const values = {
                  price: document.querySelector('#batch-price').value,
                  stock: document.querySelector('#batch-stock').value,
                  date: document.querySelector('#batch-date').value,
                  code: document.querySelector('#batch-code').value
                };
                for (const input of document.querySelectorAll('tbody input')) {
                  input.value = values[input.dataset.field];
                }
              }
            </script>
            """
        )

        with patch(
            "tmall_form_listing.shanghai_today", return_value=date(2026, 9, 2)
        ) as today_rule:
            report = await listing.fill_sku_batch(
                {
                    "价格": "586",
                    "数量": "100",
                    "现货库存": "0",
                    "货号": "NGBL-10588",
                }
            )

        today_rule.assert_called_once_with()
        self.assertTrue(report["batch_clicked"])
        self.assertEqual(report["values"]["上市时间"], "2026-09-02")
        self.assertEqual(report["values"]["库存"], "100")
        self.assertEqual(
            report["platform_codes"], ("PLATFORM-S", "PLATFORM-M")
        )

    async def test_shoe_size_cleanup_only_changes_numeric_size_dimension(self):
        listing = await self._listing(
            """
            <div class="block-specification">
              <div class="title-bg"><input value="颜色"></div>
              <div class="specification-value">
                <input class="spec-value" value="黑色码">
              </div>
              <div class="title-bg"><input value="&quot;欧码&quot;尺码"></div>
              <div class="specification-value">
                <input class="spec-value" value="38码">
                <input class="spec-value" value="38.5码">
                <input class="spec-value" value="均码">
              </div>
            </div>
            """
        )

        report = await listing.normalize_synced_specifications("流行男鞋 > 休闲鞋")

        values = await self.page.locator(".spec-value").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        )
        self.assertEqual(values, ["黑色码", "38", "38.5", "均码"])
        self.assertEqual(report["changed"], 2)

    async def test_shoe_accessory_parent_category_does_not_trigger_size_cleanup(self):
        listing = await self._listing(
            """
            <div class="block-specification">
              <div class="title-bg"><input value="袜子尺码"></div>
              <div class="specification-value">
                <input class="spec-value" value="38码">
              </div>
            </div>
            """
        )

        report = await listing.normalize_synced_specifications("鞋服配件 > 袜子")

        self.assertEqual(await self.page.locator(".spec-value").input_value(), "38码")
        self.assertEqual(report["changed"], 0)

    async def test_shoe_size_cleanup_preflights_every_changed_control(self):
        listing = await self._listing(
            """
            <div class="block-specification">
              <div class="title-bg"><input value="欧码尺码"></div>
              <div class="specification-value">
                <input class="spec-value" value="38码">
                <input class="spec-value" value="39码" disabled>
              </div>
            </div>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "不可写"):
            await listing.normalize_synced_specifications("流行男鞋 > 休闲鞋")

        self.assertEqual(
            await self.page.locator(".spec-value").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            ),
            ["38码", "39码"],
        )

    async def test_product_identity_preflights_and_fills_exact_excel_values(self):
        listing = await self._listing(
            """
            <section>
              <div class="el-form-item is-required" id="goods-code">
                <label class="el-form-item__label">*货号</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item is-required" id="brand">
                <label class="el-form-item__label">*品牌</label>
                <div class="el-form-item__content"><div class="el-select">
                  <input class="el-input__inner" readonly onclick="openTmallSelect(this)">
                  <div class="el-select-dropdown" style="display:none"><ul>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">NEIGBORL</li>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">NEIGBORL官方</li>
                  </ul></div>
                </div></div>
              </div>
              <div class="el-form-item is-required" id="season">
                <label class="el-form-item__label">*上市年份季节</label>
                <div class="el-form-item__content"><div class="el-select">
                  <input class="el-input__inner" readonly onclick="openTmallSelect(this)">
                  <div class="el-select-dropdown" style="display:none"><ul>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">2026年春季</li>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">2026年秋季</li>
                  </ul></div>
                </div></div>
              </div>
              <button onclick="window.forbiddenClicks++">发布</button>
              <button onclick="window.forbiddenClicks++">保存</button>
              <button onclick="window.forbiddenClicks++">保存并铺货到平台</button>
            </section>
            <div class="conf"><div class="complex-wrap"><div class="complex-item">
              <div class="el-form-item" id="attribute-season">
                <label class="el-form-item__label">上市年份季节</label>
                <div class="el-form-item__content"><input value="不应改动"></div>
              </div>
            </div></div></div>
            <script>
              window.forbiddenClicks = 0;
              function openTmallSelect(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }
              function chooseTmallOption(option) {
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display='none';
              }
              document.addEventListener('keydown', event => {
                if (event.key === 'Escape') {
                  document.querySelectorAll('.el-select-dropdown').forEach(node => node.style.display='none');
                }
              });
            </script>
            """
        )

        report = await self._method(listing, "fill_product_identity")(
            SimpleNamespace(
                fields={
                    "货号/商家外部编码": "NGBL-10588",
                    "品牌": "NEIGBORL",
                    "上市年份季节": "2026年秋季",
                }
            )
        )

        self.assertEqual(report["values"]["货号"], "NGBL-10588")
        self.assertEqual(report["values"]["品牌"], "NEIGBORL")
        self.assertEqual(report["values"]["上市年份季节"], "2026年秋季")
        self.assertEqual(await self.page.locator("#brand input").input_value(), "NEIGBORL")
        self.assertEqual(
            await self.page.locator("#attribute-season input").input_value(), "不应改动"
        )
        self.assertEqual(await self.page.evaluate("window.forbiddenClicks"), 0)

    async def test_product_identity_expands_collapsed_real_section(self):
        listing = await self._listing(
            """
            <div class="product-heading">
              <span>天猫产品信息</span>
              <button id="expand-product" onclick="
                window.expandClicks = (window.expandClicks || 0) + 1;
                document.querySelector('#collapsed-product').style.display='block';
                this.style.display='none';">展开</button>
            </div>
            <section id="collapsed-product" style="display:none">
              <div class="el-form-item is-required" id="collapsed-code">
                <label class="el-form-item__label">*货号</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item is-required" id="collapsed-brand">
                <label class="el-form-item__label">*品牌</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item is-required" id="collapsed-season">
                <label class="el-form-item__label">*上市年份季节</label>
                <div class="el-form-item__content"><input></div>
              </div>
            </section>
            """
        )

        report = await listing.fill_product_identity(
            {
                "货号": "NGBL-10588",
                "品牌": "NEIGBORL",
                "上市年份季节": "2026年秋季",
            }
        )

        self.assertEqual(await self.page.evaluate("window.expandClicks"), 1)
        self.assertEqual(report["values"]["货号"], "NGBL-10588")
        self.assertEqual(
            await self.page.locator("#collapsed-season input").input_value(),
            "2026年秋季",
        )

    async def test_product_identity_uses_step2_season_not_step3_attribute(self):
        listing = await self._listing(
            """
            <form class="product-form">
              <div class="el-form-item is-required" id="split-code">
                <label class="el-form-item__label">*货号</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item is-required" id="split-brand">
                <label class="el-form-item__label">*品牌</label>
                <div class="el-form-item__content"><input></div>
              </div>
            </form>
            <form class="el-form step2-form">
              <div class="props-items">
                <div class="el-form-item is-required" id="identity-season">
                  <label class="el-form-item__label">*上市年份季节</label>
                  <div class="el-form-item__content"><input></div>
                </div>
              </div>
            </form>
            <form class="el-form step3-form">
              <div class="wrap useCategory-wrap"><div class="props-items">
                <div class="el-form-item is-required" id="attribute-season">
                  <label class="el-form-item__label">*上市年份季节</label>
                  <div class="el-form-item__content"><input value="不应改动"></div>
                </div>
              </div></div>
            </form>
            """
        )

        await listing.fill_product_identity(
            {
                "货号": "NGBL-10588",
                "品牌": "NEIGBORL",
                "上市年份季节": "2026年秋季",
            }
        )

        self.assertEqual(
            await self.page.locator("#identity-season input").input_value(),
            "2026年秋季",
        )
        self.assertEqual(
            await self.page.locator("#attribute-season input").input_value(),
            "不应改动",
        )

    async def test_product_identity_missing_required_source_writes_nothing(self):
        listing = await self._listing(
            """
            <div class="el-form-item is-required" id="code">
              <label class="el-form-item__label">*货号</label>
              <div class="el-form-item__content"><input></div>
            </div>
            <div class="el-form-item is-required" id="brand">
              <label class="el-form-item__label">*品牌</label>
              <div class="el-form-item__content"><input></div>
            </div>
            <div class="el-form-item is-required" id="season">
              <label class="el-form-item__label">*上市年份季节</label>
              <div class="el-form-item__content"><input></div>
            </div>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "上市年份季节"):
            await self._method(listing, "fill_product_identity")(
                {"货号": "NGBL-10588", "品牌": "NEIGBORL"}
            )

        self.assertEqual(
            await self.page.locator("#code input, #brand input, #season input").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            ),
            ["", "", ""],
        )

    async def test_product_identity_select_uses_one_ordered_or_group_from_excel(self):
        listing = await self._listing(
            """
            <section class="tm-product-info">
              <div class="el-form-item is-required" id="code">
                <label class="el-form-item__label">*货号</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item is-required" id="brand">
                <label class="el-form-item__label">*品牌</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item is-required" id="season">
                <label class="el-form-item__label">*上市年份季节</label>
                <div class="el-form-item__content"><div class="el-select">
                  <input class="el-input__inner" readonly onclick="openTmallSelect(this)">
                  <div class="el-select-dropdown" style="display:none"><ul>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">2026年春季</li>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">2026年秋季</li>
                  </ul></div>
                </div></div>
              </div>
            </section>
            <script>
              function openTmallSelect(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }
              function chooseTmallOption(option) {
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display='none';
              }
            </script>
            """
        )

        report = await listing.fill_product_identity(
            {
                "货号": "NGBL-10588",
                "品牌": "NEIGBORL",
                "上市时间/上市年份季节/上市时节":
                    "2026/2026年秋季/动态选择当天",
            }
        )

        self.assertEqual(report["values"]["上市年份季节"], "2026年秋季")
        self.assertEqual(await self.page.locator("#season input").input_value(), "2026年秋季")

    async def test_product_identity_waits_for_season_revealed_after_brand(self):
        listing = await self._listing(
            """
            <section class="tm-product-info">
              <div class="el-form-item is-required" id="code">
                <label class="el-form-item__label">货号</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item is-required" id="brand">
                <label class="el-form-item__label">品牌</label>
                <div class="el-form-item__content"><div class="el-select">
                  <input class="el-input__inner" readonly onclick="openTmallSelect(this)">
                  <div class="el-select-dropdown" style="display:none"><ul>
                    <li class="el-select-dropdown__item" onclick="chooseBrand(this)">NEIGBORL</li>
                  </ul></div>
                </div></div>
              </div>
              <div class="el-form-item is-required" id="season" style="display:none">
                <label class="el-form-item__label">上市年份季节</label>
                <div class="el-form-item__content"><div class="el-select">
                  <input class="el-input__inner" readonly onclick="openTmallSelect(this)">
                  <div class="el-select-dropdown" style="display:none"><ul>
                    <li class="el-select-dropdown__item" onclick="chooseTmallOption(this)">2026年秋季</li>
                  </ul></div>
                </div></div>
              </div>
            </section>
            <script>
              function openTmallSelect(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }
              function chooseTmallOption(option) {
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display='none';
              }
              function chooseBrand(option) {
                chooseTmallOption(option);
                document.querySelector('#season').style.display='block';
              }
            </script>
            """
        )

        report = await listing.fill_product_identity(
            {
                "货号": "NGBL-10588",
                "品牌": "NEIGBORL",
                "上市时间/上市年份季节/上市时节":
                    "2026/2026年秋季/动态选择当天",
            }
        )

        self.assertEqual(report["values"]["上市年份季节"], "2026年秋季")
        self.assertEqual(await self.page.locator("#code input").input_value(), "NGBL-10588")
        self.assertEqual(await self.page.locator("#brand input").input_value(), "NEIGBORL")
        self.assertTrue(await self.page.locator("#season").is_visible())

    async def test_sales_and_logistics_fills_only_visible_exact_excel_fields(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="sales-logistics">
              <div class="wrap-item_label">物流信息</div>
              <div class="el-form-item is-required" id="product-price">
                <label class="el-form-item__label">*商品价格</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item" id="quantity">
                <label class="el-form-item__label">商品数量</label>
                <div class="el-form-item__content"><input disabled value="0"></div>
              </div>
              <div class="el-form-item" id="external-code">
                <label class="el-form-item__label">商家外部编码</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item" id="weight">
                <label class="el-form-item__label">商品物流重量(千克)</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item is-required" id="province">
                <label class="el-form-item__label">*省份</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item is-required" id="city">
                <label class="el-form-item__label">*城市</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item" id="pickup">
                <label class="el-form-item__label">提取方式</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item"><label class="el-form-item__label">上市年份季节</label><input></div>
              <div class="el-form-item"><label class="el-form-item__label">上市年份季节</label><input></div>
              <button onclick="window.forbiddenClicks++">发布</button>
              <button onclick="window.forbiddenClicks++">保存并铺货到平台</button>
            </div>
            <script>window.forbiddenClicks = 0;</script>
            """
        )

        report = await self._method(listing, "fill_sales_and_logistics")(
            {
                "商品价格": "586",
                "商家外部编码": "NGBL-10588",
                "商品物流重量（千克）": "1.2",
                "省份": "广东省",
                "城市": "广州市",
                "提取方式说明": "不得模糊命中",
                "页面不存在的字段": "不得写入",
            }
        )

        self.assertEqual(
            set(report["values"]),
            {
                "商品价格",
                "商家外部编码",
                "商品物流重量(千克)",
                "省份",
                "城市",
                "提取方式",
            },
        )
        self.assertEqual(await self.page.locator("#quantity input").input_value(), "0")
        self.assertEqual(await self.page.locator("#pickup input").input_value(), "邮寄")
        self.assertIn("提取方式", report["defaults_applied"])
        self.assertEqual(await self.page.evaluate("window.forbiddenClicks"), 0)

    async def test_sales_logistics_selects_delivery_before_dynamic_freight(self):
        listing = await self._listing(
            """
            <div class="el-form-item is-required" id="price">
              <label class="el-form-item__label">*商品价格</label>
              <div class="el-form-item__content"><input></div>
            </div>
            <div class="el-form-item" id="pickup">
              <label class="el-form-item__label">提取方式</label>
              <div class="el-form-item__content"><div class="el-select is-multiple" id="pickup-select">
                <div class="el-select__tags"><span></span>
                  <input class="el-select__input" onclick="openDelivery(this)">
                </div>
                <input class="el-input__inner" readonly onclick="openDelivery(this)">
                <div class="el-select-dropdown" style="display:none"><ul>
                  <li class="el-select-dropdown__item" onclick="chooseDelivery(this)">邮寄</li>
                </ul></div>
              </div></div>
            </div>
            <script>
              window.logisticsEvents = [];
              function openDelivery(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }
              function chooseDelivery(option) {
                const select = option.closest('.el-select');
                select.querySelector('.el-select__tags').innerHTML = '<span class="el-tag">邮寄</span><input class="el-select__input">';
                select.querySelector('.el-select__input').onclick = function() { openDelivery(this); };
                select.querySelector('.el-select-dropdown').style.display='none';
                window.logisticsEvents.push('提取方式');
                const host = document.querySelector('#pickup');
                host.insertAdjacentHTML('afterend', `
                  <div class="el-form-item" id="freight">
                    <label class="el-form-item__label">运费承担方式</label>
                    <div class="el-form-item__content"><div class="el-select">
                      <input class="el-input__inner" readonly onclick="openFreight(this)">
                      <div class="el-select-dropdown" style="display:none"><ul>
                        <li class="el-select-dropdown__item" onclick="chooseFreight(this)">卖家承担运费</li>
                      </ul></div>
                    </div></div>
                  </div>`);
              }
              function openFreight(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }
              function chooseFreight(option) {
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display='none';
                window.logisticsEvents.push('运费承担方式');
              }
            </script>
            """
        )

        report = await listing.fill_sales_and_logistics({"商品价格": "586"})

        self.assertEqual(
            await self.page.locator("#pickup-select .el-tag").inner_text(), "邮寄"
        )
        self.assertEqual(await self.page.locator("#freight input").input_value(), "卖家承担运费")
        self.assertEqual(
            await self.page.evaluate("window.logisticsEvents"),
            ["提取方式", "运费承担方式"],
        )
        self.assertEqual(
            report["defaults_applied"], ("提取方式", "运费承担方式")
        )

    async def test_sales_and_logistics_preflights_required_fields_before_writes(self):
        listing = await self._listing(
            """
            <div class="el-form-item is-required" id="price">
              <label class="el-form-item__label">*商品价格</label>
              <div class="el-form-item__content"><input></div>
            </div>
            <div class="el-form-item is-required" id="city">
              <label class="el-form-item__label">*城市</label>
              <div class="el-form-item__content"><input></div>
            </div>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "城市"):
            await self._method(listing, "fill_sales_and_logistics")(
                {"商品价格": "586", "城市说明": "广州市"}
            )

        self.assertEqual(
            await self.page.locator("#price input, #city input").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            ),
            ["", ""],
        )

    async def test_sales_accepts_platform_price_decimal_formatting(self):
        listing = await self._listing(
            """
            <div class="el-form-item is-required" id="price">
              <label class="el-form-item__label">*商品价格</label>
              <div class="el-form-item__content">
                <input onblur="this.value=Number(this.value).toFixed(2)">
              </div>
            </div>
            """
        )

        report = await listing.fill_sales_and_logistics({"商品价格": "586"})

        self.assertEqual(await self.page.locator("#price input").input_value(), "586.00")
        self.assertEqual(report["values"]["商品价格"], "586.00")

    async def test_sales_preserves_existing_default_city_without_excel_source(self):
        listing = await self._listing(
            """
            <div class="el-form-item is-required" id="price">
              <label class="el-form-item__label">*商品价格</label>
              <div class="el-form-item__content"><input></div>
            </div>
            <div class="el-form-item is-required" id="city">
              <label class="el-form-item__label">*城市</label>
              <div class="el-form-item__content"><input value="旧城市"></div>
            </div>
            """
        )

        report = await listing.fill_sales_and_logistics({"商品价格": "586"})

        self.assertEqual(await self.page.locator("#price input").input_value(), "586")
        self.assertEqual(await self.page.locator("#city input").input_value(), "旧城市")
        self.assertEqual(report["preserved_defaults"], ("城市",))

    async def test_sales_non_location_required_field_needs_excel_source_even_with_old_page_value(self):
        listing = await self._listing(
            """
            <div class="el-form-item is-required" id="price">
              <label class="el-form-item__label">*商品价格</label>
              <div class="el-form-item__content"><input></div>
            </div>
            <div class="el-form-item is-required" id="weight">
              <label class="el-form-item__label">*商品物流重量(千克)</label>
              <div class="el-form-item__content"><input value="1.2"></div>
            </div>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "商品物流重量"):
            await listing.fill_sales_and_logistics({"商品价格": "586"})

        self.assertEqual(await self.page.locator("#price input").input_value(), "")
        self.assertEqual(await self.page.locator("#weight input").input_value(), "1.2")

    async def test_after_sales_uses_exact_values_and_conditional_new_product_yes(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="after-sales">
              <div class="wrap-item_label">售后及其他</div>
              <div class="el-form-item is-required" id="invoice">
                <label class="el-form-item__label">*发票</label>
                <div class="el-form-item__content">
                  <label><input type="radio" name="invoice" value="yes"><span>有</span></label>
                  <label><input type="radio" name="invoice" value="no"><span>无</span></label>
                </div>
              </div>
              <div class="el-form-item" id="new-product">
                <label class="el-form-item__label">是否申报新品</label>
                <div class="el-form-item__content">
                  <label><input type="radio" name="new-product" value="yes"><span>是</span></label>
                  <label><input type="radio" name="new-product" value="no"><span>否</span></label>
                </div>
              </div>
              <div class="el-form-item" id="discount">
                <label class="el-form-item__label">是否支持会员折扣</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <button onclick="window.forbiddenClicks++">发布</button>
              <button onclick="window.forbiddenClicks++">保存</button>
              <button onclick="window.forbiddenClicks++">保存并铺货到平台</button>
            </div>
            <script>window.forbiddenClicks = 0;</script>
            """
        )

        report = await self._method(listing, "fill_after_sales")(
            {
                "发票": "有",
                "是否支持会员折扣": "支持会员打折",
                "发票说明": "不得模糊匹配",
            }
        )

        self.assertEqual(report["values"]["发票"], "有")
        self.assertEqual(report["new_product_declaration"], "是")
        self.assertTrue(await self.page.locator('#invoice input[value="yes"]').is_checked())
        self.assertTrue(await self.page.locator('#new-product input[value="yes"]').is_checked())
        self.assertEqual(await self.page.locator("#discount input").input_value(), "支持会员打折")
        self.assertEqual(await self.page.evaluate("window.forbiddenClicks"), 0)

    async def test_after_sales_uses_real_others_items_scope(self):
        listing = await self._listing(
            """
            <div class="el-form-item is-required" id="outside-title">
              <label class="el-form-item__label">商品标题</label>
              <div class="el-form-item__content"><input></div>
            </div>
            <form class="step3-form">
              <div class="title ft-12">售后及其他</div>
              <div class="others-items">
                <div class="el-form-item is-required" id="real-invoice">
                  <label class="el-form-item__label">发票</label>
                  <div class="el-form-item__content"><input></div>
                </div>
              </div>
            </form>
            """
        )

        report = await listing.fill_after_sales({"发票": "有"})

        self.assertEqual(report["values"], {"发票": "有"})
        self.assertEqual(await self.page.locator("#real-invoice input").input_value(), "有")
        self.assertEqual(await self.page.locator("#outside-title input").input_value(), "")

    async def test_after_sales_accepts_real_tmall_material_component_layout(self):
        listing = await self._listing(
            """
            <form class="step3-form">
              <div class="title ft-12">售后及其他</div>
              <div class="others-items">
                <div class="el-form-item is-required" id="real-material">
                  <label class="el-form-item__label">材质成分</label>
                  <div class="el-form-item__content">
                    <div class="multi-complex-items"><button>添加</button></div>
                  </div>
                </div>
              </div>
            </form>
            """
        )
        listing.fill_materials = AsyncMock(return_value=(("棉", 100),))

        report = await listing.fill_after_sales({"材质成分/材质": "棉/100%"})

        listing.fill_materials.assert_awaited_once()
        self.assertEqual(report["materials"], (("棉", 100),))

    async def test_real_tmall_material_component_fills_without_vue_introspection(self):
        listing = await self._listing(
            """
            <form class="step3-form">
              <div class="title ft-12">售后及其他</div>
              <div class="others-items">
                <div class="el-form-item is-required is-error" id="real-material-dom">
                  <label class="el-form-item__label">材质成分</label>
                  <div class="el-form-item__content">
                    <div class="multi-complex-items">
                      <button type="button" onclick="addMaterialRow()">添加</button>
                    </div>
                    <div class="el-form-item__error">必填</div>
                  </div>
                </div>
              </div>
            </form>
            <script>
              function addMaterialRow() {
                const root = document.querySelector('.multi-complex-items');
                const row = document.createElement('div');
                row.className = 'multi-complex-items_item';
                row.innerHTML = `
                  <div class="el-select">
                    <input class="el-input__inner" readonly onclick="openMaterial(this)">
                    <div class="el-select-dropdown" style="display:none"><ul>
                      <li class="el-select-dropdown__item" onclick="chooseMaterial(this)">棉</li>
                      <li class="el-select-dropdown__item" onclick="chooseMaterial(this)">亚麻</li>
                    </ul></div>
                  </div>
                  <input class="percentage"
                    onchange="clearMaterialError()"
                    onblur="this.value=Number(this.value).toFixed(2)">
                  <button type="button" onclick="this.parentElement.remove()">移 除</button>`;
                root.appendChild(row);
              }
              function openMaterial(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }
              function chooseMaterial(option) {
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display='none';
              }
              function clearMaterialError() {
                const item = document.querySelector('#real-material-dom');
                item.classList.remove('is-error');
                item.querySelector('.el-form-item__error').remove();
              }
            </script>
            """
        )

        report = await listing.fill_after_sales({"材质成分/材质": "棉/100%"})

        self.assertEqual(report["materials"], (("棉", 100),))
        self.assertEqual(
            await self.page.locator("#real-material-dom input").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            ),
            ["棉", "100"],
        )

    async def test_after_sales_refills_normal_fields_after_material_rerender(self):
        listing = await self._listing(
            """
            <form class="step3-form">
              <div class="title ft-12">售后及其他</div>
              <div class="others-items">
                <div class="el-form-item is-required" id="publish-type">
                  <label class="el-form-item__label">发布类型</label>
                  <div class="el-form-item__content"><input></div>
                </div>
                <div class="el-form-item is-required" id="materials-rerender">
                  <label class="el-form-item__label">材质成分</label>
                  <div class="el-form-item__content">
                    <div class="multi-complex-items">
                      <button type="button" onclick="addRerenderMaterial()">添加</button>
                    </div>
                  </div>
                </div>
              </div>
            </form>
            <script>
              function addRerenderMaterial() {
                document.querySelector('#publish-type input').value = '';
                const root = document.querySelector('#materials-rerender .multi-complex-items');
                const row = document.createElement('div');
                row.className = 'multi-complex-items_item';
                row.innerHTML = `
                  <div class="el-select">
                    <input class="el-input__inner" readonly onclick="this.nextElementSibling.style.display='block'">
                    <div class="el-select-dropdown" style="display:none"><ul>
                      <li class="el-select-dropdown__item"
                        onclick="this.closest('.el-select').querySelector('input').value='棉';this.closest('.el-select-dropdown').style.display='none'">棉</li>
                    </ul></div>
                  </div>
                  <input class="percentage">
                  <button type="button" onclick="this.parentElement.remove()">移 除</button>`;
                root.appendChild(row);
              }
            </script>
            """
        )

        report = await listing.fill_after_sales(
            {"发布类型": "一口价", "材质成分/材质": "棉/100%"}
        )

        self.assertEqual(report["values"]["发布类型"], "一口价")
        self.assertEqual(
            await self.page.locator("#publish-type input").input_value(), "一口价"
        )

    async def test_after_sales_radio_uses_one_ordered_or_group(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="after-sales">
              <div class="wrap-item_label">售后及其他</div>
              <div class="el-form-item is-required" id="invoice">
                <label class="el-form-item__label">*发票</label>
                <div class="el-form-item__content">
                  <label><input type="radio" name="invoice" value="yes"><span>有</span></label>
                  <label><input type="radio" name="invoice" value="no"><span>无</span></label>
                </div>
              </div>
            </div>
            """
        )

        report = await listing.fill_after_sales({"发票": "不存在/有"})

        self.assertEqual(report["values"]["发票"], "有")
        self.assertTrue(await self.page.locator('#invoice input[value="yes"]').is_checked())

    async def test_after_sales_required_field_does_not_fuzzy_match_excel_header(self):
        listing = await self._listing(
            """
            <div class="wrap-item">
              <div class="wrap-item_label">售后及其他</div>
              <div class="el-form-item is-required" id="invoice">
                <label class="el-form-item__label">*发票</label>
                <div class="el-form-item__content"><input></div>
              </div>
            </div>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "发票"):
            await self._method(listing, "fill_after_sales")({"发票说明": "有"})

        self.assertEqual(await self.page.locator("#invoice input").input_value(), "")

    async def test_after_sales_preserves_required_default_when_excel_has_no_field(self):
        listing = await self._listing(
            """
            <div class="wrap-item">
              <div class="wrap-item_label">售后及其他</div>
              <div class="el-form-item is-required" id="invoice">
                <label class="el-form-item__label">*发票</label>
                <div class="el-form-item__content">
                  <label><input type="radio" name="invoice" value="yes" checked><span>有</span></label>
                  <label><input type="radio" name="invoice" value="no"><span>无</span></label>
                </div>
              </div>
            </div>
            """
        )

        report = await listing.fill_after_sales({})

        self.assertTrue(await self.page.locator('#invoice input[value="yes"]').is_checked())
        self.assertEqual(report["preserved"], ("发票",))

    async def test_after_sales_reuses_structured_material_component_when_compatible(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="after-sales">
              <div class="wrap-item_label">售后及其他</div>
              <div class="conf"><div class="complex-wrap"><div class="complex-item_multi">
                <div class="el-form-item is-required" id="materials">
                  <label class="el-form-item__label">*材质成分</label>
                  <div class="el-form-item__content"><div class="multi-complex-items">
                    <button>添加</button>
                  </div></div>
                </div>
              </div></div></div>
            </div>
            """
        )
        listing.fill_materials = AsyncMock(return_value=(("棉", 100),))

        report = await listing.fill_after_sales({"材质成分/材质": "棉/100%"})

        listing.fill_materials.assert_awaited_once()
        materials = listing.fill_materials.await_args.args[0]
        self.assertEqual([(item.name, item.percentage) for item in materials], [("棉", 100)])
        self.assertEqual(report["materials"], (("棉", 100),))

    async def test_after_sales_incompatible_material_component_fails_before_clicking_add(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="after-sales">
              <div class="wrap-item_label">售后及其他</div>
              <div class="el-form-item is-required" id="materials">
                <label class="el-form-item__label">*材质成分</label>
                <div class="el-form-item__content">
                  <button onclick="window.materialAddClicks++">添加</button>
                </div>
              </div>
            </div>
            <script>window.materialAddClicks = 0;</script>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "材质成分.*不兼容"):
            await listing.fill_after_sales({"材质": "棉/100%"})

        self.assertEqual(await self.page.evaluate("window.materialAddClicks"), 0)

    async def test_size_chart_does_not_check_parameters_and_fills_visible_columns_by_size(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="size-chart">
              <div class="wrap-item_label">码表</div>
              <label><input type="checkbox">臀围（cm）</label>
              <label><input type="checkbox">裤长（cm）</label>
              <table class="tmall-size-table">
                <thead><tr><th>尺码</th><th>*体重（kg）</th><th>*腰围（cm）</th>
                  <th>*身高（cm）</th><th>臀围（cm）</th><th>裤长（cm）</th></tr></thead>
                <tbody>
                  <tr><td>S</td><td><input></td><td><input></td><td><input></td>
                    <td><input></td><td><input></td></tr>
                  <tr><td>M</td><td><input></td><td><input></td><td><input></td>
                    <td><input></td><td><input></td></tr>
                </tbody>
              </table>
            </div>
            """
        )
        rows = (
            {"尺码": "M", "身高(cm)": "165", "体重(kg)": "60", "腰围(cm)": "84", "臀围(cm)": "110"},
            {"尺码": "S", "身高(cm)": "160", "体重(kg)": "55", "腰围(cm)": "80", "臀围(cm)": "106"},
        )

        report = await listing.fill_size_chart(rows)

        self.assertEqual(report["checked_parameters"], ())
        self.assertEqual(
            report["scanned_fields"][:4],
            ("尺码", "体重（kg）", "腰围（cm）", "身高（cm）"),
        )
        self.assertFalse(await self.page.locator("#size-chart label").nth(0).locator("input").is_checked())
        self.assertFalse(await self.page.locator("#size-chart label").nth(1).locator("input").is_checked())
        first_values = await self.page.locator("#size-chart tbody tr").nth(0).locator("input").evaluate_all("nodes => nodes.map(node => node.value)")
        self.assertEqual(first_values[:4], ["55", "80", "160", "106"])

    async def test_size_chart_supports_real_split_table_and_prefixed_size_header(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
              <div class="block-std-size-extends"><div class="el-table">
                <div class="el-table__header-wrapper"><table><thead><tr>
                  <th>“中国码”尺码</th><th>* 身高（cm）</th>
                </tr></thead></table></div>
                <div class="el-table__body-wrapper"><table><tbody><tr>
                  <td>S</td><td><input id="real-height"></td>
                </tr></tbody></table></div>
              </div></div>
            </div>
            """
        )

        report = await listing.fill_size_chart(
            ({"尺码": "S", "身高(cm)": "170"},)
        )

        self.assertEqual(report["row_count"], 1)
        self.assertEqual(
            await self.page.locator("#real-height").input_value(), "170"
        )

    async def test_size_chart_matches_required_value_suffix_to_excel_labels(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
              <div class="block-std-size-extends"><div class="el-table">
                <div class="el-table__header-wrapper"><table><thead><tr>
                  <th>“中国码”尺码</th><th>* 身高（cm） 值</th><th>* 体重（kg） 值</th>
                </tr></thead></table></div>
                <div class="el-table__body-wrapper"><table><tbody><tr>
                  <td>S</td><td><input id="value-height"></td><td><input id="value-weight"></td>
                </tr></tbody></table></div>
              </div></div>
            </div>
            """
        )
        original_headers = listing._size_table_headers
        listing._size_table_headers = AsyncMock(side_effect=original_headers)

        report = await listing.fill_size_chart(
            ({"尺码": "S", "身高(cm)": "170", "体重(kg)": "60"},)
        )

        self.assertEqual(report["row_count"], 1)
        self.assertEqual(
            await self.page.locator("#value-height").input_value(), "170"
        )
        self.assertEqual(
            await self.page.locator("#value-weight").input_value(), "60"
        )
        self.assertEqual(listing._size_table_headers.await_count, 1)

    async def test_size_chart_ignores_element_fixed_column_clones(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
              <div class="block-std-size-extends"><div class="el-table">
                <div class="el-table__header-wrapper"><table><thead><tr>
                  <th>“中国码”尺码</th><th>* 身高（cm）</th><th>操作</th>
                </tr></thead></table></div>
                <div class="el-table__body-wrapper"><table><tbody><tr>
                  <td>S</td><td><input id="main-height"></td><td>清空</td>
                </tr></tbody></table></div>
                <div class="el-table__fixed">
                  <div class="el-table__fixed-header-wrapper"><table><thead><tr>
                    <th>“中国码”尺码</th>
                  </tr></thead></table></div>
                  <div class="el-table__fixed-body-wrapper"><table><tbody><tr>
                    <td>S</td>
                  </tr></tbody></table></div>
                </div>
                <div class="el-table__fixed-right">
                  <div class="el-table__fixed-header-wrapper"><table><thead><tr>
                    <th>操作</th>
                  </tr></thead></table></div>
                  <div class="el-table__fixed-body-wrapper"><table><tbody><tr>
                    <td>清空</td>
                  </tr></tbody></table></div>
                </div>
              </div></div>
            </div>
            """
        )

        report = await listing.fill_size_chart(
            ({"尺码": "S", "身高(cm)": "170"},)
        )

        self.assertEqual(report["row_count"], 1)
        self.assertEqual(
            await self.page.locator("#main-height").input_value(), "170"
        )

    async def test_size_chart_maps_fixed_size_header_to_main_cell_by_column_id(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
              <div class="block-std-size-extends"><div class="el-table">
                <div class="el-table__header-wrapper"><table><thead><tr>
                  <th class="el-table_7_column_1 is-hidden"></th>
                  <th class="el-table_7_column_2">* 身高（cm）</th>
                  <th class="el-table_7_column_3">操作</th>
                </tr></thead></table></div>
                <div class="el-table__body-wrapper"><table><tbody><tr>
                  <td class="el-table_7_column_1"></td>
                  <td class="el-table_7_column_2"><input id="column-height"></td>
                  <td class="el-table_7_column_3">清空</td>
                </tr></tbody></table></div>
                <div class="el-table__fixed">
                  <div class="el-table__fixed-header-wrapper"><table><thead><tr>
                    <th class="el-table_7_column_1">“中国码”尺码</th>
                    <th class="el-table_7_column_2 is-hidden"></th>
                    <th class="el-table_7_column_3 is-hidden"></th>
                  </tr></thead></table></div>
                  <div class="el-table__fixed-body-wrapper"><table><tbody><tr>
                    <td class="el-table_7_column_1">S</td>
                  </tr></tbody></table></div>
                </div>
                <div class="el-table__fixed-right">
                  <div class="el-table__fixed-header-wrapper"><table><thead><tr>
                    <th class="el-table_7_column_1 is-hidden"></th>
                    <th class="el-table_7_column_2 is-hidden"></th>
                    <th class="el-table_7_column_3">操作</th>
                  </tr></thead></table></div>
                </div>
              </div></div>
            </div>
            """
        )

        report = await listing.fill_size_chart(
            ({"尺码": "S", "身高(cm)": "170"},)
        )

        self.assertEqual(report["row_count"], 1)
        self.assertEqual(
            await self.page.locator("#column-height").input_value(), "170"
        )

    async def test_size_chart_preflights_required_values_before_checking_optional_parameters(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="size-preflight">
              <div class="wrap-item_label">尺码表</div>
              <label onclick="window.optionalClicks++"><input type="checkbox">臀围（cm）</label>
              <table class="tmall-size-table">
                <thead><tr><th>尺码</th><th>*身高（cm）</th></tr></thead>
                <tbody><tr><td>S</td><td><input></td></tr></tbody>
              </table>
            </div>
            <script>window.optionalClicks = 0;</script>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "必填参数缺少来源"):
            await listing.fill_size_chart(
                ({"尺码": "S", "身高(cm)": "", "臀围(cm)": "106"},)
            )

        self.assertEqual(await self.page.evaluate("window.optionalClicks"), 0)
        self.assertFalse(
            await self.page.locator("#size-preflight input[type=checkbox]").is_checked()
        )

    async def test_size_chart_switches_range_columns_and_fills_tuple_and_tilde_values(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="range-size-chart">
              <div class="wrap-item_label">尺码表</div>
              <table class="tmall-size-table">
                <thead><tr>
                  <th>尺码</th>
                  <th data-name="height">*身高（cm） <button onclick="enableHeightRange()">区间</button></th>
                  <th data-name="weight">*体重（kg） <button onclick="window.weightRangeClicks++">区间</button></th>
                </tr></thead>
                <tbody>
                  <tr><td>S</td><td data-name="height"><input></td><td><input></td></tr>
                  <tr><td>M</td><td data-name="height"><input></td><td><input></td></tr>
                </tbody>
              </table>
            </div>
            <script>
              window.heightRangeClicks = 0;
              window.weightRangeClicks = 0;
              function enableHeightRange() {
                window.heightRangeClicks++;
                document.querySelectorAll('td[data-name="height"]').forEach(cell => {
                  cell.innerHTML = '<input class="minimum"><input class="maximum">';
                });
              }
            </script>
            """
        )
        rows = (
            {"尺码": "M", "身高(cm)": "165.00~170.00", "体重(kg)": "60.00"},
            {"尺码": "S", "身高(cm)": (160.0, "165.00"), "体重(kg)": 55.0},
        )

        report = await listing.fill_size_chart(rows)

        self.assertEqual(await self.page.evaluate("window.heightRangeClicks"), 1)
        self.assertEqual(await self.page.evaluate("window.weightRangeClicks"), 0)
        values = await self.page.locator("#range-size-chart tbody tr").evaluate_all(
            "rows => rows.map(row => Array.from(row.querySelectorAll('input')).map(input => input.value))"
        )
        self.assertEqual(values, [["160", "165", "55"], ["165", "170", "60"]])
        self.assertEqual(report["rows"]["S"]["身高（cm）"], ("160", "165"))

    async def test_size_chart_removes_integer_decimal_display_added_on_blur(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
              <table class="tmall-size-table">
                <thead><tr><th>尺码</th><th>*身高（cm）</th></tr></thead>
                <tbody><tr><td>S</td><td>
                  <input id="decimal-height"
                    oninput="setTimeout(() => this.value=Number(this.value).toFixed(2), 0)"
                    onblur="this.value=Number(this.value).toFixed(2)">
                </td></tr></tbody>
              </table>
            </div>
            """
        )

        await listing.fill_size_chart(({"尺码": "S", "身高(cm)": 155.0},))

        self.assertEqual(
            await self.page.locator("#decimal-height").input_value(), "155"
        )

    async def test_size_chart_cleans_integer_decimals_after_late_page_rerender(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
              <table class="tmall-size-table">
                <thead><tr><th>尺码</th><th>*身高（cm）</th></tr></thead>
                <tbody><tr><td>S</td><td><input id="late-decimal-height"></td></tr></tbody>
              </table>
            </div>
            """
        )

        await listing.fill_size_chart(({"尺码": "S", "身高(cm)": 155},))
        # 模拟后续填写物流、图片等字段触发 Vue 对数字控件的迟到重渲染。
        await self.page.locator("#late-decimal-height").evaluate(
            "element => { element.value = '155.00'; }"
        )

        await self._method(listing, "clean_size_chart_integer_displays")()

        self.assertEqual(
            await self.page.locator("#late-decimal-height").input_value(), "155"
        )

    async def test_size_chart_keeps_equal_saved_numeric_value_without_retyping(self):
        listing = await self._listing(
            """
            <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
              <table class="tmall-size-table">
                <thead><tr><th>尺码</th><th>*身高（cm）</th></tr></thead>
                <tbody><tr><td>S</td><td>
                  <input id="saved-height" value="155.00"
                    oninput="window.savedInputEvents++">
                </td></tr></tbody>
              </table>
            </div>
            <script>window.savedInputEvents = 0;</script>
            """
        )

        await listing.fill_size_chart(({"尺码": "S", "身高(cm)": 155},))

        self.assertEqual(await self.page.evaluate("window.savedInputEvents"), 0)
        self.assertEqual(
            await self.page.locator("#saved-height").input_value(), "155"
        )

    async def test_size_chart_ignores_unrendered_optional_source_without_checking_parameter(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="dynamic-range-size-chart">
              <div class="wrap-item_label">尺码表</div>
              <label onclick="addPantsLength()"><input type="checkbox">裤长（cm）</label>
              <table class="tmall-size-table">
                <thead><tr><th>尺码</th><th>*身高（cm）</th></tr></thead>
                <tbody>
                  <tr><td>S</td><td><input></td></tr>
                  <tr><td>M</td><td><input></td></tr>
                </tbody>
              </table>
            </div>
            <script>
              window.pantsLengthRangeClicks = 0;
              function addPantsLength() {
                const table = document.querySelector('#dynamic-range-size-chart table');
                if (table.querySelector('th[data-name="pants-length"]')) return;
                const header = document.createElement('th');
                header.dataset.name = 'pants-length';
                header.innerHTML = '裤长（cm） <button type="button" onclick="enablePantsLengthRange(event)">区间</button>';
                table.querySelector('thead tr').appendChild(header);
                table.querySelectorAll('tbody tr').forEach(row => {
                  const cell = document.createElement('td');
                  cell.dataset.name = 'pants-length';
                  cell.innerHTML = '<input>';
                  row.appendChild(cell);
                });
              }
              function enablePantsLengthRange(event) {
                event.stopPropagation();
                window.pantsLengthRangeClicks++;
                document.querySelectorAll('td[data-name="pants-length"]').forEach(cell => {
                  cell.innerHTML = '<input class="minimum"><input class="maximum">';
                });
              }
            </script>
            """
        )
        rows = (
            {"尺码": "S", "身高(cm)": "160", "裤长(cm)": ("98", "102")},
            {"尺码": "M", "身高(cm)": "165", "裤长(cm)": "102~106"},
        )

        report = await listing.fill_size_chart(rows)

        self.assertEqual(await self.page.evaluate("window.pantsLengthRangeClicks"), 0)
        self.assertFalse(
            await self.page.locator(
                "#dynamic-range-size-chart input[type=checkbox]"
            ).is_checked()
        )
        values = await self.page.locator(
            "#dynamic-range-size-chart tbody tr"
        ).evaluate_all(
            "rows => rows.map(row => Array.from(row.querySelectorAll('input')).map(input => input.value))"
        )
        self.assertEqual(values, [["160"], ["165"]])
        self.assertNotIn("裤长（cm）", report["rows"]["M"])

    async def test_required_images_are_mapped_by_label_not_dom_order(self):
        listing = await self._listing(
            """
            <div class="el-form-item" id="parameter"><label class="el-form-item__label">产品参数图片</label></div>
            <div class="el-form-item" id="vertical"><label class="el-form-item__label">商品竖图</label></div>
            <div class="el-form-item" id="transparent"><label class="el-form-item__label">透明素材图</label></div>
            """
        )
        calls = []

        async def uploader(_page, item, paths, label, _timeout):
            calls.append((await item.get_attribute("id"), tuple(paths), label))
            return "uploaded"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vertical = root / "vertical.png"
            transparent = root / "transparent.png"
            parameter = root / "parameter.png"
            for image in (vertical, transparent, parameter):
                image.write_bytes(b"image")
            assets = SimpleNamespace(
                vertical_image=vertical,
                transparent_image=transparent,
                parameter_image=parameter,
            )
            report = await listing.sync_required_images(
                assets, timeout_seconds=2, uploader=uploader
            )

        self.assertEqual(
            [call[0] for call in calls], ["vertical", "transparent", "parameter"]
        )
        self.assertEqual(
            [call[2] for call in calls], ["商品竖图", "透明素材图", "产品参数图片"]
        )
        self.assertEqual(set(report), {"商品竖图", "透明素材图", "产品参数图片"})

    async def test_inherited_main_images_swap_first_and_third_for_both_tmall_rows(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="square-images">
              <div class="wrap-item_label">商品图片</div>
            </div>
            <div class="wrap-item" id="portrait-images">
              <div class="wrap-item_label">3:4商品图片</div>
            </div>
            """
        )
        calls = []

        async def uploader(_page, item, paths, label, _timeout, *, force_replace=False):
            calls.append(
                (
                    await item.get_attribute("id"),
                    tuple(path.name for path in paths),
                    label,
                    force_replace,
                )
            )
            return "replaced"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            square = tuple(root / f"square-{index}.jpg" for index in range(1, 6))
            portrait = tuple(root / f"portrait-{index}.jpg" for index in range(1, 6))
            report = await self._method(listing, "sync_inherited_main_images")(
                square,
                portrait,
                timeout_seconds=2,
                uploader=uploader,
            )

        self.assertEqual(
            calls,
            [
                (
                    "square-images",
                    ("square-3.jpg", "square-2.jpg", "square-1.jpg", "square-4.jpg", "square-5.jpg"),
                    "商品图片",
                    True,
                ),
                (
                    "portrait-images",
                    ("portrait-3.jpg", "portrait-2.jpg", "portrait-1.jpg", "portrait-4.jpg", "portrait-5.jpg"),
                    "3:4商品图片",
                    True,
                ),
            ],
        )
        self.assertEqual(report, {"商品图片": "replaced", "3:4商品图片": "replaced"})

    async def test_inherited_main_images_support_real_complex_upload_groups(self):
        listing = await self._listing(
            """
            <div class="complex-wrap" id="square-images">
              <div class="complex-image-label">* 商品图片</div>
              <div><div class="muti-upload" style="display:block;width:20px;height:20px">
                <div class="sc-upload draggable" style="display:block;width:20px;height:20px"></div>
              </div></div>
            </div>
            <div class="complex-wrap" id="portrait-images">
              <div class="complex-image-label">* 3:4商品图片</div>
              <div><div class="muti-upload" style="display:block;width:20px;height:20px">
                <div class="sc-upload draggable" style="display:block;width:20px;height:20px"></div>
              </div></div>
            </div>
            """
        )
        calls = []

        async def uploader(_page, item, paths, label, _timeout, *, force_replace=False):
            calls.append(
                (await item.get_attribute("id"), label, force_replace)
            )
            return "replaced"

        paths = tuple(Path(f"/{index}.jpg") for index in range(1, 4))
        report = await listing.sync_inherited_main_images(
            paths,
            paths,
            timeout_seconds=2,
            uploader=uploader,
        )

        self.assertEqual(
            calls,
            [
                ("square-images", "商品图片", True),
                ("portrait-images", "3:4商品图片", True),
            ],
        )
        self.assertEqual(report, {"商品图片": "replaced", "3:4商品图片": "replaced"})

    async def test_inherited_main_images_fall_back_to_the_only_two_multi_upload_rows(self):
        listing = await self._listing(
            """
            <div class="muti-upload" id="square-images" style="display:block;width:20px;height:20px">
              <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
              <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
            </div>
            <div class="muti-upload" id="portrait-images" style="display:block;width:20px;height:20px">
              <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
              <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
            </div>
            <div class="sc-upload isSingle" style="display:block;width:10px;height:10px"></div>
            """
        )
        calls = []

        async def uploader(_page, item, _paths, label, _timeout, *, force_replace=False):
            calls.append((await item.get_attribute("id"), label, force_replace))
            return "replaced"

        paths = tuple(Path(f"/{index}.jpg") for index in range(1, 4))
        await listing.sync_inherited_main_images(
            paths,
            paths,
            timeout_seconds=2,
            uploader=uploader,
        )

        self.assertEqual(
            calls,
            [
                ("square-images", "商品图片", True),
                ("portrait-images", "3:4商品图片", True),
            ],
        )

    async def test_inherited_main_images_match_live_form_item_labels_with_three_upload_groups(self):
        listing = await self._listing(
            """
            <div class="el-form-item" id="square-images">
              <label class="el-form-item__label">* 商品图片</label>
              <div class="el-form-item__content"><div class="complex-wrap"><div>
                <div class="muti-upload" style="display:block;width:20px;height:20px">
                  <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
                  <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
                </div>
              </div></div></div>
            </div>
            <div class="el-form-item" id="portrait-images">
              <label class="el-form-item__label">* 3:4商品图片</label>
              <div class="el-form-item__content"><div class="complex-wrap"><div>
                <div class="muti-upload" style="display:block;width:20px;height:20px">
                  <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
                  <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
                </div>
              </div></div></div>
            </div>
            <div class="el-form-item" id="unrelated-images">
              <label class="el-form-item__label">商品详情图片</label>
              <div class="el-form-item__content"><div class="complex-wrap"><div>
                <div class="muti-upload" style="display:block;width:20px;height:20px">
                  <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
                  <div class="sc-upload draggable" style="display:block;width:10px;height:10px"></div>
                </div>
              </div></div></div>
            </div>
            """
        )
        calls = []

        async def uploader(_page, item, _paths, label, _timeout, *, force_replace=False):
            calls.append((await item.get_attribute("id"), label, force_replace))
            return "replaced"

        paths = tuple(Path(f"/{index}.jpg") for index in range(1, 4))
        await listing.sync_inherited_main_images(
            paths,
            paths,
            timeout_seconds=2,
            uploader=uploader,
        )

        self.assertEqual(
            calls,
            [
                ("square-images", "商品图片", True),
                ("portrait-images", "3:4商品图片", True),
            ],
        )

    async def test_attribute_images_are_mapped_to_first_spec_values_in_order(self):
        listing = await self._listing(
            """
            <div class="block-specification">
              <div class="title-bg"><input value="颜色"></div>
              <div class="specification-value"><div class="specification-value-flex">
                <div class="specification-value-flex_item">
                  <div class="el-select specification-value-flex_input">
                    <input class="el-input__inner" value="军绿色" readonly>
                  </div>
                  <div class="specification-value-flex_img"><div class="sc-upload isSingle">
                    <div class="el-upload"><input type="file" accept=".jpg,.jpeg,.png"></div>
                  </div></div>
                </div>
                <div class="specification-value-flex_item">
                  <div class="el-select specification-value-flex_input">
                    <input class="el-input__inner" value="黑色" readonly>
                  </div>
                  <div class="specification-value-flex_img"><div class="sc-upload isSingle">
                    <div class="el-upload"><input type="file" accept=".jpg,.jpeg,.png"></div>
                  </div></div>
                </div>
              </div></div>
            </div>
            <div class="block-specification">
              <div class="title-bg"><input value="尺码"></div>
              <div class="specification-value"><div class="specification-value-flex">
                <div class="specification-value-flex_item"><input class="spec-value" value="S"></div>
              </div></div>
            </div>
            """
        )
        calls = []

        async def uploader(_page, item, paths, label, _timeout):
            calls.append((await item.locator(".sc-upload").count(), tuple(paths), label))
            return "uploaded"

        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "green.png"
            second = Path(directory) / "black.png"
            first.write_bytes(b"green")
            second.write_bytes(b"black")
            report = await listing.sync_attribute_images(
                (first, second), timeout_seconds=2, uploader=uploader
            )

        self.assertEqual(
            [item[2] for item in calls],
            ["属性图片[军绿色]", "属性图片[黑色]"],
        )
        self.assertEqual(
            [item[1][0].name for item in calls], ["green.png", "black.png"]
        )
        self.assertEqual(
            set(report), {"属性图片[军绿色]", "属性图片[黑色]"}
        )

    async def test_final_required_validation_reports_empty_mobile_description_without_clicking_imports(self):
        listing = await self._listing(
            """
            <div class="wrap-item" id="description">
              <div class="wrap-item_label">商品描述</div>
              <div class="el-form-item is-required" id="mobile-description">
                <label class="el-form-item__label">*手机端详情描述</label>
                <div class="el-form-item__content">
                  <button onclick="window.descriptionImportClicks++">导入PC描述</button>
                  <button onclick="window.descriptionImportClicks++">从素材空间上传</button>
                </div>
              </div>
              <div class="el-form-item is-required" id="invoice">
                <label class="el-form-item__label">*发票</label>
                <div class="el-form-item__content">
                  <label><input type="radio" name="invoice" checked><span>有</span></label>
                </div>
              </div>
            </div>
            <script>window.descriptionImportClicks = 0;</script>
            """
        )

        with self.assertRaisesRegex(TmallFormListingError, "手机端详情描述"):
            await listing.validate_remaining_required_fields()

        self.assertEqual(await self.page.evaluate("window.descriptionImportClicks"), 0)

    async def test_final_required_validation_treats_uploaded_image_as_a_value(self):
        listing = await self._listing(
            """
            <div class="el-form-item is-required">
              <label class="el-form-item__label">*商品竖图</label>
              <div class="el-form-item__content">
                <div class="file-img"><img src="data:image/png;base64,AA=="></div>
              </div>
            </div>
            """
        )

        self.assertEqual(
            await listing.validate_remaining_required_fields(),
            {"valid": True, "missing": ()},
        )

    async def test_new_product_declaration_is_yes_only_when_field_exists(self):
        listing = await self._listing(
            """
            <div class="el-form-item">
              <label class="el-form-item__label">是否申报新品</label>
              <div class="el-form-item__content">
                <label><input type="radio" name="new-product" value="yes"><span>是</span></label>
                <label><input type="radio" name="new-product" value="no"><span>否</span></label>
              </div>
            </div>
            """
        )

        result = await listing.fill_new_product_declaration()

        self.assertEqual(result, "是")
        self.assertTrue(await self.page.locator('input[value="yes"]').is_checked())


if __name__ == "__main__":
    unittest.main()
