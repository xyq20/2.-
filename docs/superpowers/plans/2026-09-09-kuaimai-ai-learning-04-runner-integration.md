# Kuaimai AI Learning Phase 4: Runner Integration and Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate constrained attribute decisions into every supported platform, pause safely for review, automatically reopen the stopped platform after confirmation, save/read back, and continue only with later platforms.

**Architecture:** A platform-neutral `AttributeRuntime` is injected into existing form writers and owns candidate snapshots, cloud decisions and local revalidation. It raises a typed `ReviewRequired` before any unsafe write. The all-platform orchestrator records verified checkpoints, closes the shared browser while waiting, then reopens the same product at the stopped platform and resumes from that index.

**Tech Stack:** Python async/await, Playwright, existing platform adapters, local SQLite, Cloudflare device API, `unittest` and Playwright fixtures.

---

## File map

- Create `attribute_runtime.py`: resolver protocol, live-candidate validation and typed review interruption.
- Create `tests/test_attribute_runtime.py`: local/cloud decision and changed-candidate tests.
- Create `platform_candidate_source.py`: API-first candidate extraction plus DOM cross-checking.
- Create `tests/test_platform_candidate_source.py`: missing API, mismatched DOM and exact-match tests.
- Modify `platform_schema.py`: expose complete non-sensitive product-attribute candidates to the runtime while keeping shop/logistics/freight samples hidden.
- Modify `tests/test_platform_schema.py`: prove full product candidates and sensitive-source redaction.
- Modify `taobao_listing.py`: optional runtime injection used by inheriting adapters.
- Modify `douyin_listing.py`: matching runtime injection for its separate base class.
- Modify `tmall_form_listing.py`, `pdd_form_listing.py`, `wxsph_form_listing.py`, `xhs_form_listing.py`, `youzan_form_listing.py`, `jd_form_listing.py`: resolve dynamic fields before DOM writes.
- Modify each corresponding `tests/test_*_form_listing.py` or listing test: candidate capture and override tests.
- Create `review_resume.py`: wait/poll and restart-safe resume selection.
- Create `tests/test_review_resume.py`: event consumption and checkpoint validation.
- Modify `kuaimai_erp.py`: stage state machine, pause, browser close, resume and readback recording.
- Modify `tests/test_kuaimai_erp.py`: resume order, idempotency and failure gates.
- Modify `run.command`: optional “恢复待审核任务” action.
- Modify `tests/test_launchers.py`: launcher assertions.
- Modify `README.md`: operator runbook and failure recovery.

### Task 1: Implement a platform-neutral attribute runtime

**Files:**
- Create: `attribute_runtime.py`
- Create: `tests/test_attribute_runtime.py`

- [ ] **Step 1: Write failing runtime tests**

```python
import unittest
from unittest.mock import Mock

from attribute_runtime import AttributeRequest, AttributeRuntime, ReviewRequired
from learning_models import CandidateValue


class FakeClient:
    def decide(self, request):
        return {"status": "auto_fill_ready", "value_id": "long", "snapshot_version": request["snapshot_version"]}


def make_length_request():
    return AttributeRequest(
        platform_id="wxsph", category_leaf_id="pants", field_id="length",
        field_label="裤长", candidates=(CandidateValue("short", "短裤"), CandidateValue("long", "长裤")),
        excel_value="", evidence={"visual": ["asset-1"]}, custom_allowed=False,
        schema_version="schema-1",
    )


class AttributeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_only_a_live_candidate(self):
        runtime = AttributeRuntime(store=Mock(), client=FakeClient(), run_id="run-1", product_version="p-1")
        result = await runtime.resolve(make_length_request())
        self.assertEqual((result.value_id, result.label), ("long", "长裤"))

    async def test_review_response_raises_before_write(self):
        client = FakeClient()
        client.decide = lambda request: {"status": "review_required", "review_id": "review-1"}
        runtime = AttributeRuntime(Mock(), client, "run-1", "p-1")
        with self.assertRaises(ReviewRequired) as caught:
            await runtime.resolve(make_length_request())
        self.assertEqual(caught.exception.review_id, "review-1")
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_attribute_runtime -v`

Expected: FAIL importing `attribute_runtime`.

- [ ] **Step 3: Implement exact request/result types**

```python
@dataclass(frozen=True)
class AttributeRequest:
    platform_id: str
    category_leaf_id: str
    field_id: str
    field_label: str
    candidates: Tuple[CandidateValue, ...]
    excel_value: str
    evidence: Mapping[str, Any]
    custom_allowed: bool
    schema_version: str


@dataclass(frozen=True)
class ResolvedAttribute:
    value_id: str
    label: str
    source: str
    snapshot_version: str


class ReviewRequired(RuntimeError):
    def __init__(self, review_id: str, request: AttributeRequest, reason_code: str):
        super().__init__("属性需要人工审核：{0}".format(request.field_label))
        self.review_id = review_id
        self.request = request
        self.reason_code = reason_code
```

- [ ] **Step 4: Implement async `AttributeRuntime.resolve`**

`async def resolve(self, request: AttributeRequest) -> ResolvedAttribute` must map the field, construct `CandidateSnapshot(..., custom_allowed=request.custom_allowed)`, persist the full non-sensitive candidate snapshot, enqueue it before network access, request a decision through `await asyncio.to_thread(self.client.decide, payload)`, validate the response with `validate_decision`, and return `ResolvedAttribute` only for `AUTO_FILL_READY`. Shop, freight and logistics selectors must be rejected as unsupported learning sources.

On `review_required`, persist the review ID in the checkpoint and raise `ReviewRequired`. On snapshot mismatch, create a new review with reason `snapshot_changed`; never substitute a label match from an old snapshot.

- [ ] **Step 5: Run runtime tests**

Run: `.venv/bin/python -m unittest tests.test_attribute_runtime -v`

Expected: PASS.

- [ ] **Step 6: Commit the runtime**

```bash
git add attribute_runtime.py tests/test_attribute_runtime.py
git commit -m "feat: add platform-neutral attribute runtime"
```

### Task 2: Inject the runtime without changing legacy behavior

**Files:**
- Modify: `taobao_listing.py:205-214`
- Modify: `douyin_listing.py:206-214`
- Modify: `tmall_form_listing.py:164-185`
- Modify: `kuaimai_erp.py:3618-3930`
- Modify: `tests/test_taobao_listing.py`
- Modify: `tests/test_douyin_listing.py`
- Modify: `tests/test_tmall_form_listing.py`

- [ ] **Step 1: Write failing constructor-compatibility tests**

Assert all existing three- or four-argument constructor calls still work and set `attribute_runtime is None`. Assert passing a fake runtime stores the same object.

- [ ] **Step 2: Add keyword-only runtime injection to both base writers**

```python
def __init__(self, page: Any, drawer: Any, logger: Any, *, attribute_runtime: Optional[AttributeRuntime] = None) -> None:
    self.page = page
    self.drawer = drawer
    self.logger = logger
    self.attribute_runtime = attribute_runtime
```

For `DouyinListing`, keep `artifact_dir` before the new keyword-only argument. In `TmallFormListing.__init__`, forward `attribute_runtime=attribute_runtime` to `super().__init__` while preserving its existing `api_index` parameter.

- [ ] **Step 3: Pass the optional runtime from `run_browser_automation`**

Add `attribute_runtime: Optional[AttributeRuntime] = None` to `run_browser_automation`, then pass it by keyword to all writer instances, including the post-save verifier instances. Do not create the runtime inside a platform adapter.

- [ ] **Step 4: Run constructor and current platform tests**

Run: `.venv/bin/python -m unittest tests.test_taobao_listing tests.test_douyin_listing tests.test_tmall_form_listing -v`

Expected: PASS with no behavior change when the runtime is absent.

- [ ] **Step 5: Commit injection plumbing**

```bash
git add taobao_listing.py douyin_listing.py tmall_form_listing.py kuaimai_erp.py tests/test_taobao_listing.py tests/test_douyin_listing.py tests/test_tmall_form_listing.py
git commit -m "refactor: inject optional attribute runtime"
```

### Task 3: Complete a WeChat Store vertical slice

**Files:**
- Modify: `wxsph_form_listing.py:586-720`
- Modify: `tests/test_wxsph_form_listing.py`
- Create: `platform_candidate_source.py`
- Create: `tests/test_platform_candidate_source.py`
- Modify: `platform_schema.py`
- Modify: `tests/test_platform_schema.py`

- [ ] **Step 1: Write a failing `裤长` candidate fixture**

Create a Playwright fixture where the page JSON/API schema and the `裤长` select both contain `短裤`, `九分裤`, and `长裤`. Supply Excel value `九分裤`, but make the fake runtime return `value_id="long"`. Assert the DOM readback is `长裤`, the request field name is `裤长`, and the runtime receives all three API candidates only after DOM cross-validation.

- [ ] **Step 2: Add an API-first candidate bridge with DOM cross-validation**

Create `reconcile_candidates(api_values, dom_values) -> Tuple[CandidateValue, ...]` in `platform_candidate_source.py`. The API values come from the captured page JSON/schema (`PlatformSchema` or the platform's existing API index) and are authoritative for IDs and labels. DOM values prove which options are currently visible and clickable. Normalize only NFKC and surrounding whitespace; require every API candidate to match exactly one enabled DOM option and reject duplicate IDs, missing API structure, extra conflicting DOM values, or label/ID disagreement with typed reason codes. A DOM-only snapshot may be logged for diagnosis but must return `review_required`, never automatic filling. Extend `FieldSchema` with an optional complete candidate tuple used only for product-category attributes; keep it empty for `shop`, `logistics`, and `freight` sources and retain `OptionSummary` for sanitized diagnostics.

Add `collect_dom_select_candidates(control)` to `taobao_listing.py`. It opens the control, collects visible enabled options, closes without changing the current value, then calls the bridge with candidates previously extracted from network JSON. For Tmall, reuse the existing `api_index`; for the other platforms, extend the existing response listener/schema capture instead of reading candidate truth from DOM.

- [ ] **Step 3: Resolve immediately before the existing DOM write**

```python
expected = excel_expected
if self.attribute_runtime is not None:
    candidates = await self.collect_verified_candidates(api_field, control)
    resolved = await self.attribute_runtime.resolve(AttributeRequest(
        platform_id="wxsph",
        category_leaf_id=category_leaf_id,
        field_id=field_id,
        field_label=label,
        candidates=candidates,
        excel_value=excel_expected,
        evidence=evidence,
        custom_allowed=False,
        schema_version=api_field.schema_version,
    ))
    expected = resolved.label
await self._select_exact(control, expected)
```

Do not catch `ReviewRequired` inside the form writer. It must propagate before the selection is changed.

- [ ] **Step 4: Add a review-interruption test**

Make the fake runtime raise `ReviewRequired` and assert the select retains its original value and the common Save button was never clicked.

- [ ] **Step 5: Run WeChat tests**

Run: `.venv/bin/python -m unittest tests.test_wxsph_form_listing -v`

Expected: PASS.

- [ ] **Step 6: Commit the vertical slice**

```bash
git add platform_candidate_source.py platform_schema.py taobao_listing.py wxsph_form_listing.py tests/test_platform_candidate_source.py tests/test_platform_schema.py tests/test_wxsph_form_listing.py
git commit -m "feat: resolve WeChat attributes from live candidates"
```

### Task 4: Route dynamic attributes for every platform

**Files:**
- Modify: `douyin_listing.py:1206-1290`
- Modify: `taobao_listing.py:1062-1135,1583-1660`
- Modify: `tmall_form_listing.py:1426-1565`
- Modify: `pdd_form_listing.py:310-540`
- Modify: `xhs_form_listing.py:743-860`
- Modify: `youzan_form_listing.py:804-930`
- Modify: `jd_form_listing.py:1231-1380`
- Modify: corresponding tests under `tests/`

- [ ] **Step 1: Add one failing candidate-routing test per platform**

For each platform, choose one category-dependent select and assert its platform ID, leaf category, source field ID, visible field name, full API candidate list, matching DOM list and Excel hint reach the fake runtime. Each fixture must fail closed when the API candidate list is unavailable or disagrees with DOM. Use these canonical examples:

```text
fxg: 裤长
tb: 裤长
tm: 厚薄
pdd: 风格
xhs: 裤型
yz: 厚薄
jd: 厚度
```

- [ ] **Step 2: Add one interruption safety test per platform**

Make the runtime raise `ReviewRequired`. Assert the target control and every later field remain untouched, and no save/publish method exists in or is called by the form writer.

- [ ] **Step 3: Route Douyin and Taobao fields**

In `DouyinListing.fill_attribute` and `TaobaoListing.fill_attribute`, read candidate IDs/labels from captured API JSON, cross-check them against the current select DOM, and call the runtime before the existing exact-match logic. Text inputs call the runtime only when API field metadata marks them `custom_allowed`; otherwise empty-candidate inputs require review.

- [ ] **Step 4: Route Tmall and PDD fields**

In `TmallFormListing.fill_attribute` and `PddFormListing.fill_attribute`, preserve existing wait/load logic, then resolve after candidates are stable and before clicking. Pass platform IDs `tm` and `pdd` exactly.

- [ ] **Step 5: Route XHS, Youzan and JD fields**

In their category-attribute methods, resolve each mapped dynamic field independently. Preserve current fixed operational values such as weight, stock, delivery mode and freight template outside the learning runtime because they are business rules, not inferred product attributes.

- [ ] **Step 6: Run all platform-focused tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_douyin_listing tests.test_taobao_listing tests.test_tmall_form_listing \
  tests.test_pdd_form_listing tests.test_wxsph_form_listing tests.test_xhs_form_listing \
  tests.test_youzan_form_listing tests.test_jd_form_listing -v
```

Expected: PASS, including the seven new routing and interruption tests.

- [ ] **Step 7: Commit all platform routing**

```bash
git add douyin_listing.py taobao_listing.py tmall_form_listing.py pdd_form_listing.py wxsph_form_listing.py xhs_form_listing.py youzan_form_listing.py jd_form_listing.py tests
git commit -m "feat: resolve dynamic attributes across platforms"
```

### Task 5: Implement review wait and restart-safe resume selection

**Files:**
- Create: `review_resume.py`
- Create: `tests/test_review_resume.py`
- Modify: `learning_client.py`

- [ ] **Step 1: Write failing resume-validation tests**

Cover matching product/run/mode, changed product hash, changed image hash, changed execution mode, already-consumed event, and a completed platform set. Only the matching case may return a resumable platform index.

- [ ] **Step 2: Define the resume decision**

```python
@dataclass(frozen=True)
class ResumeDecision:
    run_id: str
    platform_index: int
    platform_id: str
    review_id: str
    final_value_id: str
    snapshot_version: str


def validate_resume(checkpoint: RunCheckpoint, event: Mapping[str, Any], current_product_version: str, execution_mode: str) -> ResumeDecision:
    if checkpoint.product_version != current_product_version:
        raise ResumeRejected("product_version_changed")
    if checkpoint.execution_mode != execution_mode:
        raise ResumeRejected("execution_mode_changed")
    if event["run_id"] != checkpoint.run_id:
        raise ResumeRejected("run_id_mismatch")
    return ResumeDecision(
        checkpoint.run_id,
        checkpoint.current_index,
        checkpoint.platform_order[checkpoint.current_index],
        event["review_id"],
        event["final_value_id"],
        event["snapshot_version"],
    )
```

- [ ] **Step 3: Implement bounded long polling**

`wait_for_review` requests `/api/device/resume` with a 30-second server wait, uses no local sleep longer than 30 seconds, flushes the outbox between requests, and exits cleanly on cancellation. It must not acknowledge the event until the resumed checkpoint is stored locally.

- [ ] **Step 4: Add a restart command**

Support `review_resume.py --run-id <id>` and `--latest`. It loads the checkpoint, rereads the current product, verifies the product fingerprint and execution mode, then invokes the same resume function used by the in-process path. Add `--resume-run-id` to `kuaimai_erp.py` as the internal entry point used by this command; it must be mutually exclusive with starting a new run ID and must reuse the checkpoint's saved execution mode.

- [ ] **Step 5: Run resume tests**

Run: `.venv/bin/python -m unittest tests.test_review_resume -v`

Expected: PASS.

- [ ] **Step 6: Commit resume selection**

```bash
git add review_resume.py learning_client.py tests/test_review_resume.py
git commit -m "feat: validate reviewed run resumption"
```

### Task 6: Refactor all-platform execution into a checkpointed state machine

**Files:**
- Modify: `kuaimai_erp.py:4863-4937`
- Modify: `tests/test_kuaimai_erp.py:169-280`

- [ ] **Step 1: Write failing resume-order tests**

Simulate stages `base`, `douyin`, `taobao` succeeding, `tmall` raising `ReviewRequired`, review confirmation arriving, then success. Assert calls are exactly:

```python
["base", "douyin", "taobao", "tmall", "tmall", "pdd", "wxsph", "xhs", "youzan", "jd"]
```

Assert the first shared browser is closed before waiting, the resumed Tmall call receives a fresh shared session, and no earlier platform is repeated.

- [ ] **Step 2: Use registry order instead of a second hard-coded list**

Build commerce stages from `expand_platform_selection("all")`, filtering Douyin when its data is absent, and prepend `base` only in saving modes. Persist that exact tuple in the checkpoint before starting.

- [ ] **Step 3: Separate verified completion from function return**

After each platform returns, read its existing validation artifact through one `load_stage_readback(platform, stage_dir)` function. Record `verified=True` only when the platform-specific verifier reported success. If saving mode has no verified readback, raise `AutomationError` and stop.

- [ ] **Step 4: Catch only `ReviewRequired` as a resumable pause**

On review interruption: save `current_index`, `status="waiting_review"`, and `pending_review_id`; write sanitized `all-platform-result.json`; close the shared session; wait for confirmation; validate it; save `status="resume_pending"`; acknowledge the event; create a new shared session mapping; and retry the same index.

All other exceptions retain the existing failure behavior and must not be silently converted into reviews.

- [ ] **Step 5: Skip only verified stages on process restart**

When `--resume-run-id` loads a checkpoint, start at `current_index`. Before each later stage, skip it only if `completed_platforms(run_id)` contains that platform with `verified=True`. A merely submitted save or publish task is not completed.

- [ ] **Step 6: Add changed-candidate behavior**

The retried platform recollects live candidates. If the reviewed `value_id` is absent or the snapshot hash differs, create a new review and repeat the pause; never fall back to the old label.

- [ ] **Step 7: Run orchestrator tests**

Run: `.venv/bin/python -m unittest tests.test_kuaimai_erp.AsyncRegressionTests -v`

Expected: PASS for normal run, pause/resume, restart and readback-failure cases.

- [ ] **Step 8: Commit the state machine**

```bash
git add kuaimai_erp.py tests/test_kuaimai_erp.py
git commit -m "feat: resume all-platform runs from review checkpoints"
```

### Task 7: Allow dynamic attributes to omit Excel hints

**Files:**
- Modify: `douyin_data.py`, `taobao_data.py`, `tmall_data.py`, `pdd_data.py`, `wxsph_data.py`, `xhs_data.py`, `youzan_data.py`, `jd_data.py`
- Modify: corresponding data tests

- [ ] **Step 1: Add failing blank-hint tests**

For each data parser, blank one dynamic category attribute while keeping operational fields such as title, style code, price, category, size/SKU and stock valid. Assert parsing succeeds only when `learning_enabled=True` and preserves the blank as an empty hint.

- [ ] **Step 2: Add explicit parser mode**

Add `learning_enabled: bool = False` to every `parse_*_fields` function. Keep all existing required-field behavior when false. When true, only mapped dynamic category attributes may be blank; operational and compliance fields remain required.

- [ ] **Step 3: Pass parser mode from `read_product_data`**

Add `learning_enabled=False` to `read_product_data` and forward it from `main`. Do not infer learning mode from the presence of environment variables.

- [ ] **Step 4: Prove blank evidence triggers review rather than guesses**

For a blank material percentage and no OCR text, assert `ReviewRequired(reason_code="required_text_evidence_missing")`. For a blank pants length with consistent visual evidence and a mature rule, assert the live candidate can be auto-filled.

- [ ] **Step 5: Run all data and runtime tests**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_*_data.py' -v`

Run: `.venv/bin/python -m unittest tests.test_attribute_runtime tests.test_attribute_decision -v`

Expected: PASS.

- [ ] **Step 6: Commit optional Excel hints**

```bash
git add douyin_data.py taobao_data.py tmall_data.py pdd_data.py wxsph_data.py xhs_data.py youzan_data.py jd_data.py kuaimai_erp.py tests
git commit -m "feat: make learned attribute hints optional"
```

### Task 8: Add launcher recovery, documentation and end-to-end acceptance

**Files:**
- Modify: `run.command`
- Modify: `tests/test_launchers.py`
- Modify: `README.md`
- Create: `tests/test_learning_end_to_end.py`

- [ ] **Step 1: Add a failing launcher test**

Add menu option `11) 恢复待审核任务`. Assert choosing it runs `review_resume.py --latest` and never appends save/publish authorization flags by itself.

- [ ] **Step 2: Implement the launcher option**

The resume command must reuse the stored execution mode and reject any current command that attempts to change preview/save/publish scope. Print the run ID and stopped platform before opening Chrome.

- [ ] **Step 3: Create the four-product acceptance fixture**

Cover:

```text
裤子：图片明确到脚踝，候选含“长裤”
外套：字段集合不同，不能套用裤子规则
双颜色：两组颜色与两组图片顺序一一对应
新品类：未知字段映射进入审核，确认后从当前平台续跑
```

Use fake platform writers and fake cloud responses; assert prior completed platforms are called once, the stopped platform twice, and later platforms once.

- [ ] **Step 4: Add failure-path acceptance cases**

Cover Cloudflare offline/outbox recovery, duplicate confirmation, changed candidates, image hash change, readback mismatch, missing text evidence for material percentage, and 30-day original-image retention metadata.

- [ ] **Step 5: Update the operator runbook**

Document normal all-platform execution, what “等待审核” means, how automatic reopen works, how to recover after process exit, how to identify readback failure, and that confirmed review does not itself publish from the web.

- [ ] **Step 6: Run the entire suite**

Run: `.venv/bin/python -m unittest discover -s tests -v`

Expected: PASS.

Run: `cd cloudflare_review && npm test -- --run`

Expected: PASS.

- [ ] **Step 7: Perform a non-saving browser acceptance run**

Run the existing launcher in preview mode with learning enabled against a disposable product. Confirm candidate capture, review creation and browser close. Approve in the review page, confirm the product reopens at the stopped platform, and stop before any save action.

Expected: the run artifact contains the original checkpoint, review ID, new candidate snapshot and resumed platform index; no Save or Publish request is observed.

- [ ] **Step 8: Commit documentation and acceptance tests**

```bash
git add run.command tests/test_launchers.py tests/test_learning_end_to_end.py README.md
git commit -m "test: cover learned review and resume workflow"
```

## Phase 4 exit criteria

- Every supported platform routes mapped dynamic fields through the same candidate-constrained runtime.
- Legacy runs remain unchanged when learning is disabled.
- Review interruption occurs before the target control or save action is changed.
- After confirmation, the stopped platform reopens with fresh candidates and earlier platforms are not rerun.
- Saving modes continue only after verified readback.
- Blank Excel dynamic attributes are allowed only in learning mode; unsupported claims still require evidence.
- Pants, coat, two-color and new-category acceptance scenarios pass.
