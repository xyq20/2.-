import { expect, it } from "vitest";
import { SELF } from "cloudflare:test";
import { setup, api, user, testEnv, seedReview } from "./helpers";
setup();
const dheaders = {
  authorization: "Device test-only-device-token",
  "content-type": "application/json",
};
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
