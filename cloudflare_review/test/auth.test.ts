import { expect, it } from "vitest";
import { setup, api, user, testEnv } from "./helpers";
setup();
it("verifies PBKDF2 passwords and stores only a session digest", async () => {
  await user();
  const result = await api("/api/login", {
    username: "operator",
    password: "test-password",
  });
  expect(result.status).toBe(200);
  const cookie = result.headers.get("set-cookie")!;
  for (const flag of ["HttpOnly", "Secure", "SameSite=Strict", "Max-Age=28800"])
    expect(cookie).toContain(flag);
  const raw = cookie.split(";")[0]!.split("=")[1]!;
  expect(raw).toHaveLength(64);
  expect(
    await testEnv.DB.prepare(
      "SELECT count(*) n FROM sessions WHERE token_hash=?",
    )
      .bind(raw)
      .first("n"),
  ).toBe(0);
  expect((await api("/api/reviews", undefined, cookie)).status).toBe(200);
  expect(
    (await api("/api/login", { username: "operator", password: "wrong" }))
      .status,
  ).toBe(401);
});
it("rejects missing, expired and revoked sessions", async () => {
  const cookie = await user();
  expect((await api("/api/reviews")).status).toBe(401);
  await testEnv.DB.prepare(
    "UPDATE sessions SET expires_at='2000-01-01T00:00:00.000Z'",
  ).run();
  expect((await api("/api/reviews", undefined, cookie)).status).toBe(401);
});
it("enforces admin boundary and same-origin browser mutations", async () => {
  const operator = await user();
  const admin = await user("admin", "admin");
  expect((await api("/api/admin/status", undefined, operator)).status).toBe(
    403,
  );
  expect((await api("/api/admin/status", undefined, admin)).status).toBe(200);
  const { SELF } = await import("cloudflare:test");
  expect(
    (
      await SELF.fetch("https://local.test/api/logout", {
        method: "POST",
        headers: { cookie: operator, origin: "https://evil.test" },
      })
    ).status,
  ).toBe(403);
  expect((await api("/api/logout", {}, operator)).status).toBe(200);
  expect((await api("/api/reviews", undefined, operator)).status).toBe(401);
});
