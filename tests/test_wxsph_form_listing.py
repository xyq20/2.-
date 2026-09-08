import unittest

from wxsph_data import parse_wxsph_fields
from wxsph_form_listing import WxsphFormListing, WxsphFormListingError


def select_markup(options):
    rendered = "".join(
        '<li class="el-select-dropdown__item" onclick="chooseOption(this)">{0}</li>'.format(
            option
        )
        for option in options
    )
    return (
        '<div class="el-select"><input class="el-input__inner" readonly '
        'onclick="openSelect(this)"><div class="el-select-dropdown" style="display:none">'
        "<ul>{0}</ul></div></div>".format(rendered)
    )


class WxsphFormListingTests(unittest.IsolatedAsyncioTestCase):
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

    async def _listing(self):
        await self.page.route(
            "**/wxsph/getCategoryProperties.json",
            lambda route: route.fulfill(
                content_type="application/json",
                body='{"attr":['
                '{"name":"面料材质"},{"name":"面料材质成分含量"},'
                '{"name":"里料材质"},{"name":"里料材质成分含量"},'
                '{"name":"材质成分含量"},{"name":"材质成分"},'
                '{"name":"适用场景"}]}'
            ),
        )
        await self.page.set_content(
            """
            <button role="tab" aria-selected="false" onclick="openTab(this)">
              微信小店（视频号）资料
            </button>
            <div role="tabpanel" aria-label="微信小店（视频号）资料" style="display:none">
              <h3>类目属性</h3>
              <div class="el-form-item is-required" id="fabric">
                <label class="el-form-item__label">面料材质</label>
                <div class="el-form-item__content">{fabric}</div>
              </div>
              <div class="el-form-item is-required" id="fabric-percent">
                <label class="el-form-item__label">面料材质成分含量</label>
                <div class="el-form-item__content"><input>{unit}</div>
              </div>
              <div class="el-form-item" id="lining">
                <label class="el-form-item__label">里料材质</label>
                <div class="el-form-item__content">{lining}</div>
              </div>
              <div class="el-form-item" id="lining-percent">
                <label class="el-form-item__label">里料材质成分含量</label>
                <div class="el-form-item__content"><input>{unit}</div>
              </div>
              <div class="el-form-item is-required" id="content-band">
                <label class="el-form-item__label">材质成分含量</label>
                <div class="el-form-item__content">{content_band}</div>
              </div>
              <div class="el-form-item is-required" id="material-content">
                <label class="el-form-item__label">材质成分</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item" id="scene">
                <label class="el-form-item__label">适用场景</label>
                <div class="el-form-item__content">{scene}</div>
              </div>
              <h3>规格明细</h3>
              <section id="batch-area">
                <div data-wxsph-batch="售卖价"><span>售卖价</span><input></div>
                <div data-wxsph-batch="市场价"><span>市场价</span><input></div>
                <div data-wxsph-batch="库存"><span>库存</span><input></div>
                <button id="batch" data-clicks="0" onclick="applyBatch()">批量设置</button>
                <button id="delivery-batch" data-clicks="0" onclick="openDelivery()">批量编辑发货</button>
              </section>
              <table id="sku-table">
                <thead><tr><th>颜色</th><th>尺码</th><th>售卖价</th><th>市场价</th><th>库存</th><th>发货方式</th></tr></thead>
                <tbody>
                  <tr><td>军绿色</td><td>S</td><td><input></td><td><input></td><td><input></td><td class="delivery-value">现货</td></tr>
                  <tr><td>军绿色</td><td>M</td><td><input></td><td><input></td><td><input></td><td class="delivery-value">现货</td></tr>
                </tbody>
              </table>
              <h3>运费模板</h3>
              <div class="el-form-item" id="weight">
                <label class="el-form-item__label">重量</label>
                <div class="el-form-item__content"><input value="0"></div>
              </div>
            </div>
            <div id="delivery-dialog" class="el-dialog" role="dialog" style="display:none">
              <div class="el-dialog__title">发货方式</div>
              <div><span>库存情况</span>
                <label class="el-radio"><input type="radio" name="stock"><span class="el-radio__label">现货</span></label>
                <label class="el-radio"><input type="radio" name="stock"><span class="el-radio__label">全款预售</span></label>
              </div>
              <div><span>发货节点</span>
                <label class="el-radio"><input type="radio" name="node"><span class="el-radio__label">买家付款后n天发货</span></label>
                <label class="el-radio"><input type="radio" name="node"><span class="el-radio__label">商家预售结束后n天发货</span></label>
              </div>
              <div><span>发货时效</span><span>买家付款后</span><input data-wxsph-presale-days><span>天内发货</span></div>
              <button onclick="closeDelivery()">取消</button>
              <button id="delivery-confirm" data-clicks="0" onclick="applyDelivery()">确定</button>
            </div>
            <script>
              function openTab(tab) {{
                tab.setAttribute('aria-selected', 'true');
                document.querySelector('[role=tabpanel]').style.display='block';
                fetch('https://scm.superboss.cc/wxsph/getCategoryProperties.json');
              }}
              function openSelect(input) {{
                document.querySelectorAll('.el-select-dropdown').forEach(node => node.style.display='none');
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }}
              window.addEventListener('keydown', event => {{
                if (event.key === 'Escape') document.querySelectorAll('.el-select-dropdown').forEach(
                  node => node.style.display='none'
                );
              }});
              function chooseOption(option) {{
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display='none';
              }}
              document.querySelectorAll('.el-select').forEach(select => {{
                select.__vue__ = {{
                  multiple: false,
                  handleOptionSelect(option) {{
                    const input = select.querySelector('input.el-input__inner');
                    input.value = option.value;
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                    select.querySelector('.el-select-dropdown').style.display='none';
                  }},
                  $nextTick(callback) {{ Promise.resolve().then(callback); }}
                }};
              }});
              function applyBatch() {{
                const values = [...document.querySelectorAll('#batch-area input')].map(input => input.value);
                document.querySelectorAll('#sku-table tbody tr').forEach(row => {{
                  [...row.querySelectorAll('input')].forEach((input, index) => input.value = values[index]);
                }});
                const button = document.getElementById('batch');
                button.dataset.clicks = String(Number(button.dataset.clicks) + 1);
              }}
              function openDelivery() {{
                document.getElementById('delivery-dialog').style.display = 'block';
                const button = document.getElementById('delivery-batch');
                button.dataset.clicks = String(Number(button.dataset.clicks) + 1);
              }}
              function closeDelivery() {{
                document.getElementById('delivery-dialog').style.display = 'none';
              }}
              function applyDelivery() {{
                document.querySelectorAll('.delivery-value').forEach(cell => {{
                  cell.textContent = '全款预售，付款后15天内发货';
                }});
                const button = document.getElementById('delivery-confirm');
                button.dataset.clicks = String(Number(button.dataset.clicks) + 1);
                closeDelivery();
              }}
            </script>
            """.format(
                fabric=select_markup(["棉", "聚酯纤维"]),
                lining=select_markup(["棉"]),
                content_band=select_markup(["51%-70%", "95%及以上"]),
                scene=select_markup(["日常", "户外"]),
                unit=select_markup(["%"]),
            )
        )
        listing = WxsphFormListing(self.page, self.page.locator("body"), None)
        await listing.open()
        return listing

    async def test_waits_for_category_attributes_that_render_after_tab_is_visible(self):
        await self.page.set_content(
            """
            <button id="wxsph-tab" role="tab" aria-selected="false"
                    onclick="openWxsph(this)">
              微信小店（视频号）资料
            </button>
            <div id="wxsph-panel" role="tabpanel"
                 aria-label="微信小店（视频号）资料"
                 style="display:none">
              <h3>类目属性</h3>
              <div id="attribute-root"></div>
              <h3>规格明细</h3>
            </div>
            <script>
              function openWxsph(tab) {
                tab.setAttribute('aria-selected', 'true');
                document.getElementById('wxsph-panel').style.display = 'block';
                setTimeout(() => {
                  if (document.getElementById('delayed-attribute')) return;
                  document.getElementById('attribute-root').insertAdjacentHTML(
                    'beforeend',
                    '<div id="delayed-attribute" class="el-form-item is-required">'
                    + '<label class="el-form-item__label">面料材质</label>'
                    + '<div class="el-form-item__content"><input></div></div>'
                  );
                }, 250);
              }
            </script>
            """
        )
        listing = WxsphFormListing(self.page, self.page.locator("body"), None)
        listing.attribute_wait_timeout_seconds = 2.0
        listing.attribute_retry_after_seconds = 1.0
        listing.attribute_stable_seconds = 0.2

        await listing.open()
        items = await listing._attribute_items()

        self.assertEqual(tuple(items), ("面料材质",))

    async def test_fills_attributes_batch_once_and_fixed_weight(self):
        listing = await self._listing()
        fields = parse_wxsph_fields(
            {
                "面料材质/水洗标/吊牌图/面料/面料俗称": "棉（100%）",
                "里料材质/里料": "棉",
                "里料材质成分含量/材质成分含量/面料材质成分含量": "95%及以上",
                "材质成分/材质": "棉（100%）",
                "适用场景": "日常",
                "吊牌价/价格/基本售价/商品价格": "586",
                "价格/京东价/市场价/售卖价/售价": "586",
                "数量": "100",
            }
        )

        report = await listing.apply_excel_fields(fields)

        self.assertEqual(report["attributes"]["attributes"]["面料材质"], ("棉",))
        self.assertEqual(
            report["attributes"]["attributes"]["材质成分含量"],
            ("95%及以上",),
        )
        self.assertEqual(
            await self.page.locator("#fabric-percent input:not([readonly])").input_value(), "100"
        )
        self.assertEqual(
            await self.page.locator("#lining-percent input:not([readonly])").input_value(), ""
        )
        self.assertEqual(
            await self.page.locator("#material-content input").input_value(), "棉100%"
        )
        self.assertIn(
            "里料材质成分含量",
            report["attributes"]["skipped_optional_percentages"],
        )
        self.assertEqual(
            report["sku_batch"]["values"],
            {"售卖价": "586", "市场价": "586", "库存": "100"},
        )
        self.assertEqual(report["sku_batch"]["row_count"], 2)
        self.assertEqual(await self.page.locator("#batch").get_attribute("data-clicks"), "1")
        self.assertEqual(
            report["delivery"]["rows"],
            (
                "全款预售，付款后15天内发货",
                "全款预售，付款后15天内发货",
            ),
        )
        self.assertEqual(report["delivery"]["days"], "15")
        self.assertEqual(
            await self.page.locator("#delivery-batch").get_attribute("data-clicks"),
            "1",
        )
        self.assertEqual(
            await self.page.locator("#delivery-confirm").get_attribute("data-clicks"),
            "1",
        )
        self.assertEqual(await self.page.locator("#weight input").input_value(), "1")
        self.assertEqual(report["api_dom_validation"]["status"], "matched_all")
        self.assertEqual(
            report["api_dom_validation"]["endpoints"][0]["path"],
            "/wxsph/getCategoryProperties.json",
        )

        persisted = await listing.verify_persisted_values(fields)
        self.assertEqual(
            persisted["sku_values"],
            {"售卖价": "586", "市场价": "586", "库存": "100"},
        )
        self.assertEqual(persisted["row_count"], 2)
        self.assertEqual(persisted["delivery"]["days"], "15")
        self.assertEqual(persisted["weight"], "1")
        self.assertEqual(await self.page.locator("#batch").get_attribute("data-clicks"), "1")

    async def test_post_save_readback_rejects_lost_sku_value_without_rewriting(self):
        listing = await self._listing()
        fields = parse_wxsph_fields(
            {
                "面料材质/水洗标/吊牌图/面料/面料俗称": "棉（100%）",
                "里料材质/里料": "棉",
                "里料材质成分含量/材质成分含量/面料材质成分含量": "95%及以上",
                "材质成分/材质": "棉（100%）",
                "适用场景": "日常",
                "吊牌价/价格/基本售价/商品价格": "586",
                "价格/京东价/市场价/售卖价/售价": "586",
                "数量": "100",
            }
        )
        await listing.apply_excel_fields(fields)
        lost_value = self.page.locator("#sku-table tbody tr").first.locator("input").first
        await lost_value.fill("0")

        with self.assertRaisesRegex(WxsphFormListingError, "批量设置后校验失败"):
            await listing.verify_persisted_values(fields)
        self.assertEqual(await lost_value.input_value(), "0")

    async def test_post_save_readback_rejects_lost_delivery_without_rewriting(self):
        listing = await self._listing()
        fields = parse_wxsph_fields(
            {
                "面料材质/水洗标/吊牌图/面料/面料俗称": "棉（100%）",
                "里料材质/里料": "棉",
                "里料材质成分含量/材质成分含量/面料材质成分含量": "95%及以上",
                "材质成分/材质": "棉（100%）",
                "适用场景": "日常",
                "吊牌价/价格/基本售价/商品价格": "586",
                "价格/京东价/市场价/售卖价/售价": "586",
                "数量": "100",
            }
        )
        await listing.apply_excel_fields(fields)
        lost_value = self.page.locator(".delivery-value").first
        await lost_value.evaluate("node => node.textContent = '现货'")

        with self.assertRaisesRegex(
            WxsphFormListingError, "批量发货设置后校验失败"
        ):
            await listing.verify_persisted_values(fields)
        self.assertEqual(await lost_value.inner_text(), "现货")


if __name__ == "__main__":
    unittest.main()
