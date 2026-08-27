import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from douyin_data import MaterialComponent
from douyin_listing import (
    DouyinListing,
    DouyinListingError,
    choose_unique_option,
    normalize_option,
)


LOGGER = logging.getLogger("douyin-listing-tests")


class OptionMatchingTests(unittest.TestCase):
    def test_normalize_option_ignores_only_layout_punctuation(self):
        self.assertEqual(normalize_option(" 棉（100）-A_B， "), "棉100ab")

    def test_choose_unique_option_returns_exact_normalized_match(self):
        options = ({"name": "常规款", "id": "1"}, {"name": "加绒加厚款", "id": "2"})
        self.assertEqual(choose_unique_option("常 规-款", options)["id"], "1")

    def test_choose_unique_option_rejects_ambiguous_match_with_candidates(self):
        options = ({"name": "纯棉", "id": "1"}, {"name": "纯-棉", "id": "2"})
        with self.assertRaisesRegex(DouyinListingError, r"匹配数为 2.*纯棉.*纯-棉"):
            choose_unique_option("纯棉", options)

    def test_choose_unique_option_rejects_missing_match_with_candidates(self):
        options = ({"name": "棉", "id": "1"}, {"name": "亚麻", "id": "2"})
        with self.assertRaisesRegex(DouyinListingError, r"匹配数为 0.*棉.*亚麻"):
            choose_unique_option("羊毛", options)


DOUYIN_FIXTURE = r"""
<meta charset="utf-8">
<div id="prod-center-edit-dialog">
  <div role="tablist">
    <button role="tab" aria-selected="true">基础资料</button>
    <button id="douyin-tab" role="tab" aria-selected="false" onclick="openDouyin()">抖音资料</button>
  </div>
  <div id="slot"></div>
</div>
<script>
function selectBox(label, values, multi=false) {
  const options = values.map(value =>
    `<li class="el-select-dropdown__item" onclick="pickOption(this, '${value}', ${multi})">${value}</li>`
  ).join('');
  const tags = multi
    ? '<div class="el-select__tags"><input class="el-select__input" onclick="openSelect(this)"></div>'
    : '';
  return `<div class="attr-item"><div class="el-form-item">
    <label class="el-form-item__label">${label}</label>
    <div class="el-form-item__content"><div class="el-select">
      ${tags}<input class="el-input__inner" readonly placeholder="请选择" onclick="openSelect(this)">
      <div class="el-select-dropdown" style="display:none"><ul>${options}</ul></div>
    </div></div>
  </div></div>`;
}

function materialRow() {
  return `<div class="measure-item">
    <div class="el-select">
      <input class="el-input__inner" readonly placeholder="请选择" onclick="openSelect(this)">
      <div class="el-select-dropdown" style="display:none"><ul>
        <li class="el-select-dropdown__item" onclick="pickOption(this, '棉', false)">棉</li>
        <li class="el-select-dropdown__item" onclick="pickOption(this, '亚麻', false)">亚麻</li>
      </ul></div>
    </div>
    <div class="el-input el-input-digit"><input class="el-input__inner"></div>
    <i class="el-icon-delete" onclick="this.closest('.measure-item').remove()"></i>
  </div>`;
}

function openSelect(element) {
  element.closest('.el-select').querySelector('.el-select-dropdown').style.display = 'block';
}

function pickOption(option, value, multi) {
  const select = option.closest('.el-select');
  if (multi) {
    const tags = select.querySelector('.el-select__tags');
    if (![...tags.querySelectorAll('.el-tag')].some(tag => tag.dataset.value === value)) {
      const tag = document.createElement('span');
      tag.className = 'el-tag';
      tag.dataset.value = value;
      tag.append(document.createTextNode(value));
      const close = document.createElement('i');
      close.className = 'el-tag__close';
      close.onclick = () => tag.remove();
      tag.append(close);
      tags.insertBefore(tag, tags.querySelector('.el-select__input'));
    }
  } else {
    select.querySelector('input.el-input__inner').value = value;
    select.querySelector('.el-select-dropdown').style.display = 'none';
  }
}

function renderAttributes() {
  document.querySelector('#attributes').innerHTML =
    selectBox('厚度', ['常规款', '常规款（加厚）'], false) +
    selectBox('里料材质', ['棉', '亚麻', '棉麻'], true) +
    `<div class="attr-item"><div class="el-form-item">
      <label class="el-form-item__label">面料材质</label>
      <div class="el-form-item__content">
        <div class="sc-upload"></div>
        <div class="measure-wrap"><div id="material-rows">${materialRow()}</div>
          <button type="button" onclick="document.querySelector('#material-rows').insertAdjacentHTML('beforeend', materialRow())">+ 添加材质</button>
        </div>
      </div>
    </div></div>`;
}

function applyCategory(button) {
  const row = button.closest('.prediction-item');
  const category = row.querySelector('.path').textContent;
  const display = document.querySelector('.platform-category-input');
  display.firstChild.nodeValue = category.replaceAll('>', ' > ') + ' ';
  renderAttributes();
}

function openDouyin() {
  document.querySelector('[role=tab][aria-selected=true]').setAttribute('aria-selected', 'false');
  document.querySelector('#douyin-tab').setAttribute('aria-selected', 'true');
  setTimeout(() => {
    document.querySelector('#slot').innerHTML = `<section role="tabpanel" aria-label="抖音资料">
      <div class="el-form-item">
        <label class="el-form-item__label">商品分类:</label>
        <div class="el-form-item__content"><div class="platform-category-input">请选择类目
          <div class="prediction-item"><span>推荐</span><span class="path">服装>男装>休闲裤</span><button onclick="applyCategory(this)">点击使用</button></div>
        </div></div>
      </div>
      <div class="el-form-item">
        <label class="el-form-item__label">导购短标题</label>
        <div class="el-form-item__content"><input placeholder="建议填写简明准确的标题内容，避免重复表达"></div>
      </div>
      <div id="attributes"></div>
    </section>`;
  }, 60);
}
</script>
"""


class DouyinListingFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()
        await self.page.set_content(DOUYIN_FIXTURE)
        self.drawer = self.page.locator("#prod-center-edit-dialog")
        self.tempdir = tempfile.TemporaryDirectory()
        self.listing = DouyinListing(
            self.page,
            self.drawer,
            LOGGER,
            Path(self.tempdir.name),
        )

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()
        self.tempdir.cleanup()

    async def test_delayed_tab_category_title_and_exact_select_controls(self):
        await self.listing.open()
        self.assertEqual(
            await self.listing.apply_first_recommended_category(),
            "服装 > 男装 > 休闲裤",
        )
        self.assertEqual(await self.listing.fill_short_title("重磅水洗工装裤"), "重磅水洗工装裤")

        self.assertEqual(await self.listing.fill_attribute("厚度", "常 规-款"), ("常规款",))
        self.assertEqual(
            await self.listing.fill_attribute("里料材质", "棉/亚麻"),
            ("棉", "亚麻"),
        )
        tags = self.page.locator(
            ".attr-item:has(.el-form-item__label:text-is('里料材质')) .el-tag"
        )
        self.assertEqual(await tags.count(), 2)

    async def test_material_rows_selection_percentages_and_upload_adapter(self):
        await self.listing.open()
        await self.listing.apply_first_recommended_category()
        calls = []

        async def fake_sync(page, item, paths, label, timeout_seconds):
            calls.append((page, item, paths, label, timeout_seconds))
            return "skipped"

        materials = (MaterialComponent("棉", 60), MaterialComponent("亚麻", 40))
        with patch("kuaimai_erp.sync_image_group", new=fake_sync):
            actual = await self.listing.apply_materials(materials, (Path("wash-label.jpg"),))

        self.assertEqual(actual, (("棉", 60), ("亚麻", 40)))
        self.assertEqual(await self.page.locator("#material-rows .measure-item").count(), 2)
        self.assertEqual(calls[0][2], (Path("wash-label.jpg"),))
        self.assertEqual(calls[0][3], "抖音水洗标/吊牌图")

    async def test_apply_category_and_fields_uses_unique_alias_and_skips_absent_excel_field(self):
        await self.listing.open()
        fields = SimpleNamespace(
            short_title="复古工装裤",
            attributes={"厚度/厚薄": "常规款", "当前类目不存在": "应跳过"},
        )

        result = await self.listing.apply_category_and_fields(fields)

        self.assertEqual(result["category"], "服装 > 男装 > 休闲裤")
        self.assertEqual(result["short_title"], "复古工装裤")
        self.assertEqual(result["attributes"], {"厚度": ("常规款",)})

    async def test_material_total_must_be_exactly_100_before_page_changes(self):
        with self.assertRaisesRegex(DouyinListingError, "合计必须为 100"):
            await self.listing.apply_materials((MaterialComponent("棉", 99),), ())


if __name__ == "__main__":
    unittest.main()
