import { it, expect } from "vitest";
import { SELF } from "cloudflare:test";
import {
  setup,
  device,
  product,
  snapshot,
  review,
  testEnv,
  seedReview,
  user,
  api,
} from "./helpers";
setup();
it("reports a missing checkpoint device id and freezes an omitted image version as empty", async () => {
  await device("product.upsert", product);
  const { device_id: _, ...missing } = checkpoint;
  const rejected = await device("checkpoint.updated", missing);
  expect(rejected.status).toBe(400);
  expect(await rejected.json()).toEqual({ error: "missing_device_id" });
  const { image_version: __, ...withoutImage } = checkpoint;
  expect((await device("checkpoint.updated", withoutImage)).status).toBe(201);
  expect(
    await testEnv.DB.prepare("SELECT image_version FROM run_checkpoints").first(
      "image_version",
    ),
  ).toBe("");
  expect(
    (await device("checkpoint.updated", { ...checkpoint, version: 2 })).status,
  ).toBe(409);
});
const checkpoint = {
  run_id: "run1",
  product_version: "pv1",
  device_id: "device1",
  execution_mode: "all",
  platform_order: ["pdd", "tmall"],
  image_version: "images1",
  current_index: 0,
  status: "running",
  pending_review_id: null,
  version: 1,
};
it("accepts empty options only when the field allows custom input", async () => {
  const closed = {
    ...snapshot,
    snapshot_version: "sv-empty-closed",
    custom_allowed: false,
    options: [],
  };
  const rejected = await device("snapshot.created", closed);
  expect(rejected.status).toBe(400);
  expect(await rejected.json()).toEqual({ error: "invalid_options" });
  expect(
    (
      await device("snapshot.created", {
        ...closed,
        snapshot_version: "sv-empty-custom",
        custom_allowed: true,
      })
    ).status,
  ).toBe(201);
});
it("accepts a platform registry id for a runner-name checkpoint", async () => {
  const aliasProduct = { ...product, product_version: "pv-alias" };
  const aliasSnapshot = {
    ...snapshot,
    snapshot_version: "sv-alias",
    product_version: undefined,
    platform_id: "tb",
  };
  const aliasReview = {
    ...review,
    id: "review-alias",
    run_id: "run-alias",
    product_version: "pv-alias",
    platform_id: "tb",
    snapshot_version: "sv-alias",
  };
  const aliasCheckpoint = {
    ...checkpoint,
    run_id: "run-alias",
    product_version: "pv-alias",
    platform_order: ["base", "taobao", "tmall"],
    current_index: 1,
  };
  expect((await device("product.upsert", aliasProduct)).status).toBe(201);
  expect((await device("snapshot.created", aliasSnapshot)).status).toBe(201);
  expect((await device("checkpoint.updated", aliasCheckpoint)).status).toBe(201);
  expect((await device("review.created", aliasReview)).status).toBe(201);
  expect(
    (
      await device("checkpoint.updated", {
        ...aliasCheckpoint,
        status: "waiting_review",
        pending_review_id: "review-alias",
        version: 2,
      })
    ).status,
  ).toBe(201);
});
it("normalizes legacy checkpoint_id before idempotency comparison", async () => {
  await device("product.upsert", product);
  const legacy = { ...checkpoint, checkpoint_id: checkpoint.run_id };
  expect(
    (await device("checkpoint.updated", legacy, "legacy-checkpoint")).status,
  ).toBe(201);
  expect(
    await testEnv.DB.prepare(
      "SELECT payload_json FROM device_events WHERE idempotency_key=?",
    )
      .bind("legacy-checkpoint")
      .first("payload_json"),
  ).not.toContain("checkpoint_id");
  expect(
    (await device("checkpoint.updated", checkpoint, "legacy-checkpoint"))
      .status,
  ).toBe(409);
  expect(
    (
      await device("checkpoint.updated", {
        ...checkpoint,
        run_id: "different-run",
        checkpoint_id: checkpoint.run_id,
      })
    ).status,
  ).toBe(400);
});
const readback = {
  run_id: "run1",
  product_version: "pv1",
  platform_id: "pdd",
  category_leaf_id: "pants",
  field_id: "length",
  snapshot_version: "sv1",
  actual_value_id: "long",
  actual_label: "长裤",
  verified: true,
  payload_json: { source: "api" },
};
const stage = {
  run_id: "run1",
  platform_id: "pdd",
  status: "readback_verified",
  verified: true,
  expected_json: { length: "long" },
  readback_json: { length: "long" },
};
async function seed() {
  await seedReview();
  expect((await device("checkpoint.updated", checkpoint)).status).toBe(201);
}
it("records a verified readback only with its matching unique snapshot candidate", async () => {
  await seed();
  expect((await device("readback.recorded", readback)).status).toBe(201);
  expect(
    await testEnv.DB.prepare(
      "SELECT actual_value_id,snapshot_version,verified FROM persisted_readbacks",
    ).first(),
  ).toEqual({ actual_value_id: "long", snapshot_version: "sv1", verified: 1 });
});

it.each(["platform_id", "category_leaf_id", "field_id"] as const)(
  "rejects readback with mismatched %s",
  async (key) => {
    await seed();
    expect(
      (await device("readback.recorded", { ...readback, [key]: "wrong" }))
        .status,
    ).toBe(422);
    expect(
      await testEnv.DB.prepare(
        "SELECT count(*) n FROM persisted_readbacks",
      ).first("n"),
    ).toBe(0);
  },
);
it.each([null, "", "absent"])(
  "rejects non-candidate readback value %s",
  async (value) => {
    await seed();
    expect(
      (
        await device("readback.recorded", {
          ...readback,
          actual_value_id: value,
        })
      ).status,
    ).toBe(422);
    expect(
      await testEnv.DB.prepare(
        "SELECT count(*) n FROM persisted_readbacks",
      ).first("n"),
    ).toBe(0);
  },
);
it("rejects duplicate candidate readbacks and accepts explicitly custom values", async () => {
  await seed();
  await testEnv.DB.prepare("UPDATE option_snapshots SET options_json=?")
    .bind(JSON.stringify([{ value_id: "long" }, { value_id: "long" }]))
    .run();
  expect((await device("readback.recorded", readback)).status).toBe(422);
  await testEnv.DB.prepare(
    "UPDATE option_snapshots SET custom_allowed=1",
  ).run();
  expect(
    (
      await device("readback.recorded", {
        ...readback,
        actual_value_id: "custom",
      })
    ).status,
  ).toBe(201);
});
it("rejects missing actual values and duplicate review suggestions without storing events", async () => {
  await seed();
  const { actual_value_id: _, ...missing } = readback;
  expect(
    (await device("readback.recorded", missing, "missing-value")).status,
  ).toBe(422);
  await testEnv.DB.prepare("UPDATE option_snapshots SET options_json=?")
    .bind(JSON.stringify([{ value_id: "long" }, { value_id: "long" }]))
    .run();
  expect(
    (
      await device(
        "review.created",
        { ...review, id: "duplicate-suggestion" },
        "bad-suggestion",
      )
    ).status,
  ).toBe(422);
  expect(
    await testEnv.DB.prepare(
      "SELECT count(*) n FROM device_events WHERE idempotency_key IN ('missing-value','bad-suggestion')",
    ).first("n"),
  ).toBe(0);
});
it.each([
  { field_label: "材质" },
  { canonical_field: "material" },
  { suggested_value_id: "invented" },
])("rejects review metadata spoof %j", async (change) => {
  await device("product.upsert", product);
  await device("snapshot.created", snapshot);
  expect(
    (await device("review.created", { ...review, ...change })).status,
  ).toBe(422);
  expect(
    await testEnv.DB.prepare("SELECT count(*) n FROM review_tasks").first("n"),
  ).toBe(0);
});
it("derives known mappings and routes unknown mappings to field_mapping_required", async () => {
  await device("product.upsert", product);
  await device("snapshot.created", snapshot);
  const { canonical_field: _, ...unmapped } = review;
  expect((await device("review.created", unmapped)).status).toBe(201);
  expect(
    await testEnv.DB.prepare("SELECT canonical_field FROM review_tasks").first(
      "canonical_field",
    ),
  ).toBe("pants_length");
  await testEnv.DB.prepare(
    "UPDATE platform_fields SET canonical_field=NULL",
  ).run();
  expect(
    (
      await device("review.created", {
        ...review,
        id: "unknown",
        canonical_field: null,
      })
    ).status,
  ).toBe(201);
  const row = await testEnv.DB.prepare(
    "SELECT canonical_field,reason_code FROM review_tasks WHERE id='unknown'",
  ).first();
  expect(row).toEqual({
    canonical_field: null,
    reason_code: "field_mapping_required",
  });
  expect(
    (await device("review.created", { ...review, id: "spoof" })).status,
  ).toBe(422);
});
it.each([
  { status: "banana", verified: false },
  { status: "saved", verified: true },
  { status: "readback_verified", verified: false },
  { status: "failed", verified: true },
])("rejects illegal stage status/verified %j", async (change) => {
  await seed();
  expect(
    (await device("stage.completed", { ...stage, ...change })).status,
  ).toBe(422);
  expect(
    await testEnv.DB.prepare("SELECT count(*) n FROM stage_results").first("n"),
  ).toBe(0);
});
it("keeps verified stage evidence immutable across retries and delayed events", async () => {
  await seed();
  expect((await device("stage.completed", stage, "success")).status).toBe(201);
  expect((await device("stage.completed", stage, "success")).status).toBe(409);
  expect(
    (
      await device(
        "stage.completed",
        { ...stage, readback_json: {} },
        "overwrite",
      )
    ).status,
  ).toBe(409);
  expect(
    (
      await device(
        "stage.completed",
        { ...stage, status: "failed", verified: false },
        "old",
      )
    ).status,
  ).toBe(409);
  expect(
    await testEnv.DB.prepare("SELECT readback_json FROM stage_results").first(
      "readback_json",
    ),
  ).toBe(JSON.stringify(stage.readback_json));
});
it("rejects stage backwards progression and same-level different evidence", async () => {
  await seed();
  const saved = { ...stage, status: "saved", verified: false };
  expect((await device("stage.completed", saved)).status).toBe(201);
  expect(
    (await device("stage.completed", { ...saved, status: "filled" })).status,
  ).toBe(409);
  expect(
    (
      await device("stage.completed", {
        ...saved,
        expected_json: { changed: true },
      })
    ).status,
  ).toBe(409);
  expect((await device("stage.completed", stage)).status).toBe(201);
});
it("requires explicit positive checkpoint version", async () => {
  await device("product.upsert", product);
  const { version: _, ...missing } = checkpoint;
  for (const payload of [
    missing,
    { ...checkpoint, version: 0 },
    { ...checkpoint, version: -1 },
  ])
    expect((await device("checkpoint.updated", payload)).status).toBe(400);
});
it.each([
  "product_version",
  "device_id",
  "execution_mode",
  "platform_order",
  "image_version",
] as const)("rejects checkpoint immutable dimension %s", async (key) => {
  await seed();
  await device("product.upsert", { ...product, product_version: "pv2" });
  const changed = {
    ...checkpoint,
    version: 2,
    [key]:
      key === "platform_order"
        ? ["pdd"]
        : key === "product_version"
          ? "pv2"
          : "changed",
  };
  expect((await device("checkpoint.updated", changed)).status).toBe(409);
});
it("rejects old/same-conflicting versions and illegal transitions without occupying keys", async () => {
  await seed();
  const waiting = {
    ...checkpoint,
    status: "waiting_review",
    pending_review_id: "review1",
    version: 2,
  };
  expect((await device("checkpoint.updated", waiting, "wait")).status).toBe(
    201,
  );
  const duplicate = await device("checkpoint.updated", waiting, "wait");
  expect(duplicate.status).toBe(409);
  expect(await duplicate.json()).toHaveProperty("event_id");
  for (const p of [
    checkpoint,
    { ...waiting, current_index: 1 },
    { ...waiting, status: "completed", version: 3 },
    { ...waiting, status: "unknown", version: 3 },
  ])
    expect((await device("checkpoint.updated", p)).status).toBe(409);
  expect(
    await testEnv.DB.prepare(
      "SELECT version FROM run_checkpoints WHERE run_id='run1'",
    ).first("version"),
  ).toBe(2);
});
it("serializes simultaneous checkpoint updates using optimistic version", async () => {
  await seed();
  const results = await Promise.all([
    device("checkpoint.updated", {
      ...checkpoint,
      version: 2,
      status: "failed",
    }),
    device("checkpoint.updated", {
      ...checkpoint,
      version: 2,
      status: "waiting_review",
      pending_review_id: "review1",
    }),
  ]);
  expect(results.map((r) => r.status).sort()).toEqual([201, 409]);
});
it("rejects conflicting concurrent checkpoint content under the same idempotency key", async () => {
  await seed();
  const results = await Promise.all([
    device(
      "checkpoint.updated",
      { ...checkpoint, version: 2, status: "failed" },
      "same-key-race",
    ),
    device(
      "checkpoint.updated",
      {
        ...checkpoint,
        version: 2,
        status: "waiting_review",
        pending_review_id: "review1",
      },
      "same-key-race",
    ),
  ]);
  expect(results.map((r) => r.status).sort()).toEqual([201, 409]);
  const conflict = results.find((r) => r.status === 409)!;
  expect(await conflict.json()).toEqual({
    error: "idempotency_payload_conflict",
  });
});
it("keeps completed checkpoints terminal and accepts same-version identical content only", async () => {
  await seed();
  expect(
    (await device("checkpoint.updated", checkpoint, "same-version-identical"))
      .status,
  ).toBe(201);
  const completed = {
    ...checkpoint,
    version: 2,
    current_index: 2,
    status: "completed",
  };
  expect((await device("checkpoint.updated", completed)).status).toBe(201);
  expect(
    (
      await device("checkpoint.updated", {
        ...completed,
        version: 3,
        status: "running",
      })
    ).status,
  ).toBe(409);
  expect(
    (
      await device("checkpoint.updated", {
        ...completed,
        version: 3,
        pending_review_id: "review1",
      })
    ).status,
  ).toBe(409);
});
it("binds same idempotency keys to the original content", async () => {
  await device("product.upsert", product, "bound-key");
  const result = await device(
    "product.upsert",
    { ...product, title: "different" },
    "bound-key",
  );
  expect(result.status).toBe(409);
  expect(await result.json()).toEqual({
    error: "idempotency_payload_conflict",
  });
});
it("atomically serializes conflicting stage outcomes and does not retain rejected events", async () => {
  await seed();
  const results = await Promise.all([
    device("stage.completed", stage, "stage-a"),
    device(
      "stage.completed",
      { ...stage, readback_json: { length: "short" } },
      "stage-b",
    ),
  ]);
  expect(results.map((r) => r.status).sort()).toEqual([201, 409]);
  expect(
    await testEnv.DB.prepare(
      "SELECT count(*) n FROM device_events WHERE idempotency_key IN ('stage-a','stage-b')",
    ).first("n"),
  ).toBe(1);
});
it("connects durable checkpoint, review confirmation, resume delivery and explicit device acknowledgement", async () => {
  await device("product.upsert", product);
  await device("snapshot.created", snapshot);
  expect((await device("checkpoint.updated", checkpoint)).status).toBe(201);
  expect((await device("review.created", review)).status).toBe(201);
  expect(
    (
      await device("checkpoint.updated", {
        ...checkpoint,
        version: 2,
        status: "waiting_review",
        pending_review_id: "review1",
      })
    ).status,
  ).toBe(201);
  const cookie = await user();
  await api("/api/reviews/review1/claim", { version: 1 }, cookie);
  expect(
    (
      await api(
        "/api/reviews/review1/confirm",
        { version: 2, final_value_id: "long" },
        cookie,
      )
    ).status,
  ).toBe(200);
  const headers = {
    authorization: "Device test-only-device-token",
    "content-type": "application/json",
  };
  const result = await SELF.fetch(
    "https://local.test/api/device/resume?device_id=device1",
    { headers },
  );
  const { events } = (await result.json()) as {
    events: {
      event_id: string;
      payload: {
        run_id: string;
        product_version: string;
        snapshot_version: string;
      };
    }[];
  };
  expect(events).toHaveLength(1);
  expect(events[0]!.payload).toMatchObject({
    run_id: "run1",
    product_version: "pv1",
    snapshot_version: "sv1",
  });
  const path =
    "https://local.test/api/device/resume/" + events[0]!.event_id + "/ack";
  const ack = {
    device_id: "device1",
    checkpoint_id: "local-cp-3",
    checkpoint_persisted: true,
  };
  for (const key of ["device_id", "checkpoint_id", "checkpoint_persisted"]) {
    const invalid: Record<string, unknown> = { ...ack };
    delete invalid[key];
    expect(
      (
        await SELF.fetch(path, {
          method: "POST",
          headers,
          body: JSON.stringify(invalid),
        })
      ).status,
    ).toBeGreaterThanOrEqual(400);
  }
  expect(
    (
      await SELF.fetch(path, {
        method: "POST",
        headers,
        body: JSON.stringify({ ...ack, device_id: "another" }),
      })
    ).status,
  ).toBe(409);
  expect(
    (
      await SELF.fetch(path, {
        method: "POST",
        headers,
        body: JSON.stringify({ ...ack, checkpoint_persisted: false }),
      })
    ).status,
  ).toBe(422);
  expect(
    (
      await device("checkpoint.updated", {
        ...checkpoint,
        version: 3,
        status: "resume_pending",
        pending_review_id: "review1",
      })
    ).status,
  ).toBe(201);
  expect(
    (
      await SELF.fetch(path, {
        method: "POST",
        headers,
        body: JSON.stringify(ack),
      })
    ).status,
  ).toBe(200);
  expect(
    (
      await SELF.fetch(path, {
        method: "POST",
        headers,
        body: JSON.stringify({ ...ack, checkpoint_id: "different" }),
      })
    ).status,
  ).toBe(409);
  expect(
    await testEnv.DB.prepare(
      "SELECT status FROM review_tasks WHERE id='review1'",
    ).first("status"),
  ).toBe("consumed");
});
