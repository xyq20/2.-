"""Tests for JD platform form filling and verification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from attribute_runtime import ResolvedAttribute
from jd_data import JdFields, parse_jd_fields
from jd_form_listing import (
    JD_BRAND,
    JD_CATEGORY_PATH,
    JD_DELIVERY_TEMPLATE,
    JD_GROSS_WEIGHT,
    JD_SKU_THICKNESS,
    JdFormListing,
    JdFormListingError,
    _category_parts,
    classify_image_indices_by_color,
    _expand_cascader_candidates,
    _kilograms,
    jd_category_target,
    _jd_material_components,
    _material_name,
    _material_option_desired,
    _material_percentage,
    _numeric_equal,
    parse_jd_attribute_fields,
)
from taobao_listing import parse_taobao_materials, selection_value_groups


class JdFormListingHelperTests(unittest.TestCase):
    def test_numeric_equal(self):
        self.assertTrue(_numeric_equal("100", "100"))
        self.assertTrue(_numeric_equal("100.00", "100"))
        self.assertTrue(_numeric_equal("0.43", "0.430"))
        self.assertTrue(_numeric_equal("586", "586.00"))
        self.assertFalse(_numeric_equal("100", "101"))
        self.assertFalse(_numeric_equal("abc", "123"))

    def test_material_name(self):
        self.assertEqual(_material_name("棉100%/棉"), "棉")
        self.assertEqual(_material_name("棉100%/棉布"), "棉/棉布")
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

    def test_public_parser_splits_two_jd_materials(self):
        components = parse_taobao_materials({"材质成分/材质": "棉94%，氨纶6%"})
        self.assertEqual(
            [(item.name, item.percentage) for item in components],
            [("棉", 94), ("氨纶", 6)],
        )
        self.assertEqual(
            [(item.name, item.percentage) for item in _jd_material_components("棉94%，氨纶6%")],
            [("棉", 94), ("氨纶", 6)],
        )
        self.assertEqual(
            [(item.name, item.percentage) for item in _jd_material_components("棉（100%）")],
            [("棉", 100)],
        )
        self.assertEqual(_jd_material_components("棉"), ())
        self.assertEqual(_material_option_desired("氨纶"), "氨纶/聚氨酯弹性纤维(氨纶)")
        with self.assertRaisesRegex(JdFormListingError, "京东"):
            _jd_material_components("棉60%,涤纶30%")

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

    def test_category_target_comes_from_current_product_hints(self):
        self.assertEqual(
            jd_category_target(
                {"商品分类": "夹克/外套/男士休闲夹克/其他夹克"}
            ),
            (("夹克", "外套", "男士休闲夹克", "其他夹克"), "男士休闲夹克"),
        )

    def test_category_target_preserves_existing_pants_behavior(self):
        self.assertEqual(
            jd_category_target(
                {"商品分类": "男装/男士休闲裤/男士休闲直筒裤"}
            )[1],
            "男士休闲直筒裤",
        )

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
        self.assertEqual(JD_GROSS_WEIGHT, "1")
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

    def test_parses_current_jd_attr_value_list_shape(self):
        fields = parse_jd_attribute_fields(
            {
                "result": 1,
                "data": json.dumps(
                    [
                        {
                            "id": "100001",
                            "name": "裤长",
                            "catId": "44570",
                            "inputType": 1,
                            "isRequired": True,
                            "attrValueList": [
                                {"id": "200001", "attId": "100001", "name": "短裤"},
                                {"id": "200002", "attId": "100001", "name": "长裤"},
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
            }
        )

        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].source_id, "100001")
        self.assertEqual(fields[0].label, "裤长")
        self.assertTrue(fields[0].required)
        self.assertEqual(
            tuple((value.value_id, value.label) for value in fields[0].option_values),
            (("200001", "短裤"), ("200002", "长裤")),
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


COLOR_SPEC_VALUE_FIXTURE = """
<button role="tab" aria-selected="false" onclick="
  this.setAttribute('aria-selected','true');
  document.querySelector('[role=tabpanel]').style.display='block';
">京东资料</button>
<div role="tabpanel" aria-label="京东资料" style="display:none">
  <div class="spec-group" id="color-spec">
    <div><span>规格名：</span><input readonly value="颜色"></div>
    <div>
      <span>规格值：</span>
      <input class="color-value" readonly value="军绿色" onclick="openColorValue(this.value)">
      <input class="color-value" readonly value="藏青色" onclick="openColorValue(this.value)">
    </div>
  </div>
  <div class="spec-group" id="size-spec">
    <div><span>规格名：</span><input readonly value="尺码"></div>
    <div>
      <span>规格值：</span>
      <input class="size-value" readonly value="S" onclick="window.sizeOpened += 1">
      <input class="size-value" readonly value="M" onclick="window.sizeOpened += 1">
    </div>
  </div>
</div>
<div id="color-dialog" class="el-dialog" role="dialog" style="display:none">
  <div class="el-dialog__title">颜色</div>
  <div class="el-form-item">
    <label class="el-form-item__label">颜色</label>
    <div class="el-form-item__content">
      <input class="el-input__inner" value="">
    </div>
  </div>
  <div class="el-form-item">
    <label class="el-form-item__label">厚度</label>
    <div class="el-form-item__content"><input id="dialog-thickness" value=""></div>
  </div>
  <div class="el-form-item">
    <label class="el-form-item__label">备注</label>
    <div class="el-form-item__content"><input id="dialog-note" value=""></div>
  </div>
  <button onclick="closeColorValue()">取消</button>
  <button onclick="confirmColorValue()">确定</button>
</div>
<script>
  window.currentColorValue = '';
  window.confirmedColorValues = [];
  window.sizeOpened = 0;
  function openColorValue(value) {
    window.currentColorValue = value;
    document.querySelector('#color-dialog .el-form-item input').value = '';
    document.getElementById('color-dialog').style.display = 'block';
  }
  function closeColorValue() {
    document.getElementById('color-dialog').style.display = 'none';
  }
  function confirmColorValue() {
    window.confirmedColorValues.push({
      before: window.currentColorValue,
      applied: document.querySelector('#color-dialog .el-form-item input').value,
      thickness: document.getElementById('dialog-thickness').value,
      note: document.getElementById('dialog-note').value
    });
    closeColorValue();
  }
</script>
"""


CATEGORY_POPOVER_FIXTURE = """
<div role="dialog" aria-label="修改类目"><div>类目树尚未展开</div></div>
<!-- The remote-search suggestion is teleported outside the dialog. -->
<div class="el-cascader__suggestion-item">服饰内衣 &gt; 男装 &gt; 男士休闲裤 &gt; 男士休闲直筒裤</div>
"""


class JdFormListingBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_extra_material_rows_are_removed_idempotently(self):
        await self.page.set_content('''<div class="el-form-item"><div class="el-form-item__content">
            <div><div class="el-select"><input class="el-input__inner" readonly value="棉"></div><input value="100"><button onclick="this.parentElement.remove()">删除</button></div>
            <div><div class="el-select"><input value="棉毛混纺"></div><input value="1"><button onclick="this.parentElement.remove()">删除</button></div>
            </div></div>''')
        listing = JdFormListing(self.page, self.page.locator('body'), None)
        item = self.page.locator('.el-form-item')
        await listing._remove_extra_material_rows(item)
        await listing._remove_extra_material_rows(item)
        self.assertEqual(await item.locator('.el-select').count(), 1)
        self.assertEqual(await item.locator('input').first.input_value(), '棉')
        self.assertEqual(await item.locator('input').last.input_value(), '100')
        listing._collect_attribute_items = AsyncMock(return_value={"材质": ("材质", item)})
        fields = parse_jd_fields({"材质成分/材质": "棉100%/棉"})
        report = await listing._verify_single_material(fields)
        self.assertEqual(report["row_count"], 1)
        await item.locator('input').last.fill('1')
        with self.assertRaisesRegex(JdFormListingError, "百分比"):
            await listing._verify_single_material(fields)

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

        # A category-specific, unregistered label must still reach review.
        # 京东铺货 API 只接受平台数字 valueId：接口已有候选列表的字段
        # 不允许直填自造文字，否则保存后铺货会报
        # “属性[适用人群]的值需是数字格式”（2026-09-21 线上 503）。
        field, category_id = await listing._captured_api_field("裤长")
        listing._captured_api_field = AsyncMock(return_value=(field, category_id))
        resolve_calls = []
        runtime.resolve = AsyncMock(
            side_effect=lambda request: (
                resolve_calls.append(request),
                None,
            )[1]
        )
        listing._set_select_values_directly = AsyncMock(
            side_effect=AssertionError("valueId 字段禁止直填自造文字")
        )
        actual = await listing._fill_attribute_item(
            "适用人群", item, "通用", required=True
        )
        self.assertIsNone(actual)
        self.assertEqual(len(resolve_calls), 1)
        request = resolve_calls[0]
        self.assertTrue(request.custom_allowed)
        self.assertEqual(request.excel_value, "通用")
        self.assertEqual(request.field_label, "适用人群")
        self.assertNotIn(
            "通用",
            tuple(candidate.label for candidate in request.candidates),
        )
        # 页面输入框保持已选的真实候选，未被直填自造文字污染。
        self.assertEqual(
            await item.locator("input").first.input_value(), "长裤"
        )

    async def test_learning_expands_elasticity_alias_before_excel_direct_input(self):
        await self.page.set_content(
            '<div class="el-select"><input class="el-input__inner" '
            'readonly value="无弹力"></div>'
        )

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
                    "snapshot-elasticity",
                )

        runtime = Runtime()
        listing = JdFormListing(
            self.page,
            self.page.locator("body"),
            None,
            attribute_runtime=runtime,
        )
        field = parse_jd_attribute_fields(
            {
                "properties": [
                    {
                        "propId": "118767",
                        "propertyName": "弹力",
                        "propertyValues": [
                            {"valueId": "4401", "valueName": "无弹力"},
                            {"valueId": "4402", "valueName": "微弹"},
                        ],
                    }
                ]
            }
        )[0]
        listing._captured_api_field = AsyncMock(return_value=(field, "44570"))
        listing._open_select = AsyncMock()
        listing._visible_dom_options = AsyncMock(
            return_value=(
                object(),
                [
                    {"value": "4401", "name": "无弹力", "disabled": False},
                    {"value": "4402", "name": "微弹", "disabled": False},
                ],
            )
        )
        listing._dismiss_select_dropdown = AsyncMock()
        listing._set_select_values_directly = AsyncMock(
            side_effect=AssertionError("有等价平台候选时不应直接写入 Excel 原文")
        )

        actual = await listing._resolve_learning_select_value(
            "弹力", self.page.locator(".el-select"), "无弹"
        )

        self.assertEqual(actual, "无弹力")
        self.assertEqual(runtime.requests[0].excel_value, "无弹力")
        self.assertEqual(
            tuple(
                (candidate.value_id, candidate.label)
                for candidate in runtime.requests[0].candidates
            ),
            (("4401", "无弹力"), ("4402", "微弹")),
        )
        listing._set_select_values_directly.assert_not_awaited()

    async def test_valueid_field_clears_stale_self_typed_text_before_review(self):
        # 2026-09-21 17:15 线上 503：铺货 API 报“属性[适用人群]的值需是
        # 数字格式”。历史运行直填的自造文字“通用”留在表单模型里被原样
        # 提交。修复后：Excel 值匹配不到接口候选时转审核，并把底层自造
        # 文字绑定清空，真实数字绑定与空值保持不动。
        await self.page.set_content(
            """
            <div class="el-form-item" id="audience-item">
              <label class="el-form-item__label">适用人群</label>
              <div class="el-form-item__content">
                <div class="el-select" id="audience-select">
                  <input class="el-input__inner" readonly value="通用">
                  <div class="el-select-dropdown" style="display:none">
                    <ul>
                      <li class="el-select-dropdown__item">男</li>
                      <li class="el-select-dropdown__item">女</li>
                    </ul>
                  </div>
                </div>
              </div>
            </div>
            <script>
              const select = document.getElementById('audience-select');
              select.__vue__ = {
                value: '通用',
                selected: {value: '通用'},
                selectedLabel: '通用',
                multiple: false,
                deleteSelected() {
                  this.value = '';
                  this.selected = {};
                  this.selectedLabel = '';
                  select.querySelector('input').value = '';
                },
                $nextTick(callback) { callback(); }
              };
            </script>
            """
        )
        runtime = SimpleNamespace(
            resolve=AsyncMock(return_value=None),
        )
        listing = JdFormListing(
            self.page,
            self.page.locator("body"),
            None,
            attribute_runtime=runtime,
        )
        field = parse_jd_attribute_fields(
            {
                "properties": [
                    {
                        "propId": "audience",
                        "propertyName": "适用人群",
                        "propertyValues": [
                            {"valueId": "101", "valueName": "男"},
                            {"valueId": "102", "valueName": "女"},
                        ],
                    }
                ]
            }
        )[0]
        listing._captured_api_field = AsyncMock(return_value=(field, "97123"))
        listing._open_select = AsyncMock()
        listing._visible_dom_options = AsyncMock(
            return_value=(
                object(),
                [
                    {"value": "101", "name": "男", "disabled": False},
                    {"value": "102", "name": "女", "disabled": False},
                ],
            )
        )
        listing._dismiss_select_dropdown = AsyncMock()
        listing._set_select_values_directly = AsyncMock(
            side_effect=AssertionError("valueId 字段禁止直填自造文字")
        )
        item = self.page.locator("#audience-item")

        actual = await listing._fill_attribute_item(
            "适用人群", item, "通用", required=True
        )

        self.assertIsNone(actual)
        request = runtime.resolve.await_args.args[0]
        self.assertEqual(request.field_label, "适用人群")
        self.assertEqual(request.excel_value, "通用")
        # 底层自造文字绑定被清空，铺货 API 不会再拿到“通用”。
        self.assertEqual(
            await self.page.locator("#audience-select").evaluate(
                "element => element.__vue__.value"
            ),
            "",
        )
        listing._set_select_values_directly.assert_not_awaited()

    async def test_persisted_attributes_include_verified_elasticity_readback(self):
        await self.page.set_content(
            '<div class="el-form-item" id="elasticity">'
            '<label class="el-form-item__label">弹力</label>'
            '<div class="el-form-item__content"><div class="el-select">'
            '<input class="el-input__inner" readonly value="无弹力">'
            '</div></div></div>'
        )
        listing = JdFormListing(self.page, self.page.locator("body"), None)
        item = self.page.locator("#elasticity")
        listing._collect_attribute_items = AsyncMock(
            return_value={"弹力": ("弹力", item)}
        )

        actual = await listing._verify_persisted_attributes(
            {"弹力": ("无弹力",)},
            {"status": "not_single_component"},
        )

        self.assertEqual(actual, {"弹力": ("无弹力",)})

    async def test_material_reselects_real_numeric_platform_id(self):
        await self.page.set_content(
            """
            <div class="el-form-item" id="material-item">
              <label class="el-form-item__label">材质</label>
              <div class="el-form-item__content">
                <div class="el-select">
                  <input class="el-input__inner" readonly value="棉"
                         onclick="document.getElementById('material-options').style.display='block'">
                  <div id="material-options" class="el-select-dropdown" style="display:none">
                    <ul><li class="el-select-dropdown__item is-disabled"
                            onclick="selectCotton()">棉</li></ul>
                  </div>
                </div>
                <input id="material-percentage" value="1">
              </div>
            </div>
            <script>
              const select = document.querySelector('.el-select');
              const input = select.querySelector('input');
              const staleCotton = {
                value: '棉',
                currentLabel: '棉',
                label: '棉',
                created: true,
                disabled: false
              };
              const cotton = {
                value: 999999,
                currentLabel: '棉',
                label: '棉',
                created: false,
                disabled: false
              };
              select.__vue__ = {
                value: '棉',
                selected: {value: '棉'},
                selectedLabel: '棉',
                cachedOptions: [staleCotton],
                options: [cotton],
                deleteSelected() {
                  this.value = '';
                  this.selected = {};
                  this.selectedLabel = '';
                  input.value = '';
                  option.classList.remove('is-disabled');
                },
                handleOptionSelect(option) {
                  this.value = option.value;
                  this.selected = option;
                  this.selectedLabel = option.currentLabel;
                  input.value = option.currentLabel;
                },
                $nextTick(callback) { callback(); }
              };
              const option = document.querySelector('.el-select-dropdown__item');
              option.__vue__ = cotton;
              function selectCotton() {
                select.__vue__.handleOptionSelect(cotton);
                document.getElementById('material-options').style.display = 'none';
              }
            </script>
            """
        )
        listing = JdFormListing(self.page, self.page.locator("body"), None)
        item = self.page.locator("#material-item")
        field = parse_jd_attribute_fields(
            {
                "properties": [
                    {
                        "propId": "155184",
                        "propertyName": "材质",
                        "propertyValues": [
                            {"valueId": 788161, "valueName": "棉"}
                        ],
                    }
                ]
            }
        )[0]
        listing._captured_api_field_definition = AsyncMock(return_value=field)
        listing._resolve_learning_select_value = AsyncMock(
            side_effect=AssertionError("material must not use the custom-value path")
        )
        listing._set_select_values_directly = AsyncMock(
            side_effect=AssertionError("material must not synthesize a text value")
        )

        actual = await listing._fill_attribute_item(
            "材质", item, "棉（100%）", required=True
        )

        self.assertEqual(actual, ("棉",))
        self.assertEqual(
            await self.page.locator(".el-select").evaluate("node => node.__vue__.value"),
            999999,
        )
        self.assertEqual(
            await self.page.locator("#material-percentage").input_value(),
            "100",
        )
        listing._resolve_learning_select_value.assert_not_awaited()
        listing._set_select_values_directly.assert_not_awaited()

    def _jd_material_field(self, *names: str):
        return parse_jd_attribute_fields(
            {
                "properties": [
                    {
                        "propId": "155184",
                        "propertyName": "材质",
                        "propertyValues": [
                            {"valueId": 1000 + index, "valueName": name}
                            for index, name in enumerate(names, start=1)
                        ],
                    }
                ]
            }
        )[0]

    async def test_two_material_rows_are_filled_from_public_parser(self):
        await self.page.set_content(
            """
            <div class="el-form-item" id="material-item">
              <label class="el-form-item__label">材质</label>
              <div class="el-form-item__content" id="material-content">
                <div class="material-row">
                  <div class="el-select">
                    <input class="el-input__inner" readonly value="棉"
                           onclick="openMaterial(this)">
                    <div class="el-select-dropdown" style="display:none">
                      <ul>
                        <li class="el-select-dropdown__item" data-name="棉"
                            onclick="chooseMaterial(this, '棉', 1001)">棉</li>
                        <li class="el-select-dropdown__item" data-name="氨纶"
                            onclick="chooseMaterial(this, '氨纶', 1002)">氨纶</li>
                      </ul>
                    </div>
                  </div>
                  <input class="percent" value="94">
                  <button type="button" onclick="this.parentElement.remove()">删除</button>
                </div>
                <div class="material-row">
                  <div class="el-select">
                    <input class="el-input__inner" readonly value="氨纶"
                           onclick="openMaterial(this)">
                    <div class="el-select-dropdown" style="display:none">
                      <ul>
                        <li class="el-select-dropdown__item" data-name="棉"
                            onclick="chooseMaterial(this, '棉', 1001)">棉</li>
                        <li class="el-select-dropdown__item" data-name="氨纶"
                            onclick="chooseMaterial(this, '氨纶', 1002)">氨纶</li>
                      </ul>
                    </div>
                  </div>
                  <input class="percent" value="">
                  <button type="button" onclick="this.parentElement.remove()">删除</button>
                </div>
                <button type="button" id="add-material" onclick="addMaterialRow()">添加</button>
              </div>
            </div>
            <script>
              const numericByName = { '棉': 1001, '氨纶': 1002 };
              function attachSelect(select, currentLabel) {
                const input = select.querySelector('input');
                const currentValue = numericByName[currentLabel] || '';
                const options = Object.keys(numericByName).map(name => ({
                  value: numericByName[name],
                  currentLabel: name,
                  label: name,
                  created: false,
                  disabled: false
                }));
                select.__vue__ = {
                  value: currentValue,
                  selected: currentValue ? {value: currentValue} : {},
                  selectedLabel: currentLabel,
                  cachedOptions: options,
                  options: options,
                  deleteSelected() {
                    this.value = '';
                    this.selected = {};
                    this.selectedLabel = '';
                    input.value = '';
                  },
                  handleOptionSelect(option) {
                    this.value = option.value;
                    this.selected = option;
                    this.selectedLabel = option.currentLabel;
                    input.value = option.currentLabel;
                  },
                  $nextTick(callback) { callback(); }
                };
                select.querySelectorAll('.el-select-dropdown__item').forEach(node => {
                  const name = node.getAttribute('data-name');
                  node.__vue__ = options.find(item => item.label === name);
                });
              }
              function openMaterial(input) {
                document.querySelectorAll('.el-select-dropdown').forEach(el => {
                  el.style.display = 'none';
                });
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display = 'block';
              }
              function chooseMaterial(optionNode, name, value) {
                const select = optionNode.closest('.el-select');
                select.__vue__.handleOptionSelect({
                  value, currentLabel: name, label: name, created: false, disabled: false
                });
                select.querySelector('.el-select-dropdown').style.display = 'none';
              }
              function addMaterialRow() {
                const add = document.getElementById('add-material');
                const row = document.createElement('div');
                row.className = 'material-row';
                row.innerHTML = `
                  <div class="el-select">
                    <input class="el-input__inner" readonly value="" onclick="openMaterial(this)">
                    <div class="el-select-dropdown" style="display:none">
                      <ul>
                        <li class="el-select-dropdown__item" data-name="棉"
                            onclick="chooseMaterial(this, '棉', 1001)">棉</li>
                        <li class="el-select-dropdown__item" data-name="氨纶"
                            onclick="chooseMaterial(this, '氨纶', 1002)">氨纶</li>
                      </ul>
                    </div>
                  </div>
                  <input class="percent" value="">
                  <button type="button" onclick="this.parentElement.remove()">删除</button>`;
                add.parentElement.insertBefore(row, add);
                attachSelect(row.querySelector('.el-select'), '');
              }
              document.querySelectorAll('.el-select').forEach(select => {
                attachSelect(select, select.querySelector('input').value);
              });
            </script>
            """
        )
        listing = JdFormListing(self.page, self.page.locator("body"), None)
        item = self.page.locator("#material-item")
        listing._captured_api_field_definition = AsyncMock(
            return_value=self._jd_material_field("棉", "氨纶")
        )
        listing._collect_attribute_items = AsyncMock(
            return_value={"材质": ("材质", item)}
        )

        actual = await listing._fill_attribute_item(
            "材质", item, "棉94%，氨纶6%", required=True
        )

        self.assertEqual(actual, ("棉", "氨纶"))
        self.assertEqual(
            [
                await box.input_value()
                for box in await listing._editable_inputs(item)
            ],
            ["94", "6"],
        )
        report = await listing._verify_single_material(
            parse_jd_fields({"材质成分/材质": "棉94%，氨纶6%"})
        )
        self.assertEqual(report["row_count"], 2)
        self.assertEqual(report["values"], ("棉", "氨纶"))
        self.assertEqual(report["percentages"], ("94", "6"))

    async def test_annotated_jd_material_beats_substring_option(self):
        # 2026-09-21 线上中断：京东候选同时有“氨纶(聚氨酯弹性纤维)”与独立的
        # “弹性纤维”，期望别名“聚氨酯弹性纤维(氨纶)”不得再命中“弹性纤维”。
        await self.page.set_content(
            """
            <div class="el-form-item" id="material-item">
              <label class="el-form-item__label">材质</label>
              <div class="el-form-item__content" id="material-content">
                <div class="material-row">
                  <div class="el-select">
                    <input class="el-input__inner" readonly value=""
                           onclick="openMaterial(this)">
                    <div class="el-select-dropdown" style="display:none">
                      <ul>
                        <li class="el-select-dropdown__item" data-name="棉"
                            onclick="chooseMaterial(this, '棉', 1001)">棉</li>
                        <li class="el-select-dropdown__item" data-name="氨纶(聚氨酯弹性纤维)"
                            onclick="chooseMaterial(this, '氨纶(聚氨酯弹性纤维)', 1002)">氨纶(聚氨酯弹性纤维)</li>
                        <li class="el-select-dropdown__item" data-name="弹性纤维"
                            onclick="chooseMaterial(this, '弹性纤维', 1003)">弹性纤维</li>
                      </ul>
                    </div>
                  </div>
                  <input class="percent" value="">
                  <button type="button" onclick="this.parentElement.remove()">删除</button>
                </div>
                <button type="button" id="add-material" onclick="addMaterialRow()">添加</button>
              </div>
            </div>
            <script>
              const numericByName = {
                '棉': 1001,
                '氨纶(聚氨酯弹性纤维)': 1002,
                '弹性纤维': 1003
              };
              function attachSelect(select, currentLabel) {
                const input = select.querySelector('input');
                const currentValue = numericByName[currentLabel] || '';
                const options = Object.keys(numericByName).map(name => ({
                  value: numericByName[name],
                  currentLabel: name,
                  label: name,
                  created: false,
                  disabled: false
                }));
                select.__vue__ = {
                  value: currentValue,
                  selected: currentValue ? {value: currentValue} : {},
                  selectedLabel: currentLabel,
                  cachedOptions: options,
                  options: options,
                  deleteSelected() {
                    this.value = '';
                    this.selected = {};
                    this.selectedLabel = '';
                    input.value = '';
                  },
                  handleOptionSelect(option) {
                    this.value = option.value;
                    this.selected = option;
                    this.selectedLabel = option.currentLabel;
                    input.value = option.currentLabel;
                  },
                  $nextTick(callback) { callback(); }
                };
                select.querySelectorAll('.el-select-dropdown__item').forEach(node => {
                  const name = node.getAttribute('data-name');
                  node.__vue__ = options.find(item => item.label === name);
                });
              }
              function openMaterial(input) {
                document.querySelectorAll('.el-select-dropdown').forEach(el => {
                  el.style.display = 'none';
                });
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display = 'block';
              }
              function chooseMaterial(optionNode, name, value) {
                const select = optionNode.closest('.el-select');
                select.__vue__.handleOptionSelect({
                  value, currentLabel: name, label: name, created: false, disabled: false
                });
                select.querySelector('.el-select-dropdown').style.display = 'none';
              }
              function addMaterialRow() {
                const add = document.getElementById('add-material');
                const row = document.createElement('div');
                row.className = 'material-row';
                row.innerHTML = `
                  <div class="el-select">
                    <input class="el-input__inner" readonly value="" onclick="openMaterial(this)">
                    <div class="el-select-dropdown" style="display:none">
                      <ul>
                        <li class="el-select-dropdown__item" data-name="棉"
                            onclick="chooseMaterial(this, '棉', 1001)">棉</li>
                        <li class="el-select-dropdown__item" data-name="氨纶(聚氨酯弹性纤维)"
                            onclick="chooseMaterial(this, '氨纶(聚氨酯弹性纤维)', 1002)">氨纶(聚氨酯弹性纤维)</li>
                        <li class="el-select-dropdown__item" data-name="弹性纤维"
                            onclick="chooseMaterial(this, '弹性纤维', 1003)">弹性纤维</li>
                      </ul>
                    </div>
                  </div>
                  <input class="percent" value="">
                  <button type="button" onclick="this.parentElement.remove()">删除</button>`;
                add.parentElement.insertBefore(row, add);
                attachSelect(row.querySelector('.el-select'), '');
              }
              document.querySelectorAll('.el-select').forEach(select => {
                attachSelect(select, select.querySelector('input').value);
              });
            </script>
            """
        )
        listing = JdFormListing(self.page, self.page.locator("body"), None)
        item = self.page.locator("#material-item")
        listing._captured_api_field_definition = AsyncMock(
            return_value=self._jd_material_field(
                "棉", "氨纶(聚氨酯弹性纤维)", "弹性纤维"
            )
        )
        listing._collect_attribute_items = AsyncMock(
            return_value={"材质": ("材质", item)}
        )

        actual = await listing._fill_attribute_item(
            "材质", item, "棉94%，氨纶6%", required=True
        )

        self.assertEqual(actual, ("棉", "氨纶(聚氨酯弹性纤维)"))
        self.assertEqual(
            [
                await box.input_value()
                for box in await listing._editable_inputs(item)
            ],
            ["94", "6"],
        )
        report = await listing._verify_single_material(
            parse_jd_fields({"材质成分/材质": "棉94%，氨纶6%"}),
            expected_attributes={"材质": actual},
        )
        self.assertEqual(report["row_count"], 2)
        self.assertEqual(report["values"], ("棉", "氨纶(聚氨酯弹性纤维)"))
        self.assertEqual(report["percentages"], ("94", "6"))

    async def test_second_material_row_is_added_and_filled(self):
        await self.page.set_content(
            """
            <div class="el-form-item" id="material-item">
              <label class="el-form-item__label">材质</label>
              <div class="el-form-item__content" id="material-content">
                <div class="material-row">
                  <div class="el-select">
                    <input class="el-input__inner" readonly value=""
                           onclick="openMaterial(this)">
                    <div class="el-select-dropdown" style="display:none">
                      <ul>
                        <li class="el-select-dropdown__item" data-name="棉"
                            onclick="chooseMaterial(this, '棉', 1001)">棉</li>
                        <li class="el-select-dropdown__item" data-name="氨纶"
                            onclick="chooseMaterial(this, '氨纶', 1002)">氨纶</li>
                      </ul>
                    </div>
                  </div>
                  <input class="percent" value="">
                  <button type="button" onclick="this.parentElement.remove()">删除</button>
                </div>
                <button type="button" id="add-material" onclick="addMaterialRow()">添加</button>
              </div>
            </div>
            <script>
              const numericByName = { '棉': 1001, '氨纶': 1002 };
              function attachSelect(select, currentLabel) {
                const input = select.querySelector('input');
                const currentValue = numericByName[currentLabel] || '';
                const options = Object.keys(numericByName).map(name => ({
                  value: numericByName[name],
                  currentLabel: name,
                  label: name,
                  created: false,
                  disabled: false
                }));
                select.__vue__ = {
                  value: currentValue,
                  selected: currentValue ? {value: currentValue} : {},
                  selectedLabel: currentLabel,
                  cachedOptions: options,
                  options: options,
                  deleteSelected() {
                    this.value = '';
                    this.selected = {};
                    this.selectedLabel = '';
                    input.value = '';
                  },
                  handleOptionSelect(option) {
                    this.value = option.value;
                    this.selected = option;
                    this.selectedLabel = option.currentLabel;
                    input.value = option.currentLabel;
                  },
                  $nextTick(callback) { callback(); }
                };
                select.querySelectorAll('.el-select-dropdown__item').forEach(node => {
                  const name = node.getAttribute('data-name');
                  node.__vue__ = options.find(item => item.label === name);
                });
              }
              function openMaterial(input) {
                document.querySelectorAll('.el-select-dropdown').forEach(el => {
                  el.style.display = 'none';
                });
                input.closest('.el-select').querySelector('.el-select-dropdown').style.display = 'block';
              }
              function chooseMaterial(optionNode, name, value) {
                const select = optionNode.closest('.el-select');
                select.__vue__.handleOptionSelect({
                  value, currentLabel: name, label: name, created: false, disabled: false
                });
                select.querySelector('.el-select-dropdown').style.display = 'none';
              }
              function addMaterialRow() {
                const add = document.getElementById('add-material');
                const row = document.createElement('div');
                row.className = 'material-row';
                row.innerHTML = `
                  <div class="el-select">
                    <input class="el-input__inner" readonly value="" onclick="openMaterial(this)">
                    <div class="el-select-dropdown" style="display:none">
                      <ul>
                        <li class="el-select-dropdown__item" data-name="棉"
                            onclick="chooseMaterial(this, '棉', 1001)">棉</li>
                        <li class="el-select-dropdown__item" data-name="氨纶"
                            onclick="chooseMaterial(this, '氨纶', 1002)">氨纶</li>
                      </ul>
                    </div>
                  </div>
                  <input class="percent" value="">
                  <button type="button" onclick="this.parentElement.remove()">删除</button>`;
                add.parentElement.insertBefore(row, add);
                attachSelect(row.querySelector('.el-select'), '');
              }
              document.querySelectorAll('.el-select').forEach(select => {
                attachSelect(select, select.querySelector('input').value);
              });
            </script>
            """
        )
        listing = JdFormListing(self.page, self.page.locator("body"), None)
        item = self.page.locator("#material-item")
        listing._captured_api_field_definition = AsyncMock(
            return_value=self._jd_material_field("棉", "氨纶")
        )

        actual = await listing._fill_attribute_item(
            "材质", item, "棉94%，氨纶6%", required=True
        )

        self.assertEqual(actual, ("棉", "氨纶"))
        self.assertEqual(await item.locator(".material-row").count(), 2)
        self.assertEqual(
            [
                await box.input_value()
                for box in await listing._editable_inputs(item)
            ],
            ["94", "6"],
        )

    async def test_material_without_unique_excel_match_is_deferred_for_review(self):
        await self.page.set_content(
            """
            <div class="el-select"><input class="el-input__inner" readonly></div>
            """
        )
        runtime = SimpleNamespace(resolve=AsyncMock(return_value=None))
        listing = JdFormListing(
            self.page,
            self.page.locator("body"),
            None,
            attribute_runtime=runtime,
        )
        field = parse_jd_attribute_fields(
            {
                "properties": [
                    {
                        "propId": "155184",
                        "propertyName": "材质",
                        "propertyValues": [
                            {"valueId": 1001, "valueName": "竹纤维"},
                            {"valueId": 1002, "valueName": "羊毛"},
                        ],
                    }
                ]
            }
        )[0]
        listing._captured_api_field = AsyncMock(return_value=(field, "44570"))

        actual = await listing._select_numeric_material_option(
            self.page.locator(".el-select"),
            "棉",
        )

        self.assertIsNone(actual)
        request = runtime.resolve.await_args.args[0]
        self.assertEqual(request.field_label, "材质")
        self.assertEqual(request.excel_value, "棉")
        self.assertTrue(request.evidence["force_review"])
        self.assertEqual(
            tuple((item.value_id, item.label) for item in request.candidates),
            (("1001", "竹纤维"), ("1002", "羊毛")),
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

    async def test_identity_uses_fixed_weight_and_keeps_parameter_origin(self):
        listing = JdFormListing(self.page, self.page.locator("body"), None)
        brand_item = object()
        parameter_origin_item = object()
        listing._form_item_exact = AsyncMock(
            side_effect=(brand_item, parameter_origin_item)
        )
        listing._read_brand_value = AsyncMock(
            return_value=(JD_BRAND, (JD_BRAND,))
        )
        listing._fill_input_item = AsyncMock(
            side_effect=lambda _label, value, **_kwargs: value
        )
        listing._fill_attribute = AsyncMock(return_value=("中国大陆",))

        report = await listing.fill_identity_and_parameters(
            {"产地": "中国大陆"},
            style_code="NGBL-10588",
        )

        self.assertEqual(report["商品毛重(公斤)"], "1")
        listing._fill_input_item.assert_any_await(
            "商品毛重(公斤)", "1", numeric=True
        )
        listing._fill_attribute.assert_awaited_once_with(
            "产地", parameter_origin_item, "中国大陆", required=True
        )

    async def test_clears_product_attribute_origin_and_does_not_map_excel_value(self):
        await self.page.set_content(
            """
            <div id="panel">
              <h3>商品参数</h3>
              <div class="el-form-item">
                <label class="el-form-item__label">产地</label>
                <input value="中国大陆">
              </div>
              <h3>商品属性</h3>
              <div class="el-form-item" id="attribute-origin">
                <label class="el-form-item__label">产地</label>
                <div class="el-form-item__content"><input value="中国大陆"></div>
              </div>
              <h3>销售属性</h3>
            </div>
            """
        )
        listing = JdFormListing(self.page, self.page.locator("#panel"), None)
        listing.panel = self.page.locator("#panel")
        items = await listing._collect_attribute_items()
        listing._attribute_items = AsyncMock(return_value=items)

        report = await listing.fill_attributes(
            parse_jd_fields({"产地": "中国大陆"})
        )

        self.assertEqual(
            await self.page.locator("#attribute-origin input").input_value(),
            "",
        )
        self.assertEqual(report["cleared_fields"], {"产地": ""})
        self.assertNotIn("产地", report["attributes"])
        self.assertNotIn("产地", report["unmatched_page_fields"])

    async def test_does_not_fill_ignored_product_attribute_color(self):
        await self.page.set_content(
            """
            <div id="panel">
              <div class="el-form-item" id="attribute-color">
                <label class="el-form-item__label">颜色</label>
                <div class="el-form-item__content"><input value=""></div>
              </div>
            </div>
            """
        )
        listing = JdFormListing(self.page, self.page.locator("#panel"), None)
        listing.panel = self.page.locator("#panel")
        color_item = self.page.locator("#attribute-color")
        listing._attribute_items = AsyncMock(
            return_value={"颜色": ("颜色", color_item)}
        )
        listing._fill_attribute_item = AsyncMock(return_value=("深灰色",))

        report = await listing.fill_attributes(parse_jd_fields({"颜色": "深灰色"}))

        listing._fill_attribute_item.assert_not_awaited()
        self.assertEqual(await color_item.locator("input").input_value(), "")
        self.assertEqual(report["ignored_fields"], ("颜色",))
        self.assertNotIn("颜色", report["attributes"])
        self.assertNotIn("颜色", report["unmatched_page_fields"])

    async def test_selects_exact_jd_brand_from_visible_candidates(self):
        await self.page.set_content(
            """
            <div id="panel">
              <div class="el-form-item">
                <label class="el-form-item__label">品牌</label>
                <div class="el-select">
                  <input class="el-input__inner" readonly value="" onclick="openBrand()">
                </div>
              </div>
            </div>
            <div id="brand-dropdown" class="el-select-dropdown" style="display:none">
              <div class="el-select-dropdown__item" onclick="chooseBrand(this)">无品牌</div>
              <div class="el-select-dropdown__item" onclick="chooseBrand(this)">NEIGBORL</div>
            </div>
            <script>
              function openBrand() {
                document.getElementById('brand-dropdown').style.display = 'block';
              }
              function chooseBrand(option) {
                document.querySelector('#panel input').value = option.textContent.trim();
                document.getElementById('brand-dropdown').style.display = 'none';
              }
            </script>
            """
        )
        listing = JdFormListing(self.page, self.page.locator("#panel"), None)
        listing.panel = self.page.locator("#panel")
        brand_item = await listing._form_item_exact("品牌")

        brand, values = await listing._select_exact_brand(brand_item)

        self.assertEqual(brand, JD_BRAND)
        self.assertEqual(values, (JD_BRAND,))
        self.assertFalse(await self.page.locator("#brand-dropdown").is_visible())

    async def test_fill_attributes_skips_duplicate_removed_by_rerender(self):
        listing = JdFormListing(self.page, self.page.locator("body"), None)
        first_material = object()
        nested_material = object()
        original_items = {
            "材质": ("材质", first_material),
            "材质#2": ("材质", nested_material),
        }
        current_items = dict(original_items)

        async def fill_item(page_label, item, expected, *, required):
            self.assertEqual(page_label, "材质")
            self.assertIs(item, first_material)
            current_items.pop("材质#2")
            return ("棉",)

        listing._attribute_items = AsyncMock(return_value=original_items)
        listing._collect_attribute_items = AsyncMock(
            side_effect=lambda: dict(current_items)
        )
        listing._attribute_assignments = AsyncMock(
            return_value={
                "材质": ("材质", "棉（100%）"),
                "材质#2": ("材质", "棉（100%）"),
            }
        )
        listing._is_required = AsyncMock(return_value=True)
        listing._fill_attribute_item = AsyncMock(side_effect=fill_item)

        report = await listing.fill_attributes(parse_jd_fields({"材质": "棉（100%）"}))

        self.assertEqual(report["attributes"], {"材质": ("棉",)})
        self.assertEqual(listing._fill_attribute_item.await_count, 1)

    async def test_collect_attribute_items_reads_only_attribute_section(self):
        await self.page.set_content(
            """
            <div id="panel">
              <h3>商品参数</h3>
              <div class="el-form-item" id="parameter-origin">
                <label class="el-form-item__label">产地</label>
              </div>
              <h3>商品属性</h3>
              <div class="el-form-item" id="fabric">
                <label class="el-form-item__label">* 面料</label>
              </div>
              <div class="el-form-item" id="material-one">
                <label class="el-form-item__label">材质</label>
              </div>
              <div class="el-form-item" id="material-two">
                <label class="el-form-item__label">材质</label>
              </div>
              <div class="el-form-item" id="hidden" style="display:none">
                <label class="el-form-item__label">隐藏属性</label>
              </div>
              <h3>销售属性</h3>
              <div class="el-form-item" id="price">
                <label class="el-form-item__label">京东价</label>
              </div>
            </div>
            """
        )
        listing = JdFormListing(self.page, self.page.locator("#panel"), None)
        listing.panel = self.page.locator("#panel")

        items = await listing._collect_attribute_items()

        self.assertEqual(tuple(items), ("面料", "材质", "材质#2"))
        self.assertEqual(
            [await item.get_attribute("id") for _label, item in items.values()],
            ["fabric", "material-one", "material-two"],
        )

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

    async def test_reapplies_each_echoed_color_spec_value_only(self):
        listing = await self._open(COLOR_SPEC_VALUE_FIXTURE)

        report = await listing.reapply_echoed_color_spec_values()

        self.assertEqual(
            report,
            {"spec_name": "颜色", "values": ("军绿色", "藏青色")},
        )
        self.assertEqual(
            await self.page.evaluate("window.confirmedColorValues"),
            [
                {
                    "before": "军绿色",
                    "applied": "军绿色",
                    "thickness": "",
                    "note": "",
                },
                {
                    "before": "藏青色",
                    "applied": "藏青色",
                    "thickness": "",
                    "note": "",
                },
            ],
        )
        self.assertEqual(await self.page.evaluate("window.sizeOpened"), 0)

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
