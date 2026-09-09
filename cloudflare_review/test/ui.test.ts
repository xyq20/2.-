import { expect, it } from "vitest";
import { setup, user, api, seedReview } from "./helpers";
setup();
it("renders the accessible single-card review surface", async () => {
  const cookie = await user();
  await seedReview();
  const page = await api("/", undefined, cookie);
  expect(page.status).toBe(200);
  const html = await page.text();
  for (const phrase of [
    "字段名称",
    "AI 建议",
    "data-value-id",
    "查看判断依据与相似商品",
    "暂时跳过",
    "确认并继续",
    "<img",
  ])
    expect(html).toContain(phrase);
  const css = await (await api("/app.css")).text();
  for (const phrase of ["max-width", ":focus-visible", "@media"])
    expect(css).toContain(phrase);
  const script = await (await api("/app.js")).text();
  expect(script).toContain("任务已被其他运营更新");
  for (const forbidden of [
    "test-only-device-token",
    "test-only-session-secret",
    "<pre",
    "<aside",
    "/api/admin/",
  ])
    expect(html).not.toContain(forbidden);
  expect((await api("/")).status).toBe(200);
});
