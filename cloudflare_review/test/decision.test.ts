import { expect, it, vi } from "vitest";
import { decideAttribute, parseDecisionRequest } from "../src/decision";
import { conditionSignature, conditionsForProduct } from "../src/rules";
import { device, product, setup, snapshot, testEnv } from "./helpers";

setup();

const request = {
  product_version: "pv1",
  platform_id: "pdd",
  category_leaf_id: "pants",
  field_id: "length",
  canonical_field: "pants_length",
  snapshot_version: "sv1",
};

async function seed() {
  expect((await device("product.upsert", product)).status).toBe(201);
  expect((await device("snapshot.created", snapshot)).status).toBe(201);
  await testEnv.DB.prepare("INSERT INTO visual_facts(product_version,payload_json,model,created_at) VALUES('pv1',?,'test',?)")
    .bind(JSON.stringify({ garment_type: "pants", length_landmark: "ankle", silhouette: "straight" }), new Date().toISOString())
    .run();
}

it("accepts only server-loaded candidate identity fields", () => {
  expect(() => parseDecisionRequest({ ...request, candidates: [{ value_id: "fake" }] })).toThrow();
  expect(parseDecisionRequest(request)).toEqual(request);
});

it("uses human override before explicit text and rules", async () => {
  await seed();
  const now = new Date().toISOString();
  await testEnv.DB.batch([
    testEnv.DB.prepare("INSERT INTO text_facts(id,product_version,source,payload_json,created_at) VALUES('text','pv1','excel',?,?)").bind(JSON.stringify({ values: { pants_length: { value_id: "short" } } }), now),
    testEnv.DB.prepare("INSERT INTO attribute_decisions(id,product_version,platform_id,category_leaf_id,field_id,canonical_field,snapshot_version,proposed_value_id,final_value_id,source,status,reason_code,evidence_json,created_at,updated_at) VALUES('human','pv1','pdd','pants','length','pants_length','sv1','long','long','human','confirmed','review','{}',?,?)").bind(now, now),
  ]);
  const result = await decideAttribute(testEnv, request);
  expect(result.status).toBe("auto_fill_ready");
  expect(result.source).toBe("human_override");
  expect(result.value_id).toBe("long");
});

it("uses explicit text before a mature rule", async () => {
  await seed();
  const now = new Date().toISOString();
  const signature = await conditionSignature(await conditionsForProduct(testEnv.DB, "pv1"));
  await testEnv.DB.batch([
    testEnv.DB.prepare("INSERT INTO text_facts(id,product_version,source,payload_json,created_at) VALUES('text','pv1','excel',?,?)").bind(JSON.stringify({ values: { pants_length: "short" } }), now),
    testEnv.DB.prepare("INSERT INTO conditional_rules(id,platform_id,category_leaf_id,canonical_field,condition_signature,target_value_id,schema_version,consecutive_confirmations,accepted_count,total_count,status,updated_at) VALUES('rule','pdd','pants','pants_length',?,'long','schema1',3,3,3,'active',?)").bind(signature, now),
  ]);
  const result = await decideAttribute(testEnv, request);
  expect(result.source).toBe("explicit_text");
  expect(result.value_id).toBe("short");
});

it("returns review for evidence conflict and unmapped fields", async () => {
  await seed();
  await testEnv.DB.prepare("INSERT INTO text_facts(id,product_version,source,payload_json,created_at) VALUES('text','pv1','excel',?,?)")
    .bind(JSON.stringify({ conflicts: ["pants_length"] }), new Date().toISOString())
    .run();
  expect((await decideAttribute(testEnv, request)).reason_code).toBe("evidence_conflict");
  await testEnv.DB.prepare("UPDATE platform_fields SET canonical_field=NULL").run();
  expect((await decideAttribute(testEnv, request)).reason_code).toBe("field_mapping_required");
});

it("uses only server-calibrated comparable outcomes for constrained model", async () => {
  await seed();
  const signature = await conditionSignature(await conditionsForProduct(testEnv.DB, "pv1"));
  const now = new Date().toISOString();
  for (let index = 0; index < 3; index++)
    await testEnv.DB.prepare("INSERT INTO rule_outcomes(idempotency_key,product_version,platform_id,category_leaf_id,canonical_field,condition_signature,snapshot_version,target_value_id,proposed_value_id,accepted,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)")
      .bind(`case${index}`, "pv1", "pdd", "pants", "pants_length", signature, "sv1", "long", "long", now)
      .run();
  const upstream = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify({ output_text: JSON.stringify({ value_id: "long" }) }), { status: 200 }),
  );
  const result = await decideAttribute(
    { ...testEnv, MODEL_API_KEY: "x", MODEL_API_URL: "https://model.test", AI_MODEL: "test" },
    request,
    upstream,
  );
  expect(result.source).toBe("constrained_model");
  expect(result.support_count).toBe(3);
  expect(result.calibrated_acceptance_rate).toBe(1);
  expect(upstream).toHaveBeenCalledTimes(1);
});

it("rejects an out-of-candidate model result", async () => {
  await seed();
  const signature = await conditionSignature(await conditionsForProduct(testEnv.DB, "pv1"));
  const now = new Date().toISOString();
  for (let index = 0; index < 3; index++)
    await testEnv.DB.prepare("INSERT INTO rule_outcomes(idempotency_key,product_version,platform_id,category_leaf_id,canonical_field,condition_signature,snapshot_version,target_value_id,proposed_value_id,accepted,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)")
      .bind(`case${index}`, "pv1", "pdd", "pants", "pants_length", signature, "sv1", "long", "long", now)
      .run();
  const upstream = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify({ output_text: JSON.stringify({ value_id: "invented" }) }), { status: 200 }),
  );
  expect((await decideAttribute({ ...testEnv, MODEL_API_KEY: "x", MODEL_API_URL: "https://model.test" }, request, upstream)).reason_code).toBe("model_candidate_invalid");
});
