import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

import kuaimai_erp


LOGGER = logging.getLogger("kuaimai-tests")


class _VisibleItem:
    async def is_visible(self):
        return True


class _DetachedLocator:
    async def count(self):
        raise RuntimeError("Frame was detached")


class _GoodLocator:
    async def count(self):
        return 1

    def nth(self, _index):
        return _VisibleItem()


class _Frame:
    def __init__(self, locator):
        self.locator = locator

    def get_by_text(self, _text, exact=True):
        return self.locator


class _PageWithDetachedFrame:
    frames = [_Frame(_DetachedLocator()), _Frame(_GoodLocator())]


class _LabelLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def inner_text(self):
        return "款式编码："


class _FormItem:
    def locator(self, _selector):
        return _LabelLocator()


class _SlowItems:
    def __init__(self):
        self.calls = 0

    async def count(self):
        self.calls += 1
        return 0 if self.calls < 3 else 1

    def nth(self, _index):
        return _FormItem()


class _SlowDrawer:
    def __init__(self):
        self.items = _SlowItems()

    def locator(self, selector):
        if selector != ".el-form-item":
            raise AssertionError(selector)
        return self.items


class _AuthContext:
    def __init__(self, state=None):
        self._state = state or {"cookies": [], "origins": []}
        self.cookies_added = []
        self.init_scripts = []

    async def storage_state(self):
        return self._state

    async def add_cookies(self, cookies):
        self.cookies_added.extend(cookies)

    async def add_init_script(self, script):
        self.init_scripts.append(script)


class AsyncRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_iframe_is_ignored_during_login_detection(self):
        found = await kuaimai_erp.visible_text_across_frames(_PageWithDetachedFrame(), "快麦通")
        self.assertIsNotNone(found)

    async def test_form_item_waits_until_slow_form_is_rendered(self):
        drawer = _SlowDrawer()
        item = await kuaimai_erp.form_item(
            drawer,
            "款式编码",
            timeout_seconds=0.5,
            poll_interval=0.01,
        )
        self.assertIsInstance(item, _FormItem)
        self.assertGreaterEqual(drawer.items.calls, 3)

    async def test_session_cookie_is_saved_and_restored_explicitly(self):
        state = {
            "cookies": [
                {
                    "name": "scm-session",
                    "value": "secret",
                    "domain": ".superboss.cc",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [
                {
                    "origin": "https://erp.superboss.cc",
                    "localStorage": [{"name": "staff_id", "value": "123"}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "auth-state.json"
            await kuaimai_erp.save_auth_state(_AuthContext(state), state_path, LOGGER)
            on_disk = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["cookies"][0]["expires"], -1)

            restored = _AuthContext()
            result = await kuaimai_erp.restore_auth_state(restored, state_path, LOGGER)
            self.assertTrue(result)
            self.assertEqual(restored.cookies_added[0]["name"], "scm-session")
            self.assertEqual(len(restored.init_scripts), 1)

    async def test_unverified_legacy_state_does_not_restore_failed_scm_session(self):
        state = {
            "cookies": [
                {"name": "erp-session", "value": "ok", "domain": "erp.superboss.cc", "path": "/"},
                {"name": "_scmcenseid", "value": "bad", "domain": ".superboss.cc", "path": "/"},
                {"name": "scm-host", "value": "bad", "domain": "scm.superboss.cc", "path": "/"},
            ],
            "origins": [
                {"origin": "https://erp.superboss.cc", "localStorage": []},
                {"origin": "https://scm.superboss.cc", "localStorage": [{"name": "userinfo", "value": "{}"}]},
            ],
            "sessionStorage": {
                "https://erp.superboss.cc": [{"name": "erp", "value": "ok"}],
                "https://scm.superboss.cc": [{"name": "scm", "value": "bad"}],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "legacy-auth-state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            restored = _AuthContext()
            self.assertTrue(await kuaimai_erp.restore_auth_state(restored, state_path, LOGGER))
            self.assertEqual([cookie["name"] for cookie in restored.cookies_added], ["erp-session"])
            self.assertNotIn("https://scm.superboss.cc", restored.init_scripts[0])

    async def test_verified_scm_session_fast_path_still_requires_api_success(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()

            async def route_handler(route):
                if "/item/base/page.json" in route.request.url:
                    await route.fulfill(
                        content_type="application/json",
                        body='{"result":1,"data":{"records":[]}}',
                    )
                else:
                    await route.fulfill(
                        content_type="text/html",
                        body="<meta charset='utf-8'><p>商品中心</p>",
                    )

            await context.route("https://scma.superboss.cc/**", route_handler)
            page = await context.new_page()
            original_center_url = kuaimai_erp.CENTER_URL
            kuaimai_erp.CENTER_URL = "https://scma.superboss.cc/supplier/prod/center"
            try:
                reused = await kuaimai_erp.try_reuse_verified_scm_session(
                    page, "NGBL-10588", 3, LOGGER
                )
                self.assertEqual(reused.url.split("?", 1)[0], kuaimai_erp.CENTER_URL)
                self.assertTrue(await reused.get_by_text("商品中心", exact=True).is_visible())
            finally:
                kuaimai_erp.CENTER_URL = original_center_url
                await context.close()
                await browser.close()

    async def test_erp_entry_waits_for_sso_chain_before_opening_center(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()

            async def route_handler(route):
                url = route.request.url
                if url.startswith("https://erp.superboss.cc/index.html"):
                    await route.fulfill(
                        status=302,
                        headers={"Location": "https://erpa.superboss.cc/index.html#/index/"},
                    )
                elif url.startswith("https://erpa.superboss.cc/index.html"):
                    await route.fulfill(
                        content_type="text/html",
                        body=(
                            "<meta charset='utf-8'>"
                            "<div class='v-modal' style='position:fixed;inset:0;z-index:9'></div>"
                            "<div class='el-dialog' style='position:fixed;inset:0;z-index:10;background:white'>"
                            "<strong>店铺状态异常确认</strong>"
                            "<button class='el-dialog__headerbtn' onclick='this.parentElement.remove()'>关闭</button>"
                            "</div>"
                            "<button onclick=\"window.open('https://scma.superboss.cc/account/erpVisit.json')\">"
                            "快麦通</button>"
                        ),
                    )
                elif "/account/erpVisit.json" in url:
                    await route.fulfill(
                        content_type="text/html",
                        body=(
                            "<meta charset='utf-8'><p>SSO处理中</p><script>"
                            "setTimeout(() => location.href='https://scma.superboss.cc/supplier/index?hasCookie=true', 700);"
                            "</script>"
                        ),
                    )
                elif "/supplier/index" in url:
                    await route.fulfill(
                        content_type="text/html",
                        body=(
                            "<meta charset='utf-8'><p>快麦通首页初始化中</p>"
                            "<div class='el-dialog'><strong>温馨提示</strong>"
                            "<button onclick='acknowledge()'>知道了</button></div><script>"
                            "async function acknowledge() {"
                            "  await fetch('/account/session-ready');"
                            "  document.querySelector('.el-dialog').remove();"
                            "  document.body.insertAdjacentHTML('beforeend', '<p>快麦通首页</p>');"
                            "}"
                            "</script>"
                        ),
                    )
                elif "/account/session-ready" in url:
                    await route.fulfill(
                        content_type="application/json",
                        headers={"Set-Cookie": "scm-auth=ok; Path=/; HttpOnly; SameSite=Lax"},
                        body='{"result":1}',
                    )
                elif "/item/base/page.json" in url:
                    authenticated = "scm-auth=ok" in route.request.headers.get("cookie", "")
                    await route.fulfill(
                        status=200 if authenticated else 401,
                        content_type="application/json",
                        body='{"result":1,"data":{"records":[]}}' if authenticated else '{"result":0}',
                    )
                elif "/supplier/prod/center" in url:
                    if "scm-auth=ok" in route.request.headers.get("cookie", ""):
                        await route.fulfill(
                            content_type="text/html",
                            body="<meta charset='utf-8'><p>商品中心</p>",
                        )
                    else:
                        await route.fulfill(
                            status=302,
                            headers={"Location": "https://scma.superboss.cc/login/index"},
                        )
                elif "/login/index" in url:
                    await route.fulfill(
                        content_type="text/html",
                        body="<meta charset='utf-8'><p>登录</p>",
                    )
                else:
                    await route.fulfill(status=404, body="not found")

            await context.route("**/*", route_handler)
            page = await context.new_page()
            original_entry_url = kuaimai_erp.ERP_ENTRY_URL
            kuaimai_erp.ERP_ENTRY_URL = "https://erpa.superboss.cc/index.html#/index/"
            try:
                scm_page = await asyncio.wait_for(
                    kuaimai_erp.enter_kuaimai_from_erp(page, 4, True, LOGGER),
                    timeout=8,
                )
                self.assertEqual(
                    scm_page.url.split("?", 1)[0],
                    "https://scma.superboss.cc/supplier/prod/center",
                )
                self.assertTrue(await scm_page.get_by_text("商品中心", exact=True).is_visible())
            finally:
                kuaimai_erp.ERP_ENTRY_URL = original_entry_url
                await context.close()
                await browser.close()

    async def test_real_browser_waits_for_editor_loading_mask_and_style_value(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <meta charset="utf-8">
                <input placeholder="多个款式编码">
                <button onclick="showRow()">查询</button>
                <div class="el-table__body-wrapper"><table><tbody id="rows"></tbody></table></div>
                <div id="prod-center-edit-dialog" style="display:none">
                  <div>商品编辑</div><div role="tab">商品编辑</div><div class="is-active">基础资料</div>
                  <div class="el-loading-mask">加载中</div>
                  <div id="form"></div>
                </div>
                <script>
                  function showRow() {
                    rows.innerHTML = '<tr><td>NGBL-10588</td><td><span onclick="showEditor()">编辑</span></td></tr>';
                  }
                  function showEditor() {
                    const drawer = document.querySelector('#prod-center-edit-dialog');
                    drawer.style.display = 'block';
                    setTimeout(() => {
                      drawer.querySelector('.el-loading-mask').remove();
                      form.innerHTML = '<div class="el-form-item">'
                        + '<label class="el-form-item__label">款式编码：</label>'
                        + '<input value="NGBL-10588"></div>';
                    }, 350);
                  }
                </script>
                """
            )
            original_center_url = kuaimai_erp.CENTER_URL
            kuaimai_erp.CENTER_URL = page.url
            try:
                drawer = await kuaimai_erp.open_product_editor(
                    page,
                    "NGBL-10588",
                    LOGGER,
                    timeout_seconds=2,
                )
                self.assertEqual(await drawer.locator(".el-loading-mask:visible").count(), 0)
                style_item = await kuaimai_erp.form_item(drawer, "款式编码", timeout_seconds=0.1)
                self.assertEqual(await style_item.locator("input").input_value(), "NGBL-10588")
            finally:
                kuaimai_erp.CENTER_URL = original_center_url
                await browser.close()

    async def test_hidden_image_delete_button_is_revealed_by_hover(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <style>.del-btn { display:none } .file-img:hover .del-btn { display:block }</style>
                <div id="scope"><div class="sc-upload">
                  <div class="file-img"><button class="del-btn"
                    onclick="this.closest('.file-img').remove()">删除</button></div>
                </div></div>
                """
            )
            deleted = await kuaimai_erp.delete_uploaded_images(page.locator("#scope"), page)
            self.assertEqual(deleted, 1)
            self.assertEqual(await page.locator(".file-img").count(), 0)
            await browser.close()


if __name__ == "__main__":
    unittest.main()
