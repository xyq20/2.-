import { SELF } from "cloudflare:test";
import { expect, it } from "vitest";
it("returns the stable health marker", async () => {
  const response = await SELF.fetch("https://local.test/health");
  expect(response.status).toBe(200);
  expect(await response.json()).toEqual({
    ok: true,
    service: "kuaimai-review",
  });
});
