import { expect, it } from "vitest";
import { conditionSignature, findMatureRule, recordVerifiedRuleOutcome } from "../src/rules";
import { setup, testEnv } from "./helpers";

setup();

async function seedOutcome(index: number, proposed = "long", final = "long", schema = "schema1") {
  const product = `pv${index}`;
  const snapshot = `sv${index}`;
  const key = `readback${index}`;
  const now = new Date(Date.now() + index).toISOString();
  await testEnv.DB.batch([
    testEnv.DB.prepare("INSERT INTO products(product_version,style_code,title,created_at,updated_at) VALUES(?,?,?,?,?)").bind(product, `style${index}`, "pants", now, now),
    testEnv.DB.prepare("INSERT INTO visual_facts(product_version,payload_json,model,created_at) VALUES(?,?,?,?)").bind(product, JSON.stringify({ garment_type: "pants", length_landmark: "ankle", silhouette: "straight" }), "test", now),
    testEnv.DB.prepare("INSERT INTO option_snapshots(snapshot_version,platform_id,category_leaf_id,field_id,field_label,schema_version,custom_allowed,options_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)").bind(snapshot, "pdd", "pants", "length", "裤长", schema, 0, JSON.stringify([{ value_id: "long", label: "长裤" }]), now),
    testEnv.DB.prepare("INSERT INTO attribute_decisions(id,product_version,platform_id,category_leaf_id,field_id,canonical_field,snapshot_version,proposed_value_id,final_value_id,source,status,reason_code,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'human','confirmed','review','{}',?,?)").bind(`decision${index}`, product, "pdd", "pants", "length", "pants_length", snapshot, proposed, final, now, now),
    testEnv.DB.prepare("INSERT INTO run_checkpoints(run_id,product_version,device_id,execution_mode,platform_order_json,current_index,status,version,image_version,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)").bind(`run${index}`, product, "device", "save_only", '["pdd"]', 0, "running", 1, "images", now),
    testEnv.DB.prepare("INSERT INTO persisted_readbacks(idempotency_key,run_id,product_version,platform_id,field_id,snapshot_version,actual_value_id,actual_label,verified,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,1,'{}',?)").bind(key, `run${index}`, product, "pdd", "length", snapshot, final, "长裤", now),
  ]);
  expect(await recordVerifiedRuleOutcome(testEnv.DB, key)).toBe(true);
}

it("builds deterministic signatures without product identifiers", async () => {
  const first = await conditionSignature({ garment_type: "pants", length_landmark: "ankle", silhouette: "straight", text_tokens: ["cotton", "green"] });
  const reordered = await conditionSignature({ garment_type: "pants", length_landmark: "ankle", silhouette: "straight", text_tokens: ["green", "cotton"] });
  expect(first).toBe(reordered);
  expect(first).toMatch(/^[a-f0-9]{64}$/);
});

it("requires three consecutive confirmations and at least 95 percent acceptance", async () => {
  await seedOutcome(1);
  await seedOutcome(2);
  let rule = await testEnv.DB.prepare("SELECT * FROM conditional_rules").first<Record<string, number | string>>();
  expect(rule!.status).toBe("observing");
  expect(rule!.consecutive_confirmations).toBe(2);
  await seedOutcome(3);
  rule = await testEnv.DB.prepare("SELECT * FROM conditional_rules").first<Record<string, number | string>>();
  expect(rule!.status).toBe("active");
  expect(await findMatureRule(testEnv.DB, "pv3", "pdd", "pants", "pants_length", "schema1", ["long"])).toBe("long");
});

it("reuses an active rule across categories by candidate name", async () => {
  await seedOutcome(1);
  await seedOutcome(2);
  await seedOutcome(3);
  expect(
    await findMatureRule(
      testEnv.DB,
      "pv3",
      "pdd",
      "shirt",
      "pants_length",
      "different-schema",
      ["shirt-long"],
      [{ value_id: "shirt-long", label: "长裤" }],
    ),
  ).toBe("shirt-long");
});

it("a correction demotes an active rule and duplicate readback is idempotent", async () => {
  await seedOutcome(1);
  await seedOutcome(2);
  await seedOutcome(3);
  await seedOutcome(4, "short", "long");
  const rule = await testEnv.DB.prepare("SELECT * FROM conditional_rules").first<Record<string, number | string>>();
  expect(rule!.status).toBe("observing");
  expect(rule!.consecutive_confirmations).toBe(0);
  expect(rule!.accepted_count).toBe(3);
  expect(rule!.total_count).toBe(4);
  expect(await recordVerifiedRuleOutcome(testEnv.DB, "readback4")).toBe(false);
});

it("schema changes reset confirmations and make the former rule ineligible", async () => {
  await seedOutcome(1);
  await seedOutcome(2);
  await seedOutcome(3);
  await seedOutcome(4, "long", "long", "schema2");
  const rule = await testEnv.DB.prepare("SELECT * FROM conditional_rules").first<Record<string, number | string>>();
  expect(rule!.status).toBe("observing");
  expect(rule!.consecutive_confirmations).toBe(0);
  expect(await findMatureRule(testEnv.DB, "pv4", "pdd", "pants", "pants_length", "schema1", ["long"])).toBeNull();
});
