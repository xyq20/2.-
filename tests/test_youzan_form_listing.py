import unittest

from youzan_data import parse_youzan_fields
from youzan_form_listing import YouzanFormListing, YouzanFormListingError


def select_markup(options, *, empty_message=False):
    rendered = "".join(
        '<li class="el-select-dropdown__item" onclick="chooseOption(this)">{0}</li>'.format(
            option
        )
        for option in options
    )
    empty = '<p class="el-select-dropdown__empty">无数据</p>' if empty_message else ""
    return (
        '<div class="el-select"><input class="el-input__inner" readonly '
        'onclick="openSelect(this)"><div class="el-select-dropdown" style="display:none">'
        "<ul>{0}</ul>{1}</div></div>".format(rendered, empty)
    )


class YouzanFormListingTests(unittest.IsolatedAsyncioTestCase):
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

    async def _listing(
        self,
        *,
        duplicate_category=False,
        duplicate_freight=False,
        empty_style=False,
        empty_freight=False,
    ):
        async def fulfill_category_query(route):
            await route.fulfill(
                content_type="application/json",
                headers={"access-control-allow-origin": "*"},
                json={
                    "data": {
                        "records": [
                            {
                                "categoryNameList": [
                                    "服装鞋包",
                                    "男装",
                                    "休闲裤",
                                ]
                            }
                        ]
                    }
                },
            )

        await self.page.route("**/category/base/queryCategoryList.json", fulfill_category_query)
        category_rows = (
            '<li class="category-result">服装鞋包 &gt; 男装 &gt; 休闲裤</li>'
            * (2 if duplicate_category else 1)
        )
        freight_options = [
            "T恤、裤子、饰品邮费模版",
            "鞋子、皮衣、外套邮费模版",
        ]
        if duplicate_freight:
            freight_options.append("T恤、裤子、饰品邮费模版")
        await self.page.set_content(
            """
            <button role="tab" aria-selected="false" onclick="openTab(this)">有赞资料</button>
            <div role="tabpanel" aria-label="有赞资料" style="display:none">
              <section>
                <div class="el-form-item" id="product-type">
                  <label class="el-form-item__label">商品类型</label>
                  <div class="el-form-item__content">
                    <label class="el-radio"><input type="radio" name="type"><span class="el-radio__label">实物商品</span></label>
                  </div>
                </div>
                <div class="platform-category-input"><span class="current"></span><button onclick="openCategory()">修改类目</button></div>
              </section>
              <h3>类目参数</h3>
              <div class="el-form-item is-required" id="material">
                <label class="el-form-item__label">面料</label>
                <div class="el-form-item__content">{material_select}</div>
              </div>
              <div class="el-form-item is-required" id="outer-id">
                <label class="el-form-item__label">货号</label>
                <div class="el-form-item__content"><input></div>
              </div>
              <div class="el-form-item" id="style">
                <label class="el-form-item__label">基础风格</label>
                <div class="el-form-item__content">{style_select}</div>
              </div>
              <div class="el-form-item is-required" id="product-image">
                <label class="el-form-item__label">商品图</label>
                <div class="el-form-item__content"><img alt="已有商品图"></div>
              </div>
              <h3>规格明细</h3>
              <section id="batch-area">
                <div data-youzan-batch="价格"><span>价格</span><input></div>
                <div data-youzan-batch="库存"><span>库存</span><input></div>
                <div data-youzan-batch="重量(kg)"><span>重量(kg)</span><input></div>
                <button id="batch" data-clicks="0" onclick="applyBatch()">批量设置</button>
              </section>
              <table id="sku-table">
                <thead><tr><th>颜色</th><th>尺码</th><th>价格</th><th>库存</th><th>重量(kg)</th></tr></thead>
                <tbody>
                  <tr><td>军绿色</td><td>S</td><td><input></td><td><input></td><td><input></td></tr>
                  <tr><td>军绿色</td><td>M</td><td><input></td><td><input></td><td><input></td></tr>
                </tbody>
              </table>
              <div class="el-form-item" id="weight">
                <label class="el-form-item__label">重量</label>
                <div class="el-form-item__content"><input value="0.000"></div>
              </div>
              <div class="el-form-item" id="deduction">
                <label class="el-form-item__label">库存扣减方式</label>
                <div class="el-form-item__content">
                  <label class="el-radio"><input type="radio" name="deduction"><span class="el-radio__label">拍下减库存</span></label>
                  <label class="el-radio"><input type="radio" name="deduction"><span class="el-radio__label">付款减库存</span></label>
                </div>
              </div>
              <div class="el-form-item" id="delivery">
                <label class="el-form-item__label">配送方式</label>
                <div class="el-form-item__content">
                  <label class="el-checkbox"><input type="checkbox"><span class="el-checkbox__label">快递发货</span></label>
                  <label class="el-checkbox"><input type="checkbox"><span class="el-checkbox__label">同城配送</span></label>
                </div>
              </div>
              <div class="el-form-item" id="freight">
                <label class="el-form-item__label">运费设置</label>
                <div class="el-form-item__content">{freight_select}</div>
              </div>
            </div>
            <div class="el-dialog" role="dialog" aria-label="修改类目" style="display:none">
              <input data-category-search oninput="showCategory(this.value)">
              <ul id="category-results" style="display:none">{category_rows}</ul>
              <button onclick="confirmCategory()">确定</button>
            </div>
            <script>
              function openTab(tab) {{
                tab.setAttribute('aria-selected', 'true');
                document.querySelector('[role=tabpanel]').style.display='block';
              }}
              function openCategory() {{
                document.querySelector('.el-dialog').style.display='block';
              }}
              function showCategory(value) {{
                document.getElementById('category-results').style.display = value === '休闲裤' ? 'block' : 'none';
                if (value === '休闲裤') fetch('https://category.test/category/base/queryCategoryList.json');
              }}
              document.querySelectorAll('.category-result').forEach(row => row.onclick = () => {{
                document.querySelectorAll('.category-result').forEach(other => other.classList.remove('is-selected'));
                row.classList.add('is-selected');
              }});
              function confirmCategory() {{
                const selected = document.querySelector('.category-result.is-selected');
                if (!selected) return;
                document.querySelector('.platform-category-input .current').textContent = selected.textContent.trim();
                document.querySelector('.el-dialog').style.display='none';
              }}
              function openSelect(input) {{
                document.querySelectorAll('.el-select-dropdown').forEach(node => node.style.display='none');
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display='block';
              }}
              window.addEventListener('keydown', event => {{
                if (event.key === 'Escape') {{
                  document.querySelectorAll('.el-select-dropdown').forEach(
                    node => node.style.display='none'
                  );
                }}
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
              document.querySelectorAll('label.el-radio').forEach(label => label.onclick = () => {{
                label.querySelector('input').checked = true;
              }});
            </script>
            """.format(
                material_select=select_markup(["棉"]),
                style_select=select_markup(
                    [] if empty_style else ["休闲风", "时尚都市"],
                    empty_message=empty_style,
                ),
                freight_select=select_markup(
                    [] if empty_freight else freight_options,
                    empty_message=empty_freight,
                ),
                category_rows=category_rows,
            )
        )
        listing = YouzanFormListing(self.page, self.page.locator("body"), None)
        await listing.open()
        return listing

    async def test_selects_physical_product_and_exact_category(self):
        listing = await self._listing()

        physical = await listing.select_physical_product()
        category = await listing.apply_category(
            ("休闲裤", "男士休闲直筒裤", "工装休闲裤")
        )

        self.assertEqual(physical["product_type"], "实物商品")
        self.assertEqual(category["search_term"], "休闲裤")
        self.assertEqual(category["selected"], "服装鞋包 > 男装 > 休闲裤")

    async def test_rejects_duplicate_exact_category_results(self):
        listing = await self._listing(duplicate_category=True)

        with self.assertRaisesRegex(YouzanFormListingError, "有赞类目接口或 DOM 完整路径不是唯一项"):
            await listing.apply_category(("休闲裤",))

    async def test_fills_attributes_and_clicks_batch_once(self):
        listing = await self._listing()
        fields = parse_youzan_fields(
            {
                "商品分类": "休闲裤/男士休闲直筒裤/工装休闲裤",
                "面料材质/面料": "棉",
                "货号/商家外部编码": "NGBL-10588",
                "风格/细分风格/基础风格": "休闲风/时尚都市",
                "价格/一口价/基本售价": "586",
                "数量": "100",
            }
        )

        attributes = await listing.fill_category_attributes(fields, style_code="NGBL-10588")
        batch = await listing.fill_sku_batch(fields.fields)

        self.assertEqual(attributes["attributes"]["面料"], ("棉",))
        self.assertEqual(attributes["attributes"]["货号"], ("NGBL-10588",))
        self.assertEqual(batch["values"], {"价格": "586", "库存": "100", "重量(kg)": "1"})
        self.assertEqual(batch["row_count"], 2)
        self.assertEqual(await self.page.locator("#batch").get_attribute("data-clicks"), "1")

    async def test_directly_enters_first_excel_value_when_select_has_no_data(self):
        listing = await self._listing(empty_style=True)
        fields = parse_youzan_fields(
            {
                "商品分类": "休闲裤/男士休闲直筒裤/工装休闲裤",
                "面料材质/面料": "棉",
                "货号/商家外部编码": "NGBL-10588",
                "风格/细分风格/基础风格": "休闲裤/工装裤/直筒裤",
                "价格/一口价/基本售价": "586",
                "数量": "100",
            }
        )

        attributes = await listing.fill_category_attributes(
            fields, style_code="NGBL-10588"
        )

        self.assertEqual(attributes["attributes"]["基础风格"], ("休闲裤",))
        self.assertEqual(await self.page.locator("#style input").input_value(), "休闲裤")

        persisted = await listing._verify_persisted_attributes(fields)
        self.assertEqual(persisted["基础风格"], ("休闲裤",))
        await self.page.locator("#style input").evaluate(
            "element => { element.value = ''; }"
        )
        with self.assertRaisesRegex(YouzanFormListingError, "保存后类目属性回读失败"):
            await listing._verify_persisted_attributes(fields)

    async def test_fills_pants_sales_and_logistics(self):
        listing = await self._listing()

        report = await listing.fill_sales_and_logistics("pants")

        self.assertEqual(
            report,
            {
                "weight": "1",
                "inventory_deduction": "付款减库存",
                "delivery": ("快递发货",),
                "freight_template": "T恤、裤子、饰品邮费模版",
            },
        )

    async def test_fills_coat_freight_and_rejects_duplicate_pants_template(self):
        coat_listing = await self._listing()
        coat = await coat_listing.fill_sales_and_logistics("coat")
        self.assertEqual(coat["freight_template"], "鞋子、皮衣、外套邮费模版")

        duplicate_listing = await self._listing(duplicate_freight=True)
        with self.assertRaisesRegex(YouzanFormListingError, "有赞运费模板候选不是唯一项"):
            await duplicate_listing.fill_sales_and_logistics("pants")

    async def test_directly_enters_freight_template_when_dropdown_has_no_data(self):
        listing = await self._listing(empty_freight=True)

        report = await listing.fill_sales_and_logistics("pants")

        self.assertEqual(report["freight_template"], "T恤、裤子、饰品邮费模版")
        self.assertEqual(
            await self.page.locator("#freight input").input_value(),
            "T恤、裤子、饰品邮费模版",
        )

    async def test_verifies_critical_values_without_rewriting_batch(self):
        listing = await self._listing()
        fields = parse_youzan_fields(
            {
                "商品分类": "休闲裤/男士休闲直筒裤/工装休闲裤",
                "价格/一口价/基本售价": "586",
                "数量": "100",
            }
        )
        await listing.apply_category(fields.category_path)
        await listing.fill_sku_batch(fields.fields)
        await listing.fill_sales_and_logistics(fields.garment_kind)

        report = await listing.verify_persisted_values(fields)

        self.assertEqual(report["category"], "服装鞋包 > 男装 > 休闲裤")
        self.assertEqual(report["row_count"], 2)
        self.assertEqual(report["weight"], "1")
        self.assertEqual(await self.page.locator("#batch").get_attribute("data-clicks"), "1")


if __name__ == "__main__":
    unittest.main()
