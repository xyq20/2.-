import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook
import kuaimai_erp as erp

LOGGER = logging.getLogger('new-product-tests')


class SeedTests(unittest.TestCase):
    def test_seed_needs_only_style_and_one_main_image_and_ignores_full_listing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workbook = Workbook()
            workbook.active.append(['货号/商家外部编码', 'TEST-7'])
            workbook.active.append(['商品名称', '后续完善的标题'])
            workbook.active.append(['基本售价', 888])
            workbook.save(root / '产品信息.xlsx')
            images = root / '1：1主图'
            images.mkdir()
            (images / '2.png').write_bytes(b'test')
            (images / '1.png').write_bytes(b'test')
            product = erp.read_new_product_seed(root / '产品信息.xlsx')
            self.assertEqual(product.style_code, 'TEST-7')
            self.assertEqual(product.title, '1')
            self.assertEqual(product.base_price, '0')
            self.assertEqual([p.name for p in product.main_images], ['1.png'])
            self.assertEqual(product.sku_images, [])

    def test_create_cannot_run_platform_or_all_workflow(self):
        for platform in ('all', 'taobao', 'tmall', 'douyin'):
            args = erp.build_parser().parse_args(['--create-product', '--platform', platform, '--no-save'])
            with self.assertRaises(SystemExit):
                erp.validate_execution_mode(args)
        args = erp.build_parser().parse_args(['--create-product', '--platform', 'base', '--save-only'])
        erp.validate_execution_mode(args)


class NewProductTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_lookup_distinguishes_absent_from_failure(self):
        payloads = [
            {'ok': False, 'status': 500},
            {'ok': True, 'data': {'result': 0}},
            {'ok': True, 'data': {'result': 1, 'data': {}}},
            {'ok': True, 'data': {'result': 1, 'data': {'records': None, 'total': 1}}},
            {'ok': True, 'data': {'result': 1, 'data': {'records': 'invalid', 'total': 0}}},
            {'ok': True, 'data': {'result': 1, 'data': {'records': [{}], 'total': 1}}},
            {'ok': True, 'data': {'result': 1, 'data': {'records': [{'outerId': 'OTHER'}], 'total': 1}}},
            {'ok': True, 'data': {'result': 1, 'data': {'records': [{'outerId': 'TEST'}] * 2, 'total': 2}}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                page = SimpleNamespace(evaluate=AsyncMock(return_value=payload))
                with self.assertRaises(erp.AutomationError):
                    await erp.api_find_product(page, 'TEST', LOGGER, strict=True)
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=RuntimeError('offline')))
        with self.assertRaises(erp.AutomationError):
            await erp.api_find_product(page, 'TEST', LOGGER, strict=True)
        for records, total, expected in (
            (None, 0, None),
            ([], 0, None),
            ([{'outerId': 'TEST', 'baseItemId': 7}], 1, {'outerId': 'TEST', 'baseItemId': 7}),
        ):
            page = SimpleNamespace(evaluate=AsyncMock(return_value={'ok': True, 'data': {'result': 1, 'data': {'records': records, 'total': total}}}))
            self.assertEqual(await erp.api_find_product(page, 'TEST', LOGGER, strict=True), expected)

    async def test_existing_product_is_not_opened_or_saved(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(erp, 'api_find_product', AsyncMock(return_value={'baseItemId': 7})), patch.object(erp, 'open_new_product_drawer', AsyncMock()) as opening:
            await erp.run_new_product(None, None, SimpleNamespace(style_code='TEST'), Path(directory), LOGGER)
            opening.assert_not_awaited()
            report = json.loads((Path(directory) / 'create-product-result.json').read_text())
            self.assertEqual(report['status'], 'already_exists')
            self.assertFalse(report['saved'])

    async def test_preview_and_unknown_submission_never_repeat_save(self):
        for save in (False, True):
            with tempfile.TemporaryDirectory() as directory, patch.object(erp, 'api_find_product', AsyncMock(return_value=None)), patch.object(erp, 'open_new_product_drawer', AsyncMock()), patch.object(erp, 'fill_new_product_form', AsyncMock(return_value={})), patch.object(erp, 'validate_new_product_form', AsyncMock(return_value={})), patch.object(erp, 'safe_screenshot', AsyncMock()), patch.object(erp, 'click_save_and_confirm', AsyncMock(side_effect=erp.AutomationError('timeout'))) as saving:
                args = SimpleNamespace(save=save, timeout=1, sync_erp=False)
                if save:
                    with self.assertRaises(erp.AutomationError):
                        await erp.run_new_product(None, args, SimpleNamespace(style_code='TEST'), Path(directory), LOGGER)
                    self.assertEqual(saving.await_count, 1)
                else:
                    await erp.run_new_product(None, args, SimpleNamespace(style_code='TEST'), Path(directory), LOGGER)
                    saving.assert_not_awaited()
                report = json.loads((Path(directory) / 'create-product-result.json').read_text())
                self.assertEqual(report['status'], 'verification_required' if save else 'preview')
                self.assertFalse(report['saved'])

    async def test_race_between_fill_and_save_stops_submission(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(erp, 'api_find_product', AsyncMock(side_effect=[None, {'baseItemId': 7}])), patch.object(erp, 'open_new_product_drawer', AsyncMock()), patch.object(erp, 'fill_new_product_form', AsyncMock(return_value={})), patch.object(erp, 'validate_new_product_form', AsyncMock(return_value={})), patch.object(erp, 'safe_screenshot', AsyncMock()), patch.object(erp, 'click_save_and_confirm', AsyncMock()) as saving:
            with self.assertRaises(erp.AutomationError):
                await erp.run_new_product(None, SimpleNamespace(save=True, timeout=1), SimpleNamespace(style_code='TEST'), Path(directory), LOGGER)
            saving.assert_not_awaited()

    async def test_success_requires_reopened_form_validation(self):
        for persisted_ok in (True, False):
            with tempfile.TemporaryDirectory() as directory, patch.object(erp, 'api_find_product', AsyncMock(side_effect=[None, None, {'baseItemId': 7}])), patch.object(erp, 'open_new_product_drawer', AsyncMock()), patch.object(erp, 'fill_new_product_form', AsyncMock(return_value={})), patch.object(erp, 'validate_new_product_form', AsyncMock(side_effect=[{}, {} if persisted_ok else erp.AutomationError('readback mismatch')])), patch.object(erp, 'safe_screenshot', AsyncMock()), patch.object(erp, 'click_save_and_confirm', AsyncMock(return_value={'confirmed_by': 'creation_dialog'})) as saving, patch.object(erp, 'open_product_editor', AsyncMock()) as reopening:
                page = SimpleNamespace(reload=AsyncMock())
                args = SimpleNamespace(save=True, timeout=1, sync_erp=False)
                if persisted_ok:
                    await erp.run_new_product(page, args, SimpleNamespace(style_code='TEST'), Path(directory), LOGGER)
                else:
                    with self.assertRaises(erp.AutomationError):
                        await erp.run_new_product(page, args, SimpleNamespace(style_code='TEST'), Path(directory), LOGGER)
                report = json.loads((Path(directory) / 'create-product-result.json').read_text())
                self.assertEqual(report['saved'], persisted_ok)
                self.assertEqual(report['status'], 'created' if persisted_ok else 'verification_required')
                self.assertEqual(saving.await_count, 1)
                self.assertEqual(reopening.await_count, 1)


if __name__ == '__main__':
    unittest.main()
