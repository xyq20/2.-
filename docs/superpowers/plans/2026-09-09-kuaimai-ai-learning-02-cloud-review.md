# Kuaimai AI Learning Phase 2: Cloud Review Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a Cloudflare Worker with D1, R2, username/password authentication, review-task locking, an Apple-style review page, and device synchronization APIs.

**Architecture:** A native TypeScript Worker keeps the first deployment small and stable. D1 is authoritative for users, tasks, decisions and event idempotency; R2 stores private originals and compressed evidence thumbnails. The browser UI and device API share one Worker but use separate session-cookie and device-token authentication paths.

**Tech Stack:** Cloudflare Workers, TypeScript, Wrangler, D1, R2, Web Crypto PBKDF2, Vitest Workers pool, plain HTML/CSS/JavaScript.

---

## File map

- Create `cloudflare_review/package.json`: Worker tooling and scripts.
- Create `cloudflare_review/package-lock.json`: resolved dependency versions.
- Create `cloudflare_review/tsconfig.json`: strict Worker TypeScript settings.
- Create `cloudflare_review/vitest.config.ts`: Workers-pool test bindings.
- Create `cloudflare_review/wrangler.jsonc`: Worker, D1 and R2 bindings.
- Create `cloudflare_review/migrations/0001_initial.sql`: authoritative cloud schema.
- Create `cloudflare_review/src/types.ts`: bindings, roles and task types.
- Create `cloudflare_review/src/auth.ts`: PBKDF2 password verification and sessions.
- Create `cloudflare_review/src/reviews.ts`: review lifecycle and optimistic locking.
- Create `cloudflare_review/src/device-events.ts`: idempotent local-runner event ingestion.
- Create `cloudflare_review/src/assets.ts`: authenticated R2 access and deletion.
- Create `cloudflare_review/src/ui.ts`: approved single-task Apple-style page.
- Create `cloudflare_review/src/index.ts`: router and response policy.
- Create `cloudflare_review/test/*.test.ts`: Worker integration tests.
- Create `cloudflare_review/README.md`: local setup, secrets, migration, deployment and rollback.

### Task 1: Scaffold a testable Worker

**Files:**
- Create: `cloudflare_review/package.json`
- Create: `cloudflare_review/tsconfig.json`
- Create: `cloudflare_review/wrangler.jsonc`
- Create: `cloudflare_review/src/types.ts`
- Create: `cloudflare_review/src/index.ts`
- Create: `cloudflare_review/test/health.test.ts`

- [ ] **Step 1: Write the failing health test**

```ts
import { env, createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker from "../src/index";

describe("health", () => {
  it("returns a stable service marker", async () => {
    const context = createExecutionContext();
    const response = await worker.fetch(new Request("https://local.test/health"), env, context);
    await waitOnExecutionContext(context);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ ok: true, service: "kuaimai-review" });
  });
});
```

- [ ] **Step 2: Add package and Worker configuration**

Run these commands so `package-lock.json` pins the versions that actually pass the Worker tests:

```bash
cd cloudflare_review
npm init -y
npm install --save-dev wrangler typescript vitest @cloudflare/vitest-pool-workers @cloudflare/workers-types
npm pkg set type=module private=true
npm pkg set scripts.test='vitest' scripts.dev='wrangler dev' scripts.deploy='wrangler deploy' scripts.db:migrate:local='wrangler d1 migrations apply kuaimai-review --local' scripts.db:migrate:remote='wrangler d1 migrations apply kuaimai-review --remote'
```

Create the resources and let Wrangler write the real D1 identifier instead of inventing one:

```bash
npx wrangler d1 create kuaimai-review --binding DB --update-config
npx wrangler r2 bucket create kuaimai-review-assets
```

Keep the D1 block written by Wrangler and add these exact R2 and scheduled-cleanup blocks to `wrangler.jsonc`:

```jsonc
{
  "$schema": "node_modules/wrangler/config-schema.json",
  "name": "kuaimai-review",
  "main": "src/index.ts",
  "compatibility_date": "2026-09-09",
  "r2_buckets": [{ "binding": "ASSETS", "bucket_name": "kuaimai-review-assets" }],
  "triggers": { "crons": ["0 3 * * *"] }
}
```

Do not put account IDs or secrets in the config or test fixtures. Set `AI_MODEL`, `MODEL_API_URL`, `SESSION_SECRET`, `DEVICE_TOKEN`, and `MODEL_API_KEY` with `wrangler secret put`; document the selected model endpoint in the deployment record rather than hard-coding one provider in source.

Use this test configuration:

```ts
import { defineWorkersConfig } from "@cloudflare/vitest-pool-workers/config";

export default defineWorkersConfig({
  test: {
    poolOptions: {
      workers: { wrangler: { configPath: "./wrangler.jsonc" } },
    },
  },
});
```

- [ ] **Step 3: Implement the health route**

```ts
export interface Env {
  DB: D1Database;
  ASSETS: R2Bucket;
  SESSION_SECRET: string;
  DEVICE_TOKEN: string;
  MODEL_API_KEY: string;
  MODEL_API_URL: string;
  AI_MODEL: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return Response.json({ ok: true, service: "kuaimai-review" });
    }
    return Response.json({ error: "not_found" }, { status: 404 });
  },
} satisfies ExportedHandler<Env>;
```

- [ ] **Step 4: Run the health test**

Run: `cd cloudflare_review && npm test -- --run test/health.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit the scaffold**

```bash
git add cloudflare_review
git commit -m "feat: scaffold cloud review worker"
```

### Task 2: Create D1 migrations and idempotent device ingestion

**Files:**
- Create: `cloudflare_review/migrations/0001_initial.sql`
- Create: `cloudflare_review/src/device-events.ts`
- Create: `cloudflare_review/test/device-events.test.ts`
- Modify: `cloudflare_review/src/index.ts`

- [ ] **Step 1: Write failing ingestion tests**

Test that the same `idempotency_key` posted twice returns the same `event_id`, stores one row, and returns `401` when `Authorization: Device <token>` is missing.

```ts
const body = { idempotency_key: "run:pdd:review", event_type: "review.created", payload: { run_id: "run" } };
const first = await postDeviceEvent(body);
const second = await postDeviceEvent(body);
expect(first.status).toBe(201);
expect(second.status).toBe(409);
expect((await first.json()).event_id).toBe((await second.json()).event_id);
```

- [ ] **Step 2: Add the D1 schema**

The migration must create `users`, `sessions`, `device_events`, `products`, `assets`, `visual_facts`, `text_facts`, `platform_fields`, `option_snapshots`, `attribute_decisions`, `conditional_rules`, `review_tasks`, `review_actions`, `run_checkpoints`, `stage_results`, and `persisted_readbacks` with foreign keys and indexes for `review_tasks(status, created_at)` and `conditional_rules(platform_id, category_leaf_id, canonical_field, condition_signature)`.

Use `UNIQUE(idempotency_key)` on `device_events`, `review_tasks`, `stage_results` and `persisted_readbacks`. Store JSON as `TEXT`; store UTC timestamps as ISO-8601 text.

```sql
PRAGMA foreign_keys = ON;
CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_salt TEXT NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','operator')), active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL, revoked_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE device_events (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, processed_at TEXT);
CREATE TABLE products (product_version TEXT PRIMARY KEY, style_code TEXT NOT NULL, title TEXT NOT NULL, category_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE assets (id TEXT PRIMARY KEY, product_version TEXT NOT NULL REFERENCES products(product_version) ON DELETE CASCADE, r2_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('original','learning_thumbnail')), content_type TEXT NOT NULL, delete_after TEXT, created_at TEXT NOT NULL, UNIQUE(product_version,sha256,kind));
CREATE TABLE visual_facts (product_version TEXT PRIMARY KEY REFERENCES products(product_version) ON DELETE CASCADE, payload_json TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE text_facts (id TEXT PRIMARY KEY, product_version TEXT NOT NULL REFERENCES products(product_version) ON DELETE CASCADE, source TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE platform_fields (id TEXT PRIMARY KEY, platform_id TEXT NOT NULL, category_leaf_id TEXT NOT NULL, source_field_id TEXT NOT NULL, label TEXT NOT NULL, canonical_field TEXT, control_type TEXT NOT NULL, custom_allowed INTEGER NOT NULL DEFAULT 0, schema_version TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(platform_id,category_leaf_id,source_field_id,schema_version));
CREATE TABLE option_snapshots (snapshot_version TEXT PRIMARY KEY, platform_id TEXT NOT NULL, category_leaf_id TEXT NOT NULL, field_id TEXT NOT NULL, field_label TEXT NOT NULL, schema_version TEXT NOT NULL, options_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE attribute_decisions (id TEXT PRIMARY KEY, product_version TEXT NOT NULL REFERENCES products(product_version) ON DELETE CASCADE, platform_id TEXT NOT NULL, category_leaf_id TEXT NOT NULL, field_id TEXT NOT NULL, canonical_field TEXT, snapshot_version TEXT NOT NULL REFERENCES option_snapshots(snapshot_version), proposed_value_id TEXT, final_value_id TEXT, source TEXT NOT NULL, status TEXT NOT NULL, reason_code TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE conditional_rules (id TEXT PRIMARY KEY, platform_id TEXT NOT NULL, category_leaf_id TEXT NOT NULL, canonical_field TEXT NOT NULL, condition_signature TEXT NOT NULL, target_value_id TEXT NOT NULL, schema_version TEXT NOT NULL, consecutive_confirmations INTEGER NOT NULL DEFAULT 0, accepted_count INTEGER NOT NULL DEFAULT 0, total_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL CHECK(status IN ('observing','active','disabled')), updated_at TEXT NOT NULL, UNIQUE(platform_id,category_leaf_id,canonical_field,condition_signature));
CREATE TABLE review_tasks (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, device_id TEXT NOT NULL, product_version TEXT NOT NULL REFERENCES products(product_version) ON DELETE CASCADE, platform_id TEXT NOT NULL, category_leaf_id TEXT NOT NULL, field_id TEXT NOT NULL, field_label TEXT NOT NULL, canonical_field TEXT, snapshot_version TEXT NOT NULL REFERENCES option_snapshots(snapshot_version), suggested_value_id TEXT, status TEXT NOT NULL CHECK(status IN ('pending','claimed','confirmed','resume_ready','consumed','invalidated')), reason_code TEXT NOT NULL, claimed_by TEXT REFERENCES users(id), lease_until TEXT, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE review_actions (id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES review_tasks(id) ON DELETE CASCADE, user_id TEXT NOT NULL REFERENCES users(id), suggested_value_id TEXT, final_value_id TEXT NOT NULL, correction_reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE run_checkpoints (run_id TEXT PRIMARY KEY, product_version TEXT NOT NULL REFERENCES products(product_version), device_id TEXT NOT NULL, execution_mode TEXT NOT NULL, platform_order_json TEXT NOT NULL, current_index INTEGER NOT NULL, status TEXT NOT NULL, pending_review_id TEXT REFERENCES review_tasks(id), updated_at TEXT NOT NULL);
CREATE TABLE stage_results (idempotency_key TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES run_checkpoints(run_id) ON DELETE CASCADE, platform_id TEXT NOT NULL, status TEXT NOT NULL, expected_json TEXT NOT NULL, readback_json TEXT NOT NULL, verified INTEGER NOT NULL, updated_at TEXT NOT NULL, UNIQUE(run_id,platform_id));
CREATE TABLE persisted_readbacks (idempotency_key TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES run_checkpoints(run_id) ON DELETE CASCADE, product_version TEXT NOT NULL REFERENCES products(product_version), platform_id TEXT NOT NULL, field_id TEXT NOT NULL, snapshot_version TEXT REFERENCES option_snapshots(snapshot_version), actual_value_id TEXT, actual_label TEXT, verified INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX review_queue_idx ON review_tasks(status,created_at);
CREATE INDEX rule_lookup_idx ON conditional_rules(platform_id,category_leaf_id,canonical_field,condition_signature);
CREATE INDEX asset_expiry_idx ON assets(kind,delete_after);
```

- [ ] **Step 3: Implement device authentication and event dispatch**

```ts
export async function ingestDeviceEvent(request: Request, env: Env): Promise<Response> {
  if (!constantTimeEqual(request.headers.get("authorization") ?? "", `Device ${env.DEVICE_TOKEN}`)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const event = await request.json<DeviceEventInput>();
  validateDeviceEvent(event);
  const id = crypto.randomUUID();
  const result = await env.DB.prepare(
    "INSERT OR IGNORE INTO device_events(id, idempotency_key, event_type, payload_json, created_at) VALUES(?,?,?,?,?)"
  ).bind(id, event.idempotency_key, event.event_type, JSON.stringify(event.payload), new Date().toISOString()).run();
  if (!result.meta.changes) {
    const existing = await env.DB.prepare("SELECT id FROM device_events WHERE idempotency_key=?").bind(event.idempotency_key).first<{id:string}>();
    return Response.json({ event_id: existing!.id }, { status: 409 });
  }
  await dispatchDeviceEvent(env, id, event);
  return Response.json({ event_id: id }, { status: 201 });
}
```

Allow only an explicit event-type union; reject unknown types with `400`.

- [ ] **Step 4: Run migration and ingestion tests**

Run: `cd cloudflare_review && npm run db:migrate:local && npm test -- --run test/device-events.test.ts`

Expected: PASS with one row after duplicate delivery.

- [ ] **Step 5: Commit the cloud schema**

```bash
git add cloudflare_review/migrations cloudflare_review/src cloudflare_review/test
git commit -m "feat: add D1 event ingestion"
```

### Task 3: Implement username/password sessions and roles

**Files:**
- Create: `cloudflare_review/src/auth.ts`
- Create: `cloudflare_review/test/auth.test.ts`
- Modify: `cloudflare_review/src/index.ts`

- [ ] **Step 1: Write failing authentication tests**

Cover correct password, wrong password, expired session, operator blocked from `/api/admin/*`, and admin allowed. Assert cookies are `HttpOnly`, `Secure`, `SameSite=Strict`, and have an eight-hour maximum age.

- [ ] **Step 2: Implement PBKDF2 password helpers**

```ts
export async function derivePassword(password: string, salt: Uint8Array): Promise<string> {
  const material = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations: 310_000 },
    material,
    256,
  );
  return bytesToBase64(new Uint8Array(bits));
}
```

Store `salt`, derived hash, role, active flag and timestamps. Never log request bodies on login routes.

- [ ] **Step 3: Implement opaque sessions**

Generate a random 32-byte token, store only its SHA-256 digest in D1, and send the raw token in `km_session`. Look up active, non-expired sessions and join the user role for every browser API call.

- [ ] **Step 4: Add a one-time admin bootstrap command**

Create `cloudflare_review/scripts/hash-password.mjs` that reads the password from stdin, emits only `salt` and `hash`, and never accepts the password as a command-line argument. Document inserting the first admin using Wrangler D1 execute with those derived values.

- [ ] **Step 5: Run auth tests**

Run: `cd cloudflare_review && npm test -- --run test/auth.test.ts`

Expected: PASS.

- [ ] **Step 6: Commit authentication**

```bash
git add cloudflare_review/src cloudflare_review/test cloudflare_review/scripts
git commit -m "feat: add review user authentication"
```

### Task 4: Implement review locking, confirmation and resume delivery

**Files:**
- Create: `cloudflare_review/src/reviews.ts`
- Create: `cloudflare_review/test/reviews.test.ts`
- Modify: `cloudflare_review/src/index.ts`

- [ ] **Step 1: Write failing lifecycle tests**

Test `pending -> claimed -> confirmed -> resume_ready`, ten-minute lease expiry, lease renewal, version mismatch returning `409`, non-candidate final values returning `422`, and skip preserving `pending` without creating a resume event.

- [ ] **Step 2: Implement an atomic claim**

```sql
UPDATE review_tasks
SET status='claimed', claimed_by=?, lease_until=?, version=version+1
WHERE id=?
  AND status IN ('pending','claimed')
  AND (claimed_by IS NULL OR claimed_by=? OR lease_until < ?)
  AND version=?;
```

Return `409 task_locked` when no row changes. Renewal may only be performed by the current claimant.

- [ ] **Step 3: Implement confirmation validation**

Load the stored option snapshot inside the same D1 batch. Require `final_value_id` to exist exactly once, insert `review_actions`, update the task version/status, and create a `resume_ready` device event with a deterministic idempotency key.

- [ ] **Step 4: Implement the device resume endpoint**

`GET /api/device/resume?device_id=<id>` returns unconsumed `resume_ready` events. `POST /api/device/resume/<id>/ack` marks one event consumed only after the local runner reports that it has persisted the resume checkpoint.

- [ ] **Step 5: Run review tests**

Run: `cd cloudflare_review && npm test -- --run test/reviews.test.ts`

Expected: PASS.

- [ ] **Step 6: Commit review lifecycle**

```bash
git add cloudflare_review/src cloudflare_review/test
git commit -m "feat: add locked review workflow"
```

### Task 5: Add private R2 evidence and deletion

**Files:**
- Create: `cloudflare_review/src/assets.ts`
- Create: `cloudflare_review/test/assets.test.ts`
- Modify: `cloudflare_review/src/index.ts`

- [ ] **Step 1: Write failing access and deletion tests**

Assert unauthenticated reads return `401`, an operator can read an asset belonging to an assigned task, an unrelated operator gets `403`, duplicate device uploads of the same SHA-256 return the same asset ID, and an admin delete removes both R2 objects and D1 learning rows for the product.

- [ ] **Step 2: Implement authenticated streaming**

Read R2 objects only through `/api/assets/:id`, verify task/user authorization first, then return the body with a private cache policy:

```ts
return new Response(object.body, {
  headers: {
    "content-type": row.content_type,
    "cache-control": "private, max-age=300",
    "x-content-type-options": "nosniff",
  },
});
```

- [ ] **Step 3: Implement product deletion and retention metadata**

Add `PUT /api/device/assets/:sha256` using device authentication. Require `x-product-version`, `content-type`, `x-asset-kind`, and a body-size limit; recompute the body SHA-256 before writing. Use R2 key `products/<product_version>/<sha256>/<kind>` and `INSERT OR IGNORE` so retries return the existing asset ID.

Store `delete_after` for original images and `kind` as `original` or `learning_thumbnail`. Set original expiry to upload time plus 30 days. Admin deletion must enumerate the product's R2 keys, delete them, then delete D1 rows in a foreign-key-safe transaction. Export `cleanupExpiredOriginals(env, now)` from `assets.ts`; it may delete only expired `original` objects and rows, in bounded batches of 100. Add `scheduled()` to `src/index.ts` so the configured daily cron calls this function. Thumbnails remain until explicit deletion.

- [ ] **Step 4: Run asset tests**

Run: `cd cloudflare_review && npm test -- --run test/assets.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit R2 handling**

```bash
git add cloudflare_review/src cloudflare_review/test
git commit -m "feat: secure review evidence assets"
```

### Task 6: Build the approved Apple-style review page

**Files:**
- Create: `cloudflare_review/src/ui.ts`
- Create: `cloudflare_review/test/ui.test.ts`
- Modify: `cloudflare_review/src/index.ts`

- [ ] **Step 1: Write failing HTML contract tests**

Assert the page includes the independent labels `字段名称`, `AI 建议`, candidate buttons carrying `data-value-id`, product image, `查看判断依据与相似商品`, `暂时跳过`, and `确认并继续`. Assert it does not render device tokens, raw evidence JSON, admin controls or a wide permanent sidebar.

- [ ] **Step 2: Implement the page shell**

Render a centered, max-width single-task decision card with system fonts, restrained borders, a product-image region, one field per page, candidate cards, progressive evidence disclosure and primary confirmation. Use semantic buttons, visible keyboard focus, and a mobile single-column breakpoint.

- [ ] **Step 3: Implement browser behavior**

The page must:

```js
await fetch(`/api/reviews/${taskId}/claim`, { method: "POST", headers: jsonHeaders(), body: JSON.stringify({ version }) });
await fetch(`/api/reviews/${taskId}/confirm`, { method: "POST", headers: jsonHeaders(), body: JSON.stringify({ version, final_value_id, correction_reason }) });
```

Disable the confirmation button during submission, show `409` as “任务已被其他运营更新”, and move to the next pending task only after the server confirms success.

- [ ] **Step 4: Run UI tests and inspect locally**

Run: `cd cloudflare_review && npm test -- --run test/ui.test.ts`

Run: `cd cloudflare_review && npm run dev`

Expected: automated tests PASS; manual inspection at the Wrangler URL matches the approved single-task card on desktop and mobile widths.

- [ ] **Step 5: Commit the UI**

```bash
git add cloudflare_review/src cloudflare_review/test
git commit -m "feat: add operator review interface"
```

### Task 7: Document and verify deployment

**Files:**
- Create: `cloudflare_review/README.md`

- [ ] **Step 1: Document exact setup order**

Document: `npm ci`, create D1, create R2, update binding IDs, set `AI_MODEL`, `MODEL_API_URL`, `SESSION_SECRET`, `DEVICE_TOKEN`, and `MODEL_API_KEY` with `wrangler secret put`, run remote migrations, bootstrap the first admin, deploy, and verify `/health`.

- [ ] **Step 2: Add a rollback section**

Rollback must deploy the prior Worker version and must not reverse D1 migrations destructively. State that schema changes are forward-only and data recovery uses D1 Time Travel where available.

- [ ] **Step 3: Run all Worker tests and a local smoke test**

Run: `cd cloudflare_review && npm test -- --run`

Expected: PASS.

Run: `curl -fsS http://127.0.0.1:8787/health`

Expected: `{"ok":true,"service":"kuaimai-review"}` while `wrangler dev` is running.

- [ ] **Step 4: Commit deployment docs**

```bash
git add cloudflare_review/README.md
git commit -m "docs: add cloud review deployment runbook"
```

## Phase 2 exit criteria

- Operator and admin authentication is enforced.
- Duplicate device events are harmless.
- One task can be edited by only one operator at a time.
- A confirmed value must be a member of the recorded candidate snapshot.
- The review page displays field names independently from candidate values.
- R2 originals are private and carry 30-day deletion metadata.
- The Worker has repeatable local tests, migration commands, deployment and rollback instructions.
