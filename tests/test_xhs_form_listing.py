import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from xhs_data import XhsFields, parse_xhs_fields
from xhs_form_listing import XhsFormListing, title_without_neigborl


class XhsDataTests(unittest.TestCase):
    def test_category_slashes_are_an_ordered_route(self):
        fields = parse_xhs_fields(
            {
                "商品分类": "休闲裤/男士休闲直筒裤/工装休闲裤",
                "价格": 586.0,
            }
        )

        self.assertEqual(
            fields.category_path,
            ("休闲裤", "男士休闲直筒裤", "工装休闲裤"),
        )
        self.assertEqual(fields.fields["价格"], "586")

    def test_title_removes_only_forbidden_brand_token(self):
        self.assertEqual(
            title_without_neigborl("【绿巨人】NEIGBORL 钊叔制工装休闲裤"),
            "【绿巨人】钊叔制工装休闲裤",
        )


class XhsFormListingTests(unittest.IsolatedAsyncioTestCase):
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
        async def fulfill_category_query(route):
            await route.fulfill(
                content_type="application/json",
                headers={"access-control-allow-origin": "*"},
                json={
                    "data": {
                        "records": [
                            {
                                "categoryNameList": [
                                    "男装",
                                    "休闲裤",
                                    "工装休闲裤",
                                ]
                            }
                        ]
                    }
                },
            )

        await self.page.route(
            "**/category/base/queryCategoryList.json", fulfill_category_query
        )
        await self.page.set_content(
            """
            <button role="tab" aria-selected="false" onclick="
              this.setAttribute('aria-selected', 'true');
              document.querySelector('[role=tabpanel]').style.display='block';">小红书资料</button>
            <div role="tabpanel" aria-label="小红书资料" style="display:none">
              <h3>基础信息</h3>
              <div><span>商品分类：</span><button onclick="openCategory()">修改类目</button></div>
              <div data-xhs-field="商品标题"><span>商品标题：</span><input value="【绿巨人】NEIGBORL钊叔制工装休闲裤"></div>
              <div data-xhs-field="货号"><span>货号：</span><input value=""></div>
              <h3>商品属性：</h3>
              <div class="el-form-item"><label class="el-form-item__label">面料</label><div class="el-form-item__content">${select(['棉'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">厚薄</label><div class="el-form-item__content">${select(['常规'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">适用场景</label><div class="el-form-item__content">${select(['毕业答辩'])}</div></div>
              <div class="el-form-item is-required"><label class="el-form-item__label">上市年份季节</label><div class="el-form-item__content">${select(['2026年秋季'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">服装版型</label><div class="el-form-item__content">${select(['直筒'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">基础风格</label><div class="el-form-item__content">${select(['时尚都市'])}</div></div>
              <div class="el-form-item"><label class="el-form-item__label">裤长</label><div class="el-form-item__content">${select(['长裤'])}</div></div>
              <h3>价格库存</h3>
              <section id="delivery">
                <label class="el-radio"><input type="radio" name="delivery"><span class="el-radio__label">现货模式</span></label>
                <label class="el-radio"><input type="radio" name="delivery" onclick="showPresale()"><span class="el-radio__label">全款预售模式</span></label>
                <div id="presale" style="display:none">
                  <label class="el-radio"><input type="radio" name="presale"><span class="el-radio__label">时段预售</span></label>
                  <div><span>付款后：</span><input data-xhs-presale-days value=""><span>天发货</span></div>
                </div>
              </section>
              <section>
                <div data-xhs-batch-field="售价"><span>售价：</span><input value=""></div>
                <div data-xhs-batch-field="市场价"><span>市场价：</span><input value=""></div>
                <div data-xhs-batch-field="库存"><span>库存：</span><input value=""></div>
                <button onclick="applyBatch()">批量设置</button>
              </section>
              <table><thead><tr><th>颜色分类</th><th>售价</th><th>市场价</th><th>库存</th></tr></thead>
                <tbody><tr><td>黑灰</td><td><input></td><td><input></td><td><input></td></tr>
                  <tr><td>军绿色</td><td><input></td><td><input></td><td><input></td></tr></tbody></table>
              <h3>图文信息</h3>
              <div data-xhs-image-group="main"><span>主图：</span></div>
            </div>
            <div class="el-dialog" style="display:none"><div class="el-dialog__title">修改类目</div>
              <input oninput="showRoot(this.value)"><div id="nodes"><span class="el-cascader-node__label">不应扫描的整树节点</span></div><div id="chosen">已选：</div>
              <button onclick="closeCategory()">取 消</button><button onclick="confirmCategory()">确 定</button></div>
            <div id="portal-results" class="el-autocomplete-suggestion" style="display:none"></div>
            <script>
              function select(values) {
                return '<div class="el-select"><input class="el-input__inner" readonly onclick="openSelect(this)"><div class="el-select-dropdown" style="display:none"><ul>' + values.map(v => '<li class="el-select-dropdown__item" onclick="choose(this)">'+v+'</li>').join('') + '</ul></div></div>';
              }
              document.querySelectorAll('.el-form-item__content').forEach(node => {
                if (!node.innerHTML.trim()) node.innerHTML = select([]);
              });
              function openSelect(input) { input.closest('.el-select').querySelector('.el-select-dropdown').style.display = 'block'; }
              function choose(node) { const select = node.closest('.el-select'); select.querySelector('input').value = node.textContent.trim(); select.querySelector('.el-select-dropdown').style.display = 'none'; }
              document.addEventListener('keydown', event => {
                if (event.key === 'Escape') {
                  document.querySelectorAll('.el-select-dropdown').forEach(node => node.style.display = 'none');
                }
              });
              let path=[];
              function openCategory(){ document.querySelector('.el-dialog').style.display='block'; path=[]; document.getElementById('chosen').textContent='已选：'; }
              function closeCategory(){ document.querySelector('.el-dialog').style.display='none'; }
              function showRoot(value){
                const results = document.getElementById('portal-results');
                results.style.display = value === '休闲裤' ? 'block' : 'none';
                results.innerHTML = value === '休闲裤'
                  ? '<ul><li onclick="choosePortalCategory(this)">男装 > 休闲裤 > 工装休闲裤</li></ul>'
                  : '';
                if (value === '休闲裤') fetch('https://category.test/category/base/queryCategoryList.json');
              }
              function choosePortalCategory(node){
                path=['男装', '休闲裤', '工装休闲裤'];
                document.getElementById('chosen').textContent='已选：'+path.join(' > ');
                document.getElementById('portal-results').style.display='none';
              }
              function node(label, handler){ const n=document.createElement('span'); n.className='el-cascader-node__label'; n.textContent=label; n.onclick=handler; const root=document.getElementById('nodes'); root.innerHTML=''; root.appendChild(n); }
              function confirmCategory(){ document.querySelector('.el-dialog').style.display='none'; }
              function showPresale(){ document.getElementById('presale').style.display='block'; }
              function applyBatch(){ const get=l => document.querySelector('[data-xhs-batch-field="'+l+'"] input').value; document.querySelectorAll('tbody tr').forEach(row => { const cells=row.querySelectorAll('input'); cells[0].value=get('售价'); cells[1].value=get('市场价'); cells[2].value=get('库存'); }); }
            </script>
            """.replace("${select(['棉'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>棉</li></ul></div></div>")
            .replace("${select(['常规'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>常规</li></ul></div></div>")
            .replace("${select(['毕业答辩'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>毕业答辩</li></ul></div></div>")
            .replace("${select(['2026年秋季'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>2026年秋季</li></ul></div></div>")
            .replace("${select(['直筒'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>直筒</li></ul></div></div>")
            .replace("${select(['时尚都市'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>时尚都市</li></ul></div></div>")
            .replace("${select(['长裤'])}", "<div class='el-select'><input class='el-input__inner' readonly onclick='openSelect(this)'><div class='el-select-dropdown' style='display:none'><ul><li class='el-select-dropdown__item' onclick='choose(this)'>长裤</li></ul></div></div>")
        )
        listing = XhsFormListing(self.page, self.page.locator("body"), None)
        await listing.open()
        return listing

    async def test_fills_ordered_category_title_attributes_presale_batch_and_34_images(self):
        listing = await self._listing()
        uploads = []

        async def uploader(page, item, paths, label, timeout_seconds, **kwargs):
            uploads.append((tuple(paths), label, timeout_seconds, kwargs.get("force_replace")))
            return "replaced"

        report = await listing.apply_excel_fields(
            XhsFields(
                fields={
                    "面料材质/面料": "棉（100%）",
                    "厚度": "常规款",
                    "厚薄": "常规",
                    "适用场景": "日常",
                    "上市时间/上市年份季节": "2026/2026年秋季",
                    "服饰版型/版型": "直筒",
                    "风格/细分风格/基础风格": "休闲风/时尚都市",
                    "裤长": "长裤",
                    "价格/售价": "586",
                    "数量": "100",
                },
                category_path=("休闲裤", "男士休闲直筒裤", "工装休闲裤"),
            ),
            title="【绿巨人】NEIGBORL钊叔制工装休闲裤",
            style_code="NGBL-10588",
            portrait_paths=(Path("three-four-1.jpg"), Path("three-four-2.jpg")),
            timeout_seconds=30,
            uploader=uploader,
        )

        self.assertEqual(
            report["category"]["clicked_segments"],
            ("休闲裤", "男士休闲直筒裤", "工装休闲裤"),
        )
        self.assertEqual(report["category"]["strategy"], "first_and_leaf_exact")
        self.assertEqual(report["category"]["json_candidate_count"], 1)
        self.assertEqual(report["category"]["search_term"], "休闲裤")
        self.assertEqual(
            report["category"]["selected"],
            "男装 > 休闲裤 > 工装休闲裤",
        )
        self.assertEqual(report["identity"], {"商品标题": "【绿巨人】钊叔制工装休闲裤", "货号": "NGBL-10588"})
        self.assertEqual(await self.page.locator('[data-xhs-field="商品标题"] input').input_value(), "【绿巨人】钊叔制工装休闲裤")
        self.assertEqual(await self.page.locator('[data-xhs-field="货号"] input').input_value(), "NGBL-10588")
        self.assertEqual(report["attributes"]["attributes"]["面料"], ("棉",))
        self.assertEqual(report["attributes"]["attributes"]["厚薄"], ("常规",))
        self.assertEqual(
            report["attributes"]["skipped_no_exact_candidate"],
            {"适用场景": "日常"},
        )
        self.assertEqual(report["presale"], {"发货模式": "全款预售模式", "预售类型": "时段预售", "付款后": "15"})
        self.assertEqual(report["sku_batch"]["values"], {"售价": "586", "库存": "100"})
        self.assertEqual(report["sku_batch"]["row_count"], 2)
        self.assertEqual(
            uploads,
            [((Path("three-four-1.jpg"), Path("three-four-2.jpg")), "小红书3:4主图", 30, True)],
        )

    def test_adapter_has_no_save_or_publish_entrypoints(self):
        self.assertFalse(hasattr(XhsFormListing, "save"))
        self.assertFalse(hasattr(XhsFormListing, "publish"))

    async def test_required_attribute_without_exact_candidate_still_fails(self):
        listing = await self._listing()
        items = await listing._attribute_items()
        item = items["上市年份季节"][1]
        await item.locator(".el-select-dropdown__item").first.evaluate(
            "node => node.textContent = '2025年春季'"
        )

        with self.assertRaisesRegex(Exception, "没有 Excel 值的精确候选"):
            await listing._fill_attribute(
                "上市年份季节",
                item,
                "2026年秋季",
                required=True,
            )

    async def test_xhs_uses_unique_exact_dom_option_when_shared_select_skips_it(self):
        listing = await self._listing()
        items = await listing._attribute_items()
        item = items["厚薄"][1]

        with patch.object(
            listing, "_select_values", new=AsyncMock(return_value=None)
        ):
            actual = await listing._fill_attribute(
                "厚薄",
                item,
                "常规",
                required=False,
            )

        self.assertEqual(actual, ("常规",))
        self.assertEqual(
            await item.locator("input.el-input__inner").input_value(),
            "常规",
        )

    async def test_xhs_clicks_one_exact_created_remote_option_before_it_disappears(self):
        listing = await self._listing()
        items = await listing._attribute_items()
        item = items["厚薄"][1]
        await item.locator(".el-select-dropdown__item").first.evaluate(
            "node => node.__vue__ = {created: true, value: '常规'}"
        )

        with patch.object(
            listing,
            "_select_unique_exact_dom_option",
            new=AsyncMock(side_effect=AssertionError("fallback should not run")),
        ):
            actual = await listing._fill_attribute(
                "厚薄",
                item,
                "常规",
                required=False,
            )

        self.assertEqual(actual, ("常规",))


if __name__ == "__main__":
    unittest.main()
