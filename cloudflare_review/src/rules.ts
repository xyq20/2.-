export interface RuleConditions {
  garment_type?: string | null;
  length_landmark?: string | null;
  silhouette?: string | null;
  text_tokens: readonly string[];
}

type RuleDb = D1Database;
type FactsRow = { payload_json: string };

function stableConditions(input: RuleConditions): string {
  return JSON.stringify({
    garment_type: input.garment_type ?? null,
    length_landmark: input.length_landmark ?? null,
    silhouette: input.silhouette ?? null,
    text_tokens: [...new Set(input.text_tokens.filter((token) => typeof token === "string" && token.length > 0))].sort(),
  });
}

export async function conditionSignature(input: RuleConditions): Promise<string> {
  const bytes = new TextEncoder().encode(stableConditions(input));
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

export async function conditionsForProduct(
  db: RuleDb,
  productVersion: string,
): Promise<RuleConditions> {
  const visual = await db
    .prepare("SELECT payload_json FROM visual_facts WHERE product_version=?")
    .bind(productVersion)
    .first<FactsRow>();
  const text = await db
    .prepare("SELECT payload_json FROM text_facts WHERE product_version=? ORDER BY created_at DESC,id DESC LIMIT 1")
    .bind(productVersion)
    .first<FactsRow>();
  let visualFacts: Record<string, unknown> = {};
  let textFacts: Record<string, unknown> = {};
  try {
    visualFacts = visual ? JSON.parse(visual.payload_json) : {};
    textFacts = text ? JSON.parse(text.payload_json) : {};
  } catch {
    // Invalid facts are treated as absent and can never broaden a rule.
  }
  return {
    garment_type: typeof visualFacts.garment_type === "string" ? visualFacts.garment_type : null,
    length_landmark: typeof visualFacts.length_landmark === "string" ? visualFacts.length_landmark : null,
    silhouette: typeof visualFacts.silhouette === "string" ? visualFacts.silhouette : null,
    text_tokens: Array.isArray(textFacts.text_tokens)
      ? textFacts.text_tokens.filter((value): value is string => typeof value === "string")
      : [],
  };
}

interface VerifiedOutcome {
  idempotency_key: string;
  product_version: string;
  platform_id: string;
  category_leaf_id: string;
  canonical_field: string;
  snapshot_version: string;
  actual_value_id: string;
  proposed_value_id: string | null;
  final_value_id: string;
  schema_version: string;
  options_json: string;
}

interface RuleRow {
  id: string;
  target_value_id: string;
  schema_version: string;
  consecutive_confirmations: number;
  accepted_count: number;
  total_count: number;
  status: string;
}

export async function recordVerifiedRuleOutcome(
  db: RuleDb,
  readbackKey: string,
): Promise<boolean> {
  if (
    await db.prepare("SELECT 1 ok FROM rule_outcomes WHERE idempotency_key=?")
      .bind(readbackKey)
      .first()
  )
    return false;
  const outcome = await db.prepare(
    "SELECT pr.idempotency_key,pr.product_version,pr.platform_id,s.category_leaf_id,d.canonical_field,pr.snapshot_version,pr.actual_value_id,d.proposed_value_id,d.final_value_id,s.schema_version,s.options_json FROM persisted_readbacks pr JOIN option_snapshots s ON s.snapshot_version=pr.snapshot_version JOIN attribute_decisions d ON d.product_version=pr.product_version AND d.platform_id=pr.platform_id AND d.field_id=pr.field_id AND d.snapshot_version=pr.snapshot_version AND d.final_value_id=pr.actual_value_id AND d.source='human' AND d.status='confirmed' WHERE pr.idempotency_key=? AND pr.verified=1 AND d.canonical_field IS NOT NULL ORDER BY d.created_at DESC LIMIT 1",
  )
    .bind(readbackKey)
    .first<VerifiedOutcome>();
  if (!outcome) return false;
  const signature = await conditionSignature(
    await conditionsForProduct(db, outcome.product_version),
  );
  const candidates = JSON.parse(outcome.options_json) as { value_id?: unknown }[];
  const candidateValid =
    candidates.filter((candidate) => candidate.value_id === outcome.final_value_id).length === 1;
  const accepted =
    candidateValid &&
    outcome.proposed_value_id !== null &&
    outcome.proposed_value_id === outcome.final_value_id;
  const existing = await db.prepare(
    "SELECT * FROM conditional_rules WHERE platform_id=? AND category_leaf_id=? AND canonical_field=? AND condition_signature=?",
  )
    .bind(outcome.platform_id, outcome.category_leaf_id, outcome.canonical_field, signature)
    .first<RuleRow>();
  const sameTarget = existing?.target_value_id === outcome.final_value_id;
  const sameSchema = existing?.schema_version === outcome.schema_version;
  const consecutive =
    accepted && candidateValid && (!existing || (sameTarget && sameSchema))
      ? (existing?.consecutive_confirmations ?? 0) + 1
      : 0;
  const acceptedCount = (existing?.accepted_count ?? 0) + (accepted ? 1 : 0);
  const totalCount = (existing?.total_count ?? 0) + 1;
  const rate = acceptedCount / totalCount;
  const status =
    candidateValid && sameSchema !== false && consecutive >= 3 && rate >= 0.95
      ? "active"
      : "observing";
  const now = new Date().toISOString();
  const ruleId = existing?.id ?? crypto.randomUUID();
  await db.batch([
    db.prepare(
      "INSERT INTO rule_outcomes(idempotency_key,product_version,platform_id,category_leaf_id,canonical_field,condition_signature,snapshot_version,target_value_id,proposed_value_id,accepted,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
    ).bind(
      outcome.idempotency_key,
      outcome.product_version,
      outcome.platform_id,
      outcome.category_leaf_id,
      outcome.canonical_field,
      signature,
      outcome.snapshot_version,
      outcome.final_value_id,
      outcome.proposed_value_id,
      accepted ? 1 : 0,
      now,
    ),
    db.prepare(
      "INSERT INTO conditional_rules(id,platform_id,category_leaf_id,canonical_field,condition_signature,target_value_id,schema_version,consecutive_confirmations,accepted_count,total_count,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform_id,category_leaf_id,canonical_field,condition_signature) DO UPDATE SET target_value_id=excluded.target_value_id,schema_version=excluded.schema_version,consecutive_confirmations=excluded.consecutive_confirmations,accepted_count=excluded.accepted_count,total_count=excluded.total_count,status=excluded.status,updated_at=excluded.updated_at",
    ).bind(
      ruleId,
      outcome.platform_id,
      outcome.category_leaf_id,
      outcome.canonical_field,
      signature,
      outcome.final_value_id,
      outcome.schema_version,
      consecutive,
      acceptedCount,
      totalCount,
      status,
      now,
    ),
  ]);
  return true;
}

export async function findMatureRule(
  db: RuleDb,
  productVersion: string,
  platformId: string,
  categoryLeafId: string,
  canonicalField: string,
  schemaVersion: string,
  candidateIds: readonly string[],
): Promise<string | null> {
  const signature = await conditionSignature(
    await conditionsForProduct(db, productVersion),
  );
  const rule = await db.prepare(
    "SELECT target_value_id FROM conditional_rules WHERE platform_id=? AND category_leaf_id=? AND canonical_field=? AND condition_signature=? AND schema_version=? AND status='active' AND consecutive_confirmations>=3 AND total_count>0 AND (accepted_count*1.0/total_count)>=0.95",
  )
    .bind(platformId, categoryLeafId, canonicalField, signature, schemaVersion)
    .first<{ target_value_id: string }>();
  if (!rule || candidateIds.filter((id) => id === rule.target_value_id).length !== 1)
    return null;
  return rule.target_value_id;
}

