# Kuaimai AI Learning Phase 3: Constrained Decision Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce reusable product-level visual facts and resolve platform attributes only from live candidate snapshots, with evidence gates, mature conditional rules, human overrides and review fallback.

**Architecture:** The Cloudflare service calls the configured multimodal model once per product image hash and stores structured facts. A deterministic Python decision gate revalidates every cloud response against the current platform snapshot before any DOM write. Confirmed, readback-verified outcomes update conditional rules; unverified logs never train rules.

**Tech Stack:** Python dataclasses and `unittest`, Cloudflare Worker TypeScript, D1, R2, model Responses API through `fetch`, JSON Schema structured output.

---

## File map

- Create `canonical_fields.py`: platform-label aliases and evidence policies.
- Create `attribute_decision.py`: deterministic local decision validation.
- Create `tests/test_canonical_fields.py`: exact/ambiguous mapping tests.
- Create `tests/test_attribute_decision.py`: candidate, evidence and rule-gate tests.
- Create `cloudflare_review/src/model.ts`: structured visual-fact model call and cache.
- Create `cloudflare_review/src/decision.ts`: precedence, case lookup and rule evaluation.
- Create `cloudflare_review/src/rules.ts`: condition signatures and rule promotion/demotion.
- Create `cloudflare_review/test/model.test.ts`: one-call and schema tests.
- Create `cloudflare_review/test/decision.test.ts`: constrained-decision tests.
- Create `cloudflare_review/test/rules.test.ts`: three-confirmation and 95% tests.
- Create `seed_import.py`: import verified Excel/readback pairs as seed cases.
- Create `tests/test_seed_import.py`: reject unverifiable logs and secrets.
- Modify `learning_client.py`: analysis and decision endpoints.

### Task 1: Define canonical fields and conservative evidence policies

**Files:**
- Create: `canonical_fields.py`
- Create: `tests/test_canonical_fields.py`

- [ ] **Step 1: Write failing exact-mapping tests**

```python
import unittest

from canonical_fields import EvidenceKind, FieldMappingError, map_platform_field, policy_for


class CanonicalFieldTests(unittest.TestCase):
    def test_platform_alias_maps_to_pants_length(self):
        self.assertEqual(map_platform_field("wxsph", "裤长"), "pants_length")
        self.assertEqual(map_platform_field("jd", "裤长"), "pants_length")

    def test_unknown_field_requires_review(self):
        with self.assertRaises(FieldMappingError):
            map_platform_field("jd", "平台新增神秘字段")

    def test_material_percentage_requires_text(self):
        self.assertEqual(policy_for("material_percentage").required_evidence, (EvidenceKind.TEXT,))
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `.venv/bin/python -m unittest tests.test_canonical_fields -v`

Expected: FAIL importing `canonical_fields`.

- [ ] **Step 3: Implement the registry without value rules**

```python
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Tuple


class EvidenceKind(str, Enum):
    VISUAL = "visual"
    TEXT = "text"


@dataclass(frozen=True)
class FieldPolicy:
    canonical_name: str
    required_evidence: Tuple[EvidenceKind, ...]
    allow_rule: bool


ALIASES: Dict[Tuple[str, str], str] = {
    ("wxsph", "裤长"): "pants_length",
    ("jd", "裤长"): "pants_length",
    ("yz", "厚薄"): "thickness",
    ("jd", "厚度"): "thickness",
    ("wxsph", "面料材质成分含量"): "material_percentage",
    ("wxsph", "材质成分"): "material_composition",
    ("jd", "颜色"): "color",
}

POLICIES = {
    "pants_length": FieldPolicy("pants_length", (EvidenceKind.VISUAL,), True),
    "thickness": FieldPolicy("thickness", (EvidenceKind.VISUAL,), True),
    "color": FieldPolicy("color", (EvidenceKind.VISUAL,), True),
    "material_composition": FieldPolicy("material_composition", (EvidenceKind.TEXT,), True),
    "material_percentage": FieldPolicy("material_percentage", (EvidenceKind.TEXT,), False),
}
```

`map_platform_field` must normalize only NFKC, surrounding whitespace and required-marker punctuation. It must not use fuzzy matching. Missing or duplicate mappings raise `FieldMappingError` so a field-mapping review can be created.

- [ ] **Step 4: Run mapping tests**

Run: `.venv/bin/python -m unittest tests.test_canonical_fields -v`

Expected: PASS.

- [ ] **Step 5: Commit canonical fields**

```bash
git add canonical_fields.py tests/test_canonical_fields.py
git commit -m "feat: define canonical attribute policies"
```

### Task 2: Implement local candidate and evidence validation

**Files:**
- Create: `attribute_decision.py`
- Create: `tests/test_attribute_decision.py`

- [ ] **Step 1: Write failing gate tests**

```python
import unittest

from attribute_decision import DecisionInput, DecisionStatus, validate_decision
from learning_models import CandidateSnapshot, CandidateValue


SNAPSHOT = CandidateSnapshot(
    platform_id="wxsph",
    category_leaf_id="pants",
    field_id="pants-length",
    field_label="裤长",
    values=(CandidateValue("short", "短裤"), CandidateValue("long", "长裤")),
    schema_version="schema-1",
    custom_allowed=False,
)


class AttributeDecisionTests(unittest.TestCase):
    def test_value_must_exist_exactly_once(self):
        result = validate_decision(DecisionInput(
            "pants_length", "missing", ("visual",), source="mature_rule", mature_rule=True
        ), SNAPSHOT)
        self.assertEqual(result.status, DecisionStatus.REVIEW_REQUIRED)
        self.assertEqual(result.reason_code, "candidate_missing")

    def test_text_only_field_rejects_visual_guess(self):
        result = validate_decision(DecisionInput(
            "material_percentage", "long", ("visual",), source="mature_rule", mature_rule=True
        ), SNAPSHOT)
        self.assertEqual(result.reason_code, "required_text_evidence_missing")

    def test_mature_rule_still_requires_live_candidate(self):
        result = validate_decision(DecisionInput(
            "pants_length", "long", ("visual",), source="mature_rule", mature_rule=True
        ), SNAPSHOT)
        self.assertEqual(result.status, DecisionStatus.AUTO_FILL_READY)

    def test_model_suggestion_needs_calibrated_support(self):
        weak = validate_decision(DecisionInput(
            "pants_length", "long", ("visual",), source="constrained_model",
            support_count=2, calibrated_acceptance_rate=1.0,
        ), SNAPSHOT)
        strong = validate_decision(DecisionInput(
            "pants_length", "long", ("visual",), source="constrained_model",
            support_count=3, calibrated_acceptance_rate=0.95,
        ), SNAPSHOT)
        self.assertEqual(weak.reason_code, "confidence_gate_not_met")
        self.assertEqual(strong.status, DecisionStatus.AUTO_FILL_READY)
```

- [ ] **Step 2: Run the test and verify failure**

Run: `.venv/bin/python -m unittest tests.test_attribute_decision -v`

Expected: FAIL importing `attribute_decision`.

- [ ] **Step 3: Implement deterministic validation**

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class DecisionStatus(str, Enum):
    AUTO_FILL_READY = "auto_fill_ready"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True)
class DecisionInput:
    canonical_field: str
    proposed_value_id: str
    evidence_kinds: Tuple[str, ...]
    source: str
    mature_rule: bool = False
    support_count: int = 0
    calibrated_acceptance_rate: float = 0.0
    has_conflict: bool = False
    expected_snapshot_version: Optional[str] = None


@dataclass(frozen=True)
class ValidatedDecision:
    status: DecisionStatus
    value_id: Optional[str]
    value_label: Optional[str]
    reason_code: str
    snapshot_version: str
```

`validate_decision` must reject conflicts, missing required evidence, zero or duplicate candidate IDs, schema-version mismatch, and free text when `snapshot.custom_allowed` is false. After those hard gates, source-specific gates are mandatory: `human_override` and `explicit_text` may pass with their required evidence; `mature_rule` requires `mature_rule=True`; `constrained_model` requires at least three comparable verified outcomes and `calibrated_acceptance_rate >= 0.95`. Model self-reported confidence alone never passes the gate. It returns `AUTO_FILL_READY` only when every gate passes; otherwise it returns `REVIEW_REQUIRED` without a writeable value.

- [ ] **Step 4: Add duplicate-candidate and changed-snapshot tests**

Construct a snapshot containing the same `value_id` twice and assert `candidate_ambiguous`. Pass an expected snapshot version different from `snapshot.snapshot_version` and assert `snapshot_changed`.

- [ ] **Step 5: Run the decision tests**

Run: `.venv/bin/python -m unittest tests.test_attribute_decision -v`

Expected: PASS.

- [ ] **Step 6: Commit the local gate**

```bash
git add attribute_decision.py tests/test_attribute_decision.py
git commit -m "feat: constrain AI attribute decisions"
```

### Task 3: Add one-call product visual analysis and cache

**Files:**
- Create: `cloudflare_review/src/model.ts`
- Create: `cloudflare_review/test/model.test.ts`
- Modify: `cloudflare_review/src/index.ts`

- [ ] **Step 1: Write failing cache and schema tests**

Mock the model endpoint and request analysis twice with the same `product_version`. Assert one upstream call and two identical responses. Assert malformed model JSON returns `review_required` with `reason_code="model_output_invalid"` rather than storing facts.

- [ ] **Step 2: Define the visual-fact JSON schema**

```ts
export interface VisualFacts {
  garment_type: string | null;
  visible_colors: string[];
  length_landmark: "above_knee" | "knee" | "calf" | "ankle" | "unknown";
  silhouette: "slim" | "straight" | "loose" | "unknown";
  thickness_evidence: "thin" | "regular" | "thick" | "insufficient";
  image_consistency: Record<string, string>;
  evidence_asset_ids: string[];
}
```

Reject extra keys, unknown asset IDs, unsupported enum values and empty evidence for non-unknown facts.

- [ ] **Step 3: Implement cached analysis**

`analyzeProduct(env, productVersion)` must first query `visual_facts`. On a miss, load authorized R2 objects for that product, call the configured Responses API once with all images, parse the structured output, validate it, and insert it using `INSERT OR IGNORE`. A concurrent duplicate reads the winning row after the insert.

The prompt must say: report visible facts only; never infer material percentages, brand claims or functionality; use `unknown`/`insufficient` when evidence is absent; cite asset IDs.

- [ ] **Step 4: Run model tests**

Run: `cd cloudflare_review && npm test -- --run test/model.test.ts`

Expected: PASS, including one upstream call for a cache hit pair.

- [ ] **Step 5: Commit visual analysis**

```bash
git add cloudflare_review/src cloudflare_review/test
git commit -m "feat: cache structured product vision facts"
```

### Task 4: Implement precedence and constrained model choice

**Files:**
- Create: `cloudflare_review/src/decision.ts`
- Create: `cloudflare_review/test/decision.test.ts`
- Modify: `cloudflare_review/src/index.ts`

- [ ] **Step 1: Write failing precedence tests**

Cover this exact order: product override, explicit text evidence, mature rule, constrained model with similar cases, and review fallback. Assert a lower source can never overwrite a higher source.

- [ ] **Step 2: Implement decision input validation**

Accept `product_version`, `platform_id`, `category_leaf_id`, `field_id`, `canonical_field`, and `snapshot_version`. Load the snapshot from D1; do not accept candidate labels supplied only by the client request.

- [ ] **Step 3: Implement precedence**

```ts
export async function decideAttribute(env: Env, input: DecisionRequest): Promise<DecisionResponse> {
  const snapshot = await loadSnapshot(env.DB, input.snapshot_version);
  const override = await findProductOverride(env.DB, input, snapshot);
  if (override) return ready("human_override", override, snapshot);
  const explicit = await findExplicitTextValue(env.DB, input, snapshot);
  if (explicit) return ready("explicit_text", explicit, snapshot);
  const rule = await findMatureRule(env.DB, input, snapshot);
  if (rule) return ready("mature_rule", rule, snapshot);
  const suggestion = await constrainedModelChoice(env, input, snapshot);
  return suggestion ?? reviewRequired("insufficient_evidence", snapshot);
}
```

`ready` must verify the selected `value_id` exists exactly once in the loaded snapshot. The constrained model receives value IDs and labels, but structured output permits only one of those IDs or `review_required`. Its response must also carry `support_count` and a server-computed acceptance rate derived from verified comparable cases. Return `review_required` unless support is at least three and that rate is at least 95%; never use the model's own confidence number as the acceptance rate.

- [ ] **Step 4: Add conflict and new-field tests**

Assert visual/text conflict returns review, an unmapped canonical field returns `field_mapping_required`, and a model output outside the candidate enum returns `model_candidate_invalid`.

- [ ] **Step 5: Run cloud decision tests**

Run: `cd cloudflare_review && npm test -- --run test/decision.test.ts`

Expected: PASS.

- [ ] **Step 6: Commit decision orchestration**

```bash
git add cloudflare_review/src cloudflare_review/test
git commit -m "feat: add evidence-aware attribute decisions"
```

### Task 5: Implement condition signatures and rule lifecycle

**Files:**
- Create: `cloudflare_review/src/rules.ts`
- Create: `cloudflare_review/test/rules.test.ts`

- [ ] **Step 1: Write failing rule tests**

Assert two confirmations remain `observing`; three consecutive confirmations with 100% acceptance become `active`; three confirmations with one rejection do not meet 95%; a correction after activation demotes to `observing`; and schema-version change makes the rule ineligible.

- [ ] **Step 2: Define deterministic condition signatures**

```ts
export async function conditionSignature(input: RuleConditions): Promise<string> {
  const canonical = JSON.stringify({
    garment_type: input.garment_type ?? null,
    length_landmark: input.length_landmark ?? null,
    silhouette: input.silhouette ?? null,
    text_tokens: [...input.text_tokens].sort(),
  });
  return await sha256Hex(canonical);
}
```

Do not include style code or title in the signature; those identify a product, not a reusable condition. Do not include unrestricted model prose.

- [ ] **Step 3: Implement promotion and demotion**

Update rules only from `persisted_readbacks.verified=1` joined to a completed `review_action`. Set `active` only when `consecutive_confirmations >= 3`, `accepted / total >= 0.95`, snapshot candidate still exists, and schema version matches. Any correction, missing candidate or schema change sets `status='observing'` and resets consecutive confirmations.

- [ ] **Step 4: Run rule tests**

Run: `cd cloudflare_review && npm test -- --run test/rules.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit rule learning**

```bash
git add cloudflare_review/src cloudflare_review/test
git commit -m "feat: learn conditional attribute rules"
```

### Task 6: Add safe seed import from current verified data

**Files:**
- Create: `seed_import.py`
- Create: `tests/test_seed_import.py`
- Modify: `learning_client.py`

- [ ] **Step 1: Write failing import tests**

Use temporary run directories containing `input-summary.json`, a platform `*-before-save.json`, and `*-after-save-validation.json`. Assert a verified validation imports one seed case, missing validation imports none, and files containing credential markers are rejected before upload.

- [ ] **Step 2: Implement verified-pair discovery**

```python
def discover_verified_seeds(run_dir: Path) -> Tuple[SeedCase, ...]:
    seeds = []
    for validation_path in sorted(run_dir.glob("*/**/*-after-save-validation.json")):
        validation = read_safe_json(validation_path)
        if validation.get("status") not in ("success", "verified"):
            continue
        before = matching_before_save(validation_path)
        if before is None:
            continue
        seeds.extend(seed_cases_from_pair(before, validation_path))
    return tuple(seeds)
```

`read_safe_json` must reject keys matching `password`, `cookie`, `authorization`, `token`, `secret`, or `key` after NFKC normalization. Seed cases carry `source="verified_import"` and never increment consecutive human confirmations.

- [ ] **Step 3: Add a dry-run CLI**

Support:

```text
python seed_import.py --runs-dir output/kuaimai/runs --dry-run
python seed_import.py --runs-dir output/kuaimai/runs --send
```

`--dry-run` prints counts and sanitized platform/field names only. `--send` requires `KUAIMAI_LEARNING_API_URL` and `KUAIMAI_LEARNING_DEVICE_TOKEN` from the environment.

- [ ] **Step 4: Run seed-import tests**

Run: `.venv/bin/python -m unittest tests.test_seed_import -v`

Expected: PASS.

- [ ] **Step 5: Commit seed import**

```bash
git add seed_import.py learning_client.py tests/test_seed_import.py
git commit -m "feat: import verified attribute seed cases"
```

## Phase 3 exit criteria

- One product image set causes at most one primary vision call per product version.
- Visual facts contain evidence asset IDs and no unsupported material claims.
- Every automatic value is revalidated against the live candidate snapshot locally.
- Evidence conflicts, missing text evidence and unknown fields create reviews.
- Rules activate only after three consecutive confirmations and at least 95% acceptance.
- Only verified readbacks affect rule statistics.
- Existing verified runs can be previewed and imported without uploading secrets.
