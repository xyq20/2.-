import json
import unittest

from attribute_runtime import ResolvedAttribute
from pdd_data import PddFields
from pdd_form_listing import PddFormListing
from pdd_listing import parse_pdd_attribute_fields


class PddFormListingTests(unittest.IsolatedAsyncioTestCase):
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
        await self.page.route(
            "**/pdd/getCategoryProperties.json*",
            lambda route: route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "data": {
                            "goodsPropertiesRule": {
                                "properties": [
                                    {
                                        "refPid": "fit",
                                        "name": "版型",
                                        "required": True,
                                        "propertyValueType": "select",
                                        "values": [
                                            {"vid": "slim", "value": "修身"},
                                            {"vid": "loose", "value": "宽松"},
                                        ],
                                    }
                                ]
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        await self.page.set_content(
            """
            <button role="tab" aria-selected="false" onclick="
              this.setAttribute('aria-selected', 'true');
              document.querySelector('[role=tabpanel]').style.display='block';
              fetch('https://scm.superboss.cc/pdd/getCategoryProperties.json?leafCategoryId=7307');">
              拼多多资料
            </button>
            <div role="tabpanel" aria-label="拼多多资料" style="display:none">
              <div class="platform-category-input">
                <span class="current"></span>
                <div class="recommendation"><span>推荐</span><span>男装 &gt; 休闲裤</span>
                  <button onclick="document.querySelector('.current').textContent='男装 > 休闲裤'">点击使用</button>
                </div>
              </div>
              <section class="conf"><h3>类目属性</h3><div class="complex-wrap">
                <div class="complex-item"><div class="el-form-item">
                  <label class="el-form-item__label">版型</label><div class="el-form-item__content">
                    <div class="el-select"><input class="el-input__inner" readonly onclick="openSelect(this)">
                      <div class="el-select-dropdown" style="display:none"><ul>
                        <li class="el-select-dropdown__item" onclick="choose(this)">修身</li>
                        <li class="el-select-dropdown__item" onclick="choose(this)">宽松</li>
                      </ul></div>
                    </div>
                  </div>
                </div></div>
                <div class="complex-item"><div class="el-form-item">
                  <label class="el-form-item__label">裆部结构</label><div class="el-form-item__content">
                    <input id="crotch" value="">
                  </div>
                </div></div>
                <div class="complex-item"><div class="el-form-item">
                  <label class="el-form-item__label">商品货号</label><div class="el-form-item__content">
                    <input id="product-code" value="">
                  </div>
                </div></div>
              </div></section>
              <section><h3>商品轮播图</h3></section>
              <section><div class="el-form-item">
                <label class="el-form-item__label">运费设置</label>
                <div class="el-form-item__content"><input id="freight" value="0"></div>
              </div></section>
              <section class="pdd-batch">
                <div data-pdd-batch-field="拼单价"><label>拼单价</label><input value=""></div>
                <div data-pdd-batch-field="单买价"><label>单买价</label><input value=""></div>
                <div data-pdd-batch-field="库存"><label>库存</label><input value=""></div>
                <button onclick="applyBatch()">批量设置</button>
              </section>
              <table><thead><tr><th>颜色分类</th><th>拼单价</th><th>单买价</th><th>库存</th></tr></thead>
                <tbody><tr><td>黑灰</td><td><input></td><td><input></td><td><input></td></tr>
                <tr><td>军绿色</td><td><input></td><td><input></td><td><input></td></tr></tbody>
              </table>
              <div class="el-form-item"><label class="el-form-item__label">是否预售</label>
                <div class="el-form-item__content">
                  <label class="el-radio"><input type="radio" name="presale"><span class="el-radio__label">非预售</span></label>
                  <label class="el-radio"><input type="radio" name="presale"><span class="el-radio__label">时段预售</span></label>
                </div>
              </div>
              <div class="el-form-item"><label class="el-form-item__label">支付成功后</label>
                <div class="el-form-item__content"><div class="el-select"><input class="el-input__inner" readonly onclick="openSelect(this)">
                  <div class="el-select-dropdown" style="display:none"><ul>
                    <li class="el-select-dropdown__item" onclick="choose(this)">7天</li>
                    <li class="el-select-dropdown__item" onclick="choose(this)">15天</li>
                  </ul></div>
                </div></div>
              </div>
            </div>
            <script>
              function openSelect(input) { input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block'; }
              function choose(option) { const select = option.closest('.el-select'); select.querySelector('input').value = option.textContent.trim(); select.querySelector('.el-select-dropdown').style.display='none'; }
              function applyBatch() {
                const values = ['拼单价', '单买价', '库存'].map(label =>
                  document.querySelector('[data-pdd-batch-field="' + label + '"] input').value);
                document.querySelectorAll('tbody tr').forEach(row => row.querySelectorAll('input').forEach((input, index) => input.value = values[index]));
              }
            </script>
            """
        )
        listing = PddFormListing(
            self.page,
            self.page.locator("body"),
            None,
            attribute_runtime=attribute_runtime,
        )
        await listing.open()
        return listing

    async def test_parser_keeps_strict_pdd_field_and_option_ids(self):
        fields = parse_pdd_attribute_fields(
            {
                "result": True,
                "data": json.dumps(
                    {
                        "goodsPropertiesRule": {
                            "properties": [
                                {
                                    "refPid": "fit",
                                    "name": "版型",
                                    "values": [
                                        {"vid": "loose", "value": "宽松"}
                                    ],
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                ),
            }
        )

        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].source_id, "fit")
        self.assertEqual(fields[0].option_values[0].value_id, "loose")
        self.assertEqual(fields[0].option_values[0].label, "宽松")

    async def test_learning_uses_pdd_api_ids_and_dom_cross_check(self):
        class Runtime:
            def __init__(self):
                self.requests = []

            async def resolve(self, request):
                self.requests.append(request)
                chosen = next(
                    candidate
                    for candidate in request.candidates
                    if candidate.label == request.excel_value
                )
                return ResolvedAttribute(
                    chosen.value_id,
                    chosen.label,
                    "explicit_text",
                    "snapshot-pdd-1",
                )

        runtime = Runtime()
        listing = await self._listing(runtime)

        report = await listing.fill_category_attributes(
            PddFields(fields={"版型": "宽松"})
        )

        self.assertEqual(report["attributes"]["版型"], ("宽松",))
        self.assertEqual(len(runtime.requests), 1)
        request = runtime.requests[0]
        self.assertEqual(request.platform_id, "pdd")
        self.assertEqual(request.category_leaf_id, "7307")
        self.assertEqual(request.field_id, "fit")
        self.assertEqual(
            tuple((value.value_id, value.label) for value in request.candidates),
            (("slim", "修身"), ("loose", "宽松")),
        )

    async def test_existing_multi_select_value_learns_without_dropdown_open(self):
        class Runtime:
            def __init__(self):
                self.requests = []

            async def resolve(self, request):
                self.requests.append(request)
                chosen = next(
                    candidate
                    for candidate in request.candidates
                    if candidate.label == request.excel_value
                )
                return ResolvedAttribute(
                    chosen.value_id,
                    chosen.label,
                    "explicit_text",
                    "snapshot-pdd-existing",
                )

        runtime = Runtime()
        listing = await self._listing(runtime)
        async def fail_open(*_args, **_kwargs):
            raise AssertionError("当前值已匹配时不应再打开候选下拉框")
        listing._open_select = fail_open
        select = self.page.locator(
            ".complex-item:has(.el-form-item__label:text-is('版型')) .el-select"
        )
        await select.evaluate(
            """element => {
              element.style.position = 'relative';
              const tags = document.createElement('div');
              tags.className = 'el-select__tags el-select-collapsed__tags';
              tags.style.cssText = 'position:absolute;inset:0;z-index:2;background:white';
              tags.innerHTML = '<span class="el-tag">宽松</span>';
              element.prepend(tags);
            }"""
        )

        actual = await listing.fill_attribute("版型", "宽松")

        self.assertEqual(actual, ("宽松",))
        self.assertEqual(len(runtime.requests), 1)
        self.assertEqual(runtime.requests[0].excel_value, "宽松")

    async def test_collapsed_multi_select_opens_when_tag_covers_readonly_input(self):
        listing = await self._listing()
        select = self.page.locator(
            ".complex-item:has(.el-form-item__label:text-is('版型')) .el-select"
        )
        await select.evaluate(
            """element => {
              element.style.position = 'relative';
              const tags = document.createElement('div');
              tags.className = 'el-select__tags el-select-collapsed__tags';
              tags.style.cssText = 'position:absolute;inset:0;z-index:2;background:white';
              tags.innerHTML = '<span class="el-select__tags-text">日常</span>';
              element.prepend(tags);
            }"""
        )

        await listing._open_select(select, multi=True)

        self.assertTrue(
            await select.locator(".el-select-dropdown").is_visible()
        )

    async def test_fills_excel_attributes_skips_only_crotch_and_applies_batch_presale(self):
        listing = await self._listing()

        report = await listing.apply_excel_fields(
            PddFields(
                fields={
                    "版型": "宽松",
                    "裆部结构": "Excel 值不得写入",
                    "货号/商家外部编码": "NGBL-10588",
                    "拼单价": "598",
                    "单买价": "599",
                    "库存": "100",
                    "运费设置": "不应写入物流区",
                }
            )
        )

        self.assertEqual(report["attributes"]["category"], "男装 > 休闲裤")
        self.assertEqual(report["attributes"]["attributes"]["版型"], ("宽松",))
        self.assertEqual(
            report["attributes"]["attributes"]["商品货号"], ("NGBL-10588",)
        )
        self.assertEqual(report["attributes"]["ignored_fields"], ("裆部结构",))
        self.assertEqual(await self.page.locator("#crotch").input_value(), "")
        self.assertEqual(await self.page.locator("#product-code").input_value(), "NGBL-10588")
        self.assertEqual(await self.page.locator("#freight").input_value(), "0")
        self.assertNotIn("运费设置", report["attributes"]["attributes"])
        self.assertTrue(report["sku_batch"]["batch_clicked"])
        self.assertEqual(report["sku_batch"]["row_count"], 2)
        self.assertEqual(report["sku_batch"]["values"], {"拼单价": "598", "单买价": "599", "库存": "100"})
        self.assertEqual(report["timed_presale"], {"是否预售": "时段预售", "支付成功后": "15天"})
        self.assertTrue(await self.page.locator('input[name="presale"]').nth(1).is_checked())

    async def test_existing_category_is_retained_when_no_delayed_recommendation_arrives(self):
        listing = await self._listing()
        await self.page.locator(".current").evaluate(
            "node => node.textContent = '男装 > 休闲裤'"
        )
        await self.page.locator(".recommendation").evaluate("node => node.remove()")

        category = await listing.apply_recommended_category()

        self.assertEqual(category, "男装 > 休闲裤")

    async def test_batch_stock_uses_generic_quantity_not_douyin_spot_stock(self):
        listing = await self._listing()

        report = await listing.apply_excel_fields(
            PddFields(
                fields={
                    "版型": "宽松",
                    "拼单价": "598",
                    "单买价": "599",
                    "数量": "100",
                    "现货库存": "0",
                }
            )
        )

        self.assertEqual(report["sku_batch"]["values"]["库存"], "100")

    async def test_batch_values_are_applied_once_without_retyping_each_sku_row(self):
        listing = await self._listing()
        await self.page.locator("tbody input").evaluate_all(
            """inputs => inputs.forEach(input => {
              input.dataset.inputEventCount = '0';
              input.addEventListener('input', () => {
                input.dataset.inputEventCount = String(
                  Number(input.dataset.inputEventCount) + 1
                );
              });
            })"""
        )

        report = await listing.fill_price_inventory_batch(
            {"拼单价": "598", "单买价": "599", "库存": "100"}
        )

        self.assertTrue(report["batch_clicked"])
        input_event_counts = await self.page.locator("tbody input").evaluate_all(
            "inputs => inputs.map(input => Number(input.dataset.inputEventCount))"
        )
        self.assertEqual(
            input_event_counts,
            [0, 0, 0, 0, 0, 0],
        )

    async def test_presale_is_selected_before_price_inventory_batch(self):
        listing = await self._listing()
        await self.page.evaluate(
            """() => {
              window.pddActionOrder = [];
              document.querySelectorAll('input[name="presale"]')[1]
                .addEventListener('change', () => window.pddActionOrder.push('presale'));
              document.querySelector('.pdd-batch button')
                .addEventListener('click', () => window.pddActionOrder.push('batch'));
            }"""
        )

        await listing.apply_excel_fields(
            PddFields(
                fields={
                    "版型": "宽松",
                    "拼单价": "598",
                    "单买价": "599",
                    "库存": "100",
                }
            )
        )

        self.assertEqual(
            await self.page.evaluate("window.pddActionOrder"),
            ["presale", "batch"],
        )

    async def test_persisted_price_check_waits_for_async_sku_rows(self):
        listing = await self._listing()
        await self.page.evaluate(
            """() => {
              const body = document.querySelector('tbody');
              body.innerHTML = '<tr><td></td><td><input></td><td><input></td><td><input></td></tr>';
              setTimeout(() => {
                body.innerHTML = [0, 1].map(() =>
                  '<tr><td>军绿色</td><td><input value="598"></td>' +
                  '<td><input value="599"></td><td><input value="100"></td></tr>'
                ).join('');
              }, 200);
            }"""
        )

        report = await listing.verify_persisted_price_inventory(
            {"拼单价": "598", "单买价": "599", "库存": "100"}
        )

        self.assertEqual(report["row_count"], 2)

    async def test_transient_batch_validation_message_is_awaited(self):
        listing = await self._listing()
        await self.page.locator(".pdd-batch button").evaluate(
            """button => button.addEventListener('click', () => {
              const error = document.createElement('div');
              error.className = 'el-form-item__error';
              error.textContent = '最小0.01';
              button.parentElement.appendChild(error);
              setTimeout(() => error.remove(), 200);
            })"""
        )

        report = await listing.apply_excel_fields(
            PddFields(
                fields={
                    "版型": "宽松",
                    "拼单价": "598",
                    "单买价": "599",
                    "库存": "100",
                }
            )
        )

        self.assertTrue(report["sku_batch"]["batch_clicked"])

    async def test_inherited_product_category_is_never_an_attribute_assignment(self):
        listing = await self._listing()

        assignments = await listing._attribute_assignments(
            {"商品分类": "休闲裤", "版型": "宽松"},
            {
                "商品分类": ("商品分类", None),
                "版型": ("版型", None),
            },
        )

        self.assertNotIn("商品分类", assignments)
        self.assertEqual(assignments["版型"], ("版型", "宽松"))

    def test_known_pdd_option_formats_are_converted_before_matching(self):
        from pdd_form_listing import _special_category_values

        fields = {"材质成分": "棉（100%）", "是否加绒": "否"}

        self.assertEqual(_special_category_values(fields, "面料俗称", "棉（100%)"), ("棉",))
        self.assertEqual(_special_category_values(fields, "材质", "棉（100%)"), ("棉",))
        self.assertEqual(_special_category_values(fields, "是否加绒", "否"), ("不加绒",))

    def test_adapter_has_no_save_or_publish_entrypoints(self):
        self.assertFalse(hasattr(PddFormListing, "save"))
        self.assertFalse(hasattr(PddFormListing, "publish"))


if __name__ == "__main__":
    unittest.main()
