// Local UI contract smoke. API responses are synthetic; real D1/R2 routes are covered by Vitest.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
const { chromium } = await import(
  process.env.PLAYWRIGHT_MODULE ?? "playwright"
);
const origin = process.env.REVIEW_LOCAL_URL ?? "http://127.0.0.1:8789";
assert.ok(
  ["127.0.0.1", "localhost"].includes(new URL(origin).hostname),
  "Local hosts only",
);
const browser = await chromium.launch({
  headless: true,
  channel: process.env.PLAYWRIGHT_CHANNEL,
});
try {
  const page = await browser.newPage({
    viewport: { width: 1200, height: 900 },
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  let remaining = true,
    confirms = 0,
    claims = 0;
  const task = {
    id: "demo",
    version: 1,
    title: "日常直筒休闲裤",
    platform_id: "拼多多",
    field_label: "裤长",
    suggested_value_id: "long",
    options: [
      { value_id: "long", label: "长裤" },
      { value_id: "short", label: "短裤" },
    ],
    asset_ids: ["demo"],
    evidence: { summary: "裤脚接近脚踝，与长裤候选一致。" },
  };
  await page.route(origin + "/", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      body: (await response.text())
        .replace('data-authenticated="false"', 'data-authenticated="true"')
        .replace(
          '<section id="login" class="card"',
          '<section id="login" class="card" hidden',
        ),
    });
  });
  await page.route(origin + "/api/assets/demo", (route) =>
    route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="500" viewBox="0 0 400 500"><rect width="400" height="500" fill="#f7f7f9"/><path d="M115 65 H285 L307 426 L223 430 L200 204 L177 430 L93 426 Z" fill="#737d68"/><path d="M115 90 H285 M200 65 V204 M133 90 L120 150 M267 90 L280 150" fill="none" stroke="#545f4d" stroke-width="3"/><text x="200" y="478" text-anchor="middle" font-family="sans-serif" font-size="13" fill="#6e6e73">Local synthetic product fixture</text></svg>',
    }),
  );
  await page.route(origin + "/api/reviews", (route) =>
    route.fulfill({ json: { tasks: remaining ? [task] : [] } }),
  );
  await page.route(origin + "/api/reviews/demo/claim", (route) => {
    claims++;
    return route.fulfill({
      json: { id: "demo", version: 2, status: "claimed" },
    });
  });
  await page.route(origin + "/api/reviews/demo/confirm", async (route) => {
    confirms++;
    remaining = false;
    await route.fulfill({
      json: { id: "demo", version: 3, status: "resume_ready" },
    });
  });
  await page.goto(origin, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "确认并继续", exact: true }).waitFor();
  assert.equal(await page.locator("#field-name").textContent(), "裤长");
  assert.equal(claims, 1);
  assert.equal(
    await page.locator('[data-value-id="long"]').getAttribute("aria-pressed"),
    "true",
  );
  await mkdir("output/playwright", { recursive: true });
  await page.screenshot({
    path: "output/playwright/review-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "output/playwright/review-mobile.png",
    fullPage: true,
  });
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    "No mobile horizontal overflow",
  );
  await page.getByRole("button", { name: "短裤", exact: true }).click();
  await page.getByRole("button", { name: "确认并继续", exact: true }).click();
  assert.equal(confirms, 0);
  await page.locator("#reason").selectOption({ label: "图片证据与建议不符" });
  await page.getByRole("button", { name: "确认并继续", exact: true }).click();
  await page.locator("#empty").waitFor({ state: "visible" });
  assert.equal(confirms, 1);
  assert.deepEqual(errors, []);
  console.log(
    "Browser smoke passed: desktop, mobile, claim, correction gate, confirm, next-task transition; synthetic API.",
  );
} finally {
  await browser.close();
}
