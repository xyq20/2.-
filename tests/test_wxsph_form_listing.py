import json
import unittest

from attribute_runtime import ResolvedAttribute
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
    async def test_persisted_readback_uses_pre_save_resolved_values(self):
        listing = await self._listing()
        await self.page.locator('#fabric input').evaluate("e => e.value = '棉'")
        fields = parse_wxsph_fields({'面料材质': '旧 Excel 值'})
        report = await listing._verify_persisted_attributes(
            fields, expected_attributes={'面料材质': ('棉',)})
        self.assertEqual(report['attributes']['面料材质'], ('棉',))
        await self.page.locator('#fabric input').evaluate("e => e.value = '聚酯纤维'")
        with self.assertRaises(WxsphFormListingError):
            await listing._verify_persisted_attributes(
                fields, expected_attributes={'面料材质': ('棉',)})

    async def test_collapsed_multi_clears_actual_selection_by_dom(self):
        await self.page.set_content('''<div id="select" class="el-select">
          <div class="el-select__tags"><span class="el-tag">已选择 1</span></div>
          <input class="el-input__inner" readonly>
          <div class="el-select-dropdown"><ul>
          <li class="el-select-dropdown__item selected">明线</li>
          <li class="el-select-dropdown__item">做旧</li></ul></div></div>''')
        await self.page.locator('#select').evaluate("""e => {
          e.__vue__ = {selected: [{value:'明线', currentLabel:'明线'}]};
          e.querySelector('li').onclick = ev => {
            e.__vue__.selected = [];
            ev.currentTarget.classList.remove('selected');
          };
        }""")
        from unittest.mock import AsyncMock
        listing = object.__new__(WxsphFormListing)
        listing._open_select = AsyncMock()
        listing._dismiss_select_dropdown = AsyncMock()
        select = self.page.locator('#select')
        self.assertEqual(await listing._read_select_values(select, multi=True), ('明线',))
        await listing._clear_multi_select(select)
        self.assertEqual(await listing._read_select_values(select, multi=True), ())

    async def test_reviewed_label_uses_dom_not_conflicting_api_id(self):
        listing = await self._listing()
        item = self.page.locator('#fabric')
        await item.locator('li').nth(0).evaluate("e => e.textContent = '明线'")
        await item.locator('li').nth(1).evaluate("e => e.textContent = '做旧'")
        await item.locator('.el-select').evaluate("""e => {
          e.__vue__ = {cachedOptions: [
            {value: 'wrong-id', currentLabel: '明线'},
            {value: 'right-id', currentLabel: '做旧'}],
            handleOptionSelect() { throw new Error('must click DOM'); }};
        }""")
        listing._resolved_api_options['面料材质'] = {'做旧': ('wrong-id', '做旧')}
        result = await listing._apply_resolved_api_options('面料材质', item, ('做旧',))
        self.assertEqual(result, ('做旧',))

    async def test_approval_is_used_before_excel_write_attempt(self):
        from unittest.mock import AsyncMock
        class Runtime:
            def confirmed_choice(self, request):
                return ResolvedAttribute('cotton', '棉', 'human_override', 'approved')
        listing = await self._listing(Runtime())
        listing._excel_before_review = AsyncMock(side_effect=AssertionError('must not try old Excel'))
        result = await listing._resolve_learning_select_groups(
            '面料材质', self.page.locator('#fabric'), (('旧值',),))
        self.assertEqual(result, ('棉',))
        listing._excel_before_review.assert_not_awaited()

    async def test_registered_choice_survives_option_reordering(self):
        await self.page.set_content('<div id="select"></div>')
        select = self.page.locator('#select')
        await select.evaluate("""element => {
          element.__vue__ = {cachedOptions: [
            {value: 'line', currentLabel: '明线'},
            {value: 'aged', currentLabel: '做旧'}],
            handleOptionSelect(option) { window.selectedLabel = option.currentLabel; }};
        }""")
        listing = object.__new__(WxsphFormListing)
        self.assertTrue(await listing._apply_registered_vue_option(select,
            {'source': 'cachedOptions', 'index': '0', 'value': 'aged', 'name': '做旧'}))
        self.assertEqual(await self.page.evaluate('window.selectedLabel'), '做旧')

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

    async def _listing(self, attribute_runtime=None):
        response_body = json.dumps(
            {
                "attr": [
                    {"id": "fabric", "name": "面料材质", "inputType": "select", "values": [{"id": "cotton", "name": "棉"}, {"id": "polyester", "name": "聚酯纤维"}]},
                    {"id": "fabric-percent", "name": "面料材质成分含量"},
                    {"id": "lining", "name": "里料材质", "inputType": "select", "values": [{"id": "cotton", "name": "棉"}]},
                    {"id": "lining-percent", "name": "里料材质成分含量"},
                    {"id": "content-band", "name": "材质成分含量", "inputType": "select", "values": [{"id": "51-70", "name": "51%-70%"}, {"id": "95-plus", "name": "95%及以上"}]},
                    {"id": "material-content", "name": "材质成分"},
                    {"id": "scene", "name": "适用场景", "inputType": "select", "values": [{"id": "daily", "name": "日常"}, {"id": "outdoor", "name": "户外"}]},
                    {"id": "waist", "name": "腰型", "inputType": "select", "values": [{"id": "mid", "name": "中腰"}, {"id": "natural", "name": "自然腰"}]},
                ]
            },
            ensure_ascii=False,
        )
        await self.page.route(
            "**/wxsph/getCategoryProperties.json*",
            lambda route: route.fulfill(
                content_type="application/json",
                body=response_body,
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
              <div class="el-form-item" id="waist">
                <label class="el-form-item__label">腰型</label>
                <div class="el-form-item__content">{waist}</div>
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
                fetch('https://scm.superboss.cc/wxsph/getCategoryProperties.json?categoryId=545735');
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
                waist=select_markup(["中腰", "自然腰"]),
                unit=select_markup(["%"]),
            )
        )
        listing = WxsphFormListing(
            self.page,
            self.page.locator("body"),
            None,
            attribute_runtime=attribute_runtime,
        )
        await listing.open()
        return listing

    async def test_actively_fetches_current_schema_when_open_page_emits_no_request(self):
        async def route_request(route):
            url = route.request.url
            if "/wxsph/detail.json" in url:
                body = {"result": True, "data": {"categoryId": "545735"}}
            elif "/wxsph/getCategoryProperties.json" in url:
                body = {
                    "result": True,
                    "data": {
                        "attr": {
                            "productAttrList": [
                                {
                                    "name": "裤长",
                                    "type": "select_one",
                                    "typeV2": "select_one",
                                    "value": "短裤;长裤",
                                    "isRequired": "true",
                                }
                            ]
                        }
                    },
                }
            else:
                body = "<html><body>微信小店资料</body></html>"
                await route.fulfill(content_type="text/html", body=body)
                return
            await route.fulfill(
                content_type="application/json",
                body=json.dumps(body, ensure_ascii=False),
            )

        await self.page.route("https://scm.superboss.cc/**", route_request)
        await self.page.goto("https://scm.superboss.cc/supplier/prod/center")
        listing = WxsphFormListing(
            self.page,
            self.page.locator("body"),
            None,
            attribute_runtime=object(),
        )
        listing.base_item_id = "base-1"

        await listing._ensure_api_attribute_schema()
        field, category_id = await listing._captured_api_field("裤长")

        self.assertEqual(category_id, "545735")
        self.assertEqual(field.source_id, "裤长")
        self.assertEqual(
            tuple(option.label for option in field.option_values),
            ("短裤", "长裤"),
        )

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
              <nav class="anchor-nav"><button>类目属性</button><button>运费模板</button></nav>
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

    async def test_reactivates_shared_tab_once_when_attribute_json_was_not_requested(self):
        response_body = json.dumps(
            {
                "attr": [
                    {
                        "id": "length",
                        "name": "裤长",
                        "inputType": "select",
                        "values": [{"id": "long", "name": "长裤"}],
                    }
                ]
            },
            ensure_ascii=False,
        )
        await self.page.route(
            "**/wxsph/getCategoryProperties.json*",
            lambda route: route.fulfill(
                content_type="application/json",
                body=response_body,
            ),
        )
        await self.page.set_content(
            """
            <button id="base-tab" role="tab" aria-selected="true"
                    onclick="openBase()">基础资料</button>
            <button id="wxsph-tab" role="tab" aria-selected="false"
                    data-clicks="0" onclick="openWxsph(this)">
              微信小店（视频号）资料
            </button>
            <div role="tabpanel" aria-label="基础资料">基础资料</div>
            <div role="tabpanel" aria-label="微信小店（视频号）资料"
                 style="display:none">
              <h3>类目属性</h3>
              <div class="el-form-item is-required">
                <label class="el-form-item__label">裤长</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <h3>规格明细</h3>
            </div>
            <script>
              function openBase() {
                document.getElementById('base-tab').setAttribute('aria-selected', 'true');
                document.getElementById('wxsph-tab').setAttribute('aria-selected', 'false');
              }
              function openWxsph(tab) {
                document.getElementById('base-tab').setAttribute('aria-selected', 'false');
                tab.setAttribute('aria-selected', 'true');
                document.querySelector('[aria-label="微信小店（视频号）资料"]').style.display = 'block';
                const clicks = Number(tab.dataset.clicks) + 1;
                tab.dataset.clicks = String(clicks);
                if (clicks > 1) {
                  fetch('https://scm.superboss.cc/wxsph/getCategoryProperties.json?categoryId=545735');
                }
              }
            </script>
            """
        )

        class Runtime:
            pass

        listing = WxsphFormListing(
            self.page,
            self.page.locator("body"),
            None,
            attribute_runtime=Runtime(),
        )
        listing.attribute_stable_seconds = 0.1
        await listing.open()
        items = await listing._attribute_items()
        await listing._ensure_api_attribute_schema()
        field, category_id = await listing._captured_api_field("裤长")

        self.assertEqual(category_id, "545735")
        self.assertEqual(field.source_id, "length")
        self.assertEqual(
            await self.page.locator("#wxsph-tab").get_attribute("data-clicks"),
            "2",
        )

    async def test_empty_auxiliary_response_does_not_erase_captured_schema(self):
        class Response:
            status = 200

            def __init__(self, body):
                self.url = (
                    "https://scm.superboss.cc/wxsph/"
                    "getCategoryProperties.json?categoryId=545735"
                )
                self.body = body

            async def json(self):
                return self.body

        listing = WxsphFormListing(self.page, self.page.locator("body"), None)
        listing._api_observations = {}
        listing._api_capture_tasks = []
        path = "/wxsph/getCategoryProperties.json"
        await listing._capture_api_response(
            path,
            Response(
                {
                    "attr": [
                        {
                            "id": "length",
                            "name": "裤长",
                            "inputType": "select",
                            "values": [{"id": "long", "name": "长裤"}],
                        }
                    ]
                }
            ),
        )
        await listing._capture_api_response(path, Response({"attr": []}))

        field, category_id = await listing._captured_api_field("裤长")

        self.assertEqual(category_id, "545735")
        self.assertEqual(field.source_id, "length")

    async def test_open_preserves_schema_captured_before_drawer_was_rendered(self):
        response_body = json.dumps(
            {
                "attr": [
                    {
                        "id": "length",
                        "name": "裤长",
                        "inputType": "select",
                        "values": [{"id": "long", "name": "长裤"}],
                    }
                ]
            },
            ensure_ascii=False,
        )
        await self.page.route(
            "**/wxsph/getCategoryProperties.json*",
            lambda route: route.fulfill(
                content_type="application/json",
                body=response_body,
            ),
        )

        listing = WxsphFormListing(
            self.page,
            self.page.locator("body"),
            None,
            attribute_runtime=object(),
        )
        async with self.page.expect_response(
            lambda response: "getCategoryProperties.json" in response.url
        ):
            await self.page.set_content(
                """
            <button role="tab" aria-selected="true">
              微信小店（视频号）资料
            </button>
            <div role="tabpanel" aria-label="微信小店（视频号）资料">
              <h3>类目属性</h3>
              <div class="el-form-item is-required">
                <label class="el-form-item__label">裤长</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <h3>规格明细</h3>
            </div>
            <script>
              fetch('https://scm.superboss.cc/wxsph/getCategoryProperties.json?categoryId=545735');
            </script>
                """
            )
        listing.drawer = self.page.locator("body")
        await listing.open()
        field, category_id = await listing._captured_api_field("裤长")

        self.assertEqual(category_id, "545735")
        self.assertEqual(field.source_id, "length")

    async def test_new_presale_mode_reveals_batch_delivery(self):
        listing = await self._listing()
        await listing.panel.evaluate('''e => {
          e.insertAdjacentHTML('afterbegin', '<label class="el-radio"><input type="radio" name="new-mode">按规格预售</label>');
          const button = document.getElementById('delivery-batch');
          button.style.display = 'none';
          e.querySelector('input[name="new-mode"]').onchange = () => button.style.display = '';
        }''')
        result = await listing.apply_batch_delivery()
        self.assertTrue(await listing.panel.get_by_role('radio', name='按规格预售', exact=True).is_checked())
        self.assertEqual(result['days'], '15')

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
            await self.page.locator("#fabric-percent input:not([readonly])").input_value(), "95"
        )
        self.assertEqual(
            await self.page.locator("#lining-percent input:not([readonly])").input_value(), "95"
        )
        self.assertEqual(
            await self.page.locator("#material-content input").input_value(), "棉100%"
        )
        self.assertNotIn(
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

    async def test_learning_uses_api_candidate_ids_and_dom_cross_check(self):
        class Runtime:
            def __init__(self):
                self.requests = []

            async def resolve(self, request):
                self.requests.append(request)
                match = next(
                    candidate
                    for candidate in request.candidates
                    if candidate.label == request.excel_value
                )
                return ResolvedAttribute(
                    match.value_id,
                    match.label,
                    "explicit_text",
                    "snapshot-1",
                )

        runtime = Runtime()
        listing = await self._listing(runtime)
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

        self.assertEqual(
            [request.field_id for request in runtime.requests],
            ["fabric", "lining", "content-band", "scene"],
        )
        self.assertTrue(
            all(request.category_leaf_id == "545735" for request in runtime.requests)
        )
        self.assertEqual(
            tuple(
                (candidate.value_id, candidate.label)
                for candidate in runtime.requests[0].candidates
            ),
            (("cotton", "棉"), ("polyester", "聚酯纤维")),
        )

    async def test_ordered_or_select_chooses_first_live_candidate(self):
        class Runtime:
            def __init__(self):
                self.requests = []

            async def resolve(self, request):
                self.requests.append(request)
                match = next(
                    candidate
                    for candidate in request.candidates
                    if candidate.label == request.excel_value
                )
                return ResolvedAttribute(
                    match.value_id,
                    match.label,
                    "explicit_text",
                    "snapshot-1",
                )

        runtime = Runtime()
        listing = await self._listing(runtime)
        # A persisted later OR alternative must not outrank the first live
        # candidate from Excel on a subsequent run.
        await self.page.locator("#waist input").evaluate(
            "element => { element.value = '自然腰'; }"
        )
        fields = parse_wxsph_fields(
            {
                "面料材质/水洗标/吊牌图/面料/面料俗称": "棉（100%）",
                "里料材质/里料": "棉",
                "里料材质成分含量/材质成分含量/面料材质成分含量": "95%及以上",
                "材质成分/材质": "棉（100%）",
                "适用场景": "日常",
                "腰型": "中腰/自然腰",
                "吊牌价/价格/基本售价/商品价格": "586",
                "价格/京东价/市场价/售卖价/售价": "586",
                "数量": "100",
            }
        )

        report = await listing.apply_excel_fields(fields)

        self.assertEqual(report["attributes"]["attributes"]["腰型"], ("中腰",))
        waist_request = next(
            request for request in runtime.requests if request.field_id == "waist"
        )
        self.assertEqual(waist_request.excel_value, "中腰")

    async def test_material_slash_is_ordered_or_and_percentage_is_separate(self):
        class Runtime:
            def __init__(self):
                self.requests = []

            async def resolve(self, request):
                self.requests.append(request)
                match = next(
                    candidate
                    for candidate in request.candidates
                    if candidate.label == request.excel_value
                )
                return ResolvedAttribute(
                    match.value_id,
                    match.label,
                    "explicit_text",
                    "snapshot-material-or",
                )

        runtime = Runtime()
        listing = await self._listing(runtime)
        fields = parse_wxsph_fields(
            {
                "面料材质/水洗标/吊牌图/面料/面料俗称": "棉100%/棉/棉布",
                "里料材质/里料": "棉/棉混纺布",
                "里料材质成分含量/材质成分含量/面料材质成分含量": "95%及以上/95",
                "材质成分/材质": "棉100%/棉",
                "适用场景": "日常",
                "吊牌价/价格/基本售价/商品价格": "586",
                "价格/京东价/市场价/售卖价/售价": "586",
                "数量": "100",
            }
        )

        report = await listing.apply_excel_fields(fields)

        self.assertEqual(report["attributes"]["attributes"]["面料材质"], ("棉",))
        self.assertEqual(report["attributes"]["attributes"]["里料材质"], ("棉",))
        material_request = next(
            request for request in runtime.requests if request.field_id == "fabric"
        )
        lining_request = next(
            request for request in runtime.requests if request.field_id == "lining"
        )
        self.assertEqual(material_request.excel_value, "棉")
        self.assertEqual(lining_request.excel_value, "棉")
        self.assertEqual(
            await self.page.locator("#fabric-percent input:not([readonly])").input_value(),
            "95",
        )
        self.assertEqual(
            await self.page.locator("#lining-percent input:not([readonly])").input_value(),
            "95",
        )
        self.assertEqual(
            await self.page.locator("#material-content input").input_value(),
            "棉100%",
        )

    async def test_material_ordered_or_uses_api_option_outside_dom_viewport(self):
        class Runtime:
            async def resolve(self, request):
                match = next(
                    candidate
                    for candidate in request.candidates
                    if candidate.label == request.excel_value
                )
                return ResolvedAttribute(
                    match.value_id,
                    match.label,
                    "exact_excel",
                    "snapshot-virtual-material",
                )

        listing = await self._listing(Runtime())
        item = self.page.locator("#fabric")
        await item.locator(".el-select").evaluate(
            """element => {
              element.querySelector('input').value = '棉布';
              const options = Array.from(
                element.querySelectorAll('.el-select-dropdown__item')
              );
              options.find(option => option.textContent.trim() === '棉').remove();
              element.querySelector('ul').insertAdjacentHTML(
                'beforeend',
                '<li class="el-select-dropdown__item" onclick="chooseOption(this)">棉布</li>'
              );
            }"""
        )
        await listing._ensure_api_attribute_schema()

        resolved = await listing._resolve_learning_select_groups(
            "面料材质", item, (("棉", "棉布"),)
        )
        actual = await listing._fill_attribute(
            "面料材质",
            item,
            "棉/棉布",
            exact_values=resolved,
            required=True,
        )

        self.assertEqual(resolved, ("棉",))
        self.assertEqual(actual, ("棉",))

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
