import asyncio
import logging
import unittest

from taobao_data import parse_taobao_fields
from taobao_listing import (
    TaobaoListing,
    excel_aliases,
    normalize_label,
    parse_taobao_fabrics,
    parse_taobao_materials,
    selection_value_groups,
    value_candidates,
)
from size_image_recognition import SizeLength


LOGGER = logging.getLogger("test_taobao_listing")


TAOBAO_FIXTURE = r"""
<!doctype html>
<meta charset="utf-8">
<div id="drawer">
  <div role="tab" aria-selected="false" onclick="openTaobao()">淘宝资料</div>
  <div id="slot"></div>
</div>
<script>
function singleSelect(label, options, value = '') {
  return `<div class="complex-item"><div class="el-form-item is-required">
    <label class="el-form-item__label">${label}</label>
    <div class="el-form-item__content"><div class="el-select">
      <input class="el-input__inner" readonly value="${value}" onclick="openSelect(this)">
      <div class="el-select-dropdown" style="display:none"><ul>
        ${options.map(option => `<li class="el-select-dropdown__item" onclick="choose(this)">${option}</li>`).join('')}
      </ul></div>
    </div></div>
  </div></div>`;
}

function multiSelect(label, options) {
  return `<div class="complex-item"><div class="el-form-item is-required">
    <label class="el-form-item__label">${label}</label>
    <div class="el-form-item__content"><div class="el-select">
      <div class="el-select__tags"><span></span><input class="el-select__input" onclick="openSelect(this)"></div>
      <input class="el-input__inner" readonly>
      <div class="el-select-dropdown" style="display:none"><ul>
        ${options.map(option => `<li class="el-select-dropdown__item" onclick="choose(this)">${option}</li>`).join('')}
      </ul></div>
    </div></div>
  </div></div>`;
}

function priceField() {
  return `<div class="complex-item"><div class="el-form-item is-required">
    <label class="el-form-item__label">吊牌价</label>
    <div class="el-form-item__content">
      <input class="price-input">
      <div class="el-select"><input class="el-input__inner" readonly value="元" onclick="openSelect(this)">
        <div class="el-select-dropdown" style="display:none"><ul>
          <li class="el-select-dropdown__item" onclick="choose(this)">元</li>
        </ul></div>
      </div>
    </div>
  </div></div>`;
}

function materialField() {
  return `<div class="complex-item_multi"><div class="el-form-item is-required">
    <label class="el-form-item__label">材质成分</label>
    <div class="el-form-item__content"><div class="multi-complex-items">
      <button onclick="addMaterial()">添加</button>
    </div></div>
  </div></div>`;
}

function renderAttributes() {
  document.querySelector('#attributes').innerHTML =
    singleSelect('品牌 重要', ['NEIGBORL', '无品牌']) +
    multiSelect('图案 重要', ['纯色', '迷彩']) +
    priceField() +
    singleSelect('风格', ['休闲', '工装']) +
    multiSelect('面料', ['棉', '亚麻']) +
    singleSelect('防水等级', ['1级', '2级']) +
    singleSelect('适用性别', ['通用', '男']) +
    singleSelect('适用年龄段', ['青年', '中年']) +
    singleSelect('细分风格', ['时尚都市', '商务休闲', '工装军旅', '美式休闲']) +
    singleSelect('裤型', ['直筒', '宽松']) +
    singleSelect('裤型', ['直筒', '宽松']) +
    multiSelect('款式细节', ['口袋', '口袋', '多口袋']) +
    materialField();
  attachSelectComponents(document.querySelector('#attributes'));
  const materialRoot = document.querySelector('.multi-complex-items');
  materialRoot.__vue__ = {
    key: 'material_prop',
    childrenUI: [
      {name: 'material_prop_name', label: '材质', options: [
        {displayName: '棉', value: 'cotton-id'},
        {displayName: '亚麻', value: 'linen-id'}
      ]},
      {name: 'material_prop_content', label: '含量(%)'}
    ],
    list: [],
    updateForm(fields) {
      this.list = fields[this.key];
      window.materialState = JSON.parse(JSON.stringify(this.list));
    },
    $nextTick(callback) { setTimeout(callback, 0); }
  };
}

function setSelectValue(select, value) {
  const tags = select.querySelector('.el-select__tags');
  if (tags) {
    const tag = document.createElement('span');
    tag.className = 'el-tag';
    tag.append(document.createTextNode(value));
    const close = document.createElement('i');
    close.className = 'el-tag__close';
    close.onclick = () => tag.remove();
    tag.append(close);
    tags.insertBefore(tag, tags.querySelector('.el-select__input'));
  } else {
    const input = select.querySelector('input.el-input__inner');
    input.value = value;
    input.setAttribute('readonly', 'readonly');
  }
  select.querySelector('.el-select-dropdown').style.display = 'none';
}

function attachSelectComponents(root) {
  root.querySelectorAll('.el-select').forEach(select => {
    select.__vue__ = {
      multiple: Boolean(select.querySelector('.el-select__tags')),
      handleOptionSelect(option) { setSelectValue(select, option.value); },
      $nextTick(callback) { setTimeout(callback, 0); }
    };
  });
}

function openSelect(input) {
  const select = input.closest('.el-select');
  input.removeAttribute('readonly');
  select.querySelector('.el-select-dropdown').style.display = 'block';
}

document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  document.querySelectorAll('.el-select-dropdown').forEach(dropdown => {
    dropdown.style.display = 'none';
  });
});

function choose(option) {
  const select = option.closest('.el-select');
  const value = option.textContent.trim();
  setSelectValue(select, value);
}

function addMaterial() {
  const root = document.querySelector('.multi-complex-items');
  if (root.__vue__) {
    root.__vue__.list = root.__vue__.list.concat([{
      material_prop_name: undefined,
      material_prop_content: undefined
    }]);
  }
  root.insertAdjacentHTML('beforeend', `<div class="material-row">
    <div class="el-select"><input class="el-input__inner" readonly onclick="openSelect(this)">
      <div class="el-select-dropdown" style="display:none"><ul>
        <li class="el-select-dropdown__item" onclick="choose(this)">棉</li>
        <li class="el-select-dropdown__item" onclick="choose(this)">亚麻</li>
      </ul></div>
    </div>
    <input class="percentage-input">
    <button onclick="this.closest('.material-row').remove()">移除</button>
  </div>`);
  attachSelectComponents(root.querySelector('.material-row:last-child'));
}

window.actionLog = [];

function simpleSelect(options) {
  return `<div class="el-select" caninputcustom="true"><input class="el-input__inner" readonly value="" onclick="openSelect(this)">
    <div class="el-select-dropdown" style="display:none"><ul>
      ${options.map(option => `<li class="el-select-dropdown__item" onclick="choose(this)">${option}</li>`).join('')}
    </ul></div></div>`;
}

function storeCategoryRow(shop) {
  return `<div class="item"><div class="shopname"><span title="${shop}">${shop}</span></div>
    <div class="el-cascader"><input class="el-input__inner" readonly placeholder="请选择店铺分类" onclick="this.parentElement.querySelector('.el-cascader__dropdown').style.display='block'">
      <div class="el-cascader__tags"></div><div class="el-cascader__dropdown" style="display:none">
        <div class="el-cascader-node"><span class="el-checkbox" style="display:inline-block;width:14px;height:14px" onclick="chooseStoreCategory(this)"></span><span class="el-cascader-node__label">工装裤</span></div>
        <div class="el-cascader-node"><span class="el-checkbox" style="display:inline-block;width:14px;height:14px" onclick="chooseStoreCategory(this)"></span><span class="el-cascader-node__label">裤子</span></div>
      </div></div></div>`;
}

function chooseStoreCategory(checkbox) {
  const cascader = checkbox.closest('.el-cascader');
  const value = checkbox.parentElement.querySelector('.el-cascader-node__label').textContent.trim();
  if ([...cascader.querySelectorAll('.el-tag')].some(tag => tag.textContent.trim() === value)) return;
  cascader.querySelector('.el-cascader__tags').insertAdjacentHTML('beforeend', `<span class="el-tag">${value}</span>`);
}

function batchItem(label, content) {
  return `<div class="sku-batch-item"><div class="sku-batch-item_label">${label}：</div>${content}</div>`;
}

function applySkuBatch() {
  window.actionLog.push('batch');
  const batch = document.querySelector('#sku-batch');
  const value = label => {
    const item = [...batch.querySelectorAll(':scope > .sku-batch-item')]
      .find(candidate => candidate.querySelector('.sku-batch-item_label').textContent.trim() === label + '：');
    return item.querySelector('input').value;
  };
  document.querySelectorAll('#sku-table tbody tr').forEach(row => {
    row.querySelector('.price').value = value('价格');
    row.querySelector('.quantity').value = value('数量');
    row.querySelector('.fleece').value = value('是否加绒');
    row.querySelector('.sku-category').value = value('SKU分类');
    row.querySelector('.body-type').value = value('适用体型');
    row.querySelector('.pants-length').value = value('裤长');
  });
}

function skuRow(index) {
  return `<tr><td><input class="price"></td><td><input class="quantity"></td>
    <td><input class="platform-code" value="NGBL-10588-${index}"></td>
    <td><input class="fleece"></td><td><input class="sku-category"></td>
    <td><input class="body-type"></td><td><input class="pants-length"></td></tr>`;
}

function extendedFields() {
  return `<div class="el-form-item"><label class="el-form-item__label">店铺中分类</label>
    <div class="ship-wrp">${storeCategoryRow('其他店铺')}${storeCategoryRow('钊叔制')}</div></div>
    <div class="wrap-item"><div class="wrap-item_label">商品规格</div>
      <div class="block-wrap block-specification">
        <div class="title-bg"><input value="颜色"><label class="el-checkbox"><input type="checkbox"></label></div>
        <div class="title-bg"><input value="是否加绒"><label class="el-checkbox"><input type="checkbox"></label></div>
        <div class="title-bg"><input value="尺码"><label class="el-checkbox"><input type="checkbox" checked></label></div>
        <div class="title-bg"><input value="颜色"><label class="el-checkbox"><input type="checkbox" checked></label></div>
      </div></div>
    <div class="wrap-item"><div class="wrap-item_label">商品明细</div>
      <div class="block-specification-list"><div class="sku-batch-row"></div>
      <div class="sku-batch-row" id="sku-batch">
        ${batchItem('价格', '<input class="el-input__inner">')}
        ${batchItem('数量', '<input class="el-input__inner">')}
        ${batchItem('平台规格编码', '<input class="el-input__inner">')}
        ${batchItem('是否加绒', simpleSelect(['否', '是']))}
        ${batchItem('SKU分类', simpleSelect(['单品', '套餐']))}
        ${batchItem('适用体型', simpleSelect(['梨型', '直筒型']))}
        ${batchItem('裤长', simpleSelect(['长裤', '九分裤']))}
        <button onclick="applySkuBatch()">批量设置</button>
      </div>
      <div id="sku-table" class="el-table__main-wrapper">
        <div class="el-table__header-wrapper"><table><thead><tr><th>价格</th><th>数量</th><th>平台规格编码</th><th>是否加绒</th><th>SKU分类</th><th>适用体型</th><th>裤长</th></tr></thead></table></div>
        <div class="el-table__body-wrapper"><table><tbody>${[1,2,3,4,5].map(skuRow).join('')}</tbody></table></div>
      </div></div></div>
    <div class="wrap-item"><div class="wrap-item_label">尺码表</div>
      <div class="wrap-item_content">
        <label class="el-checkbox"><input type="checkbox">衣长（cm）</label>
        <label class="el-checkbox"><input type="checkbox" checked>裤长（cm）</label>
        <div class="el-table size-chart-table">
          <div class="el-table__header-wrapper"><table><thead><tr>
            <th>尺码</th><th>裤长（cm） 区间</th><th>操作</th>
          </tr></thead></table></div>
          <div class="el-table__body-wrapper"><table><tbody>
            ${['S','M','L','XL','2XL'].map(size => `<tr><td>${size}</td><td><input></td><td>清空</td></tr>`).join('')}
          </tbody></table></div>
        </div>
      </div>
    </div>
    <div class="el-form-item"><label class="el-form-item__label">一口价</label><input name="price"></div>
    <div class="el-form-item"><label class="el-form-item__label">商家编码</label><input name="outerId"></div>
    <div class="el-form-item"><label class="el-form-item__label">库存扣减方式</label>
      <label class="el-radio" onclick="window.actionLog.push('stock')"><input type="radio" name="stock"><span class="el-radio__label">拍下减库存</span></label>
      <label class="el-radio" onclick="window.actionLog.push('stock')"><input type="radio" name="stock"><span class="el-radio__label">付款减库存</span></label></div>
    <div class="el-form-item"><label class="el-form-item__label">售后服务</label>
      <label class="el-checkbox" onclick="window.actionLog.push('warranty')"><input type="checkbox"><span class="el-checkbox__label">保修服务</span></label>
      <label class="el-checkbox" onclick="window.actionLog.push('seven-day-return')"><input type="checkbox"><span class="el-checkbox__label">服务承诺：该类商品，必须支持【七天退货】服务；设置了定制的SKU除外，详见</span></label></div>
    <div class="el-form-item"><label class="el-form-item__label">上架时间</label>
      <label class="el-radio"><input type="radio" name="listing"><span class="el-radio__label">立刻上架</span></label>
      <label class="el-radio"><input type="radio" name="listing"><span class="el-radio__label">放入仓库</span></label></div>
    <div class="set-ship"><span class="shop-title">其他店铺</span>${simpleSelect(['其他模板'])}</div>
    <div class="set-ship"><span class="shop-title">钊叔制</span>${simpleSelect(['新疆，西藏，不包邮-T恤，裤子，装饰品'])}</div>`;
}

function applyCategory(button) {
  const row = button.parentElement;
  const path = row.querySelector('.path').textContent;
  const display = document.querySelector('.platform-category-input');
  display.firstChild.nodeValue = path + ' ';
  display.querySelectorAll('.prediction-item').forEach(item => item.remove());
  renderAttributes();
}

function openCategoryDialog() {
  document.querySelector('[role=dialog][aria-label="修改类目"]').style.display = 'block';
}

function searchCategory(input) {
  document.querySelector('#category-tree').style.display = input.value === '休闲裤'
    ? 'block' : 'none';
}

function chooseCategoryNode(node, className) {
  node.classList.add(className);
  if (node.classList.contains('casual-pants')) {
    document.querySelector('#selected-category').textContent = '已选： 男装 > 休闲裤';
  }
}

function confirmCasualPantsCategory() {
  const dialog = document.querySelector('[role=dialog][aria-label="修改类目"]');
  if (!dialog.querySelector('.menswear.in-active-path')
      || !dialog.querySelector('.casual-pants.is-active')) return;
  const display = document.querySelector('.platform-category-input');
  display.innerHTML = '男装 > 休闲裤 <button onclick="openCategoryDialog()">修改类目</button>';
  dialog.style.display = 'none';
  renderAttributes();
}

function openTaobao() {
  document.querySelector('[role=tab]').setAttribute('aria-selected', 'true');
  document.querySelector('#slot').innerHTML = `<section role="tabpanel" aria-label="淘宝资料">
    <div class="platform-category-input">男装 > 时尚工装裤
      <button onclick="openCategoryDialog()">修改类目</button>
      <div class="prediction-item"><span>其他</span><span class="path">固定错误路径</span><button onclick="applyCategory(this)">点击使用</button></div>
      <div class="prediction-item"><span>推荐</span><span class="path">服饰 > 动态测试类目</span><button onclick="applyCategory(this)">点击使用</button></div>
    </div>
    <div role="dialog" aria-label="修改类目" style="display:none">
      <input placeholder="请输入类目关键词，支持模糊查询" oninput="searchCategory(this)">
      <div id="category-tree" style="display:none">
        <div class="el-cascader-menu"><div class="el-cascader-node menswear" onclick="chooseCategoryNode(this, 'in-active-path')"><span class="el-cascader-node__label">男装</span></div></div>
        <div class="el-cascader-menu"><div class="el-cascader-node casual-pants" onclick="chooseCategoryNode(this, 'is-active')"><span class="el-cascader-node__label">休闲裤</span></div></div>
      </div>
      <div id="selected-category"></div>
      <button onclick="this.closest('[role=dialog]').style.display='none'">取 消</button>
      <button onclick="confirmCasualPantsCategory()">确 定</button>
    </div>
    <div class="conf"><div class="complex-wrap" id="attributes"></div>${extendedFields()}</div>
  </section>`;
  const panel = document.querySelector('[role=tabpanel]');
  attachSelectComponents(panel);
  const specificationRoot = panel.querySelector('.block-specification');
  specificationRoot.__vue__ = {
    $el: specificationRoot,
    platform: 'tb',
    specifications: [
      {name: '颜色', checked: false},
      {name: '是否加绒', checked: false},
      {name: '尺码', checked: true},
      {name: '颜色', checked: true}
    ],
    $set(target, key, value) { target[key] = value; },
    onChangeSpCheckbox() {
      window.specificationState = JSON.parse(JSON.stringify(this.specifications));
    },
    removeSpName(index) {
      window.removeSpNameCalls = (window.removeSpNameCalls || 0) + 1;
      this.specifications.splice(index, 1);
      window.specificationState = JSON.parse(JSON.stringify(this.specifications));
    },
    $nextTick(callback) { setTimeout(callback, 0); }
  };
  panel.__vue__ = {
    $el: panel,
    platform: 'tb',
    specificationsValidResult: true,
    async valid_specifications() {
      this.specificationsValidResult = true;
      return true;
    }
  };
  const salePropRoot = panel.querySelector('.wrap-item:has(.block-specification)');
  salePropRoot.__vue__ = {
    $el: salePropRoot,
    platform: 'tb',
    method_getData() {
      return [
        specificationRoot.__vue__.specifications
          .filter(specification => specification.checked)
          .map(specification => ({...specification})),
        [],
        {},
        0
      ];
    }
  };
}
</script>
"""


class TaobaoPureFunctionTests(unittest.TestCase):
    def test_garment_fit_alias_is_bidirectional(self):
        self.assertIn(normalize_label("服饰版型"), excel_aliases("服装版型"))
        self.assertIn(normalize_label("服装版型"), excel_aliases("服饰版型"))

    def test_value_aliases_are_explicit_and_keep_original_first(self):
        self.assertEqual(value_candidates("风格", "休闲风"), ("休闲风", "休闲"))
        self.assertEqual(
            value_candidates("风格", "休闲风/时尚都市"),
            ("休闲风", "休闲", "时尚都市"),
        )
        self.assertEqual(
            value_candidates("细分风格", "休闲风/时尚都市"),
            ("休闲风", "时尚都市"),
        )
        self.assertEqual(
            value_candidates("上市年份季节", "2026/2026年秋季"),
            ("2026", "2026年秋季"),
        )
        self.assertEqual(
            value_candidates("款式细节", "口袋/多口袋"),
            ("口袋", "多口袋"),
        )
        self.assertEqual(
            value_candidates("弹力", "无弹"),
            ("无弹", "无弹力"),
        )

    def test_fabric_and_material_composition_use_separate_excel_fields(self):
        fields = {
            "面料材质/面料": "亚麻（100%）",
            "材质成分/材质": "棉/100%",
        }
        self.assertEqual(
            [(item.name, item.percentage) for item in parse_taobao_fabrics(fields)],
            [("亚麻", 100)],
        )
        self.assertEqual(
            [(item.name, item.percentage) for item in parse_taobao_materials(fields)],
            [("棉", 100)],
        )

    def test_selection_commas_are_all_and_slashes_are_or(self):
        self.assertEqual(
            selection_value_groups("款式细节", "口袋/多口袋，拉链"),
            (("口袋", "多口袋"), ("拉链",)),
        )

    def test_material_validation_requires_matching_clean_form_state(self):
        valid = {
            "component": {
                "found": True,
                "key": "p-material",
                "rows": [
                    {"material_prop_name": "棉", "material_prop_content": "100"}
                ],
            },
            "form": {
                "found": True,
                "message": "",
                "fields": [
                    {
                        "prop": "p-material",
                        "validateState": "",
                        "validateMessage": "",
                        "fieldValue": [
                            {
                                "material_prop_name": "棉",
                                "material_prop_content": "100",
                            }
                        ],
                    }
                ],
            },
        }

        self.assertTrue(TaobaoListing._material_validation_is_confirmed(valid))
        valid["form"]["fields"][0]["validateMessage"] = "材质成分子项请勿留空"
        self.assertFalse(TaobaoListing._material_validation_is_confirmed(valid))


class TaobaoListingFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        await self.page.set_content(TAOBAO_FIXTURE)
        self.listing = TaobaoListing(
            self.page,
            self.page.locator("#drawer"),
            LOGGER,
        )
        await self.listing.open()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_clicks_marker_recommendation_without_fixed_category_path(self):
        actual = await self.listing.apply_recommended_category()

        self.assertEqual(actual, "服饰 > 动态测试类目")
        self.assertTrue(self.listing.category_clicked)

    async def test_casual_pants_mode_searches_exact_flat_result(self):
        await self.page.set_content(
            """
            <meta charset="utf-8">
            <section id="panel">
              <div class="platform-category-input">男装 &gt; 时尚工装裤
                <button onclick="document.querySelector('#dialog').style.display='block'">修改类目</button>
              </div>
              <div class="conf"><div class="complex-wrap"></div></div>
            </section>
            <div role="dialog" aria-label="修改类目" id="dialog" style="display:none">
              <input placeholder="请输入类目关键词，支持模糊查询"
                oninput="document.querySelector('#suggestions').style.display='block'">
              <div id="selected">已选：</div>
              <button onclick="confirmCategory()">确定</button>
            </div>
            <div class="el-autocomplete-suggestion el-popper" id="suggestions"
              style="display:none">
              <ul><li onclick="selectCategory()">男装 &gt; <span>休闲裤</span></li></ul>
            </div>
            <script>
              function selectCategory() {
                document.querySelector('#selected').textContent = '已选：男装 > 休闲裤';
                document.querySelector('#suggestions').style.display = 'none';
              }
              function confirmCategory() {
                document.querySelector('.platform-category-input').innerHTML =
                  '男装 > 休闲裤 <button>修改类目</button>';
                document.querySelector('.complex-wrap').innerHTML =
                  '<div class="complex-item">属性</div>';
                document.querySelector('#dialog').style.display = 'none';
              }
            </script>
            """
        )
        listing = TaobaoListing(
            self.page,
            self.page.locator("#panel"),
            LOGGER,
        )
        listing.panel = self.page.locator("#panel")

        actual = await listing.apply_casual_pants_category()

        self.assertEqual(actual, "男装 > 休闲裤")
        self.assertTrue(listing.category_clicked)

    async def test_recommended_mode_remains_available(self):
        actual = await self.listing.apply_category("recommended")

        self.assertEqual(actual, "服饰 > 动态测试类目")

    async def test_pants_size_chart_fills_length_by_size(self):
        actual = await self.listing.fill_size_chart_lengths(
            (
                SizeLength("S", 104),
                SizeLength("M", 106),
                SizeLength("L", 108),
                SizeLength("XL", 110),
                SizeLength("2XL", 112),
            ),
            "pants",
        )

        self.assertEqual(actual["field"], "裤长（cm）")
        self.assertEqual(
            await self.page.locator(
                ".size-chart-table > .el-table__body-wrapper input"
            ).evaluate_all("inputs => inputs.map(input => input.value)"),
            ["104", "106", "108", "110", "112"],
        )

    async def test_pants_size_chart_infers_fixed_size_column_when_header_is_blank(self):
        await self.page.locator(
            ".size-chart-table > .el-table__header-wrapper th"
        ).first.evaluate("element => { element.textContent = '' }")

        actual = await self.listing.fill_size_chart_lengths(
            (
                SizeLength("S", 104),
                SizeLength("M", 106),
                SizeLength("L", 108),
                SizeLength("XL", 110),
                SizeLength("2XL", 112),
            ),
            "pants",
        )

        self.assertEqual(actual["rows"]["2XL"], "112")

    async def test_pants_size_chart_reads_sizes_from_element_fixed_column(self):
        await self.page.locator(".size-chart-table").evaluate(
            """table => {
              table.querySelector('.el-table__header-wrapper th').textContent = '';
              const mainRows = table.querySelectorAll('.el-table__body-wrapper tbody > tr');
              mainRows.forEach(row => { row.querySelector('td').textContent = '' });
              table.insertAdjacentHTML('beforeend', `<div class="el-table__fixed">
                <div class="el-table__fixed-body-wrapper"><table><tbody>
                  ${['S','M','L','XL','2XL'].map(size => `<tr><td>${size}</td></tr>`).join('')}
                </tbody></table></div></div>`);
            }"""
        )

        actual = await self.listing.fill_size_chart_lengths(
            (
                SizeLength("S", 104),
                SizeLength("M", 106),
                SizeLength("L", 108),
                SizeLength("XL", 110),
                SizeLength("2XL", 112),
            ),
            "pants",
        )

        self.assertEqual(actual["rows"]["S"], "104")
        self.assertEqual(actual["rows"]["2XL"], "112")

    async def test_excel_matching_fills_exact_and_explicit_alias_values(self):
        fields = parse_taobao_fields(
            {
                "品牌": "NEIGBORL",
                "图案": "纯色",
                "吊牌价/价格/基本售价": 586,
                "风格/细分风格/基础风格": "休闲风/时尚都市",
                "面料材质/面料": "棉（100%）",
                "材质成分/材质": "棉/100%",
                "防水等级": "2级",
                "适用性别": "通用",
                "适用年龄段": "青年",
            }
        )

        report = await self.listing.apply_excel_attributes(fields)

        self.assertEqual(report["category"], "服饰 > 动态测试类目")
        self.assertEqual(report["attributes"]["品牌"], ("NEIGBORL",))
        self.assertEqual(report["attributes"]["图案"], ("纯色",))
        self.assertEqual(report["attributes"]["吊牌价"], ("586",))
        self.assertEqual(report["attributes"]["风格"], ("休闲",))
        self.assertEqual(report["attributes"]["面料"], ("棉",))
        self.assertEqual(report["attributes"]["适用性别"], ("通用",))
        self.assertEqual(report["attributes"]["细分风格"], ("时尚都市",))
        self.assertEqual(report["attributes"]["材质成分"], ("棉100%",))
        self.assertNotIn("防水等级", report["attributes"])
        self.assertNotIn("适用年龄段", report["attributes"])
        self.assertEqual(report["ignored_fields"], ("防水等级", "适用年龄段"))
        self.assertEqual(report["skipped_values"], {})
        self.assertEqual(
            tuple(report["attributes"]),
            (
                "品牌",
                "图案",
                "吊牌价",
                "风格",
                "面料",
                "适用性别",
                "细分风格",
                "材质成分",
            ),
        )
        self.assertEqual(
            await self.page.locator(".material-row").count(),
            1,
        )
        self.assertEqual(
            await self.page.locator(".material-row .el-select input").input_value(),
            "棉",
        )
        self.assertEqual(
            await self.page.locator(".material-row .percentage-input").input_value(),
            "100",
        )
        self.assertEqual(
            await self.page.evaluate("window.materialState"),
            [{"material_prop_name": "cotton-id", "material_prop_content": "100"}],
        )

    async def test_material_sync_does_not_hang_when_vue_next_tick_stalls(self):
        await self.listing.apply_recommended_category()
        materials = parse_taobao_materials({"材质成分/材质": "棉/100%"})
        await self.listing.fill_materials(materials)
        await self.page.locator(".multi-complex-items").evaluate(
            "root => { root.__vue__.$nextTick = () => {}; }"
        )

        actual = await asyncio.wait_for(
            self.listing.fill_materials(materials),
            timeout=2.0,
        )

        self.assertEqual(actual, (("棉", 100),))

    async def test_required_error_scan_uses_live_dom_after_attribute_rerender(self):
        await self.listing.apply_recommended_category()
        old_items = await self.listing._attribute_items()
        old_last_item = tuple(old_items.values())[-1][1]

        await self.page.locator("#attributes > :first-child").evaluate(
            "element => element.remove()"
        )

        actual = await asyncio.wait_for(
            self.listing._required_attribute_errors(),
            timeout=1.0,
        )

        self.assertEqual(actual, ())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                old_last_item.locator(":scope > .el-form-item").get_attribute("class"),
                timeout=0.2,
            )

    async def test_duplicate_same_name_option_is_skipped_instead_of_guessed(self):
        await self.listing.apply_recommended_category()

        actual = await self.listing.fill_attribute("款式细节", "口袋")

        self.assertIsNone(actual)
        self.assertEqual(
            await self.page.locator(
                ".complex-item:has(.el-form-item__label:text-is('款式细节')) .el-tag"
            ).count(),
            0,
        )

    async def test_or_value_prefers_unique_later_platform_option(self):
        await self.listing.apply_recommended_category()

        actual = await self.listing.fill_attribute("款式细节", "口袋/多口袋")

        self.assertEqual(actual, ("多口袋",))

    async def test_or_value_uses_first_same_name_platform_item_as_last_resort(self):
        await self.listing.apply_recommended_category()
        await self.page.locator(
            ".complex-item:has(.el-form-item__label:text-is('款式细节')) "
            ".el-select-dropdown__item:text-is('多口袋')"
        ).evaluate("element => element.remove()")

        actual = await self.listing.fill_attribute("款式细节", "口袋/多口袋")

        self.assertEqual(actual, ("口袋",))

    async def test_duplicate_page_labels_are_both_filled_and_reported(self):
        fields = parse_taobao_fields(
            {
                "细分风格": "休闲风",
                "裤型": "不存在/直筒",
                "材质成分/材质": "棉/100%",
            }
        )

        report = await self.listing.apply_excel_attributes(fields)

        self.assertEqual(report["attributes"]["裤型"], ("直筒",))
        self.assertEqual(report["attributes"]["裤型#2"], ("直筒",))
        values = await self.page.locator(
            ".complex-item:has(.el-form-item__label:text-is('裤型')) "
            "input.el-input__inner"
        ).evaluate_all("elements => elements.map(element => element.value)")
        self.assertEqual(values, ["直筒", "直筒"])

    async def test_extended_fields_fill_only_target_shop_and_preserve_platform_codes(self):
        fields = parse_taobao_fields(
            {
                "店铺中分类": "工装裤，裤子",
                "吊牌价/价格/基本售价": 586,
                "价格/京东价/市场价/售卖价/售价": 586,
                "数量": 100,
                "是否加绒": "未知/否",
                "SKU分类": "不存在/单品",
                "适用体型": "不存在/通用型",
                "一口价": 586,
                "货号/商家外部编码": "NGBL-10588",
                "拍下减库存": "未知/否",
                "商品状态": "不存在/立刻上架",
                "运费设置": "新疆，西藏，不包邮-T恤，裤子，装饰品",
            }
        )

        report = await self.listing.apply_extended_fields(fields)

        self.assertEqual(
            report["store_category"],
            {"shop": "钊叔制", "values": ("工装裤", "裤子")},
        )
        self.assertEqual(report["specification"]["name"], "颜色")
        self.assertFalse(report["specification"]["checked"])
        self.assertEqual(
            report["specification"]["component_state"]["after"],
            [
                {"name": "颜色", "checked": False},
                {"name": "是否加绒", "checked": False},
                {"name": "尺码", "checked": True},
                {"name": "颜色", "checked": False},
            ],
        )
        self.assertEqual(
            await self.page.evaluate("window.specificationState"),
            [
                {"name": "颜色", "checked": False},
                {"name": "是否加绒", "checked": False},
                {"name": "尺码", "checked": True},
                {"name": "颜色", "checked": False},
            ],
        )
        self.assertTrue(report["specification_validation"]["found"])
        self.assertTrue(report["specification_validation"]["valid"])
        self.assertEqual(report["sku_batch"]["row_count"], 5)
        self.assertTrue(report["sku_batch"]["platform_codes_preserved"])
        self.assertEqual(
            report["sku_batch"]["platform_codes"],
            tuple(f"NGBL-10588-{index}" for index in range(1, 6)),
        )
        self.assertEqual(report["sales"]["一口价"], "586")
        self.assertEqual(report["sales"]["商家编码"], "NGBL-10588")
        self.assertEqual(report["payment_service"]["库存扣减方式"], "付款减库存")
        self.assertTrue(report["payment_service"]["保修服务"])
        self.assertTrue(report["payment_service"]["七天退货承诺"])
        self.assertEqual(report["listing"]["上架时间"], "立刻上架")
        self.assertEqual(report["freight"]["shop"], "钊叔制")
        self.assertEqual(
            report["freight"]["template"],
            "新疆，西藏，不包邮-T恤，裤子，装饰品",
        )
        self.assertEqual(report["visible_validation_errors"], ())
        self.assertEqual(
            await self.page.locator(
                ".item:has(.shopname span[title='其他店铺']) .el-tag"
            ).count(),
            0,
        )
        self.assertEqual(
            await self.page.locator(
                ".set-ship:has(.shop-title:text-is('其他店铺')) input"
            ).input_value(),
            "",
        )
        self.assertEqual(
            await self.page.evaluate("window.actionLog"),
            ["batch", "stock", "warranty", "seven-day-return"],
        )

    async def test_sku_or_without_platform_match_fills_excel_first_value(self):
        await self.listing.apply_recommended_category()
        fields = parse_taobao_fields(
            {
                "价格": 586,
                "数量": 100,
                "是否加绒": "否",
                "SKU分类": "第一顺位/第二顺位",
                "适用体型": "直筒型",
            }
        )

        actual = await self.listing.fill_sku_batch(fields.fields)

        self.assertEqual(actual["values"]["SKU分类"], "第一顺位")
        self.assertTrue(
            all(row["SKU分类"] == "第一顺位" for row in actual["rows"])
        )

    async def test_sku_fleece_prefers_excel_no_when_page_uses_yes_no(self):
        await self.listing.apply_recommended_category()
        product_details = await self.listing._wrap_item("商品明细")
        batch_row = await self.listing._sku_batch_row(product_details)
        fleece_item = await self.listing._sku_batch_item(batch_row, "是否加绒")
        await fleece_item.locator(".el-select-dropdown ul").evaluate(
            """list => {
              list.innerHTML = ['否', '是'].map(value =>
                `<li class="el-select-dropdown__item" onclick="choose(this)">${value}</li>`
              ).join('');
            }"""
        )
        fields = parse_taobao_fields(
            {
                "价格": 586,
                "数量": 100,
                "是否加绒": "否",
                "SKU分类": "单品",
                "适用体型": "直筒型",
            }
        )

        actual = await self.listing.fill_sku_batch(fields.fields)

        self.assertEqual(actual["values"]["是否加绒"], "否")
        self.assertTrue(all(row["是否加绒"] == "否" for row in actual["rows"]))

    async def test_sku_batch_fills_dynamic_pants_length_and_records_rows(self):
        await self.listing.apply_recommended_category()
        fields = parse_taobao_fields(
            {
                "价格": 586,
                "数量": 100,
                "是否加绒": "否",
                "SKU分类": "单品",
                "适用体型": "直筒型",
                "裤长": "长裤",
            }
        )

        actual = await self.listing.fill_sku_batch(fields.fields)

        self.assertEqual(actual["values"]["裤长"], "长裤")
        self.assertTrue(all(row["裤长"] == "长裤" for row in actual["rows"]))

    async def test_sku_fleece_does_not_replace_excel_value_with_alias(self):
        await self.listing.apply_recommended_category()
        product_details = await self.listing._wrap_item("商品明细")
        batch_row = await self.listing._sku_batch_row(product_details)
        fleece_item = await self.listing._sku_batch_item(batch_row, "是否加绒")
        await fleece_item.locator(".el-select-dropdown ul").evaluate(
            """list => {
              list.innerHTML = ['不加绒', '加绒'].map(value =>
                `<li class="el-select-dropdown__item" onclick="choose(this)">${value}</li>`
              ).join('');
            }"""
        )
        fields = parse_taobao_fields(
            {
                "价格": 586,
                "数量": 100,
                "是否加绒": "否",
                "SKU分类": "单品",
                "适用体型": "直筒型",
            }
        )

        actual = await self.listing.fill_sku_batch(fields.fields)

        self.assertEqual(actual["values"]["是否加绒"], "否")
        self.assertNotEqual(actual["values"]["是否加绒"], "不加绒")

    async def test_seven_day_return_uses_first_operable_duplicate_checkbox(self):
        await self.listing.apply_recommended_category()
        form_item = await self.listing._form_item("售后服务")
        await form_item.evaluate(
            """element => {
              const source = [...element.querySelectorAll('label.el-checkbox')]
                .find(label => label.textContent.includes('七天退货'));
              const clone = source.cloneNode(true);
              element.append(clone);
            }"""
        )

        actual = await self.listing._ensure_checkbox_contains(
            "售后服务", "七天退货"
        )

        self.assertTrue(actual)
        self.assertEqual(
            await form_item.locator(
                "label.el-checkbox:visible input[type=checkbox]:checked"
            ).count(),
            1,
        )

    async def test_store_category_comma_selects_all_and_slash_is_or(self):
        fields = parse_taobao_fields({"店铺中分类": "不存在/工装裤，裤子"})

        actual = await self.listing.fill_store_categories(fields.fields)

        self.assertEqual(actual, ("工装裤", "裤子"))

    async def test_single_color_spec_is_normal_and_not_unchecked(self):
        await self.page.evaluate(
            """() => {
              const root = document.querySelector('.block-specification');
              const rows = [...root.querySelectorAll('.title-bg')];
              rows.slice(0, -1).forEach(row => row.remove());
              root.__vue__.specifications = [{name: '颜色', checked: true}];
            }"""
        )

        actual = await self.listing.uncheck_spec_name("颜色")

        self.assertIsNone(actual)
        self.assertTrue(
            await self.page.locator(
                ".block-specification .title-bg input[type=checkbox]"
            ).is_checked()
        )
        self.assertFalse(self.listing.specification_state["duplicate"])
        self.assertEqual(self.listing.specification_state["match_count"], 1)

    async def test_duplicate_color_warning_keeps_last_item_unchecked(self):
        await self.page.evaluate(
            """() => {
              const panel = document.querySelector('[role=tabpanel]');
              panel.__vue__.asyncValidCalls = 0;
              panel.__vue__.valid_specifications = async function () {
                this.asyncValidCalls += 1;
                const specifications = document.querySelector(
                  '.block-specification'
                ).__vue__.specifications;
                const colorCount = specifications.filter(
                  specification => specification.name === '颜色'
                ).length;
                this.specificationsValidResult = colorCount > 1
                  ? '规格名【颜色】重复'
                  : true;
                return colorCount <= 1;
              };
            }"""
        )
        fields = parse_taobao_fields(
            {
                "店铺中分类": "工装裤，裤子",
                "吊牌价/价格/基本售价": 586,
                "价格/京东价/市场价/售卖价/售价": 586,
                "数量": 100,
                "是否加绒": "否",
                "SKU分类": "单品",
                "适用体型": "通用型/直筒型",
                "一口价": 586,
                "货号/商家外部编码": "NGBL-10588",
                "拍下减库存": "否",
                "商品状态": "立刻上架",
                "运费设置": "新疆，西藏，不包邮-T恤，裤子，装饰品",
            }
        )

        report = await self.listing.apply_extended_fields(fields)

        self.assertFalse(report["specification"]["duplicate_removal"]["removed"])
        self.assertEqual(await self.page.evaluate("window.removeSpNameCalls || 0"), 0)
        self.assertEqual(
            report["specification"]["component_state"]["after"],
            [
                {"name": "颜色", "checked": False},
                {"name": "是否加绒", "checked": False},
                {"name": "尺码", "checked": True},
                {"name": "颜色", "checked": False},
            ],
        )
        self.assertTrue(report["specification_validation"]["valid"])
        self.assertEqual(
            report["specification_validation"]["ignored_result"],
            "规格名【颜色】重复",
        )
        self.assertEqual(
            report["specification_payload"]["specifications"],
            [
                {"name": "尺码", "checked": True},
            ],
        )
        self.assertEqual(
            await self.page.evaluate(
                "document.querySelector('[role=tabpanel]').__vue__.asyncValidCalls"
            ),
            1,
        )

    async def test_store_category_collapse_count_is_not_a_selected_value(self):
        form_item = await self.listing._form_item("店铺中分类")
        row = await self.listing._store_row(form_item, "钊叔制")
        cascader = row.locator(".el-cascader").first
        await cascader.locator(".el-cascader__tags").evaluate(
            """element => {
              element.innerHTML = '<span class="el-tag">工装裤</span>'
                + '<span class="el-tag">+ 1</span>';
            }"""
        )

        actual = await self.listing._read_cascader_values(cascader)

        self.assertEqual(actual, ("工装裤",))


if __name__ == "__main__":
    unittest.main()
