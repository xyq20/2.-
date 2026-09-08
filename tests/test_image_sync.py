import tempfile
import unittest
from pathlib import Path

import kuaimai_erp


FIXTURE = """
<meta charset="utf-8">
<style>
  .del-btn {{ display: none }}
  .file-img:hover .del-btn {{ display: block }}
</style>
<div id="scope">
  <div class="sc-upload" id="images">{images}</div>
  {control}
</div>
<script>
  window.deleteCount = 0;
  window.inputChangeCount = 0;
  window.uploadNames = [];
  function removeImage(button) {{
    window.deleteCount++;
    button.closest('.file-img').remove();
  }}
  function recordFiles(input) {{
    window.inputChangeCount++;
    window.uploadNames = Array.from(input.files).map(file => file.name);
    for (const file of input.files) {{
      const image = document.createElement('div');
      image.className = 'file-img';
      image.innerHTML = '<button class="del-btn" onclick="removeImage(this)">删除</button>';
      document.querySelector('#images').appendChild(image);
    }}
  }}
</script>
"""


def fixture_html(existing_count: int, include_control: bool = True, accept: str = "") -> str:
    images = "".join(
        '<div class="file-img"><button class="del-btn" onclick="removeImage(this)">删除</button></div>'
        for _ in range(existing_count)
    )
    control = (
        '<input id="upload" type="file" multiple accept="{0}" onchange="recordFiles(this)">'.format(accept)
        if include_control else ""
    )
    return FIXTURE.format(images=images, control=control)


class ImageSyncBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="chrome", headless=True)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def run_sync(self, existing_count, paths, include_control=True):
        page = await self.browser.new_page()
        await page.set_content(fixture_html(existing_count, include_control))
        result = await kuaimai_erp.sync_image_group(
            page,
            page.locator("#scope"),
            paths,
            "测试图片",
            timeout_seconds=2,
        )
        return page, result

    async def test_equal_count_skips_delete_and_file_input(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "image1.png", Path(directory) / "image2.png"]
            for path in paths:
                path.write_bytes(b"image")
            page, result = await self.run_sync(2, paths)
            self.assertEqual(result, "skipped")
            self.assertEqual(await page.evaluate("window.deleteCount"), 0)
            self.assertEqual(await page.evaluate("window.inputChangeCount"), 0)
            await page.close()

    async def test_force_replace_reuploads_equal_count_in_requested_order(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("third.png", "second.png", "first.png")]
            for path in paths:
                path.write_bytes(b"image")
            page = await self.browser.new_page()
            await page.set_content(fixture_html(3))

            result = await kuaimai_erp.sync_image_group(
                page,
                page.locator("#scope"),
                paths,
                "天猫商品图片",
                timeout_seconds=2,
                force_replace=True,
            )

            self.assertEqual(result, "replaced")
            self.assertEqual(await page.evaluate("window.deleteCount"), 3)
            self.assertEqual(await page.evaluate("window.inputChangeCount"), 1)
            self.assertEqual(
                await page.evaluate("window.uploadNames"), [path.name for path in paths]
            )
            await page.close()

    async def test_equal_count_replaces_one_blank_placeholder_image(self):
        import base64

        import cv2
        import numpy as np

        blank = np.full((24, 24, 3), (252, 251, 250), dtype=np.uint8)
        ok, encoded = cv2.imencode(".png", blank)
        self.assertTrue(ok)
        source = "data:image/png;base64," + base64.b64encode(
            encoded.tobytes()
        ).decode("ascii")
        page = await self.browser.new_page()
        await page.set_content(
            """
            <div id="scope"><div class="sc-upload" id="images">
              <div class="file-img"><img class="originImg" src="%s">
                <button class="del-btn" onclick="this.closest('.file-img').remove()">删除</button>
              </div>
              <input id="upload" type="file" multiple onchange="recordFiles(this)">
            </div></div>
            <script>
              window.deleteCount = 0;
              window.inputChangeCount = 0;
              function recordFiles(input) {
                window.inputChangeCount++;
                const image = document.createElement('div');
                image.className = 'file-img';
                document.querySelector('#images').appendChild(image);
              }
              document.addEventListener('click', event => {
                if (event.target.classList.contains('del-btn')) window.deleteCount++;
              });
            </script>
            """ % source
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "expected.png"
            path.write_bytes(b"expected-image")
            result = await kuaimai_erp.sync_image_group(
                page,
                page.locator("#scope"),
                (path,),
                "属性图片[军绿色]",
                timeout_seconds=2,
            )

        self.assertEqual(result, "replaced")
        self.assertEqual(await page.evaluate("window.deleteCount"), 1)
        self.assertEqual(await page.evaluate("window.inputChangeCount"), 1)
        await page.close()

    async def test_mismatch_deletes_all_and_uploads_paths_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("image10.png", "image2.png", "image3.png")]
            for path in paths:
                path.write_bytes(b"image")
            page, result = await self.run_sync(1, paths)
            self.assertEqual(result, "replaced")
            self.assertEqual(await page.locator(".file-img").count(), len(paths))
            self.assertEqual(await page.evaluate("window.deleteCount"), 1)
            self.assertEqual(await page.evaluate("window.inputChangeCount"), 1)
            self.assertEqual(await page.evaluate("window.uploadNames"), [path.name for path in paths])
            await page.close()

    async def test_zero_existing_images_uploads_all_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "image1.png"]
            paths[0].write_bytes(b"image")
            page, result = await self.run_sync(0, paths)
            self.assertEqual(result, "replaced")
            self.assertEqual(await page.locator(".file-img").count(), 1)
            self.assertEqual(await page.evaluate("window.deleteCount"), 0)
            self.assertEqual(await page.evaluate("window.inputChangeCount"), 1)
            await page.close()

    async def test_jfif_is_uploaded_with_tmall_accepted_jpg_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vertical.jfif"
            source.write_bytes(b"\xff\xd8\xfffixture")
            page = await self.browser.new_page()
            await page.set_content(
                fixture_html(0, accept=".jpg,.jpeg,.png")
            )

            result = await kuaimai_erp.sync_image_group(
                page,
                page.locator("#scope"),
                (source,),
                "商品竖图",
                timeout_seconds=2,
            )

            self.assertEqual(result, "replaced")
            self.assertEqual(await page.evaluate("window.uploadNames"), ["upload-1.jpg"])
            self.assertTrue(source.is_file())
            await page.close()

    async def test_empty_paths_are_rejected(self):
        page = await self.browser.new_page()
        await page.set_content(fixture_html(0))
        with self.assertRaisesRegex(kuaimai_erp.AutomationError, "没有可上传图片"):
            await kuaimai_erp.sync_image_group(page, page.locator("#scope"), [], "测试图片", 2)
        self.assertEqual(await page.evaluate("window.deleteCount"), 0)
        self.assertEqual(await page.evaluate("window.inputChangeCount"), 0)
        await page.close()

    async def test_missing_upload_control_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / "image1.png"]
            paths[0].write_bytes(b"image")
            page = await self.browser.new_page()
            await page.set_content(fixture_html(0, include_control=False))
            with self.assertRaisesRegex(kuaimai_erp.AutomationError, "找不到本地上传控件"):
                await kuaimai_erp.sync_image_group(page, page.locator("#scope"), paths, "测试图片", 2)
            await page.close()


if __name__ == "__main__":
    unittest.main()
