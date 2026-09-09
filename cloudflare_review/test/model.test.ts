import { expect, it, vi } from "vitest";
import { analyzeProduct, validateVisualFacts } from "../src/model";
import { device, product, setup, testEnv } from "./helpers";

setup();

const facts = {
  garment_type: "pants",
  visible_colors: ["军绿色"],
  length_landmark: "ankle",
  silhouette: "straight",
  thickness_evidence: "regular",
  image_consistency: { asset1: "same product" },
  evidence_asset_ids: ["asset1"],
};

async function seedAsset() {
  expect((await device("product.upsert", product)).status).toBe(201);
  await testEnv.ASSETS.put("products/pv1/hash/learning_thumbnail", new Uint8Array([1, 2, 3]));
  await testEnv.DB.prepare(
    "INSERT INTO assets(id,product_version,r2_key,sha256,kind,content_type,byte_size,created_at) VALUES('asset1','pv1','products/pv1/hash/learning_thumbnail','hash','learning_thumbnail','image/jpeg',3,?)",
  )
    .bind(new Date().toISOString())
    .run();
}

it("caches one structured vision call per product version", async () => {
  await seedAsset();
  const upstream = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify({ output_text: JSON.stringify(facts) }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  const env = {
    ...testEnv,
    MODEL_API_KEY: "test-key",
    MODEL_API_URL: "https://model.test/responses",
    AI_MODEL: "test-model",
  };

  const first = await analyzeProduct(env, "pv1", upstream);
  const second = await analyzeProduct(env, "pv1", upstream);

  expect(first.status).toBe("ready");
  expect(second).toEqual({ ...first, cached: true });
  expect(upstream).toHaveBeenCalledTimes(1);
  const requestBody = JSON.parse(upstream.mock.calls[0]![1]!.body as string);
  expect(requestBody.text.format.strict).toBe(true);
  expect(JSON.stringify(requestBody)).toContain("Never infer material percentages");
  expect(await testEnv.DB.prepare("SELECT count(*) n FROM visual_facts").first("n")).toBe(1);
});

it("returns review without caching malformed model output", async () => {
  await seedAsset();
  const upstream = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify({ output_text: '{"visible_colors":[]}' }), { status: 200 }),
  );
  const result = await analyzeProduct(
    { ...testEnv, MODEL_API_KEY: "x", MODEL_API_URL: "https://model.test" },
    "pv1",
    upstream,
  );
  expect(result).toEqual({ status: "review_required", reason_code: "model_output_invalid" });
  expect(await testEnv.DB.prepare("SELECT count(*) n FROM visual_facts").first("n")).toBe(0);
});

it("rejects unknown asset ids, unsupported enums, and unsupported keys", () => {
  const assets = new Set(["asset1"]);
  expect(() => validateVisualFacts({ ...facts, evidence_asset_ids: ["other"] }, assets)).toThrow();
  expect(() => validateVisualFacts({ ...facts, silhouette: "bootcut" }, assets)).toThrow();
  expect(() => validateVisualFacts({ ...facts, material_percentage: "100%" }, assets)).toThrow();
});
