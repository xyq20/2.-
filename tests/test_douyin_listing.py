import asyncio
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
from size_image_recognition import SkuRecommendation


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


class _EventPage:
    url = "https://scma.superboss.cc/supplier/prod/center"

    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def emit(self, event, value):
        for callback in self.listeners.get(event, ()):
            callback(value)


class _PropertyRequest:
    url = "https://scma.superboss.cc/fxg/getCategoryProperties.json"

    def __init__(self, leaf_id):
        self.post_data = f"shopId=fixture&leafCategoryId={leaf_id}"


class _PropertyResponse:
    url = "https://scma.superboss.cc/fxg/getCategoryProperties.json"

    def __init__(self, request, property_id, option_name):
        self.request = request
        self.property_id = property_id
        self.option_name = option_name

    async def json(self):
        return {
            "result": 1,
            "data": [
                {
                    "propertyName": "厚度",
                    "propertyId": self.property_id,
                    "type": "select",
                    "options": [{"name": self.option_name, "value": "option-id"}],
                }
            ],
        }


class CategoryPropertyCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_fresh_request_generation_can_replace_category_cache(self):
        page = _EventPage()
        listing = DouyinListing(page, None, LOGGER, Path("ignored-artifacts"))

        await listing._begin_category_property_capture()
        first_request = _PropertyRequest("old-leaf")
        late_request = _PropertyRequest("old-leaf")
        page.emit("request", first_request)
        page.emit("request", late_request)
        page.emit(
            "response",
            _PropertyResponse(first_request, "old-property", "旧选项"),
        )
        await listing._drain_property_response_tasks()
        self.assertEqual((await listing._api_property("厚度"))["id"], "old-property")

        fresh_generation = await listing._begin_category_property_capture()
        self.assertIsNone(await listing._api_property("厚度"))

        # This response started during the previous generation and arrives late.
        page.emit(
            "response",
            _PropertyResponse(late_request, "late-old-property", "迟到旧选项"),
        )
        await listing._drain_property_response_tasks()
        self.assertIsNone(await listing._api_property("厚度"))

        fresh_request = _PropertyRequest("new-leaf")
        page.emit("request", fresh_request)
        page.emit(
            "response",
            _PropertyResponse(fresh_request, "fresh-property", "新选项"),
        )
        self.assertTrue(
            await listing._wait_for_fresh_category_properties(fresh_generation)
        )
        fresh_property = await listing._api_property("厚度")
        self.assertEqual(fresh_property["id"], "fresh-property")
        self.assertEqual(fresh_property["options"][0]["name"], "新选项")

    async def test_second_leaf_response_in_same_generation_forces_dom_fallback(self):
        page = _EventPage()
        listing = DouyinListing(page, None, LOGGER, Path("ignored-artifacts"))
        generation = await listing._begin_category_property_capture()
        first_request = _PropertyRequest("first-leaf")
        second_request = _PropertyRequest("second-leaf")
        page.emit("request", first_request)
        page.emit("request", second_request)
        page.emit(
            "response",
            _PropertyResponse(first_request, "first-property", "第一选项"),
        )

        wait_task = asyncio.create_task(
            listing._wait_for_fresh_category_properties(generation)
        )
        await asyncio.sleep(0.05)
        self.assertFalse(wait_task.done(), "第二个已观测请求未完成时不应定稿")

        page.emit(
            "response",
            _PropertyResponse(second_request, "second-property", "第二选项"),
        )
        self.assertFalse(await wait_task)
        self.assertIsNone(await listing._api_property("厚度"))


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
let selectSequence = 0;
window.sizeWriteCount = 0;
window.predictionRefreshCount = 0;
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    document.querySelectorAll('.el-select-dropdown').forEach(item => item.style.display = 'none');
  }
});

function selectBox(label, values, multi=false) {
  const dropdownId = `attribute-popper-${++selectSequence}`;
  const options = values.map(value =>
    `<li class="el-select-dropdown__item" onclick="pickOption(this, '${value}', ${multi})">${value}</li>`
  ).join('');
  const tags = multi
    ? `<div class="el-select__tags"><input class="el-select__input" aria-controls="${dropdownId}" onclick="openSelect(this)"></div>`
    : '';
  return `<div class="attr-item"><div class="el-form-item">
    <label class="el-form-item__label">${label}</label>
    <div class="el-form-item__content"><div class="el-select">
      ${tags}<input class="el-input__inner" readonly placeholder="请选择" aria-controls="${dropdownId}" onclick="openSelect(this)">
    </div></div>
  </div></div>
  <div id="${dropdownId}" class="el-select-dropdown el-popper" style="display:none;z-index:${1000 + selectSequence}"><ul>${options}</ul></div>`;
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

function textAttribute(label) {
  return `<div class="attr-item"><div class="el-form-item">
    <label class="el-form-item__label">${label}</label>
    <div class="el-form-item__content"><input class="el-input__inner"></div>
  </div></div>`;
}

function sizeRow(size) {
  const input = () => `<input oninput="window.sizeWriteCount += 1">`;
  return `<tr data-size="${size}">
    <td><div class="cell"><span class="size-name">${size}</span></div></td>
    <td><div class="cell">${input()}</div></td>
    <td><div class="cell">${input()}</div></td>
    <td><div class="cell">${input()}</div></td>
    <td><div class="cell">${input()}</div></td>
    <td><div class="cell">${input()}</div></td>
  </tr>`;
}

function sizeTable() {
  const header = (label, help='') => `<th><div class="cell">
    <p>${label}</p><p class="header-ruleTip">${help}</p>
  </div></th>`;
  const headerRow = `<tr>
      ${header('尺码')}
      ${header('身高(cm)', '请填写40-220之间的数值')}
      ${header('体重(斤)', '请填写4-320之间的数值')}
      ${header('腰围(cm)', '请填写20-200之间的数值')}
      ${header('臀围(cm)', '请填写50-200之间的数值')}
      ${header('裤长(cm)', '请填写15-150之间的数值')}
    </tr>`;
  return `<div class="el-table size-recommend-table">
    <div class="el-table__header-wrapper"><table><thead>${headerRow}</thead></table></div>
    <div class="el-table__body-wrapper"><table><tbody id="size-rows"></tbody></table></div>
    <div class="el-table__fixed-right">
      <div class="el-table__fixed-header-wrapper"><table><thead>${headerRow}</thead></table></div>
    </div>
  </div>`;
}

function deliveryInventory() {
  const skuRow = size => `<tr data-sku="${size}">
    <td><div class="cell">${size}</div></td>
    <td><div class="cell"><input value="0"></div></td>
    <td><div class="cell"><input value="0"></div></td>
    <td><div class="cell"><input value="0"></div></td>
  </tr>`;
  const header = label => `<th><div class="cell" title="${label}">${label}</div></th>`;
  return `<section class="price-inventory">
    <div class="title"><span>价格库存</span></div>
    <div class="conf">
      <label class="el-radio"><input type="radio" name="delivery-mode">现货预售混合模式</label>
      <label class="el-checkbox"><input type="checkbox">48小时内发货</label>
      <label class="el-checkbox"><input type="checkbox">15天内</label>
      <div class="el-table sku-table">
        <div class="el-table__main-wrapper">
          <div class="el-table__header-wrapper"><table><thead><tr>
            ${header('尺码')}${header('价格')}${header('现货库存')}${header('预售库存(15天内)')}
          </tr></thead></table></div>
          <div class="el-table__body-wrapper"><table><tbody>
            ${['S', 'M', 'L', 'XL', '2XL'].map(skuRow).join('')}
          </tbody></table></div>
        </div>
      </div>
    </div>
  </section>`;
}

function freightSelect(current) {
  return `<div class="el-select">
    <input class="el-input__inner" readonly value="${current}" onclick="openSelect(this)">
    <div class="el-select-dropdown" style="display:none"><ul>
      <li class="el-select-dropdown__item" onclick="pickOption(this, '新疆，西藏，不包邮-T恤，裤子，装饰品', false)">新疆，西藏，不包邮-T恤，裤子，装饰品</li>
      <li class="el-select-dropdown__item" onclick="pickOption(this, '包邮', false)">包邮</li>
    </ul></div>
  </div>`;
}

function freightSection() {
  const row = (name, current) => `<div class="set-ship">
    <span class="shop-title">${name}</span>${freightSelect(current)}
  </div>`;
  return `<section class="freight-section">
    <div class="title"><span>运费模板</span></div>
    <div class="el-row">
      ${row('钊叔 NEIGBORL 制', '新疆，西藏，不包邮-T恤，裤子，装饰品')}
      ${row('夏一制', '包邮')}
      ${row('啊亮穿搭', '包邮')}
    </div>
  </section>`;
}

window.renderSizeRows = sizes => {
  document.querySelector('#size-rows').innerHTML = sizes.map(sizeRow).join('');
};

function openSelect(element) {
  document.querySelectorAll('.el-select-dropdown').forEach(dropdown => {
    dropdown.style.display = 'none';
  });
  const linkedId = element.getAttribute('aria-controls');
  if (linkedId) {
    document.getElementById(linkedId).style.display = 'block';
    return;
  }
  element.closest('.el-select').querySelector('.el-select-dropdown').style.display = 'block';
}

function pickOption(option, value, multi) {
  const dropdown = option.closest('.el-select-dropdown');
  let select = option.closest('.el-select');
  if (!select && dropdown.id) {
    select = document.querySelector(`.el-select [aria-controls="${dropdown.id}"]`).closest('.el-select');
  }
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
    dropdown.style.display = 'none';
  }
}

function renderAttributes() {
  document.querySelector('#attributes').innerHTML =
    selectBox('厚度', ['常规款', '常规款（加厚）'], false) +
    textAttribute('裤门襟') +
    selectBox('里料材质', ['棉', '亚麻', '棉麻'], true) +
    textAttribute('里料材质成分含量') +
    textAttribute('材质成分含量') +
    `<div class="wash-upload"><span>水洗标/吊牌图</span><div class="sc-upload"><input type="file"></div></div>` +
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

function refreshPredictions() {
  window.predictionRefreshCount += 1;
  document.querySelector('.prediction-item .path').textContent = '服装>男装>休闲裤';
}

function generatePredictions(button) {
  window.predictionRefreshCount += 1;
  document.querySelector('.platform-category-input').insertAdjacentHTML(
    'beforeend',
    '<div class="prediction-item"><span>推荐</span><span class="path">服装>男装>休闲裤</span><button onclick="applyCategory(this)">点击使用</button></div>'
  );
  document.querySelector('[placeholder="请输入商品标题"]').value = '8/28';
  button.remove();
  const notice = document.createElement('div');
  notice.textContent = '抖音资料已由AI自动生成';
  document.querySelector('[role="tabpanel"]').prepend(notice);
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
        <label class="el-form-item__label">商品标题</label>
        <div class="el-form-item__content"><input placeholder="请输入商品标题"></div>
      </div>
      <button type="button" onclick="generatePredictions(this)">立即生成</button>
      <button type="button" onclick="refreshPredictions()">刷新预测结果</button>
      <div class="el-form-item">
        <label class="el-form-item__label">货号</label>
        <div class="el-form-item__content"><input placeholder="请输入货号"></div>
      </div>
      <div class="el-form-item">
        <label class="el-form-item__label">导购短标题</label>
        <div class="el-form-item__content"><input placeholder="建议填写简明准确的标题内容，避免重复表达"></div>
      </div>
      ${sizeTable()}
      <div class="el-form-item">
        <label class="el-form-item__label">主图</label>
        <div class="el-form-item__content" data-image-group="main" data-existing-count="2"></div>
      </div>
      <div class="el-form-item">
        <label class="el-form-item__label">主图3:4</label>
        <div class="el-form-item__content" data-image-group="main-34" data-existing-count="0"></div>
      </div>
      <div class="el-form-item">
        <label class="el-form-item__label">商品详情图</label>
        <div class="el-form-item__content" data-image-group="details" data-existing-count="2"></div>
      </div>
      ${deliveryInventory()}
      ${freightSection()}
      <div id="attributes"></div>
    </section>`;
    renderSizeRows(['XL', 'S', '2XL', 'M', 'L']);
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
        self.assertEqual(
            await self.listing.apply_first_recommended_category(),
            "服装 > 男装 > 休闲裤",
        )
        self.assertEqual(await self.page.locator("#attributes > .el-select-dropdown").count(), 2)
        self.assertEqual(await self.listing.fill_short_title("重磅水洗工装裤"), "重磅水洗工装裤")
        self.assertEqual(await self.listing.fill_short_title("重磅水洗工装裤"), "重磅水洗工装裤")

        self.assertEqual(await self.listing.fill_attribute("厚度", "常 规-款"), ("常规款",))
        self.assertEqual(await self.listing.fill_attribute("厚度", "常 规-款"), ("常规款",))
        self.assertEqual(
            await self.listing.fill_attribute("里料材质", "棉/亚麻"),
            ("棉",),
        )
        tags = self.page.locator(
            ".attr-item:has(.el-form-item__label:text-is('里料材质')) .el-tag"
        )
        self.assertEqual(await tags.count(), 1)

    async def test_product_title_refreshes_dynamic_category_before_applying_it(self):
        await self.listing.open()
        await self.page.locator(".prediction-item").evaluate(
            "element => element.remove()"
        )

        prepared = await self.listing.prepare_product_title_and_predictions(
            "[绿巨人] 复古水洗工装裤",
            force_refresh=True,
        )

        self.assertEqual(prepared["product_title"], "[绿巨人] 复古水洗工装裤")
        self.assertTrue(prepared["prediction_refreshed"])
        self.assertEqual(
            prepared["prediction_actions"],
            ["立即生成"],
        )
        self.assertEqual(await self.page.evaluate("window.predictionRefreshCount"), 1)
        self.assertEqual(
            await self.listing.apply_first_recommended_category(),
            "服装 > 男装 > 休闲裤",
        )

        repeated = await self.listing.prepare_product_title_and_predictions(
            "[绿巨人] 复古水洗工装裤",
            force_refresh=False,
        )
        self.assertFalse(repeated["product_title_changed"])
        self.assertFalse(repeated["prediction_refreshed"])
        self.assertEqual(await self.page.evaluate("window.predictionRefreshCount"), 1)

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

    async def test_or_value_prefers_existing_later_candidate_before_creating(self):
        await self.listing.open()
        await self.listing.apply_first_recommended_category()

        actual = await self.listing.fill_attribute("里料材质", "不存在/亚麻")

        self.assertEqual(actual, ("亚麻",))

    @staticmethod
    def recommendations():
        return (
            SkuRecommendation("S", 155, 160, 50, 60, 80, 106, 104),
            SkuRecommendation("M", 155, 170, 50, 70, 84, 110, 106),
            SkuRecommendation("L", 155, 180, 50, 80, 88, 114, 108),
            SkuRecommendation("XL", 155, 190, 50, 90, 92, 118, 110),
            SkuRecommendation("2XL", 155, 200, 50, 100, 96, 122, 112),
        )

    async def test_size_recommendations_map_by_size_text_not_row_order(self):
        await self.listing.open()

        actual = await self.listing.fill_size_recommendations(self.recommendations())

        self.assertEqual(
            actual["S"],
            ("155-160", "50-60", "80", "106", "104"),
        )
        self.assertEqual(
            actual["2XL"],
            ("155-200", "50-100", "96", "122", "112"),
        )
        self.assertEqual(
            await self.page.locator("tr[data-size='M'] input").evaluate_all(
                "inputs => inputs.map(input => input.value)"
            ),
            ["155-170", "50-70", "84", "110", "106"],
        )
        self.assertGreater(await self.page.evaluate("window.sizeWriteCount"), 0)

    async def test_size_preflight_rejects_duplicate_and_missing_unexpected_before_writes(self):
        await self.listing.open()

        await self.page.evaluate(
            "sizes => renderSizeRows(sizes)",
            ["S", "M", "L", "XL", "XL"],
        )
        await self.page.evaluate("window.sizeWriteCount = 0")
        with self.assertRaisesRegex(DouyinListingError, r"重复：XL.*缺少：2XL"):
            await self.listing.fill_size_recommendations(self.recommendations())
        self.assertEqual(await self.page.evaluate("window.sizeWriteCount"), 0)

        await self.page.evaluate(
            "sizes => renderSizeRows(sizes)",
            ["S", "M", "L", "XL", "3XL"],
        )
        await self.page.evaluate("window.sizeWriteCount = 0")
        with self.assertRaisesRegex(DouyinListingError, r"缺少：2XL.*意外：3XL"):
            await self.listing.fill_size_recommendations(self.recommendations())
        self.assertEqual(await self.page.evaluate("window.sizeWriteCount"), 0)

    async def test_douyin_image_groups_delegate_independently_and_preserve_order(self):
        await self.listing.open()
        calls = []

        async def fake_sync(page, item, paths, label, timeout_seconds):
            image_group = item.locator("[data-image-group]")
            group = await image_group.get_attribute("data-image-group")
            existing_count = int(
                await image_group.get_attribute("data-existing-count") or 0
            )
            calls.append((group, paths, label, timeout_seconds))
            return "skipped" if existing_count == len(paths) else "replaced"

        main = (Path("main-2.jpg"), Path("main-10.jpg"))
        main_34 = (Path("main-34-1.jpg"),)
        details = (Path("detail-1.jpg"), Path("detail-2.jpg"))
        with patch("kuaimai_erp.sync_image_group", new=fake_sync):
            actual = await self.listing.sync_douyin_images(
                main,
                main_34,
                details,
                timeout_seconds=45,
            )

        self.assertEqual(
            actual,
            {"main": "skipped", "main_34": "replaced", "details": "skipped"},
        )
        self.assertEqual([call[0] for call in calls], ["main", "main-34", "details"])
        self.assertEqual(calls[0][1], main)
        self.assertEqual(calls[1][1], main_34)
        self.assertEqual(calls[2][1], details)
        self.assertTrue(all(call[3] == 45 for call in calls))

    async def test_delivery_mode_and_every_sku_row_are_verified(self):
        await self.listing.open()
        await self.page.locator(".sku-table tbody tr").first.locator("input").nth(2).fill("")

        self.assertEqual(
            await self.listing.apply_delivery_mode(),
            ("现货预售混合模式", "48小时内发货", "15天内"),
        )
        self.assertEqual(await self.listing.fill_sku_price_inventory("586", 0, 100), 5)

        self.assertTrue(
            await self.page.locator("label", has_text="现货预售混合模式").locator("input").is_checked()
        )
        for row_index in range(5):
            values = await self.page.locator(".sku-table tbody tr").nth(row_index).locator(
                "input"
            ).evaluate_all("inputs => inputs.map(input => input.value)")
            self.assertEqual(values, ["586", "0", "100"])

    @staticmethod
    def freight_payloads(missing_store_id=None):
        desired = "新疆，西藏，不包邮-T恤，裤子，装饰品"
        shops = [
            {"id": 1, "title": "钊叔 NEIGBORL 制"},
            {"id": 2, "title": "夏一制"},
            {"id": 3, "title": "啊亮穿搭"},
        ]
        templates = [
            {
                "shopId": shop["id"],
                "templateId": "0" if shop["id"] == missing_store_id else f"t-{shop['id']}",
                "templateName": "包邮" if shop["id"] == missing_store_id else desired,
            }
            for shop in shops
        ]
        return (
            {"result": 1, "data": {"list": shops}},
            {"result": 1, "data": {"templateList": templates}},
        )

    async def test_every_visible_store_freight_is_api_matched_and_read_back(self):
        await self.listing.open()
        payloads = self.freight_payloads()

        async def fake_fetch():
            return payloads

        self.listing._fetch_freight_payloads = fake_fetch
        actual = await self.listing.apply_freight_templates(
            ("新疆西藏不包邮T恤裤子装饰品", "新疆，西藏，不包邮-T恤，裤子，装饰品")
        )

        self.assertEqual(
            set(actual["applied"]),
            {"钊叔 NEIGBORL 制", "夏一制", "啊亮穿搭"},
        )
        self.assertTrue(all("新疆" in value for value in actual["applied"].values()))
        self.assertEqual(actual["preserved"], {})

    async def test_store_missing_initial_api_option_uses_dom_fallback(self):
        await self.listing.open()
        payloads = self.freight_payloads(missing_store_id=2)

        async def fake_fetch():
            return payloads

        self.listing._fetch_freight_payloads = fake_fetch
        actual = await self.listing.apply_freight_templates(
            ("新疆，西藏，不包邮-T恤，裤子，装饰品",)
        )
        self.assertEqual(actual["preserved"], {})
        self.assertEqual(
            set(actual["applied"]),
            {"钊叔 NEIGBORL 制", "夏一制", "啊亮穿搭"},
        )
        first_value = await self.page.locator(".set-ship .el-select input").nth(1).input_value()
        self.assertEqual(first_value, "新疆，西藏，不包邮-T恤，裤子，装饰品")

    async def test_freight_only_changes_configured_stores(self):
        await self.listing.open()
        payloads = self.freight_payloads()

        async def fake_fetch():
            return payloads

        self.listing._fetch_freight_payloads = fake_fetch
        actual = await self.listing.apply_freight_templates(
            ("新疆，西藏，不包邮-T恤，裤子，装饰品",),
            target_shops=("钊叔 NEIGBORL 制", "啊亮穿搭"),
            default_untargeted_template="包邮",
        )

        self.assertEqual(
            set(actual["applied"]),
            {"钊叔 NEIGBORL 制", "夏一制", "啊亮穿搭"},
        )
        self.assertEqual(actual["preserved"], {})
        values = await self.page.locator(".set-ship .el-select input").evaluate_all(
            "inputs => inputs.map(input => input.value)"
        )
        self.assertEqual(
            values,
            [
                "新疆，西藏，不包邮-T恤，裤子，装饰品",
                "包邮",
                "新疆，西藏，不包邮-T恤，裤子，装饰品",
            ],
        )

    async def test_apply_category_and_fields_reports_unmatched_current_category_attribute(self):
        await self.listing.open()
        fields = SimpleNamespace(
            short_title="复古工装裤",
            attributes={"厚度/厚薄": "常规款", "当前类目不存在/未知属性": "不能静默跳过"},
        )

        actual = await self.listing.apply_category_and_fields(fields)

        self.assertEqual(actual["skipped_attributes"], ["当前类目不存在/未知属性"])
        self.assertEqual(actual["attributes"]["厚度"], ("常规款",))

    async def test_goods_code_and_sku_values_survive_read_only_recheck(self):
        await self.listing.open()
        goods_code_input = self.page.locator(
            ".el-form-item:has(.el-form-item__label:text-is('货号')) input"
        )
        await goods_code_input.fill("NGBL-10588")
        await goods_code_input.evaluate("element => element.readOnly = true")
        fields = SimpleNamespace(
            short_title="复古工装裤",
            attributes={
                "货号/商家外部编码": "NGBL-10588",
                "厚度": "常规款",
            },
        )

        applied = await self.listing.apply_category_and_fields(fields)
        await self.listing.fill_text_field("商品标题", "复古水洗工装裤")
        await self.listing.fill_sku_price_inventory("586", 0, 100)
        persisted = await self.listing.verify_persisted_values(
            applied["category"],
            "复古水洗工装裤",
            applied["short_title"],
            applied["attributes"],
            "586",
            0,
            100,
        )

        self.assertEqual(applied["attributes"]["货号"], ("NGBL-10588",))
        self.assertEqual(persisted["attributes"]["货号"], ("NGBL-10588",))
        self.assertTrue(all(row["价格"] == "586" for row in persisted["sku"]))

    async def test_apply_category_and_fields_maps_historical_trouser_fly_typo(self):
        await self.listing.open()
        fields = SimpleNamespace(
            short_title="复古工装裤",
            attributes={"裤门禁": "拉链"},
        )

        actual = await self.listing.apply_category_and_fields(fields)

        self.assertEqual(actual["attributes"]["裤门襟"], ("拉链",))

    async def test_apply_category_and_fields_reports_value_missing_from_platform_options(self):
        await self.listing.open()
        fields = SimpleNamespace(
            short_title="复古工装裤",
            attributes={"厚度": "平台没有这个值"},
        )

        actual = await self.listing.apply_category_and_fields(fields)

        self.assertEqual(actual["attributes"], {})
        self.assertEqual(actual["skipped_values"], {"厚度": "平台没有这个值"})
        self.assertEqual(await self.page.locator(".el-select-dropdown:visible").count(), 0)

    async def test_apply_category_and_fields_accepts_bijective_aliases(self):
        await self.listing.open()
        fields = SimpleNamespace(
            short_title="复古工装裤",
            attributes={
                "厚度/厚薄": "常规款",
                "里料材质/里料": "棉/亚麻",
            },
        )

        actual = await self.listing.apply_category_and_fields(fields)

        self.assertEqual(actual["short_title"], "复古工装裤")
        self.assertEqual(actual["attributes"]["厚度"], ("常规款",))
        self.assertEqual(actual["attributes"]["里料材质"], ("棉",))

    async def test_apply_category_and_fields_fills_all_explicit_slash_alias_targets(self):
        await self.listing.open()
        fields = SimpleNamespace(
            short_title="复古工装裤",
            attributes={"里料材质成分含量/材质成分含量": "95%及以上"},
        )

        actual = await self.listing.apply_category_and_fields(fields)

        self.assertEqual(
            actual["attributes"],
            {
                "里料材质成分含量": ("95%及以上",),
                "材质成分含量": ("95%及以上",),
            },
        )

    async def test_material_total_must_be_exactly_100_before_page_changes(self):
        with self.assertRaisesRegex(DouyinListingError, "合计必须为 100"):
            await self.listing.apply_materials((MaterialComponent("棉", 99),), ())


if __name__ == "__main__":
    unittest.main()
