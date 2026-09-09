import { expect, it } from "vitest";
import { SELF } from "cloudflare:test";
import { createHash } from "node:crypto";
import { setup, api, user, testEnv, seedReview, device } from "./helpers";
setup();
const bytes = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
const sha = createHash("sha256").update(bytes).digest("hex");
async function upload(kind = "original", hash = sha) {
  return SELF.fetch("https://local.test/api/device/assets/" + hash, {
    method: "PUT",
    headers: {
      authorization: "Device test-only-device-token",
      "x-product-version": "pv1",
      "x-asset-kind": kind,
      "content-type": "image/png",
    },
    body: bytes,
  });
}
it("validates and deduplicates private R2 uploads", async () => {
  const cookie = await user();
  const unrelated = await user("other");
  await seedReview();
  expect((await upload("invalid")).status).toBe(400);
  expect((await upload("original", "0".repeat(64))).status).toBe(422);
  const first = await upload();
  expect(first.status).toBe(201);
  const data = (await first.json()) as { asset_id: string };
  const second = await upload();
  expect(second.status).toBe(200);
  expect(await second.json()).toEqual(data);
  const path = "/api/assets/" + data.asset_id;
  expect((await api(path)).status).toBe(401);
  expect((await api(path, undefined, unrelated)).status).toBe(403);
  await api("/api/reviews/review1/claim", { version: 1 }, cookie);
  const read = await api(path, undefined, cookie);
  expect(read.status).toBe(200);
  expect(read.headers.get("cache-control")).toContain("private");
  expect(new Uint8Array(await read.arrayBuffer())).toEqual(bytes);
  const row = await testEnv.DB.prepare(
    "SELECT r2_key,delete_after FROM assets",
  ).first<{ r2_key: string; delete_after: string }>();
  expect(row!.r2_key).toBe("products/pv1/" + sha + "/original");
  expect(Date.parse(row!.delete_after) - Date.now()).toBeGreaterThan(
    29 * 86400000,
  );
});
it("cleans only expired originals and at most 100 per invocation", async () => {
  await seedReview();
  await upload();
  await upload("learning_thumbnail");
  await testEnv.DB.prepare(
    "UPDATE assets SET delete_after='2000-01-01T00:00:00.000Z'",
  ).run();
  const { cleanupExpiredOriginals } = await import("../src/assets");
  expect(await cleanupExpiredOriginals(testEnv, new Date())).toBe(1);
  expect(
    await testEnv.DB.prepare("SELECT kind FROM assets").first("kind"),
  ).toBe("learning_thumbnail");
});
it("caps a cleanup batch at 100 while preserving future originals", async () => {
  await seedReview();
  await upload();
  const now = new Date().toISOString();
  await testEnv.DB.batch(
    Array.from({ length: 101 }, (_, i) =>
      testEnv.DB.prepare(
        "INSERT INTO assets(id,product_version,r2_key,sha256,kind,content_type,byte_size,delete_after,created_at) VALUES(?, 'pv1', ?, ?, 'original','image/png',8,'2000-01-01T00:00:00.000Z',?)",
      ).bind("expired" + i, "expired/" + i, String(i), now),
    ),
  );
  const { cleanupExpiredOriginals } = await import("../src/assets");
  expect(await cleanupExpiredOriginals(testEnv, new Date())).toBe(100);
  expect(
    await testEnv.DB.prepare("SELECT count(*) n FROM assets").first("n"),
  ).toBe(2);
});
it("rejects oversized streams without trusting content length", async () => {
  await seedReview();
  const response = await SELF.fetch(
    "https://local.test/api/device/assets/" + sha,
    {
      method: "PUT",
      headers: {
        authorization: "Device test-only-device-token",
        "x-product-version": "pv1",
        "x-asset-kind": "original",
        "content-type": "image/png",
      },
      body: new Uint8Array(10 * 1024 * 1024 + 1),
    },
  );
  expect(response.status).toBe(413);
  expect((await testEnv.ASSETS.list()).objects).toHaveLength(0);
});
it("admin deletion removes original, thumbnail, events and FK-related learning records", async () => {
  const admin = await user("admin", "admin");
  await seedReview();
  await upload();
  await upload("learning_thumbnail");
  await api("/api/reviews/review1/claim", { version: 1 }, admin);
  await api(
    "/api/reviews/review1/confirm",
    { version: 2, final_value_id: "long" },
    admin,
  );
  await device("checkpoint.updated", {
    version: 1,
    run_id: "run1",
    product_version: "pv1",
    device_id: "device1",
    execution_mode: "all",
    platform_order: ["pdd"],
    current_index: 0,
    status: "waiting_review",
    pending_review_id: "review1",
  });
  await device("stage.completed", {
    run_id: "run1",
    platform_id: "pdd",
    status: "readback_verified",
    expected_json: {},
    readback_json: {},
    verified: true,
  });
  await device("readback.recorded", {
    category_leaf_id: "pants",
    snapshot_version: "sv1",
    actual_value_id: "long",
    run_id: "run1",
    product_version: "pv1",
    platform_id: "pdd",
    field_id: "length",
    verified: true,
    payload_json: {},
  });
  const response = await api(
    "/api/admin/products/pv1",
    undefined,
    admin,
    "DELETE",
  );
  expect(response.status).toBe(200);
  expect((await testEnv.ASSETS.list()).objects).toHaveLength(0);
  for (const table of [
    "products",
    "assets",
    "review_tasks",
    "device_events",
    "review_actions",
    "attribute_decisions",
    "run_checkpoints",
    "stage_results",
    "persisted_readbacks",
  ])
    expect(
      await testEnv.DB.prepare("SELECT count(*) n FROM " + table).first("n"),
    ).toBe(table === "device_events" ? 1 : 0);
});
