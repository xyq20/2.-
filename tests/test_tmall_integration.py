import asyncio
from contextlib import ExitStack, contextmanager
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import kuaimai_erp
from tmall_form_listing import TmallFormListingError, TmallProductWriteRequired
from tmall_size_sources import TmallSizeReviewRequired


LOGGER = logging.getLogger("tmall-integration-tests")


class _ValueInput:
    def __init__(self, value):
        self.value = value

    @property
    def first(self):
        return self

    async def input_value(self):
        return self.value


class _FormItem:
    def __init__(self, value=""):
        self.input = _ValueInput(value)

    def locator(self, _selector):
        return self.input


class _AsyncPlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, _error_type, _error, _traceback):
        return False


def _product():
    return SimpleNamespace(
        excel_path=Path("/input/产品信息.xlsx"),
        product_dir=Path("/input"),
        title="PRIVATE TITLE",
        style_code="STYLE-TMALL",
        base_price="586",
        main_images=[Path("/input/1：1主图/1.jpg")],
        main_images_34=[Path("/input/3：4主图/1.jpg")],
        detail_images=[Path("/input/详情页图/1.jpg")],
        sku_images=[Path("/input/SKU图/S.jpg")],
        douyin_fields=None,
        douyin_assets=None,
        taobao_fields=None,
        tmall_fields=SimpleNamespace(
            fields={
                "风格": "休闲",
                "吊牌价/价格/基本售价": "586",
                "库存": "20",
                "货号": "STYLE-TMALL",
                "尺码": "S/M",
            }
        ),
        tmall_assets=SimpleNamespace(
            vertical_image=Path("/input/2：3图/1.jfif"),
            transparent_image=Path("/input/透明素材图/1.png"),
            parameter_image=Path("/input/尺码信息表/1.jpg"),
        ),
        tmall_size_rows=(
            {
                "尺码": "M",
                "身高(cm)": "165",
                "体重(kg)": "60",
                "腰围(cm)": "84",
            },
        ),
        category_hints=("男装", "休闲裤"),
    )


def _listing(require_full_form_error=None):
    listing = SimpleNamespace(
        open=AsyncMock(),
        apply_recommended_category=AsyncMock(return_value="男装 > 休闲裤"),
        fill_product_identity=AsyncMock(return_value={"style_code": "STYLE-TMALL"}),
        require_full_form=AsyncMock(),
        inspect_initial_product_images=AsyncMock(
            return_value={"count": 1, "expected_count": 1, "decoded": True}
        ),
        wait_for_initial_product_images=AsyncMock(return_value={"count": 5}),
        sync_initial_product_images=AsyncMock(return_value={"count": 5}),
        publish_product_information=AsyncMock(return_value={"clicked": True}),
        fill_attributes=AsyncMock(return_value={"attributes": {}}),
        normalize_synced_specifications=AsyncMock(return_value={"changed": 0}),
        fill_sku_batch=AsyncMock(return_value={"batch_clicked": True}),
        fill_size_chart=AsyncMock(return_value={"row_count": 1}),
        clean_size_chart_integer_displays=AsyncMock(),
        fill_sales_and_logistics=AsyncMock(return_value={}),
        sync_attribute_images=AsyncMock(return_value={}),
        sync_inherited_main_images=AsyncMock(return_value={}),
        sync_required_images=AsyncMock(return_value={}),
        fill_after_sales=AsyncMock(return_value={}),
        fill_new_product_declaration=AsyncMock(return_value=None),
        validate_remaining_required_fields=AsyncMock(
            return_value={"valid": True, "missing": ()}
        ),
    )
    listing.open.return_value = listing
    if require_full_form_error is not None:
        listing.require_full_form.side_effect = require_full_form_error
    return listing


class TmallCliGateIntegrationTests(unittest.TestCase):
    def _assert_rejected_before_inputs(self, *arguments):
        setup_logging = Mock(return_value=Mock())
        resolve_excel_path = Mock(return_value=Path("/input/产品信息.xlsx"))
        read_product_data = Mock(return_value=_product())
        browser_run = Mock(return_value=object())
        asyncio_run = Mock()
        with patch.object(kuaimai_erp, "setup_logging", setup_logging), patch.object(
            kuaimai_erp, "resolve_excel_path", resolve_excel_path
        ), patch.object(
            kuaimai_erp, "read_product_data", read_product_data
        ), patch.object(
            kuaimai_erp, "product_summary", return_value={}
        ), patch.object(
            kuaimai_erp, "run_browser_automation", browser_run
        ), patch.object(
            kuaimai_erp.asyncio, "run", asyncio_run
        ), patch.object(
            Path, "write_text", autospec=True
        ), patch(
            "sys.argv", ["kuaimai_erp", *arguments]
        ):
            with self.assertRaises(SystemExit):
                kuaimai_erp.main()

        setup_logging.assert_not_called()
        resolve_excel_path.assert_not_called()
        read_product_data.assert_not_called()
        browser_run.assert_not_called()
        asyncio_run.assert_not_called()

    def test_tmall_formal_save_and_publish_is_allowed_without_once_authorization(self):
        args = kuaimai_erp.build_parser().parse_args(["--platform", "tmall"])

        selected = kuaimai_erp.validate_execution_mode(args)

        self.assertEqual(tuple(spec.cli_name for spec in selected), ("tmall",))
        self.assertTrue(args.save)
        self.assertFalse(args.save_only)

    def test_tmall_formal_save_only_is_allowed_without_once_authorization(self):
        args = kuaimai_erp.build_parser().parse_args(
            ["--platform", "tmall", "--save-only"]
        )

        selected = kuaimai_erp.validate_execution_mode(args)

        self.assertEqual(tuple(spec.cli_name for spec in selected), ("tmall",))
        self.assertTrue(args.save)
        self.assertTrue(args.save_only)

    def test_tmall_cannot_reuse_taobao_once_authorization(self):
        for authorization in (
            "--allow-taobao-save-once",
            "--allow-taobao-publish-once",
        ):
            with self.subTest(authorization=authorization):
                self._assert_rejected_before_inputs(
                    "--platform",
                    "tmall",
                    "--no-save",
                    authorization,
                )

    def test_preview_report_summary_does_not_persist_product_values_or_codes(self):
        report = kuaimai_erp.tmall_preview_summary(
            {
                "status": "ready_for_manual_review",
                "category": "男装 > 休闲裤",
                "product_identity": {
                    "values": {"货号": "PRIVATE-CODE", "品牌": "PRIVATE-BRAND"}
                },
                "attributes": {"attributes": {"风格": ("休闲",)}},
                "sku_batch": {
                    "row_count": 2,
                    "values": {"货号": "PRIVATE-CODE", "价格": "586"},
                    "platform_codes": ("PRIVATE-PLATFORM-S",),
                    "platform_codes_preserved": True,
                },
                "size_chart": {"row_count": 2, "rows": {"S": {"腰围": "80"}}},
                "images": {"商品竖图": "uploaded"},
                "existing_product_images_before": {
                    "count": 1,
                    "expected_count": 5,
                    "slot_count": 5,
                    "decoded": False,
                    "sources": ("PRIVATE-IMAGE-URL",),
                },
                "existing_product_images_ready": {
                    "count": 5,
                    "expected_count": 5,
                    "decoded": True,
                },
                "saved": False,
                "published": False,
            }
        )

        rendered = str(report)
        self.assertNotIn("PRIVATE-CODE", rendered)
        self.assertNotIn("PRIVATE-BRAND", rendered)
        self.assertNotIn("PRIVATE-PLATFORM-S", rendered)
        self.assertNotIn("PRIVATE-IMAGE-URL", rendered)
        self.assertNotIn("586", rendered)
        self.assertEqual(report["product_identity_fields"], ("货号", "品牌"))
        self.assertEqual(report["sku_batch"]["row_count"], 2)
        self.assertEqual(report["existing_product_images_before"]["count"], 1)
        self.assertEqual(report["existing_product_images_ready"]["count"], 5)


class TmallBrowserPreviewIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _args(self):
        args = kuaimai_erp.build_parser().parse_args(
            [
                "--platform",
                "tmall",
                "--no-save",
                "--cdp-url",
                "http://127.0.0.1:9222",
            ]
        )
        selected = kuaimai_erp.validate_execution_mode(args)
        self.assertEqual(tuple(spec.cli_name for spec in selected), ("tmall",))
        return args

    def _publish_args(self):
        args = kuaimai_erp.build_parser().parse_args(
            [
                "--platform",
                "tmall",
                "--cdp-url",
                "http://127.0.0.1:9222",
            ]
        )
        selected = kuaimai_erp.validate_execution_mode(args)
        self.assertEqual(tuple(spec.cli_name for spec in selected), ("tmall",))
        return args

    @contextmanager
    def _patched_flow(self, listing):
        page = SimpleNamespace(
            url="https://scm.superboss.cc/supplier/prod/center",
            set_default_timeout=Mock(),
            screenshot=AsyncMock(),
            reload=AsyncMock(),
        )
        context = SimpleNamespace(pages=[page], close=AsyncMock(), service_workers=[])
        page.context = context
        remote_browser = SimpleNamespace(contexts=[context], close=AsyncMock())
        connect_over_cdp = AsyncMock(return_value=remote_browser)
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=connect_over_cdp)
        )
        manager = _AsyncPlaywrightManager(playwright)

        controls = SimpleNamespace(
            listing_factory=Mock(return_value=listing),
            page=page,
            open_product_editor=AsyncMock(return_value=object()),
            wait_for_base_form_ready_after_save=AsyncMock(),
            form_item=AsyncMock(
                side_effect=lambda _drawer, label, **_kwargs: _FormItem(
                    "STYLE-TMALL"
                    if label == "款式编码"
                    else "PRIVATE TITLE"
                    if label == "商品名称"
                    else ""
                )
            ),
            fill_input=AsyncMock(return_value=_ValueInput("PRIVATE TITLE")),
            sync_image_group=AsyncMock(return_value="unchanged"),
            replace_sku_images=AsyncMock(return_value=0),
            set_base_price=AsyncMock(return_value=_ValueInput("586")),
            collect_visible_errors=AsyncMock(return_value=[]),
            click_save_and_confirm=AsyncMock(
                return_value={"result": 1, "confirmed_by": "toast"}
            ),
            publish_to_selected_douyin_shops=AsyncMock(),
            prepare_taobao_publish_dialog=AsyncMock(),
            submit_taobao_publish_dialog=AsyncMock(),
            dismiss_publish_progress_dialog=AsyncMock(
                return_value={"found": False, "dismissed": False, "action": None}
            ),
            safe_screenshot=AsyncMock(),
            read_tmall_assets=Mock(return_value=_product().tmall_assets),
            resolve_tmall_size_sources=Mock(
                return_value=SimpleNamespace(
                    category_kind="pants",
                    sizes=("S", "M"),
                    headers=("尺码", "身高(cm)"),
                    rows=_product().tmall_size_rows,
                    evidence=("excel:尺码",),
                )
            ),
        )

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "playwright.async_api.async_playwright",
                    return_value=manager,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "try_reuse_verified_scm_session",
                    new=AsyncMock(return_value=page),
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "api_find_product",
                    new=AsyncMock(return_value={"baseItemId": "BASE-TMALL"}),
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "open_product_editor",
                    new=controls.open_product_editor,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "wait_for_base_form_ready_after_save",
                    new=controls.wait_for_base_form_ready_after_save,
                )
            )
            stack.enter_context(
                patch.object(kuaimai_erp, "form_item", new=controls.form_item)
            )
            stack.enter_context(
                patch.object(kuaimai_erp, "fill_input", new=controls.fill_input)
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "sync_image_group",
                    new=controls.sync_image_group,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "replace_sku_images",
                    new=controls.replace_sku_images,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "set_base_price",
                    new=controls.set_base_price,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "collect_visible_errors",
                    new=controls.collect_visible_errors,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "click_save_and_confirm",
                    new=controls.click_save_and_confirm,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "publish_to_selected_douyin_shops",
                    new=controls.publish_to_selected_douyin_shops,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "prepare_taobao_publish_dialog",
                    new=controls.prepare_taobao_publish_dialog,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "submit_taobao_publish_dialog",
                    new=controls.submit_taobao_publish_dialog,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "dismiss_publish_progress_dialog",
                    new=controls.dismiss_publish_progress_dialog,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "safe_screenshot",
                    new=controls.safe_screenshot,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "read_tmall_assets",
                    new=controls.read_tmall_assets,
                    create=True,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "resolve_tmall_size_sources",
                    new=controls.resolve_tmall_size_sources,
                    create=True,
                )
            )
            stack.enter_context(
                patch.object(
                    kuaimai_erp,
                    "TmallFormListing",
                    new=controls.listing_factory,
                    create=True,
                )
            )
            yield controls

    async def test_tmall_no_save_skips_base_and_keeps_tmall_listing_unsaved(self):
        listing = _listing()
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(
            listing
        ) as controls:
            await kuaimai_erp.run_browser_automation(
                self._args(),
                _product(),
                Path(directory),
                LOGGER,
            )
            self.assertFalse((Path(directory) / "base-save-result.json").exists())

        controls.fill_input.assert_not_awaited()
        self.assertEqual(controls.sync_image_group.await_count, 0)
        controls.replace_sku_images.assert_not_awaited()
        controls.set_base_price.assert_not_awaited()
        controls.listing_factory.assert_called_once()
        listing.open.assert_awaited_once()
        listing.sync_inherited_main_images.assert_awaited_once_with(
            _product().main_images,
            _product().main_images_34,
            timeout_seconds=self._args().upload_timeout,
            uploader=controls.sync_image_group,
        )
        listing.clean_size_chart_integer_displays.assert_awaited_once()
        listing.validate_remaining_required_fields.assert_awaited_once()
        controls.click_save_and_confirm.assert_not_awaited()
        controls.page.reload.assert_not_awaited()
        controls.open_product_editor.assert_awaited_once()
        controls.wait_for_base_form_ready_after_save.assert_not_awaited()
        controls.publish_to_selected_douyin_shops.assert_not_awaited()
        controls.prepare_taobao_publish_dialog.assert_not_awaited()
        controls.submit_taobao_publish_dialog.assert_not_awaited()

    async def test_shared_flow_reuses_current_editor_without_reload_or_reopen(self):
        listing = _listing()
        shared_drawer = SimpleNamespace(wait_for=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(
            listing
        ) as controls:
            shared_session = {
                "playwright": object(),
                "context": controls.page.context,
                "page": controls.page,
                "drawer": shared_drawer,
                "record": {"baseItemId": "BASE-TMALL"},
                "style_code": "STYLE-TMALL",
                "owns_context": False,
            }
            await kuaimai_erp.run_browser_automation(
                self._args(),
                _product(),
                Path(directory),
                LOGGER,
                shared_session=shared_session,
            )

        shared_drawer.wait_for.assert_awaited_once()
        controls.open_product_editor.assert_not_awaited()
        controls.page.reload.assert_not_awaited()
        controls.wait_for_base_form_ready_after_save.assert_not_awaited()
        controls.form_item.assert_not_awaited()
        listing.open.assert_awaited_once()

    async def test_tmall_formal_save_and_publish_selects_only_target_shop(self):
        listing = _listing()
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(
            listing
        ) as controls:
            controls.click_save_and_confirm.return_value = {
                "result": 1,
                "confirmed_by": "publish_dialog_open",
            }
            dialog = object()
            selection = {
                "platform": "天猫",
                "selected_shops": ["NEIGBORL批判家专卖店"],
            }
            controls.prepare_taobao_publish_dialog.return_value = (
                dialog,
                selection,
            )
            controls.submit_taobao_publish_dialog.return_value = {
                "result": 1,
                "confirmed_by": "toast",
                "submitted": True,
                "submitted_shops": ["NEIGBORL批判家专卖店"],
            }

            await kuaimai_erp.run_browser_automation(
                self._publish_args(),
                _product(),
                Path(directory),
                LOGGER,
            )

            self.assertEqual(controls.click_save_and_confirm.await_count, 1)
            self.assertEqual(
                controls.click_save_and_confirm.await_args.kwargs["button_text"],
                "保存并铺货到平台",
            )
            controls.prepare_taobao_publish_dialog.assert_awaited_once_with(
                controls.prepare_taobao_publish_dialog.await_args.args[0],
                ("NEIGBORL批判家专卖店",),
                controls.prepare_taobao_publish_dialog.await_args.args[2],
                LOGGER,
                platform_name="天猫",
            )
            controls.submit_taobao_publish_dialog.assert_awaited_once_with(
                controls.submit_taobao_publish_dialog.await_args.args[0],
                dialog,
                selection,
                controls.submit_taobao_publish_dialog.await_args.args[3],
                LOGGER,
                platform_name="天猫",
            )
            self.assertEqual(
                controls.dismiss_publish_progress_dialog.await_count,
                2,
            )
            self.assertEqual(
                controls.dismiss_publish_progress_dialog.await_args_list[1].kwargs[
                    "appearance_timeout_seconds"
                ],
                3.0,
            )
            controls.publish_to_selected_douyin_shops.assert_not_awaited()
            self.assertTrue((Path(directory) / "publish-result.json").is_file())

    async def test_first_tmall_form_waits_replaces_images_then_publishes_before_filling(self):
        listing = _listing(
            TmallProductWriteRequired("天猫后续表单需要独立写入")
        )
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(
            listing
        ) as controls:
            await kuaimai_erp.run_browser_automation(
                self._args(),
                _product(),
                Path(directory),
                LOGGER,
            )

        controls.click_save_and_confirm.assert_not_awaited()
        controls.publish_to_selected_douyin_shops.assert_not_awaited()
        controls.prepare_taobao_publish_dialog.assert_not_awaited()
        controls.submit_taobao_publish_dialog.assert_not_awaited()
        listing.require_full_form.assert_awaited_once()
        listing.wait_for_initial_product_images.assert_awaited_once_with(
            expected_count=len(_product().main_images),
            timeout_seconds=self._args().upload_timeout
        )
        listing.sync_initial_product_images.assert_awaited_once_with(
            _product().main_images,
            timeout_seconds=self._args().upload_timeout,
            uploader=controls.sync_image_group,
        )
        listing.publish_product_information.assert_awaited_once_with(
            timeout_seconds=self._args().timeout,
            expected_image_count=len(_product().main_images),
        )
        controls.resolve_tmall_size_sources.assert_called_once()

    async def test_existing_tmall_form_does_not_wait_for_first_time_sync(self):
        listing = _listing()
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(
            listing
        ) as controls:
            await kuaimai_erp.run_browser_automation(
                self._args(),
                _product(),
                Path(directory),
                LOGGER,
            )

        listing.wait_for_initial_product_images.assert_not_awaited()
        listing.sync_initial_product_images.assert_not_awaited()
        listing.require_full_form.assert_awaited_once()
        listing.publish_product_information.assert_not_awaited()

    async def test_existing_tmall_form_repairs_incomplete_top_images(self):
        listing = _listing()
        listing.inspect_initial_product_images.return_value = {
            "count": 0,
            "expected_count": 1,
            "decoded": False,
        }
        listing.wait_for_initial_product_images.side_effect = [
            TmallFormListingError("顶部图片仍为 0/1"),
            {"count": 1, "expected_count": 1, "decoded": True},
        ]
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(
            listing
        ) as controls:
            await kuaimai_erp.run_browser_automation(
                self._args(),
                _product(),
                Path(directory),
                LOGGER,
            )

        listing.sync_initial_product_images.assert_awaited_once_with(
            _product().main_images,
            timeout_seconds=self._args().upload_timeout,
            uploader=controls.sync_image_group,
        )
        self.assertEqual(listing.wait_for_initial_product_images.await_count, 2)
        self.assertFalse(
            listing.wait_for_initial_product_images.await_args_list[0].kwargs[
                "allow_existing_product_shortcut"
            ]
        )
        self.assertFalse(
            listing.wait_for_initial_product_images.await_args_list[1].kwargs[
                "allow_existing_product_shortcut"
            ]
        )
        listing.publish_product_information.assert_not_awaited()

    async def test_initial_image_timeout_records_stage_and_never_publishes(self):
        listing = _listing(TmallProductWriteRequired("首次待发布"))
        listing.wait_for_initial_product_images.side_effect = TmallFormListingError(
            "图片同步未完成：1/5"
        )
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(listing):
            with self.assertRaisesRegex(TmallFormListingError, "1/5"):
                await kuaimai_erp.run_browser_automation(
                    self._args(), _product(), Path(directory), LOGGER
                )
            report = (Path(directory) / "tmall-review-required.json").read_text()
            self.assertIn("tmall_initial_product_images_ready_review_required", report)
            self.assertIn('"form_mode": "initial"', report)
        listing.sync_initial_product_images.assert_not_awaited()
        listing.publish_product_information.assert_not_awaited()
        listing.fill_attributes.assert_not_awaited()

    async def test_async_product_match_repairs_images_without_publishing(self):
        listing = _listing(TmallProductWriteRequired("首次待发布"))
        listing.wait_for_initial_product_images.side_effect = [
            {"count": 1, "matched_existing_product": True, "decoded": False},
            {"count": 5, "decoded": True},
        ]
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(listing):
            await kuaimai_erp.run_browser_automation(
                self._args(), _product(), Path(directory), LOGGER
            )
        listing.sync_initial_product_images.assert_awaited_once()
        listing.publish_product_information.assert_not_awaited()
        listing.fill_attributes.assert_awaited_once()

    async def test_missing_verified_size_source_stops_after_gate_before_form_writes(self):
        listing = _listing()
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(
            listing
        ) as controls:
            controls.resolve_tmall_size_sources.side_effect = TmallSizeReviewRequired(
                "尺码表缺少来源",
                reason_code="missing_height_weight_image",
            )
            with self.assertRaises(TmallSizeReviewRequired):
                await kuaimai_erp.run_browser_automation(
                    self._args(),
                    _product(),
                    Path(directory),
                    LOGGER,
                )

            report = Path(directory) / "tmall-review-required.json"
            self.assertTrue(report.is_file())
            payload = report.read_text(encoding="utf-8")
            self.assertIn("missing_height_weight_image", payload)
            self.assertNotIn("尺码表缺少来源", payload)

        listing.open.assert_awaited_once()
        listing.apply_recommended_category.assert_awaited_once()
        listing.fill_product_identity.assert_awaited_once()
        listing.require_full_form.assert_awaited_once()
        listing.fill_attributes.assert_not_awaited()
        listing.normalize_synced_specifications.assert_not_awaited()
        listing.fill_sku_batch.assert_not_awaited()
        listing.fill_size_chart.assert_not_awaited()
        controls.click_save_and_confirm.assert_not_awaited()

    async def test_remaining_required_field_stops_preview_before_ready_status(self):
        listing = _listing()
        listing.validate_remaining_required_fields.side_effect = TmallFormListingError(
            "手机端详情描述未填"
        )
        with tempfile.TemporaryDirectory() as directory, self._patched_flow(
            listing
        ) as controls:
            with self.assertRaisesRegex(TmallFormListingError, "手机端详情描述"):
                await kuaimai_erp.run_browser_automation(
                    self._args(),
                    _product(),
                    Path(directory),
                    LOGGER,
                )

            self.assertFalse((Path(directory) / "tmall-before-save.json").exists())
            report = Path(directory) / "tmall-review-required.json"
            self.assertTrue(report.is_file())
            self.assertIn(
                "tmall_required_fields_review_required",
                report.read_text(encoding="utf-8"),
            )
        controls.click_save_and_confirm.assert_not_awaited()


class TmallMainErrorIntegrationTests(unittest.TestCase):
    def test_product_write_required_is_reported_as_safe_business_error(self):
        logger = Mock()
        save = AsyncMock()
        publish = AsyncMock()
        browser_run = AsyncMock(
            side_effect=TmallProductWriteRequired(
                "天猫后续表单需要独立写入"
            )
        )
        with patch.object(
            kuaimai_erp, "setup_logging", return_value=logger
        ), patch.object(
            kuaimai_erp,
            "resolve_excel_path",
            return_value=Path("/input/产品信息.xlsx"),
        ), patch.object(
            kuaimai_erp, "read_product_data", return_value=_product()
        ), patch.object(
            kuaimai_erp, "product_summary", return_value={}
        ), patch.object(
            kuaimai_erp, "run_browser_automation", new=browser_run
        ), patch.object(
            kuaimai_erp, "click_save_and_confirm", new=save
        ), patch.object(
            kuaimai_erp, "publish_to_selected_douyin_shops", new=publish
        ), patch.object(
            Path, "write_text", autospec=True
        ), patch(
            "sys.argv", ["kuaimai_erp", "--platform", "tmall", "--no-save"]
        ):
            result = kuaimai_erp.main()

        self.assertEqual(result, 2)
        logger.error.assert_called_once()
        logger.exception.assert_not_called()
        save.assert_not_awaited()
        publish.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
