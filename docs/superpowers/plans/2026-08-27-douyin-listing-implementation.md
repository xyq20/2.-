# Douyin Listing Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing Kuaimai automation to read Douyin product data and local size-chart images, fill and validate the Douyin tab, then safely click “保存并铺货到平台”.

**Architecture:** Keep login, product lookup, and the base-data workflow in `kuaimai_erp.py`. Add focused modules for Excel/material parsing, local macOS Vision/OpenCV size recognition, and Douyin-page interactions. Every stage produces typed data and validation evidence; browser actions use same-origin APIs where discoverable and text/label-scoped DOM locators otherwise.

**Tech Stack:** Python 3.9+, Playwright async API, openpyxl, macOS Vision via Swift, OpenCV headless, NumPy, `unittest`, Playwright CLI for real-page discovery.

---

## File map

- Create `douyin_data.py`: typed Douyin input model, Excel alias extraction, material parsing, and asset discovery.
- Create `size_image_recognition.py`: Vision OCR bridge, size-table parser, height/weight chart parser, and safety validation.
- Create `scripts/vision_ocr.swift`: local Vision text recognition emitting normalized JSON boxes.
- Create `douyin_listing.py`: Douyin-tab API/DOM adapters, image policy, form filling, validation, and publishing.
- Modify `kuaimai_erp.py`: reuse generic upload helpers, invoke new data/OCR/browser stages, and preserve old CLI behavior.
- Modify `requirements.txt`: add deterministic image-processing dependencies.
- Modify `README.md`: document new folders, safe rehearsal, OCR requirements, and artifacts.
- Create `tests/test_douyin_data.py`: Excel aliases, material, assets, price/inventory, and freight tests.
- Create `tests/test_size_image_recognition.py`: sample image OCR/parser and validation tests.
- Create `tests/test_douyin_listing.py`: mocked DOM/API tests for fields, images, SKU values, freight, and publish behavior.
- Modify `tests/test_kuaimai_erp.py`: orchestration and backward-compatibility regression tests.

### Task 1: Parse and validate Douyin input data

**Files:**
- Create: `douyin_data.py`
- Create: `tests/test_douyin_data.py`
- Modify: `kuaimai_erp.py:53-198`

- [ ] **Step 1: Write failing tests for the input model and aliases**

```python
# tests/test_douyin_data.py
from pathlib import Path
from douyin_data import MaterialComponent, parse_douyin_fields


def test_parse_current_product_fields():
    fields = {
        "导购短标题": "重磅洗水宽松多口袋工装裤",
        "面料材质/水洗标/吊牌图/面料/面料俗称": "棉（100%）",
        "尺码": "S/M/L/XL/2XL",
        "价格/京东价/市场价/售卖价/售价": 586,
        "现货库存": 0,
        "预售库存": 100,
        "运费设置": "新疆，西藏，不包邮-T恤，裤子，装饰品/新疆西藏不包邮T恤裤子装饰品",
    }
    data = parse_douyin_fields(fields)
    assert data.short_title == "重磅洗水宽松多口袋工装裤"
    assert data.materials == (MaterialComponent("棉", 100),)
    assert data.sizes == ("S", "M", "L", "XL", "2XL")
    assert (data.price, data.spot_stock, data.presale_stock) == ("586", 0, 100)
    assert len(data.freight_aliases) == 2
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run: `./.venv/bin/python -m unittest -v tests/test_douyin_data.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'douyin_data'`.

- [ ] **Step 3: Implement typed field parsing**

```python
# douyin_data.py
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re
from typing import Dict, Mapping, Tuple


class DouyinDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterialComponent:
    name: str
    percentage: int


@dataclass(frozen=True)
class DouyinFields:
    short_title: str
    attributes: Mapping[str, str]
    materials: Tuple[MaterialComponent, ...]
    sizes: Tuple[str, ...]
    price: str
    spot_stock: int
    presale_stock: int
    freight_aliases: Tuple[str, ...]


def normalize_key(value: object) -> str:
    return re.sub(r"[\s：:]", "", str(value or "")).casefold()


def aliases(key: str) -> Tuple[str, ...]:
    return tuple(part.strip() for part in key.split("/") if part.strip())


def find_value(fields: Mapping[str, object], *wanted: str) -> object:
    wanted_keys = {normalize_key(item) for item in wanted}
    for key, value in fields.items():
        if any(normalize_key(part) in wanted_keys for part in aliases(key)):
            return value
    return None


def parse_materials(value: object) -> Tuple[MaterialComponent, ...]:
    text = str(value or "").strip().replace("（", "(").replace("）", ")")
    match = re.fullmatch(r"\s*([^/,(]+)\s*(?:[/,(]\s*(\d{1,3})\s*%?\)?)\s*", text)
    if not match:
        raise DouyinDataError(f"无法解析面料材质：{text!r}")
    percentage = int(match.group(2))
    if not 0 <= percentage <= 100:
        raise DouyinDataError(f"面料百分比超出 0-100：{percentage}")
    return (MaterialComponent(match.group(1).strip(), percentage),)


def parse_douyin_fields(fields: Mapping[str, object]) -> DouyinFields:
    short_title = str(find_value(fields, "导购短标题") or "").strip()
    sizes = tuple(part.strip() for part in str(find_value(fields, "尺码") or "").split("/") if part.strip())
    price = format(Decimal(str(find_value(fields, "价格", "京东价", "市场价", "售卖价", "售价"))), "f")
    freight = str(find_value(fields, "运费设置") or "")
    if not short_title or not sizes or not freight:
        raise DouyinDataError("抖音必需 Excel 字段不完整")
    reserved = {"导购短标题", "尺码", "价格", "京东价", "市场价", "售卖价", "售价", "现货库存", "预售库存", "运费设置"}
    attributes = {key: str(value).strip() for key, value in fields.items() if value is not None and not any(part in reserved for part in aliases(key))}
    return DouyinFields(
        short_title=short_title,
        attributes=attributes,
        materials=parse_materials(find_value(fields, "面料材质", "水洗标", "吊牌图", "面料", "面料俗称")),
        sizes=sizes,
        price=price,
        spot_stock=int(find_value(fields, "现货库存")),
        presale_stock=int(find_value(fields, "预售库存")),
        freight_aliases=tuple(part.strip() for part in freight.split("/") if part.strip()),
    )
```

- [ ] **Step 4: Add exact-one-image asset discovery tests and implementation**

Add tests using `tempfile.TemporaryDirectory()` for `水洗标图片`, `尺码信息表`, and `身高体重推荐表`; assert the latter two reject zero or two images. Add `DouyinAssets` and `read_douyin_assets(product_dir: Path)` that natural-sorts image files and enforces those counts.

- [ ] **Step 5: Refactor `kuaimai_erp.read_product_data` to expose the Excel field map**

Extract the current row loop into `read_excel_fields(rows) -> Dict[str, object]`, call `parse_douyin_fields(fields)` and `read_douyin_assets(product_dir)`, and add `douyin_fields`/`douyin_assets` to `ProductData`. Keep existing title, style code, base price, and image paths unchanged.

- [ ] **Step 6: Run focused and legacy tests**

Run: `./.venv/bin/python -m unittest -v tests/test_douyin_data.py tests/test_kuaimai_erp.py`

Expected: all tests PASS, including the existing eight regression tests.

- [ ] **Step 7: Commit**

```bash
git add douyin_data.py kuaimai_erp.py tests/test_douyin_data.py
git commit -m "feat: parse Douyin product inputs"
```

### Task 2: Add the local macOS Vision OCR bridge

**Files:**
- Create: `scripts/vision_ocr.swift`
- Create: `size_image_recognition.py`
- Create: `tests/test_size_image_recognition.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Write a failing OCR bridge test**

```python
# tests/test_size_image_recognition.py
from pathlib import Path
from size_image_recognition import vision_ocr

PRODUCT = Path("/Volumes/共享文件/谭/products/绿巨人+NGBL-10588")


def test_vision_ocr_reads_size_labels():
    tokens = vision_ocr(PRODUCT / "尺码信息表/1_09(1).jpg")
    text = " ".join(token.text.upper() for token in tokens)
    assert all(size in text for size in ("S", "M", "L", "XL", "2XL"))
    assert "WAISTLINE" in text and "HIPLINE" in text and "LENGTH" in text
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `./.venv/bin/python -m unittest -v tests/test_size_image_recognition.py`

Expected: FAIL because `size_image_recognition` does not exist.

- [ ] **Step 3: Implement the Swift Vision executable source**

`scripts/vision_ocr.swift` must accept one image path, use `VNRecognizeTextRequest` with `.accurate`, languages `zh-Hans` and `en-US`, and print a JSON array containing `text`, `confidence`, `x`, `y`, `width`, and `height`. Convert Vision's bottom-left coordinates to normalized top-left coordinates before encoding.

Core output type:

```swift
struct OCRToken: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}
```

- [ ] **Step 4: Implement the Python bridge and typed token**

```python
# size_image_recognition.py
from dataclasses import dataclass
import json, subprocess
from pathlib import Path
from typing import Tuple


class RecognitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRToken:
    text: str
    confidence: float
    x: float
    y: float
    width: float
    height: float


def vision_ocr(image_path: Path) -> Tuple[OCRToken, ...]:
    script = Path(__file__).with_name("scripts") / "vision_ocr.swift"
    result = subprocess.run(
        ["xcrun", "swift", str(script), str(image_path)],
        check=False, capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise RecognitionError(result.stderr.strip() or "macOS Vision OCR 执行失败")
    payload = json.loads(result.stdout)
    tokens = tuple(OCRToken(**item) for item in payload if item["confidence"] >= 0.35)
    if not tokens:
        raise RecognitionError(f"OCR 未识别到文字：{image_path}")
    return tokens
```

- [ ] **Step 5: Add image-processing dependencies**

Append to `requirements.txt`:

```text
numpy>=1.26,<3
opencv-python-headless>=4.10,<5
```

Run: `./.venv/bin/python -m pip install -r requirements.txt`

Expected: installation succeeds without replacing Playwright/openpyxl with incompatible versions.

- [ ] **Step 6: Run the OCR bridge test**

Run: `./.venv/bin/python -m unittest -v tests/test_size_image_recognition.py`

Expected: PASS and complete in under 60 seconds.

- [ ] **Step 7: Commit**

```bash
git add scripts/vision_ocr.swift size_image_recognition.py requirements.txt tests/test_size_image_recognition.py
git commit -m "feat: add local Vision OCR bridge"
```

### Task 3: Parse both size-chart formats with strict validation

**Files:**
- Modify: `size_image_recognition.py`
- Modify: `tests/test_size_image_recognition.py`

- [ ] **Step 1: Write failing sample-image regression tests**

```python
from size_image_recognition import recognize_recommendations


def test_current_product_size_images_are_parsed():
    result = recognize_recommendations(
        PRODUCT / "尺码信息表/1_09(1).jpg",
        PRODUCT / "身高体重推荐表/1.jpg",
        ("S", "M", "L", "XL", "2XL"),
    )
    assert [(row.size, row.height_min, row.height_max, row.weight_min, row.weight_max,
             row.waist, row.hip, row.length) for row in result] == [
        ("S", 155, 160, 50, 60, 80, 106, 104),
        ("M", 155, 170, 50, 70, 84, 110, 106),
        ("L", 155, 180, 50, 80, 88, 114, 108),
        ("XL", 155, 190, 50, 90, 92, 118, 110),
        ("2XL", 155, 200, 50, 100, 96, 122, 112),
    ]
```

- [ ] **Step 2: Run the focused test and verify it fails**

Expected: FAIL with missing `recognize_recommendations`.

- [ ] **Step 3: Implement size measurement table parsing**

Add `SkuRecommendation` with all eight numeric fields. Normalize OCR aliases (`WAISTLINE`/腰围, `HIPLINE`/臀围, `LENGTH`/裤长), cluster tokens into rows/columns by normalized center coordinates, and map numeric cells under size headers. Reject duplicate or absent cells.

- [ ] **Step 4: Implement height/weight chart parsing**

Load the image with OpenCV, locate the outer chart from long horizontal/vertical lines, map OCR numeric tokens to x/y axes, then identify nested grayscale region boundaries from vertical/horizontal intensity transitions. For each OCR size label, select the nearest enclosing right and lower boundary and map those positions to axis values. Use the minimum recognized height and weight as common lower bounds.

Required internal signature:

```python
def parse_height_weight_chart(
    image_path: Path,
    tokens: Tuple[OCRToken, ...],
    expected_sizes: Tuple[str, ...],
) -> dict[str, tuple[int, int, int, int]]:
    """Return size -> (height_min, height_max, weight_min, weight_max)."""
```

- [ ] **Step 5: Implement cross-source safety validation**

`recognize_recommendations` must require exact set equality across Excel sizes, size-table sizes, and recommendation-chart sizes; require positive numeric fields; and verify nondecreasing maxima/measurements in Excel size order. Raise `RecognitionError` with the missing/extra size names and source path.

- [ ] **Step 6: Add transformed-image regression cases**

Generate temporary 0.9x and 1.1x resized copies and a 0.5-degree rotated copy using OpenCV. Assert the parsed structured result remains equal to the baseline or raise a deterministic `RecognitionError` rather than returning wrong values.

- [ ] **Step 7: Run recognition tests and commit**

Run: `./.venv/bin/python -m unittest -v tests/test_size_image_recognition.py`

Expected: all recognition tests PASS.

```bash
git add size_image_recognition.py tests/test_size_image_recognition.py
git commit -m "feat: recognize product size recommendations"
```

### Task 4: Make base and Douyin image groups count-aware

**Files:**
- Modify: `kuaimai_erp.py:872-944`
- Create: `tests/test_douyin_listing.py`
- Modify: `tests/test_kuaimai_erp.py`

- [ ] **Step 1: Write failing tests for skip and replace behavior**

Create a real Playwright HTML fixture with `.sc-upload .file-img`, hidden `.del-btn`, and an `input[type=file]`. Test that `sync_image_group` returns `"skipped"` without clicking delete when existing and expected counts match, and returns `"replaced"` after deleting all existing nodes when counts differ.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `./.venv/bin/python -m unittest -v tests/test_douyin_listing.py tests/test_kuaimai_erp.py`

Expected: FAIL because `sync_image_group` is absent.

- [ ] **Step 3: Implement the shared policy**

```python
async def sync_image_group(page, item, paths, label, timeout_seconds):
    await item.scroll_into_view_if_needed()
    existing = await item.locator(".sc-upload .file-img").count()
    if existing == len(paths):
        logging.getLogger("kuaimai_erp").info(
            "%s：已有 %s 张，与素材数量一致，跳过", label, existing
        )
        return "skipped"
    await delete_uploaded_images(item, page)
    file_input = item.locator('input[type="file"]').first
    if not await file_input.count():
        raise AutomationError(f"{label}区域找不到本地上传控件")
    await file_input.set_input_files([str(path) for path in paths])
    await wait_for_image_uploads(item, len(paths), label, timeout_seconds)
    return "replaced"
```

Replace the three base-data calls to `replace_image_group` with `sync_image_group`. Use the same function from `douyin_listing.py` for wash label, Douyin main, 3:4 main, and details.

- [ ] **Step 4: Run all current tests and commit**

Run: `./.venv/bin/python -m unittest discover -v`

Expected: all tests PASS.

```bash
git add kuaimai_erp.py tests/test_kuaimai_erp.py tests/test_douyin_listing.py
git commit -m "feat: skip complete image groups"
```

### Task 5: Discover the real Douyin page and implement category, title, attributes, and materials

**Files:**
- Create: `douyin_listing.py`
- Modify: `tests/test_douyin_listing.py`
- Artifacts only: `output/playwright/`

- [ ] **Step 1: Use Playwright CLI to capture the real tab and network calls**

```bash
export PWCLI="$HOME/.codex/skills/playwright/scripts/playwright_cli.sh"
export PLAYWRIGHT_CLI_SESSION=kuaimai-douyin
mkdir -p output/playwright
"$PWCLI" open 'https://scma.superboss.cc/supplier/prod/center' --headed
"$PWCLI" snapshot
"$PWCLI" network
```

Complete login manually if the named CLI session is not authenticated. Use snapshot refs to query `NGBL-10588`, click `编辑`, then click `抖音资料`; take a new snapshot after each major change. Start/stop tracing around one attribute dropdown and one freight dropdown. Store screenshots/traces under `output/playwright/` and do not commit them.

- [ ] **Step 2: Record selectors and endpoints as tested constants**

Add a test fixture reflecting the observed DOM labels and stable container attributes. Add endpoint matcher constants only for URLs observed in `pwcli network`; do not match unrelated requests by broad substrings such as `list` or `query`.

- [ ] **Step 3: Write failing page-adapter tests**

Test `open_douyin_tab`, `apply_first_recommended_category`, `fill_short_title`, and `fill_attribute`. The fixture must include a delayed Vue-style rerender and an attribute with multiple candidate options to verify exact normalized matching.

- [ ] **Step 4: Implement the typed adapter and exact option matching**

```python
# douyin_listing.py
class DouyinListingError(RuntimeError):
    pass


def normalize_option(value: str) -> str:
    return re.sub(r"[\s，,;；、・·\-_（）()]", "", value).casefold()


def choose_unique_option(expected: str, options: Sequence[Mapping[str, str]]) -> Mapping[str, str]:
    matches = [item for item in options if normalize_option(item["name"]) == normalize_option(expected)]
    if len(matches) != 1:
        names = "、".join(item["name"] for item in options)
        raise DouyinListingError(f"选项 {expected!r} 匹配数为 {len(matches)}；候选：{names}")
    return matches[0]
```

`fill_attribute` should obtain option IDs through an observed same-origin endpoint when available, select through the UI, and confirm the visible label after Vue settles. If the endpoint fails, open the DOM dropdown and apply the same unique normalized match.

Define the adapter class used by later tasks at this point; Tasks 6–8 add methods to this same class without renaming these methods:

```python
class DouyinListing:
    def __init__(self, page, drawer, logger, artifact_dir):
        self.page = page
        self.drawer = drawer
        self.logger = logger
        self.artifact_dir = artifact_dir
```

- [ ] **Step 5: Implement wash-label and material composition filling**

Use `sync_image_group`; create/remove material rows until their count equals `DouyinFields.materials`; select each material and fill its percentage; verify displayed selections and that the total percentage is 100.

- [ ] **Step 6: Run tests and commit**

Run: `./.venv/bin/python -m unittest -v tests/test_douyin_listing.py`

Expected: all category/title/attribute/material tests PASS.

```bash
git add douyin_listing.py tests/test_douyin_listing.py
git commit -m "feat: fill Douyin category and attributes"
```

### Task 6: Fill size recommendations and conditional Douyin images

**Files:**
- Modify: `douyin_listing.py`
- Modify: `tests/test_douyin_listing.py`

- [ ] **Step 1: Write failing size-row tests**

Build a fixture with five SKU rows and the columns `身高(cm)`, `体重(斤)`, `腰围(cm)`, `臀围(cm)`, and `裤长(cm)`. Assert that `fill_size_recommendations` maps by size text, not row position, and rejects missing or duplicate sizes.

- [ ] **Step 2: Implement size-row mapping and readback**

```python
async def fill_size_recommendations(scope, recommendations):
    by_size = {item.size: item for item in recommendations}
    rows = scope.locator("tbody tr")
    seen = set()
    for index in range(await rows.count()):
        row = rows.nth(index)
        size = (await row.locator(".size-name").inner_text()).strip()
        if size not in by_size or size in seen:
            raise DouyinListingError(f"尺码推荐行无法唯一匹配：{size!r}")
        seen.add(size)
        item = by_size[size]
        values = [f"{item.height_min}-{item.height_max}", f"{item.weight_min}-{item.weight_max}",
                  str(item.waist), str(item.hip), str(item.length)]
        inputs = row.locator("input")
        for cell_index, value in enumerate(values):
            await inputs.nth(cell_index).fill(value)
            await inputs.nth(cell_index).press("Tab")
    if seen != set(by_size):
        raise DouyinListingError(f"页面缺少尺码：{sorted(set(by_size) - seen)}")
```

Adapt `.size-name` and row scope to the selectors confirmed in Task 5's snapshot. Re-read all five inputs per row after filling.

- [ ] **Step 3: Write and implement Douyin image group tests**

Test Douyin main, main 3:4, and details independently. Use the user-confirmed rule: equal count skips, unequal count deletes all and reuploads in natural filename order.

- [ ] **Step 4: Run tests and commit**

Run: `./.venv/bin/python -m unittest -v tests/test_douyin_listing.py tests/test_size_image_recognition.py`

Expected: all tests PASS.

```bash
git add douyin_listing.py tests/test_douyin_listing.py
git commit -m "feat: fill Douyin sizes and images"
```

### Task 7: Fill delivery mode, every SKU price/inventory row, and every store freight template

**Files:**
- Modify: `douyin_listing.py`
- Modify: `tests/test_douyin_listing.py`

- [ ] **Step 1: Write failing delivery and SKU tests**

Test the ordered selections `现货预售混合模式`, `48小时内发货`, and `15天内`. Test a five-row SKU table where batch buttons propagate `586`, `0`, and `100`, then assert every row is read back as those values.

- [ ] **Step 2: Implement delivery mode with selected-state checks**

Use label text within the `价格库存` section, click only when not already checked, and wait for each dependent option group before selecting the next value. Reject a page that exposes no mixed-mode controls.

- [ ] **Step 3: Implement generic batch column setting**

```python
async def batch_set_and_verify(section, batch_label, column_label, value):
    batch_item = section.locator(".sku-batch-item").filter(has_text=batch_label).first
    await batch_item.locator("input").fill(str(value))
    await batch_item.locator("input").press("Tab")
    button = await first_visible(section.get_by_role("button", name="批量设置", exact=True))
    if button is None:
        raise DouyinListingError(f"{batch_label}找不到批量设置按钮")
    await button.click()
    values = await read_table_column_inputs(section, column_label)
    if any(Decimal(item) != Decimal(str(value)) for item in values):
        raise DouyinListingError(f"{column_label}逐行复核失败：{values}")
    return values
```

Scope each batch button to its batch item or use the real button-to-field relationship observed in Task 5; do not click the first global `批量设置` if the page has multiple buttons.

- [ ] **Step 4: Write failing per-store freight tests**

Mock three stores with separate option lists. Assert both Excel aliases match after normalization, and assert one missing store raises an error containing store name, Excel aliases, and candidate templates.

- [ ] **Step 5: Implement freight API capture and UI selection**

Use the exact endpoint observed in Task 5. Build `store_id -> [{id, name}]`, call `choose_unique_option` against all Excel aliases, select the option in the corresponding store row, and read back its displayed text. Require the set of processed stores to equal the set of visible authorized store rows.

- [ ] **Step 6: Run tests and commit**

Run: `./.venv/bin/python -m unittest -v tests/test_douyin_listing.py`

Expected: all delivery/SKU/freight tests PASS.

```bash
git add douyin_listing.py tests/test_douyin_listing.py
git commit -m "feat: fill Douyin stock and freight"
```

### Task 8: Add safe publishing and end-to-end orchestration

**Files:**
- Modify: `douyin_listing.py`
- Modify: `kuaimai_erp.py:1111-1325`
- Modify: `tests/test_douyin_listing.py`
- Modify: `tests/test_kuaimai_erp.py`

- [ ] **Step 1: Write failing publish-result tests**

Test success by observed publish API, success toast fallback, explicit API failure, and ambiguous timeout. For timeout, assert the status endpoint/DOM is queried once and the publish button is never clicked a second time.

- [ ] **Step 2: Implement final validation**

`validate_douyin_form` must return a JSON-serializable report containing category, title, attribute values, materials, size rows, image counts/actions, delivery state, all SKU price/inventory values, and every store freight template. It must also collect visible `.el-form-item__error` text and reject any nonempty errors.

- [ ] **Step 3: Implement `publish_and_confirm`**

Attach a response listener only to the exact publish endpoint observed in Task 5, click the visible button whose whitespace-normalized text equals `保存并铺货到平台`, handle only bounded optional confirmation dialogs, and return structured evidence. On timeout, query the exact status endpoint or the product's visible completed-platform status; return `uncertain` and raise without a second click when no authoritative result exists.

- [ ] **Step 4: Implement orchestration in `run_browser_automation`**

Before opening Chrome, recognize the size images and write `ocr-result.json`. After the existing base stage, call:

```python
douyin = DouyinListing(page, drawer, logger, artifact_dir)
await douyin.open()
await douyin.apply_category_and_fields(product.douyin_fields)
await douyin.apply_materials(product.douyin_fields.materials, product.douyin_assets.wash_labels)
await douyin.apply_sizes(recommendations)
await douyin.apply_images(product.main_images, product.main_images_34, product.detail_images)
await douyin.apply_delivery_and_skus(product.douyin_fields)
await douyin.apply_freight(product.douyin_fields.freight_aliases)
report = await douyin.validate()
```

Write `douyin-before-publish.json` and `before-publish.png`. Reuse `--no-save` as rehearsal mode: it fills and validates both base and Douyin tabs but does not click either save button. With default `--save`, click only `保存并铺货到平台` after the combined validation succeeds.

- [ ] **Step 5: Run the full automated suite**

Run: `./.venv/bin/python -m unittest discover -v`

Expected: all legacy and new tests PASS with no unexpected browser warnings.

- [ ] **Step 6: Commit**

```bash
git add douyin_listing.py kuaimai_erp.py tests/test_douyin_listing.py tests/test_kuaimai_erp.py
git commit -m "feat: orchestrate safe Douyin publishing"
```

### Task 9: Document and perform staged real-page acceptance

**Files:**
- Modify: `README.md`
- Runtime artifacts: `output/kuaimai/runs/<timestamp>/`, `output/playwright/`

- [ ] **Step 1: Update documentation**

Document required product folders, local Vision/OpenCV behavior, exact image count policy, Excel field aliases, `--no-save` rehearsal, default publish action, and generated OCR/publish evidence. State that one missing attribute/freight option or uncertain OCR result stops before publishing.

- [ ] **Step 2: Run static and full regression checks**

```bash
./.venv/bin/python -m py_compile kuaimai_erp.py douyin_data.py size_image_recognition.py douyin_listing.py tests/*.py
./.venv/bin/python -m unittest discover -v
git diff --check
```

Expected: compilation succeeds, all tests PASS, and `git diff --check` prints nothing.

- [ ] **Step 3: Run a real rehearsal without saving**

Because the isolated worktree excludes credentials, explicitly reuse the original workspace's ignored auth/profile paths:

```bash
./.venv/bin/python kuaimai_erp.py \
  --no-save \
  --user-data-dir '/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/output/kuaimai/chrome-profile' \
  --auth-state '/Users/linchaoyang/Desktop/电商自动化/2.快麦一键铺货/output/kuaimai/auth-state.json'
```

Expected: the run stops after combined validation, with no save/publish request; the report shows five exact size rows, five SKU rows at `586/0/100`, all stores matched, and no form errors.

- [ ] **Step 4: Inspect the rehearsal evidence**

Open `before-publish.png`, `ocr-result.json`, and `douyin-before-publish.json`. Confirm image counts/actions, all five size rows, selected delivery mode, SKU values, and freight templates. If any mismatch exists, fix it and repeat Step 3; do not proceed to publish.

- [ ] **Step 5: Perform the authorized real publish**

Run the same command without `--no-save`. This action is authorized by the user for `NGBL-10588`.

Expected: one publish request, an authoritative success result, `publish-result.json`, and an after-publish screenshot/status. If the result is uncertain, report uncertainty and do not rerun automatically.

- [ ] **Step 6: Final verification and commit**

```bash
./.venv/bin/python -m unittest discover -v
git status --short
git add README.md
git commit -m "docs: document Douyin listing workflow"
```

Expected: all tests PASS; only ignored runtime artifacts remain untracked; the documentation commit succeeds.
