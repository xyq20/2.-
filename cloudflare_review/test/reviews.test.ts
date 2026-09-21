import { expect, it } from "vitest";
import { SELF } from "cloudflare:test";
import { setup, api, user, testEnv, seedReview } from "./helpers";
setup();
const dheaders = {
  authorization: "Device test-only-device-token",
  "content-type": "application/json",
};
it("lists review thumbnails in upload order", async () => {
  const cookie = await user();
  await seedReview();
  const now = new Date().toISOString();
  await testEnv.DB.batch([
    testEnv.DB.prepare(
      "INSERT INTO assets(id,product_version,r2_key,sha256,kind,content_type,byte_size,created_at) VALUES('first','pv1','first','z','learning_thumbnail','image/png',1,?)",
    ).bind(now),
    testEnv.DB.prepare(
      "INSERT INTO assets(id,product_version,r2_key,sha256,kind,content_type,byte_size,created_at) VALUES('second','pv1','second','a','learning_thumbnail','image/png',1,?)",
    ).bind(new Date(Date.parse(now) + 1).toISOString()),
  ]);
  const response = await api("/api/reviews", undefined, cookie);
  expect(response.status).toBe(200);
  const data = (await response.json()) as {
    tasks: { asset_ids: string[] }[];
  };
  expect(data.tasks[0]!.asset_ids.slice(0, 2)).toEqual(["first", "second"]);
});
it("hides pending reviews that belong to a failed run", async () => {
  const cookie = await user();
  await seedReview();
  await testEnv.DB.prepare(
    "INSERT INTO run_checkpoints(run_id,product_version,device_id,execution_mode,platform_order_json,current_index,status,pending_review_id,version,image_version,updated_at) VALUES('run1','pv1','device1','save_only','[\"pdd\"]',0,'failed',NULL,2,'images1',?)",
  )
    .bind(new Date().toISOString())
    .run();
  const response = await api("/api/reviews", undefined, cookie);
  expect(response.status).toBe(200);
  expect(await response.json()).toEqual({ tasks: [] });
});
it("serializes claims and confirmation into one action and one durable resume event", async () => {
  const cookie = await user();
  const other = await user("other");
  await seedReview();
  const claimed = await api(
    "/api/reviews/review1/claim",
    { version: 1 },
    cookie,
  );
  expect(claimed.status).toBe(200);
  expect(
    (await api("/api/reviews/review1/claim", { version: 1 }, other)).status,
  ).toBe(409);
  const version = ((await claimed.json()) as { version: number }).version;
  const confirms = await Promise.all([
    api(
      "/api/reviews/review1/confirm",
      { version, final_value_id: "long" },
      cookie,
    ),
    api(
      "/api/reviews/review1/confirm",
      { version, final_value_id: "long" },
      cookie,
    ),
  ]);
  expect(confirms.map((r) => r.status).sort()).toEqual([200, 409]);
  expect(
    await testEnv.DB.prepare("SELECT count(*) n FROM review_actions").first(
      "n",
    ),
  ).toBe(1);
  expect(
    await testEnv.DB.prepare("SELECT status FROM review_tasks").first("status"),
  ).toBe("resume_ready");
  const resume = await SELF.fetch(
    "https://local.test/api/device/resume?device_id=device1",
    { headers: dheaders },
  );
  const events = (await resume.json()) as {
    events: { event_id: string; payload: { final_value_id: string } }[];
  };
  expect(events.events).toHaveLength(1);
  expect(events.events[0]!.payload.final_value_id).toBe("long");
  const url =
    "https://local.test/api/device/resume/" +
    events.events[0]!.event_id +
    "/ack";
  expect(
    (
      await SELF.fetch(url, {
        method: "POST",
        headers: dheaders,
        body: JSON.stringify({ device_id: "device1" }),
      })
    ).status,
  ).toBe(422);
  expect(
    (
      await SELF.fetch(url, {
        method: "POST",
        headers: dheaders,
        body: JSON.stringify({
          device_id: "wrong",
          checkpoint_persisted: true,
          checkpoint_id: "cp1",
        }),
      })
    ).status,
  ).toBe(409);
  for (let i = 0; i < 2; i++)
    expect(
      (
        await SELF.fetch(url, {
          method: "POST",
          headers: dheaders,
          body: JSON.stringify({
            device_id: "device1",
            checkpoint_persisted: true,
            checkpoint_id: "cp1",
          }),
        })
      ).status,
    ).toBe(200);
  expect(
    await testEnv.DB.prepare("SELECT status FROM review_tasks").first("status"),
  ).toBe("consumed");
});
it("validates candidate membership and requires a correction reason for changes", async () => {
  const cookie = await user();
  await seedReview();
  await api("/api/reviews/review1/claim", { version: 1 }, cookie);
  expect(
    (
      await api(
        "/api/reviews/review1/confirm",
        { version: 2, final_value_id: "invented" },
        cookie,
      )
    ).status,
  ).toBe(422);
  expect(
    (
      await api(
        "/api/reviews/review1/confirm",
        { version: 2, final_value_id: "short" },
        cookie,
      )
    ).status,
  ).toBe(422);
  await testEnv.DB.prepare("UPDATE option_snapshots SET options_json=?")
    .bind(
      JSON.stringify([
        { value_id: "long", label: "A" },
        { value_id: "long", label: "B" },
      ]),
    )
    .run();
  expect(
    (
      await api(
        "/api/reviews/review1/confirm",
        { version: 2, final_value_id: "long" },
        cookie,
      )
    ).status,
  ).toBe(422);
});
it("accepts operator input for a custom field with no candidates", async () => {
  const cookie = await user();
  await seedReview();
  await testEnv.DB.prepare(
    "UPDATE option_snapshots SET custom_allowed=1,options_json='[]' WHERE snapshot_version='sv1'",
  ).run();
  const claimed = await api(
    "/api/reviews/review1/claim",
    { version: 1 },
    cookie,
  );
  const version = ((await claimed.json()) as { version: number }).version;
  const confirmed = await api(
    "/api/reviews/review1/confirm",
    {
      version,
      final_value_id: "170",
      correction_reason: "尺码表人工填写",
    },
    cookie,
  );
  expect(confirmed.status).toBe(200);
  expect(
    await testEnv.DB.prepare(
      "SELECT final_value_id FROM review_actions WHERE review_id='review1'",
    ).first("final_value_id"),
  ).toBe("170");
});
it("renews only live owned leases and expired leases can be reclaimed", async () => {
  const cookie = await user();
  const other = await user("other");
  await seedReview();
  await api("/api/reviews/review1/claim", { version: 1 }, cookie);
  expect(
    (await api("/api/reviews/review1/renew", { version: 2 }, other)).status,
  ).toBe(409);
  expect(
    (await api("/api/reviews/review1/renew", { version: 2 }, cookie)).status,
  ).toBe(200);
  await testEnv.DB.prepare(
    "UPDATE review_tasks SET lease_until='2000-01-01T00:00:00.000Z'",
  ).run();
  expect(
    (
      await api(
        "/api/reviews/review1/confirm",
        { version: 3, final_value_id: "long" },
        cookie,
      )
    ).status,
  ).toBe(409);
  expect(
    (await api("/api/reviews/review1/claim", { version: 3 }, other)).status,
  ).toBe(200);
});
it("skip returns task to pending and never emits resume", async () => {
  const cookie = await user();
  await seedReview();
  await api("/api/reviews/review1/claim", { version: 1 }, cookie);
  expect(
    (await api("/api/reviews/review1/skip", { version: 2 }, cookie)).status,
  ).toBe(200);
  expect(
    await testEnv.DB.prepare("SELECT status FROM review_tasks").first("status"),
  ).toBe("pending");
  expect(
    await testEnv.DB.prepare(
      "SELECT count(*) n FROM device_events WHERE event_type='resume_ready'",
    ).first("n"),
  ).toBe(0);
});
it("admin invalidation prevents stale resume consumption", async () => {
  const cookie = await user();
  const admin = await user("admin", "admin");
  await seedReview();
  await api("/api/reviews/review1/claim", { version: 1 }, cookie);
  await api(
    "/api/reviews/review1/confirm",
    { version: 2, final_value_id: "long" },
    cookie,
  );
  expect(
    (await api("/api/admin/reviews/review1/invalidate", { version: 3 }, admin))
      .status,
  ).toBe(200);
  expect(
    await testEnv.DB.prepare("SELECT status FROM review_tasks").first("status"),
  ).toBe("invalidated");
  const result = await SELF.fetch(
    "https://local.test/api/device/resume?device_id=device1",
    { headers: dheaders },
  );
  expect(await result.json()).toEqual({ events: [] });
  expect(
    await testEnv.DB.prepare("SELECT status FROM attribute_decisions").first(
      "status",
    ),
  ).toBe("invalidated");
});
