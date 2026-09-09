import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    async def test_single_platform_learning_records_verified_stage(self):
        args = SimpleNamespace(platform="pdd", save=True, save_only=True)
        product = SimpleNamespace()
        store = Mock()
        context = SimpleNamespace(
            store=store,
            run_id="run-1",
            product_version="product-1",
        )

        async def runner(_args, _product, stage_dir, _logger, **_kwargs):
            (stage_dir / "pdd-after-save-validation.json").write_text(
                json.dumps({"status": "verified"}), encoding="utf-8"
            )

        with tempfile.TemporaryDirectory() as directory, patch.object(
            kuaimai_erp, "run_browser_automation", side_effect=runner
        ):
            await kuaimai_erp.run_single_platform_with_learning(
                args,
                product,
                Path(directory),
                LOGGER,
                learning_context=context,
            )

        result = store.record_stage.call_args.args[0]
        self.assertTrue(result.verified)
        self.assertEqual(result.platform_id, "pdd")
        self.assertEqual(store.save_checkpoint.call_args_list[-1].args[0].status, "completed")

    async def test_learning_orchestrator_records_only_verified_readback_as_completed(self):
        args = SimpleNamespace(
            platform="all",
            save=True,
            save_only=True,
            taobao_publish_preview=False,
            allow_taobao_save_once=False,
            allow_taobao_publish_once=False,
        )
        product = SimpleNamespace(douyin_fields=None)
        store = Mock()
        context = SimpleNamespace(
            store=store,
            run_id="run-1",
            product_version="product-1",
        )

        async def runner(_args, _product, stage_dir, _logger, **_kwargs):
            if _args.platform == "pdd":
                (stage_dir / "pdd-after-save-validation.json").write_text(
                    json.dumps({"status": "verified", "row_count": 5}),
                    encoding="utf-8",
                )

        with tempfile.TemporaryDirectory() as directory, patch.object(
            kuaimai_erp, "run_browser_automation", side_effect=runner
        ):
            await kuaimai_erp.run_all_implemented_platforms(
                args,
                product,
                Path(directory),
                LOGGER,
                learning_context=context,
            )

        recorded = [call.args[0] for call in store.record_stage.call_args_list]
        self.assertEqual(len(recorded), 8)
        self.assertEqual(
            tuple(result.platform_id for result in recorded if result.verified),
            ("pdd",),
        )
        pdd = next(result for result in recorded if result.platform_id == "pdd")
        self.assertEqual(pdd.status, "readback_verified")
        self.assertEqual(pdd.readback["row_count"], 5)
        self.assertEqual(store.save_checkpoint.call_args_list[-1].args[0].status, "completed")
        enqueued_types = tuple(call.args[1] for call in store.enqueue.call_args_list)
        self.assertIn("checkpoint.updated", enqueued_types)
        self.assertIn("readback.recorded", enqueued_types)
        self.assertIn("stage.completed", enqueued_types)

    async def test_learning_orchestrator_failure_is_unverified_and_stops_later_stages(self):
        args = SimpleNamespace(
            platform="all",
            save=False,
            save_only=False,
            taobao_publish_preview=False,
            allow_taobao_save_once=False,
            allow_taobao_publish_once=False,
        )
        product = SimpleNamespace(douyin_fields=None)
        store = Mock()
        context = SimpleNamespace(
            store=store,
            run_id="run-1",
            product_version="product-1",
        )
        runner = AsyncMock(side_effect=RuntimeError("platform failed"))

        with tempfile.TemporaryDirectory() as directory, patch.object(
            kuaimai_erp, "run_browser_automation", runner
        ):
            with self.assertRaisesRegex(RuntimeError, "platform failed"):
                await kuaimai_erp.run_all_implemented_platforms(
                    args,
                    product,
                    Path(directory),
                    LOGGER,
                    learning_context=context,
                )

        self.assertEqual(runner.await_count, 1)
        store.record_stage.assert_not_called()
        failed_checkpoint = store.save_checkpoint.call_args_list[-1].args[0]
        self.assertEqual(failed_checkpoint.status, "failed")
        self.assertEqual(failed_checkpoint.current_index, 0)

    async def test_shared_playwright_session_starts_once_and_closes_once(self):
        playwright = SimpleNamespace(stop=AsyncMock())
        manager = SimpleNamespace(start=AsyncMock(return_value=playwright))
        factory = Mock(return_value=manager)
        shared_session = {}

        async with kuaimai_erp.playwright_for_browser_run(
            factory, shared_session
        ) as first:
            self.assertIs(first, playwright)
        async with kuaimai_erp.playwright_for_browser_run(
            factory, shared_session
        ) as second:
            self.assertIs(second, playwright)

        await kuaimai_erp.close_shared_browser_session(shared_session)

        factory.assert_called_once()
        manager.start.assert_awaited_once()
        playwright.stop.assert_awaited_once()
        self.assertEqual(shared_session, {})

    async def test_detects_reusable_cdp_for_exact_dedicated_profile(self):
        profile = Path("/tmp/2.快麦一键铺货/output/kuaimai/chrome-profile")
        process_text = "\n".join(
            (
                "/Applications/Google Chrome --remote-debugging-port=9333 "
                "--user-data-dir=/tmp/another-profile",
                "/Applications/Google Chrome --remote-debugging-port=9222 "
                f"--user-data-dir={profile} --no-first-run",
            )
        )

        self.assertEqual(
            kuaimai_erp.cdp_url_for_profile_processes(profile, process_text),
            "http://127.0.0.1:9222",
        )

    async def test_does_not_attach_cdp_from_another_chrome_profile(self):
        process_text = (
            "/Applications/Google Chrome --remote-debugging-port=9222 "
            "--user-data-dir=/tmp/another-profile"
        )

        self.assertIsNone(
            kuaimai_erp.cdp_url_for_profile_processes(
                Path("/tmp/expected-profile"), process_text
            )
        )

    async def test_finds_only_root_browser_for_exact_dedicated_profile(self):
        profile = Path("/tmp/2.快麦一键铺货/output/kuaimai/chrome-profile")
        process_text = "\n".join(
            (
                "101 /Applications/Google Chrome --user-data-dir=/tmp/other",
                f"202 /Applications/Google Chrome --user-data-dir={profile}",
                "203 /Applications/Google Chrome Helper --type=renderer "
                f"--user-data-dir={profile}",
            )
        )

        self.assertEqual(
            kuaimai_erp.browser_pids_for_profile_processes(profile, process_text),
            (202,),
        )

    async def test_all_platform_save_orchestrator_runs_base_first(self):
        args = SimpleNamespace(
            platform="all",
            save=True,
            save_only=False,
            taobao_publish_preview=False,
            allow_taobao_save_once=False,
            allow_taobao_publish_once=False,
        )
        product = SimpleNamespace(douyin_fields=object())
        runner = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            kuaimai_erp, "run_browser_automation", runner
        ):
            artifact_dir = Path(directory)
            await kuaimai_erp.run_all_implemented_platforms(
                args,
                product,
                artifact_dir,
                LOGGER,
            )
            report = json.loads(
                (artifact_dir / "all-platform-result.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(runner.await_count, 9)
        shared_sessions = [
            call.kwargs["shared_session"] for call in runner.await_args_list
        ]
        self.assertTrue(all(item is shared_sessions[0] for item in shared_sessions))
        (
            first_args,
            second_args,
            third_args,
            fourth_args,
            fifth_args,
            sixth_args,
            seventh_args,
            eighth_args,
            ninth_args,
        ) = [call.args[0] for call in runner.await_args_list]
        self.assertEqual(first_args.platform, "base")
        self.assertFalse(first_args.allow_taobao_publish_once)
        self.assertEqual(second_args.platform, "douyin")
        self.assertFalse(second_args.allow_taobao_publish_once)
        self.assertEqual(third_args.platform, "taobao")
        self.assertTrue(third_args.allow_taobao_publish_once)
        self.assertFalse(third_args.allow_taobao_save_once)
        self.assertEqual(fourth_args.platform, "tmall")
        self.assertFalse(fourth_args.allow_taobao_publish_once)
        self.assertFalse(fourth_args.allow_taobao_save_once)
        self.assertEqual(fifth_args.platform, "pdd")
        self.assertFalse(second_args.allow_taobao_save_once)
        self.assertFalse(fifth_args.save_only)
        self.assertEqual(sixth_args.platform, "wxsph")
        self.assertTrue(sixth_args.save)
        self.assertFalse(sixth_args.save_only)
        self.assertEqual(seventh_args.platform, "xhs")
        self.assertTrue(seventh_args.save)
        self.assertFalse(seventh_args.save_only)
        self.assertEqual(eighth_args.platform, "youzan")
        self.assertTrue(eighth_args.save)
        self.assertFalse(eighth_args.save_only)
        self.assertEqual(ninth_args.platform, "jd")
        self.assertTrue(ninth_args.save)
        self.assertFalse(ninth_args.save_only)
        self.assertEqual(
            report,
            [
                {"platform": "base", "status": "success"},
                {"platform": "douyin", "status": "success"},
                {"platform": "taobao", "status": "success"},
                {"platform": "tmall", "status": "success"},
                {"platform": "pdd", "status": "success"},
                {"platform": "wxsph", "status": "success"},
                {"platform": "xhs", "status": "success"},
                {"platform": "youzan", "status": "success"},
                {"platform": "jd", "status": "success"},
            ],
        )

    async def test_all_platform_preview_keeps_base_out_of_stages(self):
        args = SimpleNamespace(
            platform="all",
            save=False,
            save_only=False,
            taobao_publish_preview=False,
            allow_taobao_save_once=False,
            allow_taobao_publish_once=False,
        )
        product = SimpleNamespace(douyin_fields=object())
        runner = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            kuaimai_erp, "run_browser_automation", runner
        ):
            await kuaimai_erp.run_all_implemented_platforms(
                args,
                product,
                Path(directory),
                LOGGER,
            )

        self.assertEqual(runner.await_count, 8)
        shared_sessions = [
            call.kwargs["shared_session"] for call in runner.await_args_list
        ]
        self.assertTrue(all(item is shared_sessions[0] for item in shared_sessions))
        self.assertEqual(
            [call.args[0].platform for call in runner.await_args_list],
            ["douyin", "taobao", "tmall", "pdd", "wxsph", "xhs", "youzan", "jd"],
        )

    async def test_detached_iframe_is_ignored_during_login_detection(self):
        found = await kuaimai_erp.visible_text_across_frames(_PageWithDetachedFrame(), "快麦通")
        self.assertIsNotNone(found)

    async def test_erp_login_checks_agreement_then_clicks_existing_login(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <label><input id="agreement" type="checkbox">我已阅读并同意《用户协议》</label>
                <button id="login" onclick="window.loginClicks=(window.loginClicks||0)+1">登录</button>
                <script>window.loginClicks = 0;</script>
                """
            )
            try:
                attempted = await kuaimai_erp.try_erp_login_with_agreement(page, LOGGER)

                self.assertTrue(attempted)
                self.assertTrue(await page.locator("#agreement").is_checked())
                self.assertEqual(await page.evaluate("window.loginClicks"), 1)
            finally:
                await browser.close()

    async def test_erp_login_handles_real_sibling_label_and_hidden_checkbox(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <input type="checkbox" id="reading">
                <label for="reading">我已阅读并同意</label>
                <a>《用户协议》</a>、<a>《隐私政策》</a>
                <button id="login-btn" type="button"
                        onclick="window.loginClicks=(window.loginClicks||0)+1">
                    登 录
                </button>
                <div style="display:none">
                    <input type="checkbox" id="js-qrcode-bind-checkbox">
                </div>
                <script>window.loginClicks = 0;</script>
                """
            )
            try:
                attempted = await kuaimai_erp.try_erp_login_with_agreement(page, LOGGER)

                self.assertTrue(attempted)
                self.assertTrue(await page.locator("#reading").is_checked())
                self.assertEqual(await page.evaluate("window.loginClicks"), 1)
            finally:
                await browser.close()

    async def test_erp_login_fills_password_from_secure_resolver_before_submit(self):
        from playwright.async_api import async_playwright

        html = """
            <input placeholder="请输入公司名称" value="测试公司">
            <input id="account" placeholder="请输入账号" value="test-account">
            <input id="password" placeholder="请输入密码" type="password">
            <input type="checkbox" id="reading">
            <label for="reading">我已阅读并同意</label>
            <button id="login-btn" type="button"
                    onclick="window.loginSnapshot={password:document.getElementById('password').value,checked:document.getElementById('reading').checked}">
                登 录
            </button>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.route(
                "https://erp.superboss.cc/**",
                lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html
                ),
            )
            await page.goto("https://erp.superboss.cc/login.html")
            try:
                with patch.object(
                    kuaimai_erp,
                    "resolve_erp_login_password",
                    return_value=("test-only-password", "keychain"),
                ) as resolver:
                    attempted = await kuaimai_erp.try_erp_login_with_agreement(page, LOGGER)

                self.assertTrue(attempted)
                resolver.assert_called_once_with("test-account", LOGGER)
                self.assertEqual(
                    await page.evaluate("window.loginSnapshot"),
                    {"password": "test-only-password", "checked": True},
                )
            finally:
                await browser.close()

    async def test_erp_login_does_not_submit_an_empty_resolved_password(self):
        from playwright.async_api import async_playwright

        html = """
            <input placeholder="请输入账号" value="missing-account">
            <input id="password" type="password">
            <input type="checkbox" id="reading">
            <label for="reading">我已阅读并同意</label>
            <button id="login-btn" type="button"
                    onclick="window.loginClicks=(window.loginClicks||0)+1">登录</button>
            <script>window.loginClicks = 0;</script>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.route(
                "https://erp.superboss.cc/**",
                lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html
                ),
            )
            await page.goto("https://erp.superboss.cc/login.html")
            try:
                with patch.object(
                    kuaimai_erp,
                    "resolve_erp_login_password",
                    return_value=("", ""),
                ):
                    attempted = await kuaimai_erp.try_erp_login_with_agreement(page, LOGGER)

                self.assertFalse(attempted)
                self.assertEqual(await page.locator("#password").input_value(), "")
                self.assertEqual(await page.evaluate("window.loginClicks"), 0)
            finally:
                await browser.close()

    async def test_erp_password_resolver_uses_keychain_when_runtime_env_is_empty(self):
        account = "keychain-test-account"
        kuaimai_erp._ERP_LOGIN_CREDENTIAL_RETRY_AT.pop(account, None)
        with patch.dict(os.environ, {"KUAIMAI_ERP_PASSWORD": ""}, clear=False), patch.object(
            kuaimai_erp,
            "_read_macos_keychain_password",
            return_value="test-only-keychain-password",
        ) as keychain_read, patch.object(
            kuaimai_erp, "_prompt_and_store_macos_keychain_password"
        ) as keychain_prompt:
            password, source = kuaimai_erp.resolve_erp_login_password(account, LOGGER)

        self.assertEqual(password, "test-only-keychain-password")
        self.assertEqual(source, "keychain")
        keychain_read.assert_called_once_with(account)
        keychain_prompt.assert_not_called()

    async def test_keychain_setup_prompts_without_password_in_process_arguments(self):
        account = "prompt-test-account"
        kuaimai_erp._ERP_LOGIN_KEYCHAIN_PROMPTED_ACCOUNTS.discard(account)
        terminal = SimpleNamespace(isatty=lambda: True)
        completed = SimpleNamespace(returncode=0)
        with patch.object(kuaimai_erp.sys, "platform", "darwin"), patch.object(
            kuaimai_erp.sys, "stdin", terminal
        ), patch.object(
            kuaimai_erp.subprocess, "run", return_value=completed
        ) as run:
            stored = kuaimai_erp._prompt_and_store_macos_keychain_password(
                account, LOGGER
            )

        self.assertTrue(stored)
        command = run.call_args.args[0]
        self.assertEqual(command[-1], "-w")
        self.assertEqual(command.count(account), 1)

    async def test_erp_login_does_not_click_without_agreement_control(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <button id="login" onclick="window.loginClicks=(window.loginClicks||0)+1">登录</button>
                <script>window.loginClicks = 0;</script>
                """
            )
            try:
                attempted = await kuaimai_erp.try_erp_login_with_agreement(page, LOGGER)

                self.assertFalse(attempted)
                self.assertEqual(await page.evaluate("window.loginClicks"), 0)
            finally:
                await browser.close()

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

    async def test_product_center_welcome_dialog_is_dismissed_after_navigation(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()

            async def route_handler(route):
                url = route.request.url
                if "/item/base/page.json" in url:
                    await route.fulfill(
                        content_type="application/json",
                        body='{"result":1,"data":{"records":[]}}',
                    )
                elif "/supplier/prod/center" in url:
                    await route.fulfill(
                        content_type="text/html",
                        body=(
                            "<meta charset='utf-8'><p>商品中心</p>"
                            "<script>"
                            "const nativeFetch = window.fetch.bind(window);"
                            "window.fetch = (...args) => nativeFetch(...args).then(response => {"
                            "  if (String(args[0]).includes('/item/base/page.json')) {"
                            "    const dialog = document.createElement('div');"
                            "    dialog.className = 'el-dialog';"
                            "    dialog.innerHTML = '<strong>温馨提示</strong><button>知道了</button>';"
                            "    dialog.querySelector('button').onclick = () => dialog.remove();"
                            "    document.body.appendChild(dialog);"
                            "  }"
                            "  return response;"
                            "});"
                            "</script>"
                        ),
                    )
                else:
                    await route.fulfill(status=404, body="not found")

            await context.route("https://scma.superboss.cc/**", route_handler)
            page = await context.new_page()
            original_center_url = kuaimai_erp.CENTER_URL
            kuaimai_erp.CENTER_URL = "https://scma.superboss.cc/supplier/prod/center"
            try:
                reused = await kuaimai_erp.try_reuse_verified_scm_session(
                    page, "NGBL-10588", 3, LOGGER
                )
                dialog = reused.locator(".el-dialog").filter(has_text="温馨提示")
                self.assertFalse(await dialog.is_visible())
            finally:
                kuaimai_erp.CENTER_URL = original_center_url
                await context.close()
                await browser.close()

    async def test_product_center_waits_for_late_first_entry_dialog(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()

            async def route_handler(route):
                url = route.request.url
                if "/item/base/page.json" in url:
                    await route.fulfill(
                        content_type="application/json",
                        body='{"result":1,"data":{"records":[]}}',
                    )
                elif "/supplier/prod/center" in url:
                    await route.fulfill(
                        content_type="text/html",
                        body=(
                            "<meta charset='utf-8'><p>商品中心</p>"
                            "<script>"
                            "window.acknowledgeClicked = false;"
                            "window.immediateClicked = false;"
                            "setTimeout(() => {"
                            "  const dialog = document.createElement('div');"
                            "  dialog.className = 'el-dialog';"
                            "  dialog.innerHTML = '<strong>温馨提示</strong>' +"
                            "    '<button id=\"inspect\">立即查看</button>' +"
                            "    '<button id=\"acknowledge\">知道了</button>';"
                            "  dialog.querySelector('#inspect').onclick = () => "
                            "    window.immediateClicked = true;"
                            "  dialog.querySelector('#acknowledge').onclick = () => {"
                            "    window.acknowledgeClicked = true;"
                            "    dialog.remove();"
                            "  };"
                            "  document.body.appendChild(dialog);"
                            "}, 350);"
                            "</script>"
                        ),
                    )
                else:
                    await route.fulfill(status=404, body="not found")

            await context.route("https://scma.superboss.cc/**", route_handler)
            page = await context.new_page()
            original_center_url = kuaimai_erp.CENTER_URL
            kuaimai_erp.CENTER_URL = "https://scma.superboss.cc/supplier/prod/center"
            try:
                reused = await kuaimai_erp.try_reuse_verified_scm_session(
                    page, "NGBL-10588", 3, LOGGER
                )
                self.assertTrue(await reused.evaluate("window.acknowledgeClicked"))
                self.assertFalse(await reused.evaluate("window.immediateClicked"))
                dialog = reused.locator(".el-dialog").filter(has_text="温馨提示")
                self.assertFalse(await dialog.is_visible())
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

    async def test_base_save_waits_for_current_drawer_without_reload(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <meta charset="utf-8">
                <div id="prod-center-edit-dialog">
                  <div class="el-loading-mask">保存中</div>
                  <div class="el-form-item">
                    <label class="el-form-item__label">款式编码：</label>
                    <input value="NGBL-10588">
                  </div>
                  <div class="el-form-item">
                    <label class="el-form-item__label">商品名称：</label>
                    <input value="测试商品">
                  </div>
                </div>
                <script>
                  setTimeout(() => document.querySelector('.el-loading-mask').remove(), 300);
                </script>
                """
            )
            drawer = page.locator("#prod-center-edit-dialog")
            await kuaimai_erp.wait_for_base_form_ready_after_save(
                drawer,
                "NGBL-10588",
                "测试商品",
                timeout_seconds=2,
            )
            self.assertEqual(await drawer.locator(".el-loading-mask:visible").count(), 0)
            self.assertEqual(page.url, "about:blank")
            await browser.close()

    async def test_base_save_prefers_delayed_api_success_over_toast(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()

            async def route_handler(route):
                if "/item/base/edit.json" in route.request.url:
                    await asyncio.sleep(0.35)
                    await route.fulfill(
                        content_type="application/json",
                        body='{"result":1,"data":{"saved":true}}',
                    )
                    return
                await route.fulfill(
                    content_type="text/html",
                    body=(
                        "<meta charset='utf-8'>"
                        "<div id='drawer'><div class='drawer-footer'>"
                        "<button id='save'>保存</button></div></div>"
                        "<div id='toast' class='el-message--success' "
                        "style='display:none'>保存成功</div>"
                        "<script>save.onclick = () => {"
                        "toast.style.display = 'block';"
                        "fetch('/item/base/edit.json', {method: 'POST'});"
                        "};</script>"
                    ),
                )

            await page.route("https://scma.superboss.cc/**", route_handler)
            await page.goto("https://scma.superboss.cc/test")
            result = await kuaimai_erp.click_save_and_confirm(
                page,
                page.locator("#drawer"),
                False,
                3,
                LOGGER,
            )
            self.assertEqual(result["confirmed_by"], "api")
            self.assertEqual(result["payload"]["result"], 1)
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

    async def test_sku_images_preserve_existing_slots_and_upload_only_missing_slots(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <div id="item">
                  <div class="block-specification">
                    <div class="specification-value">
                      <div class="specification-value-flex_img" id="existing">
                        <div class="sc-upload"><div class="file-img"></div></div>
                        <input type="file">
                      </div>
                      <div class="specification-value-flex_img" id="missing">
                        <div class="sc-upload"></div>
                        <input type="file">
                      </div>
                    </div>
                  </div>
                </div>
                """
            )
            with tempfile.TemporaryDirectory() as directory:
                first_image = Path(directory) / "01.jpg"
                second_image = Path(directory) / "02.jpg"
                first_image.write_bytes(b"existing-local-image")
                second_image.write_bytes(b"missing-local-image")
                wait_for_uploads = AsyncMock()
                with patch.object(
                    kuaimai_erp,
                    "wait_for_image_uploads",
                    new=wait_for_uploads,
                ), patch.object(
                    kuaimai_erp,
                    "delete_uploaded_images",
                    new=AsyncMock(),
                ) as delete_images:
                    uploaded = await kuaimai_erp.replace_sku_images(
                        page,
                        page.locator("#item"),
                        (first_image, second_image),
                        2,
                    )

            self.assertEqual(uploaded, 1)
            self.assertEqual(
                await page.locator("#existing input").evaluate(
                    "input => input.files.length"
                ),
                0,
            )
            self.assertEqual(
                await page.locator("#missing input").evaluate(
                    "input => input.files.length"
                ),
                1,
            )
            delete_images.assert_not_awaited()
            wait_for_uploads.assert_awaited_once()
            await browser.close()

    def test_blank_sku_placeholder_detection_only_accepts_near_white_uniform_image(self):
        import cv2
        import numpy as np

        blank = np.full((181, 178, 3), (252, 251, 250), dtype=np.uint8)
        blank[:, ::2, :] = (253, 251, 250)
        ok, blank_png = cv2.imencode(".png", blank)
        self.assertTrue(ok)

        product = blank.copy()
        product[40:140, 55:125, :] = (30, 80, 160)
        ok, product_png = cv2.imencode(".png", product)
        self.assertTrue(ok)

        self.assertTrue(kuaimai_erp.is_blank_sku_placeholder(blank_png.tobytes()))
        self.assertFalse(kuaimai_erp.is_blank_sku_placeholder(product_png.tobytes()))
        self.assertFalse(kuaimai_erp.is_blank_sku_placeholder(b"not-an-image"))

    async def test_blank_sku_data_url_is_decoded_without_network_fetch(self):
        import base64

        import cv2
        import numpy as np
        from playwright.async_api import async_playwright

        blank = np.full((24, 24, 3), (252, 251, 250), dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", blank)
        self.assertTrue(ok)
        source = "data:image/png;base64," + base64.b64encode(
            encoded.tobytes()
        ).decode("ascii")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                '<div id="slot"><div class="sc-upload"><div class="file-img">'
                '<img class="originImg" src="{0}"></div></div></div>'.format(source)
            )

            self.assertFalse(
                await kuaimai_erp.sku_slot_has_real_image(
                    page,
                    page.locator("#slot"),
                )
            )
            await browser.close()

    async def test_ambiguous_multi_image_sku_slot_is_preserved(self):
        import cv2
        import numpy as np
        from playwright.async_api import async_playwright

        blank = np.full((24, 24, 3), (252, 251, 250), dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", blank)
        self.assertTrue(ok)
        response = SimpleNamespace(
            ok=True,
            body=AsyncMock(return_value=encoded.tobytes()),
            dispose=AsyncMock(),
        )
        request = SimpleNamespace(get=AsyncMock(return_value=response))
        page_for_request = SimpleNamespace(context=SimpleNamespace(request=request))

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <div id="slot"><div class="sc-upload">
                  <div class="file-img">
                    <img class="originImg" src="https://example.test/blank.png">
                  </div>
                  <div class="file-img">
                    <img class="originImg" src="https://example.test/real.png">
                  </div>
                </div></div>
                """
            )

            self.assertTrue(
                await kuaimai_erp.sku_slot_has_real_image(
                    page_for_request,
                    page.locator("#slot"),
                ),
                "槽位存在多张图时不得只凭第一张空白图删除全部",
            )
            await browser.close()

    async def test_sku_images_replace_blank_placeholder_but_preserve_real_image(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <div id="item">
                  <div class="block-specification">
                    <div class="specification-value">
                      <div class="specification-value-flex_img" id="blank">
                        <div class="sc-upload">
                          <div class="file-img">
                            <img class="originImg" src="https://example.test/blank.png">
                            <button class="del-btn"
                              onclick="this.closest('.file-img').remove()">删除</button>
                          </div>
                        </div>
                        <input type="file">
                      </div>
                      <div class="specification-value-flex_img" id="real">
                        <div class="sc-upload">
                          <div class="file-img">
                            <img class="originImg" src="https://example.test/real.png">
                          </div>
                        </div>
                        <input type="file">
                      </div>
                    </div>
                  </div>
                </div>
                """
            )
            with tempfile.TemporaryDirectory() as directory:
                first_image = Path(directory) / "01.jpg"
                second_image = Path(directory) / "02.jpg"
                first_image.write_bytes(b"replace-blank-placeholder")
                second_image.write_bytes(b"preserve-real-image")
                wait_for_uploads = AsyncMock()
                with patch.object(
                    kuaimai_erp,
                    "sku_slot_has_real_image",
                    new=AsyncMock(side_effect=(False, True)),
                ), patch.object(
                    kuaimai_erp,
                    "wait_for_image_uploads",
                    new=wait_for_uploads,
                ):
                    uploaded = await kuaimai_erp.replace_sku_images(
                        page,
                        page.locator("#item"),
                        (first_image, second_image),
                        2,
                    )

            self.assertEqual(uploaded, 1)
            self.assertEqual(await page.locator("#blank .file-img").count(), 0)
            self.assertEqual(
                await page.locator("#blank input").evaluate(
                    "input => input.files.length"
                ),
                1,
            )
            self.assertEqual(await page.locator("#real .file-img").count(), 1)
            self.assertEqual(
                await page.locator("#real input").evaluate(
                    "input => input.files.length"
                ),
                0,
            )
            wait_for_uploads.assert_awaited_once()
            await browser.close()

    async def test_taobao_publish_preview_selects_only_target_shop_without_submitting(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <meta charset="utf-8">
                <div role="dialog" id="publish-dialog">
                  <h2>铺货到店铺</h2>
                  <button onclick="shops.style.display='block'">淘宝</button>
                  <button>抖音</button>
                  <div id="shops" style="display:none">
                    <label class="el-checkbox">取消全选
                      <input type="checkbox" checked>
                    </label>
                    <label class="el-checkbox">中古Remake商店
                      <input type="checkbox" checked>
                    </label>
                    <label class="el-checkbox">钊叔制
                      <input type="checkbox">
                    </label>
                    <label class="el-checkbox">Wuli钊哥穿搭
                      <input type="checkbox" checked>
                    </label>
                    <label class="el-checkbox">使用AI裂变规则
                      <input type="checkbox" checked>
                    </label>
                  </div>
                  <button onclick="window.finalSubmitted=true">确定</button>
                  <span id="icon-close">×</span>
                </div>
                <script>
                  window.finalSubmitted = false;
                  document.addEventListener('keydown', event => {
                    if (event.key === 'Escape') {
                      document.querySelector('[role=dialog]').style.display = 'none';
                    }
                  });
                </script>
                """
            )
            dialog, report = await kuaimai_erp.prepare_taobao_publish_dialog(
                page,
                ("钊叔制",),
                2,
                LOGGER,
            )

            self.assertEqual(report["platform"], "淘宝")
            self.assertEqual(report["selected_shops"], ["钊叔制"])
            self.assertFalse(report["submitted"])
            checked = await dialog.locator(
                '.el-checkbox input[type="checkbox"]:checked'
            ).evaluate_all(
                "els => els.map(el => el.closest('.el-checkbox').innerText.trim())"
            )
            self.assertEqual(
                set(checked),
                {"取消全选", "钊叔制", "使用AI裂变规则"},
            )
            self.assertFalse(await page.evaluate("window.finalSubmitted"))

            await kuaimai_erp.close_publish_preview(dialog)
            self.assertFalse(await dialog.is_visible())
            self.assertFalse(await page.evaluate("window.finalSubmitted"))
            await browser.close()

    async def test_youzan_publish_preview_selects_only_official_shop_without_submitting(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <meta charset="utf-8">
                <div role="dialog" id="publish-dialog">
                  <h2>铺货到店铺</h2>
                  <button onclick="shops.style.display='block'">有赞</button>
                  <button>小红书</button>
                  <div id="shops" style="display:none">
                    <label class="el-checkbox">其他有赞店
                      <input type="checkbox" checked>
                    </label>
                    <label class="el-checkbox">NEIGBORL官方旗舰店
                      <input type="checkbox">
                    </label>
                    <label class="el-checkbox">使用AI裂变规则
                      <input type="checkbox" checked>
                    </label>
                  </div>
                  <button onclick="window.finalSubmitted=true">确定</button>
                </div>
                <script>window.finalSubmitted = false;</script>
                """
            )

            dialog, report = await kuaimai_erp.prepare_taobao_publish_dialog(
                page,
                kuaimai_erp.DEFAULT_YOUZAN_PUBLISH_SHOPS,
                2,
                LOGGER,
                platform_name="有赞",
            )

            self.assertEqual(report["platform"], "有赞")
            self.assertEqual(report["selected_shops"], ["NEIGBORL官方旗舰店"])
            self.assertFalse(report["submitted"])
            checked = await dialog.locator(
                '.el-checkbox input[type="checkbox"]:checked'
            ).evaluate_all(
                "els => els.map(el => el.closest('.el-checkbox').innerText.trim())"
            )
            self.assertEqual(
                set(checked),
                {"NEIGBORL官方旗舰店", "使用AI裂变规则"},
            )
            self.assertFalse(await page.evaluate("window.finalSubmitted"))
            await browser.close()

    async def test_wxsph_publish_preview_selects_only_two_target_shops_without_submitting(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <meta charset="utf-8">
                <div role="dialog" id="publish-dialog">
                  <h2>铺货到店铺</h2>
                  <button onclick="shops.style.display='block'">微信小店（视频号）</button>
                  <button>有赞</button>
                  <div id="shops" style="display:none">
                    <label class="el-checkbox">杰叔织造JSHU
                      <input type="checkbox" checked>
                    </label>
                    <label class="el-checkbox">NEIGBORL钊叔制鞋服
                      <input type="checkbox">
                    </label>
                    <label class="el-checkbox">NEIGBORL钊叔制造局
                      <input type="checkbox">
                    </label>
                    <label class="el-checkbox">夏一制
                      <input type="checkbox" checked>
                    </label>
                    <label class="el-checkbox">使用AI裂变规则
                      <input type="checkbox" checked>
                    </label>
                  </div>
                  <button onclick="window.finalSubmitted=true">确定</button>
                </div>
                <script>window.finalSubmitted = false;</script>
                """
            )

            _dialog, report = await kuaimai_erp.prepare_taobao_publish_dialog(
                page,
                kuaimai_erp.DEFAULT_WXSPH_PUBLISH_SHOPS,
                2,
                LOGGER,
                platform_name="微信小店（视频号）",
            )

            self.assertEqual(report["platform"], "微信小店（视频号）")
            self.assertEqual(
                report["selected_shops"],
                ["NEIGBORL钊叔制鞋服", "NEIGBORL钊叔制造局"],
            )
            checked = await page.locator(
                '.el-checkbox input[type="checkbox"]:checked'
            ).evaluate_all(
                "els => els.map(el => el.closest('.el-checkbox').innerText.trim())"
            )
            self.assertEqual(
                set(checked),
                {
                    "NEIGBORL钊叔制鞋服",
                    "NEIGBORL钊叔制造局",
                    "使用AI裂变规则",
                },
            )
            self.assertFalse(report["submitted"])
            self.assertFalse(await page.evaluate("window.finalSubmitted"))
            await browser.close()

    async def test_jd_publish_preview_selects_only_target_shop_without_submitting(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <meta charset="utf-8">
                <div role="dialog" id="publish-dialog">
                  <h2>铺货到店铺</h2>
                  <button onclick="shops.style.display='block'">京东</button>
                  <button>有赞</button>
                  <div id="shops" style="display:none">
                    <label class="el-checkbox">其他京东店铺
                      <input type="checkbox" checked>
                    </label>
                    <label class="el-checkbox">NEIGBORL服饰旗舰店
                      <input type="checkbox">
                    </label>
                    <label class="el-checkbox">使用AI裂变规则
                      <input type="checkbox" checked>
                    </label>
                  </div>
                  <button onclick="window.finalSubmitted=true">确定</button>
                </div>
                <script>window.finalSubmitted = false;</script>
                """
            )

            dialog, report = await kuaimai_erp.prepare_taobao_publish_dialog(
                page,
                kuaimai_erp.DEFAULT_JD_PUBLISH_SHOPS,
                2,
                LOGGER,
                platform_name="京东",
            )

            self.assertEqual(report["platform"], "京东")
            self.assertEqual(report["selected_shops"], ["NEIGBORL服饰旗舰店"])
            self.assertFalse(report["submitted"])
            checked = await dialog.locator(
                '.el-checkbox input[type="checkbox"]:checked'
            ).evaluate_all(
                "els => els.map(el => el.closest('.el-checkbox').innerText.trim())"
            )
            self.assertEqual(
                set(checked),
                {"NEIGBORL服饰旗舰店", "使用AI裂变规则"},
            )
            self.assertFalse(await page.evaluate("window.finalSubmitted"))
            await browser.close()

    async def test_xhs_publish_selects_only_configured_shops_and_reads_compact_status(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <meta charset="utf-8">
                <div role="dialog" id="publish-dialog">
                  <h2>铺货到店铺</h2>
                  <button onclick="shops.style.display='block'">小红书</button>
                  <button>拼多多</button>
                  <div id="shops" style="display:none">
                    <label class="el-checkbox">白胖BAIPAN的店
                      <input type="checkbox" checked>
                    </label>
                    <label class="el-checkbox">钊叔的店
                      <input type="checkbox">
                    </label>
                    <label class="el-checkbox"><span>啊亮熟NEIGBORL的店</span><span>已铺货</span><input type="checkbox" disabled></label>
                    <label class="el-checkbox">钊叔制NEIGBORL的店
                      <input type="checkbox">
                    </label>
                    <label class="el-checkbox">老朱和NEIGBORL的店
                      <input type="checkbox">
                    </label>
                    <label class="el-checkbox"><span>NEIGBORL钊叔旁伦的店</span><span>已铺货</span><input type="checkbox" disabled></label>
                    <label class="el-checkbox">使用AI裂变规则
                      <input type="checkbox" checked>
                    </label>
                  </div>
                  <button>确定</button>
                </div>
                """
            )

            dialog, report = await kuaimai_erp.prepare_taobao_publish_dialog(
                page,
                kuaimai_erp.DEFAULT_XHS_PUBLISH_SHOPS,
                2,
                LOGGER,
                platform_name="小红书",
            )

            self.assertEqual(report["platform"], "小红书")
            self.assertEqual(
                set(report["selected_shops"]),
                {"钊叔的店", "钊叔制NEIGBORL的店", "老朱和NEIGBORL的店"},
            )
            self.assertEqual(
                set(report["already_published"]),
                {"啊亮熟NEIGBORL的店", "NEIGBORL钊叔旁伦的店"},
            )
            self.assertFalse(
                await dialog.locator("label", has_text="白胖BAIPAN的店")
                .locator('input[type="checkbox"]')
                .is_checked()
            )
            await browser.close()

    async def test_publish_submit_skips_when_every_target_is_already_published(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <div role="dialog" id="publish-dialog">
                  <h2>铺货到店铺</h2>
                  <button onclick="this.closest('[role=dialog]').style.display='none'">取消</button>
                  <button onclick="window.finalSubmitted=true">确定</button>
                </div>
                <script>window.finalSubmitted = false;</script>
                """
            )
            dialog = page.locator('[role="dialog"]')

            result = await kuaimai_erp.submit_taobao_publish_dialog(
                page,
                dialog,
                {
                    "platform": "拼多多",
                    "requested_shops": ["NEIGBORL鞋服旗舰店"],
                    "selected_shops": [],
                    "already_published": ["NEIGBORL鞋服旗舰店"],
                    "submitted": False,
                },
                2,
                LOGGER,
                platform_name="拼多多",
            )

            self.assertFalse(result["submitted"])
            self.assertEqual(result["confirmed_by"], "already_published")
            self.assertFalse(await dialog.is_visible())
            self.assertFalse(await page.evaluate("window.finalSubmitted"))
            await browser.close()

    async def test_publish_progress_dialog_waits_for_late_render_and_collapses_it(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <div role="dialog" id="progress" style="display:none">
                  <h2>铺货中...</h2>
                  <p>商品总数：1款</p>
                  <p>商品铺货中...(成功：0，失败：0)</p>
                  <div>33%</div>
                  <button onclick="this.closest('[role=dialog]').style.display='none'">收起</button>
                </div>
                <script>
                  setTimeout(() => {
                    document.querySelector('#progress').style.display = 'block';
                  }, 100);
                </script>
                """
            )

            result = await kuaimai_erp.dismiss_publish_progress_dialog(
                page,
                LOGGER,
                appearance_timeout_seconds=1,
            )

            self.assertTrue(result["found"])
            self.assertTrue(result["dismissed"])
            self.assertEqual(result["action"], "collapse")
            self.assertEqual(result["progress_percent"], 33)
            self.assertFalse(await page.locator("#progress").is_visible())
            await browser.close()

    async def test_publish_progress_dialog_is_optional_when_none_is_visible(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content("<main>商品中心</main>")

            result = await kuaimai_erp.dismiss_publish_progress_dialog(
                page,
                LOGGER,
                appearance_timeout_seconds=0,
            )

            self.assertEqual(
                result,
                {"found": False, "dismissed": False, "action": None},
            )
            await browser.close()

    async def test_douyin_publish_accepts_completion_dialog_and_closes_it(self):
        from playwright.async_api import async_playwright

        shop_controls = "".join(
            f'<label class="el-checkbox">{shop}<input type="checkbox"></label>'
            for shop in kuaimai_erp.KNOWN_DOUYIN_SHOPS
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                f"""
                <meta charset="utf-8">
                <div role="dialog" id="publish-dialog">
                  <h2>铺货到店铺</h2>
                  <button onclick="shops.style.display='block'">抖音</button>
                  <div id="shops" style="display:none">
                    {shop_controls}
                    <label class="el-checkbox">使用AI裂变规则
                      <input type="checkbox" checked>
                    </label>
                  </div>
                  <button onclick="finishPublish()">确定</button>
                </div>
                <div role="dialog" id="completion" style="display:none">
                  <h2>铺货完成</h2>
                  <p>商品总数：1款，成功1款，失败0款</p>
                  <button class="el-dialog__headerbtn"
                    onclick="this.closest('[role=dialog]').style.display='none'">关闭</button>
                </div>
                <script>
                  function finishPublish() {{
                    document.querySelector('#publish-dialog').style.display = 'none';
                    document.querySelector('#completion').style.display = 'block';
                  }}
                </script>
                """
            )

            result = await kuaimai_erp.publish_to_selected_douyin_shops(
                page,
                (kuaimai_erp.KNOWN_DOUYIN_SHOPS[0],),
                2,
                LOGGER,
            )

            self.assertEqual(result["confirmed_by"], "completion_dialog")
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["succeeded"], 1)
            self.assertEqual(result["failed"], 0)
            self.assertFalse(await page.locator("#completion").is_visible())
            await browser.close()

    async def test_taobao_publish_submit_uses_confirm_and_continue_after_exact_selection(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <meta charset="utf-8">
                <div role="dialog" id="publish-dialog">
                  <h2>铺货到店铺</h2>
                  <button onclick="shops.style.display='block'">淘宝</button>
                  <div id="shops" style="display:none">
                    <label class="el-checkbox">中古Remake商店
                      <input type="checkbox" checked>
                    </label>
                    <label class="el-checkbox">钊叔制
                      <input type="checkbox">
                    </label>
                    <label class="el-checkbox">Wuli钊哥穿搭
                      <input type="checkbox" checked>
                    </label>
                  </div>
                  <button onclick="openContinue()">确定</button>
                </div>
                <button id="continue" style="display:none" onclick="finishPublish()">继续铺货</button>
                <div class="el-message--success" style="display:none">铺货成功</div>
                <script>
                  function openContinue() {
                    document.querySelector('#publish-dialog').style.display = 'none';
                    document.querySelector('#continue').style.display = 'block';
                  }
                  function finishPublish() {
                    document.querySelector('#continue').style.display = 'none';
                    document.querySelector('.el-message--success').style.display = 'block';
                  }
                </script>
                """
            )
            dialog, selection = await kuaimai_erp.prepare_taobao_publish_dialog(
                page,
                ("钊叔制",),
                2,
                LOGGER,
            )
            result = await kuaimai_erp.submit_taobao_publish_dialog(
                page,
                dialog,
                selection,
                2,
                LOGGER,
            )

            self.assertTrue(result["submitted"])
            self.assertEqual(result["submitted_shops"], ["钊叔制"])
            self.assertEqual(result["confirmed_by"], "toast")
            self.assertTrue(
                await page.locator(".el-message--success").is_visible()
            )
            await browser.close()

    async def test_taobao_publish_submit_accepts_background_task_handoff(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            page = await browser.new_page()
            await page.set_content(
                """
                <meta charset="utf-8">
                <div role="dialog" id="publish-dialog">
                  <h2>铺货到店铺</h2>
                  <button onclick="this.closest('[role=dialog]').style.display='none'">确定</button>
                </div>
                """
            )
            dialog = page.locator('[role="dialog"]')
            result = await kuaimai_erp.submit_taobao_publish_dialog(
                page,
                dialog,
                {
                    "platform": "淘宝",
                    "requested_shops": ["钊叔制"],
                    "selected_shops": ["钊叔制"],
                    "already_published": [],
                    "submitted": False,
                },
                2,
                LOGGER,
            )

            self.assertTrue(result["submitted"])
            self.assertEqual(result["submitted_shops"], ["钊叔制"])
            self.assertEqual(result["confirmed_by"], "background_task_submitted")
            await browser.close()


class ExecutionModeTests(unittest.TestCase):
    def _args(self, *arguments):
        return kuaimai_erp.build_parser().parse_args(list(arguments))

    def test_learning_is_opt_in_with_safe_environment_defaults(self):
        with patch.dict(
            os.environ,
            {
                "KUAIMAI_LEARNING_DB": "/tmp/learning-state.sqlite3",
                "KUAIMAI_LEARNING_API_URL": "https://review.example",
                "KUAIMAI_LEARNING_DEVICE_TOKEN": "must-not-appear",
            },
        ):
            args = self._args("--platform", "all")

        self.assertFalse(args.learning_enabled)
        self.assertEqual(args.learning_db, "/tmp/learning-state.sqlite3")
        self.assertEqual(args.learning_api_url, "https://review.example")
        self.assertNotIn("must-not-appear", repr(args))
        self.assertFalse(hasattr(args, "learning_device_token"))

        with patch.dict(os.environ, {}, clear=True):
            defaults = self._args("--platform", "all")
        self.assertEqual(defaults.learning_db, ".local-state/learning.sqlite3")
        self.assertEqual(defaults.learning_api_url, "")

    def test_disabled_learning_context_does_not_open_a_store(self):
        args = self._args("--platform", "all")
        with patch.object(kuaimai_erp, "LearningStore") as store:
            context = kuaimai_erp.create_learning_context(
                args,
                SimpleNamespace(),
            )
        self.assertIsNone(context)
        store.assert_not_called()

    def test_enabled_learning_context_fingerprints_and_upserts_product(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.jpg"
            square = root / "square.jpg"
            detail = root / "detail.jpg"
            main.write_bytes(b"main")
            square.write_bytes(b"square")
            detail.write_bytes(b"detail")
            product = SimpleNamespace(
                style_code="NGBL-1",
                title="标题",
                main_images=[main],
                main_images_34=[square],
                detail_images=[detail],
            )
            args = self._args(
                "--platform",
                "all",
                "--learning-enabled",
                "--learning-db",
                str(root / "learning.sqlite3"),
            )
            fake_store = Mock()
            with patch.object(kuaimai_erp, "LearningStore", return_value=fake_store):
                context = kuaimai_erp.create_learning_context(args, product)

        self.assertIsNotNone(context)
        fake_store.migrate.assert_called_once_with()
        fingerprint = fake_store.upsert_product.call_args.args[0]
        self.assertEqual(fingerprint.style_code, "NGBL-1")
        self.assertEqual(len(fingerprint.assets), 3)
        self.assertNotIn("must-not-appear", repr(context))
        fake_store.enqueue.assert_called_once()
        self.assertEqual(fake_store.enqueue.call_args.args[1], "product.upsert")

    def test_checkpoint_event_uses_committed_store_version(self):
        args = self._args("--platform", "pdd", "--save-only")
        store = Mock()
        store.save_checkpoint.return_value = kuaimai_erp.RunCheckpoint(
            "run-1", "product-1", "save_only", ("pdd",), 1, "completed", version=3
        )
        context = kuaimai_erp.LearningRunContext(store, "run-1", "product-1")

        kuaimai_erp.save_learning_checkpoint(
            context, args, ("pdd",), 1, "completed"
        )

        key, event_type, payload = store.enqueue.call_args.args
        self.assertEqual(key, "checkpoint.updated:run-1:3")
        self.assertEqual(event_type, "checkpoint.updated")
        self.assertEqual(payload["version"], 3)

    def test_parser_uses_registry_platforms_and_accepts_inspect_only(self):
        args = self._args("--platform", "tmall", "--inspect-only", "--no-save")

        self.assertEqual(args.platform, "tmall")
        self.assertTrue(args.inspect_only)
        self.assertFalse(args.save)

    def test_preview_and_scaffold_execution_mode_matrix(self):
        valid = (
            ("--platform", "tmall", "--no-save"),
            ("--platform", "tmall", "--inspect-only", "--no-save"),
            ("--platform", "tmall"),
            ("--platform", "tmall", "--save-only"),
            ("--platform", "pdd", "--dry-run"),
            ("--platform", "wxsph", "--no-save"),
            ("--platform", "wxsph"),
            ("--platform", "wxsph", "--save-only"),
            ("--platform", "wxsph", "--wxsph-publish-preview"),
            ("--platform", "wxsph", "--inspect-only", "--no-save"),
            ("--platform", "wxsph", "--dry-run"),
            ("--platform", "xhs", "--no-save"),
            ("--platform", "xhs"),
            ("--platform", "xhs", "--save-only"),
            ("--platform", "xhs", "--inspect-only", "--no-save"),
            ("--platform", "youzan", "--no-save"),
            ("--platform", "youzan"),
            ("--platform", "youzan", "--save-only"),
            ("--platform", "youzan", "--dry-run"),
        )
        invalid = (
            ("--platform", "tmall", "--inspect-only"),
            ("--platform", "tmall", "--inspect-only", "--no-save", "--save-only"),
            ("--platform", "tmall", "--inspect-only", "--no-save", "--dry-run"),
            (
                "--platform",
                "tmall",
                "--inspect-only",
                "--no-save",
                "--allow-taobao-save-once",
            ),
        )

        for arguments in valid:
            with self.subTest(arguments=arguments):
                selected = kuaimai_erp.validate_execution_mode(self._args(*arguments))
                self.assertEqual(len(selected), 1)
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    kuaimai_erp.validate_execution_mode(self._args(*arguments))

    def test_all_expands_only_commerce_platforms_not_base(self):
        selected = kuaimai_erp.validate_execution_mode(self._args("--platform", "all"))

        self.assertEqual(
            tuple(spec.cli_name for spec in selected),
            ("douyin", "taobao", "tmall", "pdd", "wxsph", "xhs", "youzan", "jd"),
        )

    def test_only_base_platform_runs_base_save_stage(self):
        cases = (
            ("base", True),
            ("douyin", False),
            ("taobao", False),
            ("tmall", False),
            ("pdd", False),
            ("wxsph", False),
            ("xhs", False),
            ("youzan", False),
        )

        for platform, expected in cases:
            with self.subTest(platform=platform):
                self.assertEqual(
                    kuaimai_erp.requires_base_save_before_platform(platform),
                    expected,
                )

    def test_xhs_direct_save_allows_store_publish(self):
        selected = kuaimai_erp.validate_execution_mode(self._args("--platform", "xhs"))
        self.assertEqual(tuple(spec.cli_name for spec in selected), ("xhs",))
        self.assertTrue(selected[0].publish_allowed)

        invalid = (
            ("--platform", "xhs", "--allow-taobao-save-once"),
            ("--platform", "xhs", "--allow-taobao-publish-once"),
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    kuaimai_erp.validate_execution_mode(self._args(*arguments))

    def test_commerce_publish_targets_are_exact_and_save_only_disables_them(self):
        pdd_target = kuaimai_erp.resolve_commerce_publish_target(
            self._args("--platform", "pdd")
        )
        wxsph_target = kuaimai_erp.resolve_commerce_publish_target(
            self._args("--platform", "wxsph")
        )
        xhs_target = kuaimai_erp.resolve_commerce_publish_target(
            self._args("--platform", "xhs")
        )
        youzan_target = kuaimai_erp.resolve_commerce_publish_target(
            self._args("--platform", "youzan")
        )
        jd_target = kuaimai_erp.resolve_commerce_publish_target(
            self._args("--platform", "jd")
        )

        self.assertEqual(
            pdd_target,
            ("拼多多", ("NEIGBORL鞋服旗舰店",)),
        )
        self.assertEqual(
            wxsph_target,
            (
                "微信小店（视频号）",
                ("NEIGBORL钊叔制鞋服", "NEIGBORL钊叔制造局"),
            ),
        )
        self.assertEqual(
            xhs_target,
            (
                "小红书",
                (
                    "钊叔的店",
                    "啊亮熟NEIGBORL的店",
                    "钊叔制NEIGBORL的店",
                    "老朱和NEIGBORL的店",
                    "NEIGBORL钊叔旁伦的店",
                ),
            ),
        )
        self.assertEqual(
            youzan_target,
            ("有赞", ("NEIGBORL官方旗舰店",)),
        )
        self.assertEqual(
            jd_target,
            ("京东", ("NEIGBORL服饰旗舰店",)),
        )
        self.assertIsNone(
            kuaimai_erp.resolve_commerce_publish_target(
                self._args("--platform", "pdd", "--save-only")
            )
        )
        self.assertIsNone(
            kuaimai_erp.resolve_commerce_publish_target(
                self._args("--platform", "wxsph", "--save-only")
            )
        )
        self.assertIsNone(
            kuaimai_erp.resolve_commerce_publish_target(
                self._args("--platform", "xhs", "--save-only")
            )
        )
        self.assertIsNone(
            kuaimai_erp.resolve_commerce_publish_target(
                self._args("--platform", "youzan", "--save-only")
            )
        )
        self.assertIsNone(
            kuaimai_erp.resolve_commerce_publish_target(
                self._args("--platform", "jd", "--save-only")
            )
        )

    def test_jd_formal_mode_publishes_but_save_only_uses_plain_save(self):
        publish_args = self._args("--platform", "jd")
        self.assertEqual(
            kuaimai_erp.resolve_platform_save_action(
                publish_args,
                publish_mode=False,
            ),
            ("保存并铺货到平台", True),
        )
        save_args = self._args("--platform", "jd", "--save-only")
        self.assertEqual(
            kuaimai_erp.resolve_platform_save_action(
                save_args,
                publish_mode=False,
            ),
            ("保存", False),
        )

    def test_youzan_publish_preview_requires_formal_unsubmitted_mode(self):
        invalid = (
            ("--platform", "xhs", "--youzan-publish-preview"),
            ("--platform", "youzan", "--youzan-publish-preview", "--no-save"),
            ("--platform", "youzan", "--youzan-publish-preview", "--save-only"),
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    kuaimai_erp.validate_execution_mode(self._args(*arguments))

        selected = kuaimai_erp.validate_execution_mode(
            self._args("--platform", "youzan", "--youzan-publish-preview")
        )
        self.assertEqual(tuple(spec.cli_name for spec in selected), ("youzan",))

    def test_wxsph_publish_preview_requires_formal_unsubmitted_mode(self):
        invalid = (
            ("--platform", "xhs", "--wxsph-publish-preview"),
            ("--platform", "wxsph", "--wxsph-publish-preview", "--no-save"),
            ("--platform", "wxsph", "--wxsph-publish-preview", "--save-only"),
            ("--platform", "wxsph", "--wxsph-publish-preview", "--dry-run"),
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    kuaimai_erp.validate_execution_mode(self._args(*arguments))

        selected = kuaimai_erp.validate_execution_mode(
            self._args("--platform", "wxsph", "--wxsph-publish-preview")
        )
        self.assertEqual(tuple(spec.cli_name for spec in selected), ("wxsph",))

    def test_jd_publish_preview_requires_formal_unsubmitted_mode(self):
        invalid = (
            ("--platform", "xhs", "--jd-publish-preview"),
            ("--platform", "jd", "--jd-publish-preview", "--no-save"),
            ("--platform", "jd", "--jd-publish-preview", "--save-only"),
            ("--platform", "jd", "--jd-publish-preview", "--dry-run"),
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    kuaimai_erp.validate_execution_mode(self._args(*arguments))

        selected = kuaimai_erp.validate_execution_mode(
            self._args("--platform", "jd", "--jd-publish-preview")
        )
        self.assertEqual(tuple(spec.cli_name for spec in selected), ("jd",))

    def test_taobao_explicit_once_gate_is_preserved(self):
        with self.assertRaises(SystemExit):
            kuaimai_erp.validate_execution_mode(self._args("--platform", "taobao"))

        selected = kuaimai_erp.validate_execution_mode(
            self._args("--platform", "taobao", "--allow-taobao-save-once")
        )
        self.assertEqual(tuple(spec.cli_name for spec in selected), ("taobao",))

    def test_taobao_publish_preview_requires_full_once_authorized_save(self):
        invalid = (
            ("--platform", "taobao", "--taobao-publish-preview"),
            (
                "--platform",
                "taobao",
                "--taobao-publish-preview",
                "--allow-taobao-save-once",
                "--no-save",
            ),
            (
                "--platform",
                "taobao",
                "--taobao-publish-preview",
                "--allow-taobao-save-once",
                "--taobao-test-scope",
                "sku-batch",
                "--no-save",
            ),
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    kuaimai_erp.validate_execution_mode(self._args(*arguments))

        selected = kuaimai_erp.validate_execution_mode(
            self._args(
                "--platform",
                "taobao",
                "--taobao-publish-preview",
                "--allow-taobao-save-once",
            )
        )
        self.assertEqual(tuple(spec.cli_name for spec in selected), ("taobao",))

    def test_taobao_publish_requires_dedicated_once_authorization(self):
        invalid = (
            (
                "--platform",
                "taobao",
                "--allow-taobao-publish-once",
                "--no-save",
            ),
            (
                "--platform",
                "taobao",
                "--allow-taobao-publish-once",
                "--save-only",
            ),
            (
                "--platform",
                "taobao",
                "--allow-taobao-publish-once",
                "--taobao-publish-preview",
                "--allow-taobao-save-once",
            ),
            ("--platform", "douyin", "--allow-taobao-publish-once"),
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    kuaimai_erp.validate_execution_mode(self._args(*arguments))

        selected = kuaimai_erp.validate_execution_mode(
            self._args("--platform", "taobao", "--allow-taobao-publish-once")
        )
        self.assertEqual(tuple(spec.cli_name for spec in selected), ("taobao",))

    def test_invalid_mode_exits_before_logging_excel_or_asyncio(self):
        setup_logging = Mock()
        resolve_excel_path = Mock()
        read_product_data = Mock()
        asyncio_run = Mock()
        with patch.object(kuaimai_erp, "setup_logging", setup_logging), patch.object(
            kuaimai_erp, "resolve_excel_path", resolve_excel_path
        ), patch.object(kuaimai_erp, "read_product_data", read_product_data), patch.object(
            kuaimai_erp.asyncio, "run", asyncio_run
        ), patch(
            "sys.argv",
            [
                "kuaimai_erp",
                "--platform",
                "wxsph",
                "--wxsph-publish-preview",
                "--save-only",
            ],
        ):
            with self.assertRaises(SystemExit):
                kuaimai_erp.main()

        setup_logging.assert_not_called()
        resolve_excel_path.assert_not_called()
        read_product_data.assert_not_called()
        asyncio_run.assert_not_called()


class ProductCategoryHintTests(unittest.TestCase):
    def test_category_hints_split_ascii_and_fullwidth_slashes(self):
        self.assertEqual(
            kuaimai_erp.parse_category_hints(" 男装 / 休闲裤 ／ 工装休闲裤 "),
            ("男装", "休闲裤", "工装休闲裤"),
        )

    def test_category_hints_are_empty_when_excel_value_is_missing(self):
        self.assertEqual(kuaimai_erp.parse_category_hints(None), ())

    def test_tmall_size_rows_keep_ranges_and_dynamic_measurement_headers(self):
        source = (
            kuaimai_erp.SkuRecommendation(
                size="S",
                height_min=155,
                height_max=160,
                weight_min=45,
                weight_max=50,
                waist=68,
                hip=96,
                length=99,
            ),
        )

        rows = kuaimai_erp.build_tmall_size_rows(source)

        self.assertEqual(
            rows,
            (
                {
                    "尺码": "S",
                    "身高(cm)": (155, 160),
                    "体重(kg)": (45, 50),
                    "腰围(cm)": 68,
                    "臀围(cm)": 96,
                    "裤长(cm)": 99,
                },
            ),
        )


class _StyleInput:
    @property
    def first(self):
        return self

    async def input_value(self):
        return "STYLE-INSPECT"


class _StyleItem:
    def locator(self, _selector):
        return _StyleInput()


class _AsyncPlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, _error_type, _error, _traceback):
        return False


class InspectMainIntegrationTests(unittest.TestCase):
    def _product(self):
        return kuaimai_erp.ProductData(
            excel_path=Path("/input/产品信息.xlsx"),
            product_dir=Path("/input"),
            title="PRIVATE TITLE",
            style_code="STYLE-INSPECT",
            base_price="586",
            main_images=[],
            main_images_34=[],
            detail_images=[],
            sku_images=[],
            category_hints=("男装", "休闲裤"),
        )

    def test_inspect_main_uses_schema_artifact_dir_and_skips_input_summary(self):
        logger = Mock()
        setup_logging = Mock(return_value=logger)
        product_summary = Mock(return_value={"title": "PRIVATE TITLE"})
        browser_run = AsyncMock()
        with patch.object(kuaimai_erp, "setup_logging", setup_logging), patch.object(
            kuaimai_erp, "resolve_excel_path", return_value=Path("/input/产品信息.xlsx")
        ), patch.object(
            kuaimai_erp, "read_product_data", return_value=self._product()
        ), patch.object(
            kuaimai_erp, "product_summary", product_summary
        ), patch.object(
            kuaimai_erp, "run_browser_automation", browser_run
        ), patch.object(
            Path, "write_text", autospec=True
        ) as write_text, patch.object(
            kuaimai_erp.time, "strftime", return_value="20260830-120000"
        ), patch(
            "sys.argv",
            [
                "kuaimai_erp",
                "--platform",
                "tmall",
                "--inspect-only",
                "--no-save",
                "--cdp-url",
                "http://127.0.0.1:9222",
            ],
        ):
            self.assertEqual(kuaimai_erp.main(), 0)

        artifact_dir = setup_logging.call_args.args[0]
        self.assertEqual(
            artifact_dir,
            kuaimai_erp.SCRIPT_DIR
            / "output/platform-schema/20260830-120000",
        )
        self.assertIsNotNone(setup_logging.call_args.kwargs["redactor"])
        product_summary.assert_not_called()
        write_text.assert_not_called()
        self.assertIsNotNone(browser_run.call_args.kwargs["redactor"])


class InspectBrowserShortCircuitTests(unittest.IsolatedAsyncioTestCase):
    async def test_base_item_identity_is_registered_before_api_success_log(self):
        from platform_inspection import SensitiveLogRedactor

        page = SimpleNamespace(
            evaluate=AsyncMock(
                return_value={
                    "ok": True,
                    "data": {
                        "result": 1,
                        "data": {
                            "records": [
                                {
                                    "outerId": "STYLE-INSPECT",
                                    "baseItemId": "PRIVATE-BASE-9",
                                }
                            ]
                        },
                    },
                }
            )
        )
        redactor = SensitiveLogRedactor(("STYLE-INSPECT",))
        with tempfile.TemporaryDirectory() as directory:
            logger = kuaimai_erp.setup_logging(Path(directory), redactor=redactor)
            record = await kuaimai_erp.api_find_product(
                page,
                "STYLE-INSPECT",
                logger,
                redactor=redactor,
            )
            for handler in logger.handlers:
                handler.flush()
            text = (Path(directory) / "run.log").read_text(encoding="utf-8")
            for handler in logger.handlers:
                handler.close()

        self.assertEqual(record["baseItemId"], "PRIVATE-BASE-9")
        self.assertIn("PRIVATE-BASE-9", redactor.sensitive_values)
        self.assertNotIn("PRIVATE-BASE-9", text)

    async def test_inspect_calls_discovery_before_all_mutations_and_keeps_cdp_open(self):
        from platform_inspection import SensitiveLogRedactor

        page = SimpleNamespace(
            url="https://scm.superboss.cc/supplier/prod/center",
            set_default_timeout=Mock(),
            screenshot=AsyncMock(),
        )
        context = SimpleNamespace(pages=[page], close=AsyncMock(), service_workers=[])
        page.context = context
        remote_browser = SimpleNamespace(contexts=[context], close=AsyncMock())
        connect_over_cdp = AsyncMock(return_value=remote_browser)
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp)
        )
        manager = _AsyncPlaywrightManager(playwright)
        args = SimpleNamespace(
            platform="tmall",
            inspect_only=True,
            cdp_url="http://127.0.0.1:9222",
            timeout=5,
            login_timeout=5,
            headless=False,
            user_data_dir="/tmp/profile",
            auth_state="/tmp/auth.json",
            upload_timeout=5,
            sync_erp=False,
            save=False,
            save_only=False,
            publish_shop=[],
        )
        product = kuaimai_erp.ProductData(
            excel_path=Path("/input/产品信息.xlsx"),
            product_dir=Path("/input"),
            title="PRIVATE TITLE",
            style_code="STYLE-INSPECT",
            base_price="586",
            main_images=[],
            main_images_34=[],
            detail_images=[],
            sku_images=[],
            category_hints=("男装",),
        )
        logger = Mock()
        drawer = object()
        run_inspection = AsyncMock()
        zero_async_names = (
            "fill_input",
            "sync_image_group",
            "replace_sku_images",
            "set_base_price",
            "collect_visible_errors",
            "click_save_and_confirm",
            "publish_to_selected_douyin_shops",
            "recognize_product_recommendations",
        )

        patches = [patch.object(kuaimai_erp, name, new=AsyncMock()) for name in zero_async_names[:-1]]
        patches.append(
            patch.object(kuaimai_erp, "recognize_product_recommendations", new=Mock())
        )
        started = [item.start() for item in patches]
        try:
            with patch(
                "playwright.async_api.async_playwright", return_value=manager
            ), patch.object(
                kuaimai_erp,
                "try_reuse_verified_scm_session",
                new=AsyncMock(return_value=page),
            ), patch.object(
                kuaimai_erp,
                "api_find_product",
                new=AsyncMock(return_value={"baseItemId": "BASE-1"}),
            ), patch.object(
                kuaimai_erp,
                "open_product_editor",
                new=AsyncMock(return_value=drawer),
            ), patch.object(
                kuaimai_erp,
                "form_item",
                new=AsyncMock(return_value=_StyleItem()),
            ), patch.object(
                kuaimai_erp,
                "run_platform_inspection",
                run_inspection,
            ), patch.object(
                kuaimai_erp, "DouyinListing"
            ) as douyin_listing, patch.object(
                kuaimai_erp, "TaobaoListing"
            ) as taobao_listing:
                await kuaimai_erp.run_browser_automation(
                    args,
                    product,
                    Path("/tmp/inspect-artifacts"),
                    logger,
                    redactor=SensitiveLogRedactor((product.style_code, product.title)),
                )

            run_inspection.assert_awaited_once()
            for mock in started:
                mock.assert_not_called()
            douyin_listing.assert_not_called()
            taobao_listing.assert_not_called()
            context.close.assert_not_awaited()
            remote_browser.close.assert_not_awaited()
        finally:
            for item in reversed(patches):
                item.stop()


if __name__ == "__main__":
    unittest.main()
