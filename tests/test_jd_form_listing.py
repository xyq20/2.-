"""Tests for JD platform form filling and verification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from attribute_runtime import ResolvedAttribute
from jd_data import JdFields, parse_jd_fields
from jd_form_listing import (
    JD_BRAND,
    JD_CATEGORY_PATH,
    JD_DELIVERY_TEMPLATE,
    JD_SKU_THICKNESS,
    JdFormListing,
    JdFormListingError,
    _category_parts,
    classify_image_indices_by_color,
    _expand_cascader_candidates,
    _kilograms,
    _material_name,
    _material_percentage,
    _numeric_equal,
    parse_jd_attribute_fields,
)
from taobao_listing import selection_value_groups


class JdFormListingHelperTests(unittest.TestCase):
    def test_numeric_equal(self):
        self.assertTrue(_numeric_equal("100", "100"))
        self.assertTrue(_numeric_equal("100.00", "100"))
        self.assertTrue(_numeric_equal("0.43", "0.430"))
        self.assertTrue(_numeric_equal("586", "586.00"))
        self.assertFalse(_numeric_equal("100", "101"))
        self.assertFalse(_numeric_equal("abc", "123"))

    def test_material_name(self):
        self.assertEqual(_material_name("棉"), "棉")
        self.assertEqual(_material_name("棉（100%）"), "棉")
        self.assertEqual(_material_name("棉 (100%)"), "棉")
        self.assertEqual(_material_name("涤纶（50%）"), "涤纶")
        self.assertEqual(_material_name("棉 ( 100 % )"), "棉")

    def test_material_percentage(self):
        self.assertEqual(_material_percentage("棉（100%）"), "100")
        self.assertEqual(_material_percentage("棉 (50%)"), "50")
        self.assertEqual(_material_percentage("涤纶50%"), "50")
        self.assertIsNone(_material_percentage("棉"))
        self.assertIsNone(_material_percentage("100"))

    def test_kilograms(self):
        self.assertEqual(_kilograms("430g"), "0.43")
        self.assertEqual(_kilograms("430克"), "0.43")
        self.assertEqual(_kilograms("0.43kg"), "0.43")
        self.assertEqual(_kilograms("0.43公斤"), "0.43")
        self.assertEqual(_kilograms("1000g"), "1")
        self.assertEqual(_kilograms("1kg"), "1")
        with self.assertRaisesRegex(JdFormListingError, "无法换算"):
            _kilograms("abc")
        with self.assertRaisesRegex(JdFormListingError, "必须大于 0"):
            _kilograms("0g")

    def test_category_parts(self):
        self.assertEqual(
            _category_parts("服饰内衣>男装>男士休闲裤>男士休闲直筒裤"),
            ("服饰内衣", "男装", "男士休闲裤", "男士休闲直筒裤"),
        )
        self.assertEqual(
            _category_parts("服饰内衣/男装/男士休闲裤"),
            ("服饰内衣", "男装", "男士休闲裤"),
        )
        self.assertEqual(_category_parts("服饰内衣 > 男装"), ("服饰内衣", "男装"))

    def test_parse_jd_fields(self):
        jd_fields = parse_jd_fields(
            {
                "品牌": "NEIGBORL",
                "货号": "NGBL-10588",
                "产地": "中国大陆",
                "克重": "430g",
                "京东价": "586",
                "库存": "100",
                "面料": "棉",
                "材质": "棉（100%）",
                "裤长": "长裤",
                "": "",
                "空值": None,
            }
        )
        self.assertIsInstance(jd_fields, JdFields)
        self.assertEqual(jd_fields.fields["品牌"], "NEIGBORL")
        self.assertEqual(jd_fields.fields["货号"], "NGBL-10588")
        self.assertEqual(jd_fields.fields["产地"], "中国大陆")
        self.assertEqual(jd_fields.fields["克重"], "430g")
        self.assertEqual(jd_fields.fields["京东价"], "586")
        self.assertEqual(jd_fields.fields["库存"], "100")
        self.assertEqual(jd_fields.fields["面料"], "棉")
        self.assertEqual(jd_fields.fields["材质"], "棉（100%）")
        self.assertEqual(jd_fields.fields["裤长"], "长裤")
        self.assertNotIn("", jd_fields.fields)
        self.assertNotIn("空值", jd_fields.fields)

    def test_jd_fields_immutable(self):
        jd_fields = parse_jd_fields({"品牌": "NEIGBORL", "货号": "NGBL-10588"})
        with self.assertRaises(TypeError):
            jd_fields.fields["品牌"] = "OTHER"
        with self.assertRaises(AttributeError):
            jd_fields.fields.new_field = "value"

    def test_constants(self):
        self.assertEqual(
            JD_CATEGORY_PATH,
            ("服饰内衣", "男装", "男士休闲裤", "男士休闲直筒裤"),
        )
        self.assertEqual(JD_BRAND, "NEIGBORL")
        self.assertEqual(JD_DELIVERY_TEMPLATE, "48小时发货")
        self.assertEqual(JD_SKU_THICKNESS, "常规")

    def test_json_category_nodes(self):
        payload = {
            "data": {
                "categories": [
                    {"id": 1, "name": "服饰内衣", "children": []},
                    {"id": 2, "name": "男装", "children": []},
                    {"id": 3, "name": "男士休闲直筒裤", "parent": "男士休闲裤"},
                ]
            }
        }
        nodes = JdFormListing._json_category_nodes(payload, "男士休闲直筒裤")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["name"], "男士休闲直筒裤")

    def test_json_contains_category(self):
        payload = {"categories": [{"name": "男士休闲直筒裤"}]}
        self.assertTrue(JdFormListing._json_contains_category(payload, "男士休闲直筒裤"))
        self.assertFalse(
            JdFormListing._json_contains_category(payload, "不存在的类目")
        )

    def test_parses_strict_jd_attribute_ids_and_option_ids(self):
        fields = parse_jd_attribute_fields(
            {
                "success": True,
                "data": json.dumps(
                    {
                        "properties": [
                            {
                                "propId": "pants-length",
                                "propertyName": "裤长",
                                "propertyValues": [
                                    {"valueId": "short", "valueName": "短裤"},
                                    {"valueId": "long", "valueName": "长裤"},
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            }
        )

        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].source_id, "pants-length")
        self.assertEqual(fields[0].label, "裤长")
        self.assertEqual(
            tuple((value.value_id, value.label) for value in fields[0].option_values),
            (("short", "短裤"), ("long", "长裤")),
        )

    def test_style_leaf_aliases_map_casual_to_simple(self):
        self.assertEqual(
            _expand_cascader_candidates("风格", ("休闲风", "时尚都市")),
            ("休闲风", "简约风", "时尚都市"),
        )

    def test_classifies_two_colors_and_keeps_square_portrait_order_paired(self):
        import cv2
        import numpy as np

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            square_dir = root / "square"
            portrait_dir = root / "portrait"
            square_dir.mkdir()
            portrait_dir.mkdir()

            def write(path: Path, bgr: tuple[int, int, int]) -> Path:
                image = np.full((120, 120, 3), bgr, dtype=np.uint8)
                self.assertTrue(cv2.imwrite(str(path), image))
                return path

            square = [
                write(square_dir / "1.png", (40, 110, 45)),
                write(square_dir / "2.png", (150, 60, 25)),
                write(square_dir / "3.png", (45, 120, 50)),
                write(square_dir / "4.png", (160, 65, 30)),
            ]
            portrait = [
                write(portrait_dir / "1.png", (40, 110, 45)),
                write(portrait_dir / "2.png", (150, 60, 25)),
                write(portrait_dir / "3.png", (45, 120, 50)),
                write(portrait_dir / "4.png", (160, 65, 30)),
            ]
            refs = [
                write(root / "green-ref.png", (42, 115, 48)),
                write(root / "blue-ref.png", (155, 62, 27)),
            ]
            self.assertEqual(
                classify_image_indices_by_color(square, portrait, refs, 2),
                ((0, 2), (1, 3)),
            )

    def test_rejects_unpaired_square_and_portrait_image_order(self):
        with self.assertRaisesRegex(JdFormListingError, "文件序号不一致"):
            classify_image_indices_by_color(
                [Path("/input/square/1.png")],
                [Path("/input/portrait/2.png")],
                [Path("/input/sku/1.png")],
                1,
            )


STYLE_CASCADER_FIXTURE = """
<style>.el-cascader-menu { display:inline-block; vertical-align:top; border:1px solid #ccc; min-width:80px; }</style>
<button role="tab" aria-selected="false" onclick="
  this.setAttribute('aria-selected','true');
  document.querySelector('[role=tabpanel]').style.display='block';
">京东资料</button>
<div role="tabpanel" aria-label="京东资料" style="display:none">
  <div class="el-form-item is-required">
    <label class="el-form-item__label">风格</label>
    <div class="el-form-item__content">
      <div class="el-cascader">
        <input class="el-input__inner" readonly value="" onclick="openCascader()">
      </div>
    </div>
  </div>
</div>
<div id="dropdown" style="display:none"></div>
<script>
  const children = {
    '休闲风': ['简约风', 'oversize'],
    '工装': ['机能']
  };
  function openCascader() {
    const dropdown = document.getElementById('dropdown');
    dropdown.style.display = 'block';
    dropdown.innerHTML = menu(Object.keys(children), true);
  }
  function menu(names, parents) {
    return '<div class="el-cascader-menu" style="display:block">' + names.map((name, index) => {
      const arrow = parents ? '<i class="el-icon-arrow-right"></i>' : '';
      const handler = parents ? "selectParent(this,'"+name+"')" : "selectLeaf(this,'"+name+"')";
      return '<div class="el-cascader-node" onclick="'+handler+'"><span class="el-cascader-node__label">'+name+'</span>'+arrow+'</div>';
    }).join('') + '</div>';
  }
  function selectParent(node, name) {
    node.classList.add('is-active');
    const dropdown = document.getElementById('dropdown');
    const second = dropdown.querySelectorAll('.el-cascader-menu')[1];
    if (second) second.remove();
    dropdown.insertAdjacentHTML('beforeend', menu(children[name], false));
    const input = document.querySelector('.el-cascader input');
    input.value = name;
    input.dataset.parent = name;
  }
  function selectLeaf(node, name) {
    node.classList.add('is-active');
    const input = document.querySelector('.el-cascader input');
    input.value = (input.dataset.parent || '') + ' / ' + name;
    document.getElementById('dropdown').style.display = 'none';
  }
</script>
"""


IMAGE_FIXTURE = """
<style>.del-btn { display:none } .file-img:hover .del-btn { display:block }</style>
<button role="tab" aria-selected="false" onclick="
  this.setAttribute('aria-selected','true');
  document.querySelector('[role=tabpanel]').style.display='block';
">京东资料</button>
<div role="tabpanel" aria-label="京东资料" style="display:none">
  <!-- Real JD page repeats this section title; the action text remains unique. -->
  <div class="duplicate-section-title">商品图片</div>
  <section>
    <div class="row">
      <div>商品图片</div>
      <div class="action"><span onclick="appendSku()">一键追加到所有sku图</span></div>
      <div class="action"><span onclick="coverSku()">一键覆盖到所有SKU图</span></div>
    </div>
    <div class="el-form-item">
      <label class="el-form-item__label">商品展示图</label>
      <div class="el-form-item__content"><div class="muti-upload" id="main-upload">{main}</div></div>
    </div>
  </section>
  <section>
    <div class="row">
      <div>商品长图</div>
      <div class="action"><span onclick="appendLong()">一键追加到所有sku长图</span></div>
      <div class="action"><span onclick="coverLong()">一键覆盖到所有SKU图</span></div>
    </div>
    <div class="el-form-item">
      <label class="el-form-item__label">长图展示图</label>
      <div class="el-form-item__content"><div class="muti-upload" id="long-upload">{long}</div></div>
    </div>
  </section>
  <section>
    <div class="sku-color-block" data-color="军绿色">
      <div class="sku-color-heading"><span>军绿色</span><button>使用商品图片</button></div>
      <div class="el-form-item">
        <label class="el-form-item__label">*商品展示图</label>
        <div class="el-form-item__content">
          <div class="muti-upload color-display" id="green-display">
            <div class="file-img" data-name="green-original"><button class="del-btn" onclick="this.closest('.file-img').remove()">删除</button></div>
          </div>
        </div>
      </div>
      <div class="el-form-item showSearchImg">
        <label class="el-form-item__label">规格长图</label>
        <div class="el-form-item__content"><div class="muti-upload search-img-upload color-long" id="green-long"></div></div>
      </div>
    </div>
    {second_color}
  </section>
</div>
<script>
  window.skuAppended = false;
  window.longAppended = false;
  window.coverClicked = false;
  function echoColorImages() {
    if (window.skuAppended && window.longAppended) {
      document.querySelectorAll('.color-display').forEach(group => {
        if (!group.dataset.appended) {
          group.insertAdjacentHTML('beforeend', document.getElementById('main-upload').innerHTML);
          group.dataset.appended = 'true';
        }
      });
      document.querySelectorAll('.color-long').forEach(group => {
        if (!group.dataset.appended) {
          group.insertAdjacentHTML('beforeend', document.getElementById('long-upload').innerHTML);
          group.dataset.appended = 'true';
        }
      });
    }
  }
  function appendSku() { window.skuAppended = true; echoColorImages(); }
  function appendLong() { window.longAppended = true; echoColorImages(); }
  function coverSku() { window.coverClicked = true; }
  function coverLong() { window.coverClicked = true; }
</script>
"""


SKU_ATTRIBUTE_HEADER_FIXTURE = """
<button role="tab" aria-selected="false" onclick="
  this.setAttribute('aria-selected','true');
  document.querySelector('[role=tabpanel]').style.display='block';
">京东资料</button>
<div role="tabpanel" aria-label="京东资料" style="display:none">
  <table>
    <thead><tr>
      <th><div>价格</div><a>批量设置</a></th>
      <th><div>SKU属性<a id="sku-batch" onclick="openSkuDialog()">批量设置</a></div></th>
    </tr></thead>
  </table>
</div>
<div id="sku-dialog" class="el-dialog" role="dialog" style="display:none">
  <div>SKU属性</div>
  <div id="empty-attributes">暂无属性 <button onclick="refreshSkuAttributes()">刷新数据</button></div>
  <div id="thickness-item" class="el-form-item" style="display:none">
    <label class="el-form-item__label">厚度</label>
    <div class="el-form-item__content"><input></div>
  </div>
  <button onclick="closeSkuDialog()">取消</button>
  <button onclick="closeSkuDialog()">确定</button>
</div>
<script>
  window.skuAttributesRefreshed = false;
  function openSkuDialog() {
    document.getElementById('sku-dialog').style.display = 'block';
  }
  function refreshSkuAttributes() {
    window.skuAttributesRefreshed = true;
    document.getElementById('empty-attributes').style.display = 'none';
    document.getElementById('thickness-item').style.display = 'block';
  }
  function closeSkuDialog() {
    document.getElementById('sku-dialog').style.display = 'none';
  }
</script>
"""


CATEGORY_POPOVER_FIXTURE = """
<div role="dialog" aria-label="修改类目"><div>类目树尚未展开</div></div>
<!-- The remote-search suggestion is teleported outside the dialog. -->
<div class="el-cascader__suggestion-item">服饰内衣 &gt; 男装 &gt; 男士休闲裤 &gt; 男士休闲直筒裤</div>
"""


class JdFormListingBrowserTests(unittest.IsolatedAsyncioTestCase):
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

    async def _open(self, html: str) -> JdFormListing:
        await self.page.set_content(html)
        listing = JdFormListing(self.page, self.page.locator("body"), None)
        await listing.open()
        return listing

    async def test_style_cascader_selects_leaf_from_slash_or_candidates(self):
        listing = await self._open(STYLE_CASCADER_FIXTURE)
        item = self.page.locator(".el-form-item").first
        actual = await listing._fill_attribute_item(
            "风格", item, "休闲风/时尚都市", required=True
        )
        self.assertEqual(actual, ("休闲风", "简约风"))
        self.assertEqual(
            await self.page.locator(".el-cascader input").input_value(),
            "休闲风 / 简约风",
        )

    async def test_learning_uses_jd_api_ids_and_dom_cross_check(self):
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
                    "snapshot-jd-1",
                )

        response_body = json.dumps(
            {
                "data": {
                    "properties": [
                        {
                            "propId": "pants-length",
                            "propertyName": "裤长",
                            "propertyValues": [
                                {"valueId": "short", "valueName": "短裤"},
                                {"valueId": "long", "valueName": "长裤"},
                            ],
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )
        await self.page.route(
            "**/jd/getCategoryProperties.json*",
            lambda route: route.fulfill(
                content_type="application/json",
                body=response_body,
            ),
        )
        await self.page.set_content(
            """
            <button role="tab" aria-selected="false" onclick="openTab(this)">京东资料</button>
            <div role="tabpanel" aria-label="京东资料" style="display:none">
              <h3>商品属性</h3>
              <div class="el-form-item is-required" id="pants-length">
                <label class="el-form-item__label">裤长</label>
                <div class="el-form-item__content">
                  <div class="el-select"><input class="el-input__inner" readonly onclick="openSelect(this)">
                    <div class="el-select-dropdown" style="display:none">
                      <ul>
                        <li class="el-select-dropdown__item" onclick="chooseOption(this)">短裤</li>
                        <li class="el-select-dropdown__item" onclick="chooseOption(this)">长裤</li>
                      </ul>
                    </div>
                  </div>
                </div>
              </div>
              <h3>销售属性</h3>
            </div>
            <script>
              function openTab(tab) {
                tab.setAttribute('aria-selected', 'true');
                document.querySelector('[role=tabpanel]').style.display = 'block';
                fetch('https://scm.superboss.cc/jd/getCategoryProperties.json?categoryId=97123');
              }
              function openSelect(input) {
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display = 'block';
              }
              function chooseOption(option) {
                const select = option.closest('.el-select');
                select.querySelector('input').value = option.textContent.trim();
                select.querySelector('.el-select-dropdown').style.display = 'none';
              }
              window.addEventListener('keydown', event => {
                if (event.key === 'Escape') {
                  document.querySelectorAll('.el-select-dropdown').forEach(
                    node => node.style.display = 'none'
                  );
                }
              });
            </script>
            """
        )
        runtime = Runtime()
        listing = JdFormListing(
            self.page,
            self.page.locator("body"),
            None,
            attribute_runtime=runtime,
        )
        await listing.open()
        item = self.page.locator("#pants-length")

        actual = await listing._fill_attribute_item(
            "裤长", item, "长裤", required=True
        )

        self.assertEqual(actual, ("长裤",))
        self.assertEqual(len(runtime.requests), 1)
        request = runtime.requests[0]
        self.assertEqual(request.platform_id, "jd")
        self.assertEqual(request.category_leaf_id, "97123")
        self.assertEqual(request.field_id, "pants-length")
        self.assertEqual(
            tuple((value.value_id, value.label) for value in request.candidates),
            (("short", "短裤"), ("long", "长裤")),
        )

    async def test_style_cascader_follows_comma_path(self):
        listing = await self._open(STYLE_CASCADER_FIXTURE)
        cascader = self.page.locator(".el-cascader").first
        actual = await listing._select_cascader_values(
            cascader,
            selection_value_groups("风格", "休闲风,简约风"),
            label="风格",
        )
        self.assertEqual(actual, ("休闲风", "简约风"))
        self.assertEqual(
            await self.page.locator(".el-cascader input").input_value(),
            "休闲风 / 简约风",
        )

    async def test_reads_persisted_brand_from_readonly_select_input(self):
        await self.page.set_content(
            """
            <div id="panel">
              <div class="el-form-item">
                <label class="el-form-item__label">品牌</label>
                <div class="el-select">
                  <input class="el-input__inner" readonly value="NEIGBORL">
                </div>
              </div>
            </div>
            """
        )
        listing = JdFormListing(self.page, self.page.locator("#panel"), None)
        listing.panel = self.page.locator("#panel")
        brand, values = await listing._read_brand_value()
        self.assertEqual(brand, JD_BRAND)
        self.assertEqual(values, (JD_BRAND,))

    async def test_append_sku_images_deletes_each_color_display_first_image(self):
        thumbs = "".join(
            '<div class="file-img" data-name="img-{0}">'
            '<button class="del-btn" onclick="this.closest(\'.file-img\').remove()">删除</button>'
            "</div>".format(index)
            for index in range(1, 6)
        )
        listing = await self._open(
            IMAGE_FIXTURE.replace("{main}", thumbs)
            .replace("{long}", thumbs)
            .replace("{second_color}", "")
        )
        report = await listing.append_sku_images()
        display_names = await self.page.locator("#green-display .file-img").evaluate_all(
            "nodes => nodes.map(node => node.dataset.name)"
        )
        long_names = await self.page.locator("#green-long .file-img").evaluate_all(
            "nodes => nodes.map(node => node.dataset.name)"
        )
        main_names = await self.page.locator("#main-upload .file-img").evaluate_all(
            "nodes => nodes.map(node => node.dataset.name)"
        )
        self.assertEqual(display_names, ["img-1", "img-2", "img-3", "img-4", "img-5"])
        self.assertEqual(long_names, display_names)
        self.assertEqual(main_names, ["img-1", "img-2", "img-3", "img-4", "img-5"])
        self.assertEqual(
            report["colors"],
            [
                {
                    "color": "军绿色",
                    "product_display_before": 6,
                    "product_display_remaining": 5,
                    "long_image_count": 5,
                    "source_image_positions": [1, 2, 3, 4, 5],
                    "sequence_verified": True,
                }
            ],
        )
        self.assertTrue(await self.page.evaluate("window.skuAppended"))
        self.assertTrue(await self.page.evaluate("window.longAppended"))
        self.assertFalse(await self.page.evaluate("window.coverClicked"))

    async def test_append_sku_images_is_idempotent_after_saved_page_reopens(self):
        thumbs = "".join(
            '<div class="file-img" data-name="img-{0}">'
            '<button class="del-btn" onclick="this.closest(\'.file-img\').remove()">删除</button>'
            "</div>".format(index)
            for index in range(1, 6)
        )
        listing = await self._open(
            IMAGE_FIXTURE.replace("{main}", thumbs)
            .replace("{long}", thumbs)
            .replace("{second_color}", "")
        )
        await self.page.locator("#green-display").evaluate(
            "(node, html) => { node.innerHTML = html; }",
            thumbs,
        )
        await self.page.locator("#green-long").evaluate(
            "(node, html) => { node.innerHTML = html; }",
            thumbs,
        )

        report = await listing.append_sku_images(
            image_indices_by_color=((0, 1, 2, 3, 4),)
        )

        self.assertEqual(report["sku_images"], "already_normalized")
        self.assertEqual(report["sku_long_images"], "already_normalized")
        self.assertEqual(report["colors"][0]["product_display_remaining"], 5)
        self.assertEqual(report["colors"][0]["long_image_count"], 5)
        self.assertTrue(report["colors"][0]["already_normalized"])
        self.assertFalse(await self.page.evaluate("window.skuAppended"))
        self.assertFalse(await self.page.evaluate("window.longAppended"))

    async def test_append_sku_images_keeps_two_color_groups_in_matching_order(self):
        thumbs = "".join(
            '<div class="file-img" data-name="img-{0}">'
            '<button class="del-btn" onclick="this.closest(\'.file-img\').remove()">删除</button>'
            "</div>".format(index)
            for index in range(1, 5)
        )
        second_color = """
          <div class="sku-color-block" data-color="藏蓝色">
            <div class="sku-color-heading"><span>藏蓝色</span><button>使用商品图片</button></div>
            <div class="el-form-item">
              <label class="el-form-item__label">*商品展示图</label>
              <div class="el-form-item__content">
                <div class="muti-upload color-display" id="blue-display">
                  <div class="file-img" data-name="blue-original"><button class="del-btn" onclick="this.closest('.file-img').remove()">删除</button></div>
                </div>
              </div>
            </div>
            <div class="el-form-item showSearchImg">
              <label class="el-form-item__label">规格长图</label>
              <div class="el-form-item__content"><div class="muti-upload search-img-upload color-long" id="blue-long"></div></div>
            </div>
          </div>
        """
        listing = await self._open(
            IMAGE_FIXTURE.replace("{main}", thumbs)
            .replace("{long}", thumbs)
            .replace("{second_color}", second_color)
        )
        report = await listing.append_sku_images(
            image_indices_by_color=((0, 2), (1, 3))
        )
        for prefix, expected in (
            ("green", ["img-1", "img-3"]),
            ("blue", ["img-2", "img-4"]),
        ):
            display = await self.page.locator(
                "#{0}-display .file-img".format(prefix)
            ).evaluate_all("nodes => nodes.map(node => node.dataset.name)")
            long_images = await self.page.locator(
                "#{0}-long .file-img".format(prefix)
            ).evaluate_all("nodes => nodes.map(node => node.dataset.name)")
            self.assertEqual(display, expected)
            self.assertEqual(long_images, expected)
        self.assertEqual(
            [item["color"] for item in report["colors"]],
            ["军绿色", "藏蓝色"],
        )
        self.assertTrue(all(item["sequence_verified"] for item in report["colors"]))

    async def test_finds_sku_attribute_batch_action_from_action_ancestor(self):
        listing = await self._open(SKU_ATTRIBUTE_HEADER_FIXTURE)
        actions = await listing._header_actions("SKU属性", "批量设置")
        self.assertEqual(len(actions), 1)
        self.assertEqual(await actions[0].get_attribute("id"), "sku-batch")

    async def test_refreshes_empty_sku_attribute_dialog_and_sets_thickness(self):
        listing = await self._open(SKU_ATTRIBUTE_HEADER_FIXTURE)
        report = await listing.apply_sku_thickness()
        self.assertEqual(report["厚度"], "常规")
        self.assertTrue(await self.page.evaluate("window.skuAttributesRefreshed"))

    async def test_category_candidate_can_be_in_teleported_search_popover(self):
        await self.page.set_content(CATEGORY_POPOVER_FIXTURE)
        listing = JdFormListing(self.page, self.page.locator("body"), None)
        dialog = self.page.get_by_role("dialog", name="修改类目")
        candidates = await listing._category_dom_candidates(
            dialog, "男士休闲直筒裤"
        )
        self.assertEqual(len(candidates), 1)
        self.assertIn("男士休闲直筒裤", await candidates[0].inner_text())


if __name__ == "__main__":
    unittest.main()
