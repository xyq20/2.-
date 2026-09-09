import { env, SELF, applyD1Migrations } from "cloudflare:test";
import { beforeAll, beforeEach } from "vitest";
import { pbkdf2Sync, createHash } from "node:crypto";
export const testEnv = env as typeof env & {
  DB: D1Database;
  ASSETS: R2Bucket;
  TEST_MIGRATIONS: { name: string; queries: string[] }[];
};
export function setup() {
  beforeAll(async () => {
    await applyD1Migrations(testEnv.DB, testEnv.TEST_MIGRATIONS);
  });
  beforeEach(async () => {
    for (const table of [
      "products",
      "device_events",
      "sessions",
      "users",
      "option_snapshots",
      "platform_fields",
      "conditional_rules",
    ])
      await testEnv.DB.prepare("DELETE FROM " + table).run();
    const objects = await testEnv.ASSETS.list();
    if (objects.objects.length)
      await testEnv.ASSETS.delete(objects.objects.map((o) => o.key));
  });
}
export async function api(
  path: string,
  body?: unknown,
  cookie?: string,
  method?: string,
) {
  return SELF.fetch("https://local.test" + path, {
    method: method ?? (body === undefined ? "GET" : "POST"),
    headers: {
      "content-type": "application/json",
      origin: "https://local.test",
      ...(cookie ? { cookie } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
export async function device(
  event_type: string,
  payload: unknown,
  key = crypto.randomUUID(),
) {
  return SELF.fetch("https://local.test/api/device/events", {
    method: "POST",
    headers: {
      authorization: "Device test-only-device-token",
      "content-type": "application/json",
    },
    body: JSON.stringify({ idempotency_key: key, event_type, payload }),
  });
}
export async function user(id = "operator", role = "operator") {
  const salt = Buffer.from("local-test-salt-123");
  const hash = Buffer.from(
    pbkdf2Sync("test-password", salt, 310000, 32, "sha256"),
  ).toString("base64");
  await testEnv.DB.prepare(
    "INSERT INTO users(id,username,password_salt,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
  )
    .bind(id, id, salt.toString("base64"), hash, role, new Date().toISOString())
    .run();
  const token = "test-session-" + id;
  await testEnv.DB.prepare(
    "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
  )
    .bind(
      createHash("sha256").update(token).digest("hex"),
      id,
      new Date(Date.now() + 3600000).toISOString(),
      new Date().toISOString(),
    )
    .run();
  return "km_session=" + token;
}
export const product = {
  product_version: "pv1",
  style_code: "style1",
  title: "直筒裤",
};
export const snapshot = {
  snapshot_version: "sv1",
  platform_id: "pdd",
  category_leaf_id: "pants",
  field_id: "length",
  field_label: "裤长",
  schema_version: "schema1",
  custom_allowed: false,
  canonical_field: "pants_length",
  options: [
    { value_id: "long", label: "长裤", position: 0 },
    { value_id: "short", label: "短裤", position: 1 },
  ],
};
export const review = {
  id: "review1",
  run_id: "run1",
  device_id: "device1",
  product_version: "pv1",
  platform_id: "pdd",
  category_leaf_id: "pants",
  field_id: "length",
  field_label: "裤长",
  canonical_field: "pants_length",
  snapshot_version: "sv1",
  suggested_value_id: "long",
  reason_code: "needs_review",
  evidence_json: { summary: "裤脚覆盖脚踝" },
};
export async function seedReview() {
  for (const [type, payload] of [
    ["product.upsert", product],
    ["snapshot.created", snapshot],
    ["review.created", review],
  ] as const) {
    const response = await device(type, payload);
    if (response.status !== 201)
      throw new Error(
        type + ": " + response.status + " " + (await response.text()),
      );
  }
}
