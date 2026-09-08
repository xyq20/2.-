"""使用本地 DOM 重现已观察到的快麦模板控件，不连接或写入线上商品。"""
import unittest
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
import kuaimai_erp as erp

HTML = '''
<button onclick="document.querySelector('.el-popover').style.display='block'">新增商品</button>
<div class="el-popover" style="display:none"><div class="drop-list-item" onclick="document.querySelector('.el-drawer').style.display='block'"><span>手工新增商品</span></div></div>
<table><tbody><tr><td>手工新增商品</td></tr><tr><td>手工新增商品</td></tr></tbody></table>
<div class="el-drawer" style="display:none">
  <header>手工新增商品</header>
  <div class="el-form-item"><label class="el-form-item__label">款式编码</label><input id="style-code"></div>
  <div class="el-form-item"><label class="el-form-item__label">商品名称</label><input></div>
  <div class="el-form-item"><label class="el-form-item__label">商品主图</label><div class="sc-upload"><img class="file-img"></div></div>
  <div class="block-specification">
    <div><div class="title-bg"><div class="el-input"><input value="颜色"></div><button>填充常用规格</button></div>
      <div class="specification-value" id="colors"><button onclick="addColor()">添加规格值</button></div>
    </div>
    <div><div class="title-bg"><div class="el-input"><input value="尺码"></div><button onclick="showTemplate()">填充常用规格</button></div>
      <div class="specification-value" id="sizes"></div>
    </div>
  </div>
  <div class="block-specification-list"><button onclick="document.querySelector('#generator').style.display='block'">批量生成</button>
    <div class="el-table"><div class="el-table__header-wrapper"><table><thead><tr id="head"></tr></thead></table></div>
      <div class="el-table__body-wrapper"><table><tbody id="rows"></tbody></table></div></div>
  </div>
  <div class="drawer-footer"><button>保 存</button></div>
</div>
<div class="el-dialog" id="template" style="display:none"><span class="el-dialog__title">常用规格</span><span id="apply"></span></div>
<div class="el-dialog" id="generator" style="display:none"><span class="el-dialog__title">批量生成商品编码</span>
  <label><input type="radio" name="rule">款式编码+序号</label>
  <label><input type="radio" name="rule" checked>款式编码+规格值</label>
  <button onclick="generate()">确 定</button>
</div>
<script>
const sizes=['S','M','L','XL','2XL'];
const labels=['颜色','尺码','商品编码','基本售价','销售价','市场价','成本价','库存','重量(kg)'];
document.querySelector('#head').innerHTML=labels.map(x=>`<th><div class="cell" title="${x}">${x}</div></th>`).join('');
function addColor(){ document.querySelector('#colors').insertAdjacentHTML('afterbegin','<div class="specification-value-flex_input"><input></div>'); }
function showTemplate(){
 document.querySelector('#template').style.display='block';
 setTimeout(()=>document.querySelector('#apply').innerHTML='<a onclick="applyTemplate()">应用至资料</a>',80);
}
function applyTemplate(){
 document.querySelector('#sizes').innerHTML=sizes.map(s=>`<div class="specification-value-flex_input"><input value="${s}"></div>`).join('');
 document.querySelector('#template').style.display='none';
 document.querySelector('#rows').innerHTML=sizes.map(s=>`<tr><td>军绿色</td><td>${s}</td><td><input type="text"></td>${Array(6).fill('<td><input type="text" value="0"></td>').join('')}</tr>`).join('');
}
function generate(){
 [...document.querySelectorAll('#rows tr')].forEach((row,i)=>row.querySelector('input').value=document.querySelector('#style-code').value+'军绿色'+sizes[i]);
 document.querySelector('#generator').style.display='none';
}
</script>
'''


class BrowserTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_locators_apply_default_templates_and_validate_every_sku(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(HTML)
                # 新增按钮展开后，真实页面会出现“手工新增商品”菜单项；此处标题兼作该菜单项。
                drawer = await erp.open_new_product_drawer(page, 3)
                product = SimpleNamespace(style_code='TEST-7', main_images=[Path('/tmp/product.png')])
                args = SimpleNamespace(timeout=3, upload_timeout=3)
                with patch.object(erp, 'sync_image_group', AsyncMock()) as uploading:
                    result = await erp.fill_new_product_form(page, drawer, product, args)
                self.assertEqual(result['size_template'], list(erp.NEW_PRODUCT_SIZES))
                self.assertEqual(result['code_rule'], '款式编码+规格值')
                self.assertEqual(uploading.call_args.args[2], product.main_images)
                report = await erp.validate_new_product_form(drawer, product)
                self.assertEqual(report['sku_count'], 5)
                self.assertEqual(report['rows'][-1]['商品编码'], 'TEST-7军绿色2XL')
                await page.locator('#rows tr').last.locator('input').first.fill('WRONG')
                with self.assertRaises(erp.AutomationError):
                    await erp.validate_new_product_form(drawer, product)
                # 新增使用“创建成功”弹窗，并非编辑接口或“保存成功”toast。
                await page.set_content('''<div id="drawer"><div class="drawer-footer"><button onclick="document.querySelector('.el-dialog').style.display='block'">保 存</button></div></div><div class="el-dialog" style="display:none">提示 创建成功 查看商品 继续发布</div>''')
                saved = await erp.click_save_and_confirm(
                    page, page.locator('#drawer'), False, 3,
                    logging.getLogger('create-browser-test'), creation=True,
                )
                self.assertEqual(saved['confirmed_by'], 'creation_dialog')
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
