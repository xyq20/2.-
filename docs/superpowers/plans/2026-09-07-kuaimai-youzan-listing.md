# Kuaimai Youzan Listing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe Youzan form adapter that fills Excel-driven category attributes, SKU price/inventory, fixed weight, stock deduction, delivery and freight template, verifies a standalone no-save run, then verifies save/readback before enabling Youzan in the all-platform sequence.

**Architecture:** Add a small immutable Excel data model and a dedicated `YouzanFormListing` DOM adapter that reuses the proven label normalization and exact option-selection helpers from `TaobaoListing`. Keep save and publish outside the adapter: `kuaimai_erp.py` remains the only owner of final writes, and Youzan stays out of `all` until its standalone save/readback evidence passes.

**Tech Stack:** Python 3, asyncio, Playwright, openpyxl, unittest, zsh launcher.

---

The worktree is already dirty with user-owned multi-platform changes. Do not create commits during this plan unless the user separately asks for them; staging a shared file such as `kuaimai_erp.py` could capture unrelated work.

## File map

- Create `youzan_data.py`: immutable Excel field snapshot, category segments and garment-kind classification.
- Create `youzan_form_listing.py`: Youzan tab/category/attribute/SKU/logistics/freight DOM operations and persisted-value verification.
- Create `tests/test_youzan_data.py`: data normalization and garment/template rules.
- Create `tests/test_youzan_form_listing.py`: Playwright fixture coverage of the complete form and failure cases.
- Modify `platform_registry.py`: register standalone Youzan first with `enabled_in_all=False` and `publish_allowed=False`; enable it only after live save/readback.
- Modify `kuaimai_erp.py`: attach Youzan fields to `ProductData`, run the adapter, emit reports, save, reopen and verify.
- Modify `run.command`: add a Youzan menu entry for no-save and save-only; reject publish because no Youzan store-selection scope was provided.
- Modify `tests/test_platform_registry.py`, `tests/test_launchers.py`, and `tests/test_kuaimai_erp.py`: integration and write-gate regression coverage.
- Modify `README.md`: document standalone Youzan commands and the post-verification all-platform sequence.

### Task 1: Define the Youzan Excel contract

**Files:**
- Create: `youzan_data.py`
- Create: `tests/test_youzan_data.py`
- Modify: `kuaimai_erp.py:24-70,245-270,400-445,465-495`

- [ ] **Step 1: Write failing data-model tests**

```python
import unittest

from youzan_data import parse_youzan_fields


class YouzanDataTests(unittest.TestCase):
    def test_preserves_fields_and_ordered_category(self):
        parsed = parse_youzan_fields({
            "商品分类": "休闲裤/男士休闲直筒裤/工装休闲裤",
            "价格/一口价": 586.0,
            "数量": 100.0,
        })
        self.assertEqual(
            parsed.category_path,
            ("休闲裤", "男士休闲直筒裤", "工装休闲裤"),
        )
        self.assertEqual(parsed.fields["价格/一口价"], "586")
        self.assertEqual(parsed.garment_kind, "pants")

    def test_classifies_coat_and_preserves_unknown_kind(self):
        coat = parse_youzan_fields({"商品分类": "外套/皮衣"})
        self.assertEqual(coat.garment_kind, "coat")
        unknown = parse_youzan_fields({"商品分类": "男装"})
        self.assertEqual(unknown.garment_kind, "unknown")
```

- [ ] **Step 2: Run the tests and confirm the module is absent**

Run: `.venv/bin/python -m unittest tests.test_youzan_data -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'youzan_data'`.

- [ ] **Step 3: Implement the immutable data contract**

```python
@dataclass(frozen=True)
class YouzanFields:
    fields: Mapping[str, str]
    category_path: Tuple[str, ...]
    garment_kind: str


def parse_youzan_fields(fields: Mapping[str, Any]) -> YouzanFields:
    normalized = {
        str(key).strip(): _cell_text(value)
        for key, value in fields.items()
        if str(key).strip() and _cell_text(value)
    }
    raw_category = _single_category_value(fields)
    category_path = tuple(
        part.strip() for part in re.split(r"[/／>＞]", raw_category) if part.strip()
    )
    searchable = " ".join(category_path)
    has_pants = "裤" in searchable
    has_coat = any(token in searchable for token in ("外套", "皮衣"))
    return YouzanFields(
        fields=FrozenFields(normalized),
        category_path=category_path,
        garment_kind=(
            "pants" if has_pants and not has_coat
            else "coat" if has_coat and not has_pants
            else "unknown"
        ),
    )
```

Use a dedicated `FrozenFields` implementation with the same immutable semantics as `xhs_data.py`; do not import XHS-specific error text. Parsing an unrelated platform must not fail merely because its category is not pants or coat. The Youzan runner rejects `garment_kind="unknown"` only when Youzan is actually selected.

- [ ] **Step 4: Attach the data to `ProductData`**

Add `youzan_fields: Optional[YouzanFields] = None`, call `parse_youzan_fields(fields)` in `read_product_data()`, and include `youzan_field_count` in `product_summary()`. Add imports for `YouzanFields`, `YouzanDataError`, and `parse_youzan_fields`.

- [ ] **Step 5: Run the data tests**

Run: `.venv/bin/python -m unittest tests.test_youzan_data -v`

Expected: all Youzan data tests PASS.

### Task 2: Register a standalone, non-publishing Youzan platform

**Files:**
- Modify: `platform_registry.py:94-106`
- Modify: `tests/test_platform_registry.py:14-63`
- Modify: `run.command:15-138`
- Modify: `tests/test_launchers.py:45-110`

- [ ] **Step 1: Write failing registry and launcher assertions**

Add this expected registry row immediately after XHS:

```python
(
    "youzan", "yz", "有赞资料", "有赞资料", "implemented",
    False, False, "allowed", False, None,
)
```

Assert the initial all expansion remains:

```python
("douyin", "taobao", "tmall", "pdd", "xhs")
```

Add launcher cases:

```python
def test_youzan_preview_is_no_save(self):
    arguments, _ = self._run_launcher(input_text="8\n1\n")
    self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "youzan", "--no-save"))

def test_youzan_save_only_never_publishes(self):
    arguments, _ = self._run_launcher(input_text="8\n2\n")
    self.assertEqual(arguments, ("kuaimai_erp.py", "--platform", "youzan", "--save-only"))
```

Move the existing create-product menu choice from 8 to 9 and update its tests.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `.venv/bin/python -m unittest tests.test_platform_registry tests.test_launchers -v`

Expected: FAIL because `youzan` and menu choice 8 are not registered.

- [ ] **Step 3: Add the initial registry entry**

```python
PlatformSpec(
    cli_name="youzan",
    platform_id="yz",
    display_name="有赞资料",
    tab_label="有赞资料",
    lifecycle="implemented",
    enabled_in_all=False,
    supports_inspect=False,
    save_policy="allowed",
    publish_allowed=False,
    discovery_adapter=None,
),
```

The initial `False` is the live-verification gate. Do not enable it in this task.

- [ ] **Step 4: Add launcher choices**

Show `8) 有赞（填写、保存，暂不铺货）` and move create product to `9)`. Map:

```zsh
youzan:preview) PYTHON_ARGS=(--platform youzan --no-save) ;;
youzan:save_only) PYTHON_ARGS=(--platform youzan --save-only) ;;
```

When choice 8 selects mode 3, print `有赞尚未配置铺货店铺，请选择 1 或 2。` and reprompt. Do not silently convert publish to save.

- [ ] **Step 5: Run the focused tests**

Run: `.venv/bin/python -m unittest tests.test_platform_registry tests.test_launchers -v`

Expected: PASS while `expand_platform_selection("all")` still excludes Youzan.

### Task 3: Implement product type and exact category selection

**Files:**
- Create: `youzan_form_listing.py`
- Create: `tests/test_youzan_form_listing.py`

- [ ] **Step 1: Build a Playwright fixture and failing category tests**

The fixture must expose:

```html
<button role="tab" aria-selected="false">有赞资料</button>
<div role="tabpanel" aria-label="有赞资料">
  <label><input type="radio" name="product-type"><span>实物商品</span></label>
  <button onclick="openCategory()">修改类目</button>
</div>
<div role="dialog" aria-label="修改类目">
  <input data-category-search>
  <div data-category-path="服装鞋包 > 男装 > 休闲裤">服装鞋包 > 男装 > 休闲裤</div>
  <button>确定</button>
</div>
```

Test exact output and ambiguity:

```python
result = await listing.apply_category(("休闲裤", "男士休闲直筒裤"))
self.assertEqual(result["selected"], "服装鞋包 > 男装 > 休闲裤")
self.assertEqual(result["search_term"], "休闲裤")
```

Duplicate the exact path in a second fixture case and assert `YouzanFormListingError` contains `有赞类目完整路径不是唯一项`.

- [ ] **Step 2: Run and confirm the adapter is absent**

Run: `.venv/bin/python -m unittest tests.test_youzan_form_listing.YouzanCategoryTests -v`

Expected: FAIL importing `youzan_form_listing`.

- [ ] **Step 3: Implement opening, physical product, and exact category**

Define `YouzanFormListing(TaobaoListing)` with these exact async method
signatures: `open(self) -> YouzanFormListing`,
`select_physical_product(self) -> Mapping[str, Any]`, and
`apply_category(self, category_path: Sequence[str]) -> Mapping[str, Any]`.

`apply_category()` must:

1. Require a non-empty `category_path`.
2. Use the first segment as the search term.
3. Click only the unique visible `修改类目` button.
4. Fill the unique visible dialog input.
5. Poll visible result rows until one full route has exact normalized segments `服装鞋包`, `男装`, and the search leaf.
6. Reject zero or multiple exact routes.
7. Click the unique exact row and the unique `确定` button.
8. Re-read the page category text after the dialog closes.

Do not use substring-only matching and do not hard-code an API leaf ID.

- [ ] **Step 4: Run category tests**

Run: `.venv/bin/python -m unittest tests.test_youzan_form_listing.YouzanCategoryTests -v`

Expected: PASS.

### Task 4: Implement Excel attributes and deterministic SKU batch fields

**Files:**
- Modify: `youzan_form_listing.py`
- Modify: `tests/test_youzan_form_listing.py`

- [ ] **Step 1: Add failing tests for attributes and one batch click**

Use category fields for `品牌`, `面料`, `款式`, `季节`, `图案`, `性别`, `厚薄`, `款式细节`, `货号`, `基础风格`, `细分风格`, `工艺处理`, `裤长`, `弹力`, `腰型`, `裤脚口款式`, `裤门襟`, `款式版型`, and `主材含量`. Assert exact candidates from Excel are selected and `货号` equals the style code.

Expose batch fields and two SKU rows:

```html
<div data-youzan-batch="价格"><input></div>
<div data-youzan-batch="库存"><input></div>
<div data-youzan-batch="重量(kg)"><input></div>
<button id="batch" onclick="applyBatch()">批量设置</button>
```

Test:

```python
report = await listing.fill_sku_batch(parsed.fields)
self.assertEqual(report["values"], {"价格": "586", "库存": "100", "重量(kg)": "1"})
self.assertEqual(report["row_count"], 2)
self.assertEqual(await page.locator("#batch").get_attribute("data-clicks"), "1")
```

- [ ] **Step 2: Run and confirm missing methods fail**

Run: `.venv/bin/python -m unittest tests.test_youzan_form_listing.YouzanAttributeAndSkuTests -v`

Expected: FAIL with missing `fill_category_attributes` or `fill_sku_batch`.

- [ ] **Step 3: Implement exact label/option matching**

Add explicit field-name aliases only for known workbook headings:

```python
YOUZAN_FIELD_ALIASES = {
    normalize_label("面料"): tuple(map(normalize_label, ("面料材质", "面料俗称"))),
    normalize_label("工艺处理"): tuple(map(normalize_label, ("服饰工艺", "工艺"))),
    normalize_label("款式版型"): tuple(map(normalize_label, ("服饰版型", "版型"))),
    normalize_label("货号"): tuple(map(normalize_label, ("商家外部编码", "款式编码"))),
}
```

Reuse `excel_aliases()`, `selection_value_groups()`, and the inherited exact select helper. Limit field discovery to the vertical bounds between `类目参数` and `规格明细` so later logistics fields cannot be mistaken for category attributes.

- [ ] **Step 4: Implement batch value extraction and row verification**

Price aliases are `(价格, 一口价, 基本售价, 商品价格, 售卖价, 售价)`; inventory aliases are `(数量, 库存)`. Require price `> 0` and integer inventory `>= 0`.

Fill all three batch controls, dispatch `input`, `change`, and `blur`, click the unique `批量设置` button exactly once, then poll every visible SKU row until price, inventory and weight numerically equal the expected values for two consecutive snapshots.

- [ ] **Step 5: Run focused tests**

Run: `.venv/bin/python -m unittest tests.test_youzan_form_listing.YouzanAttributeAndSkuTests -v`

Expected: PASS, including the single-click assertion.

### Task 5: Implement inventory, delivery, freight, and persisted readback

**Files:**
- Modify: `youzan_form_listing.py`
- Modify: `tests/test_youzan_form_listing.py`

- [ ] **Step 1: Add failing logistics and freight tests**

Fixture controls must include total weight, `付款减库存`, `快递发货`, and a freight select with both exact options.

```python
report = await listing.fill_sales_and_logistics("pants")
self.assertEqual(report, {
    "weight": "1",
    "inventory_deduction": "付款减库存",
    "delivery": ("快递发货",),
    "freight_template": "T恤、裤子、饰品邮费模版",
})
```

Add a coat case expecting `鞋子、皮衣、外套邮费模版`, and an ambiguity case where duplicate visible options raise an error.

- [ ] **Step 2: Run and confirm missing logistics methods fail**

Run: `.venv/bin/python -m unittest tests.test_youzan_form_listing.YouzanLogisticsTests -v`

Expected: FAIL with missing `fill_sales_and_logistics`.

- [ ] **Step 3: Implement deterministic logistics writes**

Map garment kind using:

```python
YOUZAN_FREIGHT_TEMPLATES = {
    "pants": "T恤、裤子、饰品邮费模版",
    "coat": "鞋子、皮衣、外套邮费模版",
}
```

Fill total weight `1`, click the exact radio label `付款减库存`, check `快递发货` only if not already checked, and select exactly one visible freight option. Re-read each value after setting it.

- [ ] **Step 4: Implement full-form and persisted verification reports**

Expose:

```python
async def apply_excel_fields(self, fields: YouzanFields, *, style_code: str) -> Mapping[str, Any]
async def verify_persisted_values(self, fields: YouzanFields, *, style_code: str) -> Mapping[str, Any]
```

The first method runs product type, category, attribute, SKU, logistics, and visible-error checks in order. The second method reads without changing values and requires the same category, style code, every SKU price/inventory/weight, total weight, deduction mode, delivery option, and freight template.

- [ ] **Step 5: Run all adapter tests**

Run: `.venv/bin/python -m unittest tests.test_youzan_data tests.test_youzan_form_listing -v`

Expected: PASS.

### Task 6: Wire Youzan into the common runner without publishing

**Files:**
- Modify: `kuaimai_erp.py:24-70,255-270,3080-3130,3450-3620,3950-4120,4170-4260`
- Modify: `tests/test_kuaimai_erp.py:1407-1530,1623-1675`
- Modify: `README.md`

- [ ] **Step 1: Add failing runner and execution-mode tests**

Add Youzan to the non-base matrix and parser help assertions. Verify:

```python
args = self._args("--platform", "youzan", "--save-only")
selected = kuaimai_erp.validate_execution_mode(args)
self.assertEqual(tuple(spec.cli_name for spec in selected), ("youzan",))
self.assertIsNone(kuaimai_erp.resolve_commerce_publish_target(args))
self.assertFalse(kuaimai_erp.requires_base_save_before_platform("youzan"))
```

Also assert standalone `--platform youzan --save` is rejected until a publish target is explicitly designed, while `--no-save` and `--save-only` are valid.

- [ ] **Step 2: Run focused runner tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_kuaimai_erp.ExecutionModeTests -v`

Expected: FAIL because the runner has no Youzan branch or publish restriction.

- [ ] **Step 3: Add runner preflight and form branch**

Add `youzan_requested = args.platform == "youzan"`, require
`product.youzan_fields`, a non-empty category, and a garment kind other than
`"unknown"` when Youzan is selected and the run is not inspect-only. Other
platforms must remain usable with an unknown Youzan garment kind. Then branch:

```python
elif youzan_requested:
    assert product.youzan_fields is not None
    youzan = YouzanFormListing(page, drawer, logger)
    await youzan.open()
    try:
        youzan_report = await youzan.apply_excel_fields(
            product.youzan_fields,
            style_code=product.style_code,
        )
    except YouzanFormListingError:
        await safe_screenshot(page, artifact_dir / "youzan-error.png")
        raise
    (artifact_dir / "youzan-before-save.json").write_text(
        json.dumps(youzan_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    await safe_screenshot(page, artifact_dir / "youzan-before-save.png")
```

- [ ] **Step 4: Add save-only persisted verification**

After the common call below, reload and reopen the product for standalone
Youzan, open `有赞资料`, call `verify_persisted_values()`, and write
`youzan-after-save-validation.json`.

```python
result = await click_save_and_confirm(
    page,
    drawer,
    args.sync_erp,
    args.timeout,
    logger,
    button_text="保存",
)
```

Reject direct publish with:

```python
if args.platform == "youzan" and args.save and not args.save_only:
    raise SystemExit("有赞尚未配置铺货店铺；请使用 --save-only")
```

- [ ] **Step 5: Update README and run integration tests**

Document:

```text
./run.command --platform youzan --no-save
./run.command --platform youzan --save-only
```

Run: `.venv/bin/python -m unittest tests.test_youzan_data tests.test_youzan_form_listing tests.test_platform_registry tests.test_launchers tests.test_kuaimai_erp -v`

Expected: PASS.

### Task 7: Run the standalone no-save browser acceptance test

**Files:**
- Runtime evidence: `output/kuaimai/runs/<timestamp>/youzan-before-save.json`
- Runtime evidence: `output/kuaimai/runs/<timestamp>/youzan-before-save.png`
- Runtime evidence: `output/kuaimai/runs/<timestamp>/run.log`

- [ ] **Step 1: Confirm no other automation process owns the profile**

Run: `pgrep -af 'kuaimai_erp.py|chrome.*output/kuaimai/chrome-profile'`

Expected: no active `kuaimai_erp.py`. A stale dedicated Chrome may be closed by the existing safe profile cleanup; do not target the user's normal Chrome profile.

- [ ] **Step 2: Run standalone no-save**

Run: `.venv/bin/python kuaimai_erp.py --platform youzan --no-save`

Expected log stages: Youzan tab opened, physical product selected, exact category confirmed, attributes filled, one SKU batch click, all SKU rows verified, total weight/deduction/delivery/freight verified, and final `--no-save` message.

- [ ] **Step 3: Inspect evidence before authorizing a save test**

Verify JSON values are `价格=586`, `库存=100`, `重量=1`, `付款减库存`, `快递发货`, and `T恤、裤子、饰品邮费模版`. Visually inspect the screenshot and confirm no save or publish dialog appeared.

- [ ] **Step 4: If the live page differs, preserve evidence and patch only the Youzan adapter**

Use the generated `youzan-error.png`, `error.png`, stage log and DOM-visible labels. Add a failing fixture reproducing the exact live structure before adjusting selectors. Repeat Tasks 5-7 until the no-save evidence passes.

### Task 8: Run save/readback, then enable Youzan in all

**Files:**
- Modify after successful readback: `platform_registry.py`
- Modify after successful readback: `kuaimai_erp.py:4365-4410`
- Modify after successful readback: `run.command:16`
- Modify after successful readback: `tests/test_platform_registry.py`, `tests/test_launchers.py`, `tests/test_kuaimai_erp.py`
- Runtime evidence: `output/kuaimai/runs/<timestamp>/save-result.json`
- Runtime evidence: `output/kuaimai/runs/<timestamp>/youzan-after-save-validation.json`

- [ ] **Step 1: Run the authorized save-only test**

Run: `.venv/bin/python kuaimai_erp.py --platform youzan --save-only`

Expected: the common runner clicks only `保存`, records a save-confirmation source, reopens the product, and produces a passing `youzan-after-save-validation.json`; no publish dialog appears.

- [ ] **Step 2: Require complete persisted evidence**

The readback JSON must contain the exact category, style code, row count, price/inventory/weight values, total weight, `付款减库存`, `快递发货`, and the pants freight template. If any field is absent or different, leave `enabled_in_all=False` and return to the failing fixture first.

- [ ] **Step 3: Write failing all-platform order tests**

Change expected expansion and saved-flow stage order to:

```python
("douyin", "taobao", "tmall", "pdd", "xhs", "youzan")
```

For all-platform `--save`, assert the Youzan stage is forced to save-only semantics because `publish_allowed=False`.

- [ ] **Step 4: Enable Youzan and append it to the shared-session stages**

Set `enabled_in_all=True`. Append `youzan` after `xhs` in `commerce_stages`. In `run_all_implemented_platforms()`, set `stage_args.save_only = True` for `platform_name == "youzan"` when the outer command is a publish run, so Youzan saves without opening a store dialog.

Update launcher text to `基础资料 + 抖音 + 淘宝 + 天猫 + 拼多多 + 小红书 + 有赞`.

- [ ] **Step 5: Run the full regression suite**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: PASS with no Youzan change to existing platform assertions except the intentional registry/menu/stage-order additions.

- [ ] **Step 6: Run a safe all-platform preview smoke test**

Run: `.venv/bin/python kuaimai_erp.py --platform all --no-save`

Expected: shared browser/editor session reaches Youzan after XHS without page refresh, performs no save or publish action, and writes all stage results as success.

- [ ] **Step 7: Final evidence review**

Report separately:

1. Unit/regression test command and counts.
2. Standalone no-save run directory.
3. Standalone save-only confirmation and reopened readback directory.
4. All-platform preview directory and stage order.
5. Explicit statement that no Youzan铺货 was submitted.
