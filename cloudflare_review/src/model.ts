import { HttpError, requireValue } from "./http";
import type { ReviewEnv } from "./types";

export interface VisualFacts {
  garment_type: string | null;
  visible_colors: string[];
  length_landmark: "above_knee" | "knee" | "calf" | "ankle" | "unknown";
  silhouette: "slim" | "straight" | "loose" | "unknown";
  thickness_evidence: "thin" | "regular" | "thick" | "insufficient";
  image_consistency: Record<string, string>;
  evidence_asset_ids: string[];
}

export type AnalysisResult =
  | { status: "ready"; facts: VisualFacts; cached: boolean }
  | { status: "review_required"; reason_code: string };

type ModelEnv = Pick<ReviewEnv, "DB" | "ASSETS" | "MODEL_API_KEY" | "MODEL_API_URL" | "AI_MODEL">;
type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
type AssetRow = { id: string; r2_key: string; content_type: string };

const FACT_KEYS = new Set([
  "garment_type",
  "visible_colors",
  "length_landmark",
  "silhouette",
  "thickness_evidence",
  "image_consistency",
  "evidence_asset_ids",
]);
const LENGTHS = new Set(["above_knee", "knee", "calf", "ankle", "unknown"]);
const SILHOUETTES = new Set(["slim", "straight", "loose", "unknown"]);
const THICKNESS = new Set(["thin", "regular", "thick", "insufficient"]);

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

export function validateVisualFacts(value: unknown, assetIds: ReadonlySet<string>): VisualFacts {
  requireValue(Boolean(value) && typeof value === "object" && !Array.isArray(value), 422, "model_output_invalid");
  const facts = value as Record<string, unknown>;
  requireValue(
    Object.keys(facts).length === FACT_KEYS.size &&
      Object.keys(facts).every((key) => FACT_KEYS.has(key)),
    422,
    "model_output_invalid",
  );
  requireValue(facts.garment_type === null || typeof facts.garment_type === "string", 422, "model_output_invalid");
  requireValue(isStringArray(facts.visible_colors), 422, "model_output_invalid");
  requireValue(typeof facts.length_landmark === "string" && LENGTHS.has(facts.length_landmark), 422, "model_output_invalid");
  requireValue(typeof facts.silhouette === "string" && SILHOUETTES.has(facts.silhouette), 422, "model_output_invalid");
  requireValue(typeof facts.thickness_evidence === "string" && THICKNESS.has(facts.thickness_evidence), 422, "model_output_invalid");
  requireValue(
    Boolean(facts.image_consistency) &&
      typeof facts.image_consistency === "object" &&
      !Array.isArray(facts.image_consistency) &&
      Object.entries(facts.image_consistency as Record<string, unknown>).every(
        ([id, note]) => assetIds.has(id) && typeof note === "string",
      ),
    422,
    "model_output_invalid",
  );
  requireValue(
    isStringArray(facts.evidence_asset_ids) &&
      new Set(facts.evidence_asset_ids).size === facts.evidence_asset_ids.length &&
      facts.evidence_asset_ids.every((id) => assetIds.has(id)),
    422,
    "model_output_invalid",
  );
  const hasVisibleClaim =
    facts.garment_type !== null ||
    facts.visible_colors.length > 0 ||
    facts.length_landmark !== "unknown" ||
    facts.silhouette !== "unknown" ||
    facts.thickness_evidence !== "insufficient";
  requireValue(!hasVisibleClaim || facts.evidence_asset_ids.length > 0, 422, "model_output_invalid");
  return facts as unknown as VisualFacts;
}

function outputText(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const response = payload as Record<string, unknown>;
  if (typeof response.output_text === "string") return response.output_text;
  if (!Array.isArray(response.output)) return null;
  for (const item of response.output) {
    if (!item || typeof item !== "object") continue;
    const content = (item as Record<string, unknown>).content;
    if (!Array.isArray(content)) continue;
    for (const part of content) {
      if (part && typeof part === "object" && typeof (part as Record<string, unknown>).text === "string")
        return (part as Record<string, unknown>).text as string;
    }
  }
  return null;
}

function bytesToBase64(bytes: ArrayBuffer): string {
  const view = new Uint8Array(bytes);
  let binary = "";
  for (let index = 0; index < view.length; index += 0x8000)
    binary += String.fromCharCode(...view.subarray(index, index + 0x8000));
  return btoa(binary);
}

const visualSchema = {
  type: "object",
  additionalProperties: false,
  required: [...FACT_KEYS],
  properties: {
    garment_type: { type: ["string", "null"] },
    visible_colors: { type: "array", items: { type: "string" } },
    length_landmark: { type: "string", enum: [...LENGTHS] },
    silhouette: { type: "string", enum: [...SILHOUETTES] },
    thickness_evidence: { type: "string", enum: [...THICKNESS] },
    image_consistency: { type: "object", additionalProperties: { type: "string" } },
    evidence_asset_ids: { type: "array", items: { type: "string" } },
  },
};

export async function analyzeProduct(
  env: ModelEnv,
  productVersion: string,
  fetcher: Fetcher = fetch,
): Promise<AnalysisResult> {
  const cached = await env.DB.prepare(
    "SELECT payload_json FROM visual_facts WHERE product_version=?",
  )
    .bind(productVersion)
    .first<{ payload_json: string }>();
  if (cached)
    return { status: "ready", facts: JSON.parse(cached.payload_json) as VisualFacts, cached: true };

  requireValue(
    await env.DB.prepare("SELECT 1 ok FROM products WHERE product_version=? AND deleting=0")
      .bind(productVersion)
      .first(),
    404,
    "product_missing",
  );
  if (!env.MODEL_API_KEY || !env.MODEL_API_URL)
    return { status: "review_required", reason_code: "model_not_configured" };
  const rows = await env.DB.prepare(
    "SELECT id,r2_key,content_type FROM assets WHERE product_version=? ORDER BY CASE kind WHEN 'learning_thumbnail' THEN 0 ELSE 1 END,created_at,id",
  )
    .bind(productVersion)
    .all<AssetRow>();
  if (!rows.results.length)
    return { status: "review_required", reason_code: "image_evidence_missing" };
  const content: Record<string, unknown>[] = [
    {
      type: "input_text",
      text:
        "Only report facts visibly supported by the supplied product images. Never infer material percentages, brand claims, or functionality. Use unknown or insufficient when evidence is absent. Cite only the supplied asset IDs in evidence_asset_ids.",
    },
  ];
  for (const row of rows.results) {
    const object = await env.ASSETS.get(row.r2_key);
    if (!object) continue;
    content.push({ type: "input_text", text: `asset_id=${row.id}` });
    content.push({
      type: "input_image",
      image_url: `data:${row.content_type};base64,${bytesToBase64(await object.arrayBuffer())}`,
    });
  }
  if (content.length === 1)
    return { status: "review_required", reason_code: "image_evidence_missing" };
  const response = await fetcher(env.MODEL_API_URL, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.MODEL_API_KEY}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: env.AI_MODEL ?? "gpt-5.1",
      input: [{ role: "user", content }],
      text: {
        format: {
          type: "json_schema",
          name: "kuaimai_visual_facts",
          strict: true,
          schema: visualSchema,
        },
      },
    }),
  });
  if (!response.ok) throw new HttpError(response.status >= 500 ? 503 : 422, "model_request_failed");
  let parsed: unknown;
  try {
    const text = outputText(await response.json());
    parsed = text === null ? null : JSON.parse(text);
  } catch {
    return { status: "review_required", reason_code: "model_output_invalid" };
  }
  let facts: VisualFacts;
  try {
    facts = validateVisualFacts(parsed, new Set(rows.results.map((row) => row.id)));
  } catch (error) {
    if (error instanceof HttpError)
      return { status: "review_required", reason_code: "model_output_invalid" };
    throw error;
  }
  const now = new Date().toISOString();
  await env.DB.prepare(
    "INSERT OR IGNORE INTO visual_facts(product_version,payload_json,model,created_at) VALUES(?,?,?,?)",
  )
    .bind(productVersion, JSON.stringify(facts), env.AI_MODEL ?? "gpt-5.1", now)
    .run();
  const winner = await env.DB.prepare(
    "SELECT payload_json FROM visual_facts WHERE product_version=?",
  )
    .bind(productVersion)
    .first<{ payload_json: string }>();
  if (!winner) throw new HttpError(409, "analysis_cache_conflict");
  return { status: "ready", facts: JSON.parse(winner.payload_json) as VisualFacts, cached: false };
}
