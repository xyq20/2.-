import { HttpError, requireValue, text } from "./http";
import type { ReviewEnv } from "./types";
import { conditionSignature, conditionsForProduct, findMatureRule } from "./rules";

export interface DecisionRequest {
  product_version: string;
  platform_id: string;
  category_leaf_id: string;
  field_id: string;
  canonical_field: string;
  snapshot_version: string;
}

export interface DecisionResponse {
  status: "auto_fill_ready" | "review_required";
  value_id: string | null;
  value_label: string | null;
  source: string | null;
  reason_code: string;
  snapshot_version: string;
  evidence_kinds: string[];
  mature_rule: boolean;
  support_count: number;
  calibrated_acceptance_rate: number;
}

type DecisionEnv = Pick<ReviewEnv, "DB" | "MODEL_API_KEY" | "MODEL_API_URL" | "AI_MODEL">;
type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
type SnapshotRow = {
  platform_id: string;
  category_leaf_id: string;
  field_id: string;
  field_label: string;
  schema_version: string;
  options_json: string;
  custom_allowed: number;
};
type Option = { value_id: string; label: string; position?: number };

export function parseDecisionRequest(data: Record<string, unknown>): DecisionRequest {
  const keys = [
    "product_version",
    "platform_id",
    "category_leaf_id",
    "field_id",
    "canonical_field",
    "snapshot_version",
  ];
  requireValue(
    Object.keys(data).length === keys.length && Object.keys(data).every((key) => keys.includes(key)),
    400,
    "invalid_decision_request",
  );
  return {
    product_version: text(data.product_version, "product_version"),
    platform_id: text(data.platform_id, "platform_id"),
    category_leaf_id: text(data.category_leaf_id, "category_leaf_id"),
    field_id: text(data.field_id, "field_id"),
    canonical_field: text(data.canonical_field, "canonical_field"),
    snapshot_version: text(data.snapshot_version, "snapshot_version"),
  };
}

function review(input: DecisionRequest, reason: string, support = 0, rate = 0): DecisionResponse {
  return {
    status: "review_required",
    value_id: null,
    value_label: null,
    source: null,
    reason_code: reason,
    snapshot_version: input.snapshot_version,
    evidence_kinds: [],
    mature_rule: false,
    support_count: support,
    calibrated_acceptance_rate: rate,
  };
}

function ready(
  input: DecisionRequest,
  options: Option[],
  source: string,
  valueId: string,
  evidenceKinds: string[],
  support = 0,
  rate = 0,
): DecisionResponse | null {
  const matches = options.filter((option) => option.value_id === valueId);
  if (matches.length !== 1) return null;
  return {
    status: "auto_fill_ready",
    value_id: matches[0]!.value_id,
    value_label: matches[0]!.label,
    source,
    reason_code: "validated",
    snapshot_version: input.snapshot_version,
    evidence_kinds: evidenceKinds,
    mature_rule: source === "mature_rule",
    support_count: support,
    calibrated_acceptance_rate: rate,
  };
}

function safeFacts(payload: string | null): Record<string, unknown> {
  if (!payload) return {};
  try {
    const value: unknown = JSON.parse(payload);
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

async function persistDecision(
  db: D1Database,
  input: DecisionRequest,
  response: DecisionResponse,
  evidence: Record<string, unknown>,
) {
  const now = new Date().toISOString();
  await db.prepare(
    "INSERT INTO attribute_decisions(id,product_version,platform_id,category_leaf_id,field_id,canonical_field,snapshot_version,proposed_value_id,final_value_id,source,status,reason_code,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
  )
    .bind(
      crypto.randomUUID(),
      input.product_version,
      input.platform_id,
      input.category_leaf_id,
      input.field_id,
      input.canonical_field,
      input.snapshot_version,
      response.value_id,
      response.status === "auto_fill_ready" ? response.value_id : null,
      response.source ?? "review",
      response.status,
      response.reason_code,
      JSON.stringify(evidence),
      now,
      now,
    )
    .run();
}

function extractModelChoice(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const response = payload as Record<string, unknown>;
  let raw = typeof response.output_text === "string" ? response.output_text : null;
  if (!raw && Array.isArray(response.output)) {
    for (const item of response.output) {
      if (!item || typeof item !== "object" || !Array.isArray((item as Record<string, unknown>).content)) continue;
      for (const part of (item as Record<string, unknown>).content as unknown[]) {
        if (part && typeof part === "object" && typeof (part as Record<string, unknown>).text === "string") {
          raw = (part as Record<string, unknown>).text as string;
          break;
        }
      }
    }
  }
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return typeof parsed.value_id === "string" ? parsed.value_id : null;
  } catch {
    return null;
  }
}

export async function decideAttribute(
  env: DecisionEnv,
  input: DecisionRequest,
  fetcher: Fetcher = fetch,
): Promise<DecisionResponse> {
  requireValue(
    await env.DB.prepare("SELECT 1 ok FROM products WHERE product_version=? AND deleting=0")
      .bind(input.product_version)
      .first(),
    404,
    "product_missing",
  );
  const snapshot = await env.DB.prepare(
    "SELECT * FROM option_snapshots WHERE snapshot_version=? AND platform_id=? AND category_leaf_id=? AND field_id=?",
  )
    .bind(input.snapshot_version, input.platform_id, input.category_leaf_id, input.field_id)
    .first<SnapshotRow>();
  requireValue(snapshot, 422, "snapshot_mismatch");
  const options = JSON.parse(snapshot.options_json) as Option[];
  requireValue(
    options.length > 0 &&
      options.every((option) => typeof option.value_id === "string" && typeof option.label === "string") &&
      new Set(options.map((option) => option.value_id)).size === options.length,
    422,
    "snapshot_candidates_invalid",
  );
  const mapping = await env.DB.prepare(
    "SELECT canonical_field FROM platform_fields WHERE platform_id=? AND category_leaf_id=? AND source_field_id=? AND schema_version=?",
  )
    .bind(input.platform_id, input.category_leaf_id, input.field_id, snapshot.schema_version)
    .first<{ canonical_field: string | null }>();
  if (!mapping?.canonical_field || mapping.canonical_field !== input.canonical_field) {
    const response = review(input, "field_mapping_required");
    await persistDecision(env.DB, input, response, {});
    return response;
  }

  const human = await env.DB.prepare(
    "SELECT final_value_id FROM attribute_decisions WHERE product_version=? AND platform_id=? AND category_leaf_id=? AND field_id=? AND canonical_field=? AND snapshot_version=? AND source='human' AND status='confirmed' AND final_value_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
  )
    .bind(input.product_version, input.platform_id, input.category_leaf_id, input.field_id, input.canonical_field, input.snapshot_version)
    .first<{ final_value_id: string }>();
  if (human) {
    const response = ready(input, options, "human_override", human.final_value_id, ["human"]);
    if (response) {
      await persistDecision(env.DB, input, response, { override: true });
      return response;
    }
  }

  const textRow = await env.DB.prepare(
    "SELECT payload_json FROM text_facts WHERE product_version=? ORDER BY created_at DESC,id DESC LIMIT 1",
  )
    .bind(input.product_version)
    .first<{ payload_json: string }>();
  const textFacts = safeFacts(textRow?.payload_json ?? null);
  if (
    Array.isArray(textFacts.conflicts) &&
    textFacts.conflicts.includes(input.canonical_field)
  ) {
    const response = review(input, "evidence_conflict");
    await persistDecision(env.DB, input, response, { text: true, visual: true });
    return response;
  }
  const values = textFacts.values;
  if (values && typeof values === "object" && !Array.isArray(values)) {
    const fact = (values as Record<string, unknown>)[input.canonical_field];
    const explicitValue =
      typeof fact === "string"
        ? fact
        : fact && typeof fact === "object"
          ? (fact as Record<string, unknown>).value_id
          : null;
    if (typeof explicitValue === "string") {
      const response = ready(input, options, "explicit_text", explicitValue, ["text"]);
      if (response) {
        await persistDecision(env.DB, input, response, { text_fact: true });
        return response;
      }
    }
  }

  const candidateIds = options.map((option) => option.value_id);
  const mature = await findMatureRule(
    env.DB,
    input.product_version,
    input.platform_id,
    input.category_leaf_id,
    input.canonical_field,
    snapshot.schema_version,
    candidateIds,
    options,
  );
  if (mature) {
    const response = ready(input, options, "mature_rule", mature, ["visual"]);
    if (response) {
      await persistDecision(env.DB, input, response, { rule: true });
      return response;
    }
  }

  const signature = await conditionSignature(
    await conditionsForProduct(env.DB, input.product_version),
  );
  const metrics = await env.DB.prepare(
    "SELECT count(*) support,sum(accepted) accepted FROM rule_outcomes WHERE platform_id=? AND category_leaf_id=? AND canonical_field=? AND condition_signature=?",
  )
    .bind(input.platform_id, input.category_leaf_id, input.canonical_field, signature)
    .first<{ support: number; accepted: number | null }>();
  const support = Number(metrics?.support ?? 0);
  const rate = support ? Number(metrics?.accepted ?? 0) / support : 0;
  if (support < 3 || rate < 0.95 || !env.MODEL_API_KEY || !env.MODEL_API_URL) {
    const response = review(input, "insufficient_evidence", support, rate);
    await persistDecision(env.DB, input, response, { condition_signature: signature });
    return response;
  }
  const visual = await env.DB.prepare("SELECT payload_json FROM visual_facts WHERE product_version=?")
    .bind(input.product_version)
    .first<{ payload_json: string }>();
  const upstream = await fetcher(env.MODEL_API_URL, {
    method: "POST",
    headers: { authorization: `Bearer ${env.MODEL_API_KEY}`, "content-type": "application/json" },
    body: JSON.stringify({
      model: env.AI_MODEL ?? "gpt-5.1",
      input: [{ role: "user", content: [{ type: "input_text", text: JSON.stringify({ instruction: "Choose exactly one supplied value_id or review_required. Never invent an ID.", visual_facts: safeFacts(visual?.payload_json ?? null), text_facts: textFacts, candidates: options }) }] }],
      text: { format: { type: "json_schema", name: "attribute_choice", strict: true, schema: { type: "object", additionalProperties: false, required: ["value_id"], properties: { value_id: { type: "string", enum: [...candidateIds, "review_required"] } } } } },
    }),
  });
  if (!upstream.ok) throw new HttpError(upstream.status >= 500 ? 503 : 422, "model_request_failed");
  const chosen = extractModelChoice(await upstream.json());
  if (chosen === "review_required") {
    const response = review(input, "model_requested_review", support, rate);
    await persistDecision(env.DB, input, response, { condition_signature: signature });
    return response;
  }
  const response = chosen
    ? ready(input, options, "constrained_model", chosen, ["visual"], support, rate)
    : null;
  if (!response) {
    const invalid = review(input, "model_candidate_invalid", support, rate);
    await persistDecision(env.DB, input, invalid, { condition_signature: signature });
    return invalid;
  }
  await persistDecision(env.DB, input, response, { condition_signature: signature });
  return response;
}
