import { expect, it } from "vitest";
import {
  setup,
  api,
  device,
  testEnv,
  product,
  snapshot,
  review,
  seedReview,
} from "./helpers";
setup();
it("allows a failed stage to recover but never downgrades verified success", async () => {
  await seedReview();
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
  const stage = {
    run_id: "run1",
    platform_id: "pdd",
    status: "failed",
    expected_json: { length: "long" },
    readback_json: {},
    verified: false,
  };
  expect((await device("stage.completed", stage)).status).toBe(201);
  expect(
    (
      await device("stage.completed", {
        ...stage,
        status: "readback_verified",
        readback_json: { length: "long" },
        verified: true,
      })
    ).status,
  ).toBe(201);
  expect((await device("stage.completed", stage)).status).toBe(409);
  expect(
    await testEnv.DB.prepare("SELECT verified FROM stage_results").first(
      "verified",
    ),
  ).toBe(1);
});
it("requires device authorization", async () => {
  expect((await api("/api/device/events", {})).status).toBe(401);
});
it("ingests once and returns the original event id on retry", async () => {
  const first = await device("product.upsert", product, "same");
  const second = await device("product.upsert", product, "same");
  expect(first.status).toBe(201);
  expect(second.status).toBe(409);
  expect(await second.json()).toEqual(await first.json());
  expect(
    await testEnv.DB.prepare("SELECT count(*) as n FROM device_events").first(
      "n",
    ),
  ).toBe(1);
  expect(
    await testEnv.DB.prepare("SELECT title FROM products").first("title"),
  ).toBe(product.title);
});
it("rejects unknown types, sensitive keys and malformed payloads", async () => {
  expect((await device("mystery", {})).status).toBe(400);
  expect(
    (
      await device("product.upsert", {
        ...product,
        cookie: "must-not-be-stored",
      })
    ).status,
  ).toBe(400);
  expect(
    (
      await device("product.upsert", {
        ...product,
        category_json: { nested: { token: "never" } },
      })
    ).status,
  ).toBe(400);
  expect((await device("review.created", {})).status).toBe(400);
  expect(
    await testEnv.DB.prepare("SELECT count(*) as n FROM device_events").first(
      "n",
    ),
  ).toBe(0);
});
it("dispatches review and snapshots and rejects cross-field snapshots", async () => {
  await seedReview();
  expect(
    await testEnv.DB.prepare("SELECT status FROM review_tasks").first("status"),
  ).toBe("pending");
  expect(
    (
      await device("review.created", {
        ...review,
        id: "wrong",
        field_id: "color",
      })
    ).status,
  ).toBe(422);
});
it("rolls back events whose dependencies are missing, allowing later retry", async () => {
  const first = await device("review.created", review, "retry");
  expect(first.status).toBe(422);
  await device("product.upsert", product);
  await device("snapshot.created", snapshot);
  expect((await device("review.created", review, "retry")).status).toBe(201);
});
it("concurrent duplicate ingestion creates only one task", async () => {
  await device("product.upsert", product);
  await device("snapshot.created", snapshot);
  const results = await Promise.all([
    device("review.created", review, "concurrent"),
    device("review.created", review, "concurrent"),
  ]);
  expect(results.map((r) => r.status).sort()).toEqual([201, 409]);
  expect(await results[0]!.json()).toEqual(await results[1]!.json());
  expect(
    await testEnv.DB.prepare("SELECT count(*) n FROM review_tasks").first("n"),
  ).toBe(1);
});
it("rejects concurrent reuse of a snapshot version with different options", async () => {
  const other = {
    ...snapshot,
    options: [{ value_id: "other", label: "另一项", position: 0 }],
  };
  const results = await Promise.all([
    device("snapshot.created", snapshot),
    device("snapshot.created", other),
  ]);
  expect(results.filter((r) => r.status === 201)).toHaveLength(1);
  expect(results.filter((r) => [409, 422].includes(r.status))).toHaveLength(1);
});
it("rejects mismatched readback product and checkpoint provenance", async () => {
  await seedReview();
  await device("checkpoint.updated", {
    version: 1,
    run_id: "run1",
    product_version: "pv1",
    device_id: "device1",
    execution_mode: "test",
    platform_order: ["pdd"],
    current_index: 0,
    status: "waiting_review",
    pending_review_id: "review1",
  });
  await device("product.upsert", { ...product, product_version: "pv2" });
  expect(
    (
      await device("readback.recorded", {
        run_id: "run1",
        product_version: "pv2",
        platform_id: "pdd",
        category_leaf_id: "pants",
        snapshot_version: "sv1",
        field_id: "length",
        verified: true,
        payload_json: {},
      })
    ).status,
  ).toBe(422);
});
