import { requireValue } from "./http";
import type { Json, ReviewEnv } from "./types";
export function stableJson(value: Json): string {
  if (Array.isArray(value)) return "[" + value.map(stableJson).join(",") + "]";
  if (value && typeof value === "object")
    return (
      "{" +
      Object.keys(value)
        .sort()
        .map((key) => JSON.stringify(key) + ":" + stableJson(value[key]!))
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
export interface StateGuard {
  sql: string;
  values: (string | number)[];
}
interface CheckpointRow {
  run_id: string;
  product_version: string;
  device_id: string;
  execution_mode: string;
  platform_order_json: string;
  image_version: string;
  current_index: number;
  status: string;
  pending_review_id: string | null;
  version: number;
}
const checkpointTransitions: Record<string, readonly string[]> = {
  running: ["running", "waiting_review", "failed", "completed", "cancelled"],
  waiting_review: ["waiting_review", "resume_pending", "failed", "cancelled"],
  resume_pending: ["resume_pending", "running", "failed", "cancelled"],
  failed: ["failed", "running", "waiting_review", "cancelled"],
  completed: ["completed"],
  cancelled: ["cancelled"],
};
export async function validateCheckpoint(
  env: ReviewEnv,
  p: Record<string, Json>,
): Promise<StateGuard> {
  const status = String(p.status),
    run = String(p.run_id);
  requireValue(
    Object.hasOwn(checkpointTransitions, status),
    409,
    "checkpoint_state_conflict",
  );
  const prior = await env.DB.prepare(
    "SELECT * FROM run_checkpoints WHERE run_id=?",
  )
    .bind(run)
    .first<CheckpointRow>();
  p.image_version ??= "";
  if (prior) {
    requireValue(
      ["product_version", "device_id", "execution_mode", "image_version"].every(
        (key) => p[key] === prior[key as keyof CheckpointRow],
      ) &&
        stableJson(p.platform_order!) ===
          stableJson(JSON.parse(prior.platform_order_json)),
      409,
      "checkpoint_identity_conflict",
    );
    requireValue(
      Number(p.version) >= prior.version,
      409,
      "checkpoint_version_conflict",
    );
    const sameMutable =
      p.current_index === prior.current_index &&
      p.status === prior.status &&
      p.pending_review_id === prior.pending_review_id;
    requireValue(
      Number(p.version) !== prior.version || sameMutable,
      409,
      "checkpoint_version_conflict",
    );
    requireValue(
      checkpointTransitions[prior.status]?.includes(status) &&
        Number(p.current_index) >= prior.current_index,
      409,
      "checkpoint_state_conflict",
    );
    requireValue(
      !["completed", "cancelled"].includes(prior.status) || sameMutable,
      409,
      "checkpoint_terminal_conflict",
    );
  } else
    requireValue(
      ["running", "waiting_review"].includes(status),
      409,
      "checkpoint_initial_state_conflict",
    );
  if (["waiting_review", "resume_pending"].includes(status)) {
    requireValue(
      typeof p.pending_review_id === "string" && p.pending_review_id.length > 0,
      409,
      "checkpoint_review_required",
    );
    const task = await env.DB.prepare(
      "SELECT status,platform_id FROM review_tasks WHERE id=? AND run_id=? AND product_version=? AND device_id=?",
    )
      .bind(p.pending_review_id, p.run_id, p.product_version, p.device_id)
      .first<{ status: string; platform_id: string }>();
    requireValue(
      task &&
        Array.isArray(p.platform_order) &&
        p.platform_order[Number(p.current_index)] === task.platform_id,
      409,
      "checkpoint_review_mismatch",
    );
    if (status === "resume_pending")
      requireValue(
        ["resume_ready", "consumed"].includes(task.status),
        409,
        "checkpoint_review_not_confirmed",
      );
  }
  return prior
    ? {
        sql: "NOT EXISTS(SELECT 1 FROM run_checkpoints WHERE run_id=? AND version=?)",
        values: [run, prior.version],
      }
    : {
        sql: "EXISTS(SELECT 1 FROM run_checkpoints WHERE run_id=?)",
        values: [run],
      };
}
interface StageRow {
  idempotency_key: string;
  status: string;
  verified: number;
  expected_json: string;
  readback_json: string;
}
const stageTransitions: Record<string, readonly string[]> = {
  started: ["filled", "saved", "failed", "readback_verified"],
  filled: ["saved", "failed", "readback_verified"],
  saved: ["failed", "readback_verified"],
  failed: ["readback_verified"],
  readback_verified: [],
};
export async function validateStage(
  env: ReviewEnv,
  p: Record<string, Json>,
): Promise<StateGuard> {
  const status = String(p.status),
    run = String(p.run_id),
    platform = String(p.platform_id);
  requireValue(
    Object.hasOwn(stageTransitions, status) &&
      p.verified === (status === "readback_verified"),
    422,
    "invalid_stage_state",
  );
  const prior = await env.DB.prepare(
    "SELECT * FROM stage_results WHERE run_id=? AND platform_id=?",
  )
    .bind(run, platform)
    .first<StageRow>();
  if (prior) {
    const same =
      prior.status === status &&
      prior.verified === (p.verified ? 1 : 0) &&
      stableJson(JSON.parse(prior.expected_json)) ===
        stableJson(p.expected_json!) &&
      stableJson(JSON.parse(prior.readback_json)) ===
        stableJson(p.readback_json!);
    requireValue(
      same || stageTransitions[prior.status]?.includes(status),
      409,
      "stage_state_or_evidence_conflict",
    );
  }
  return prior
    ? {
        sql: "NOT EXISTS(SELECT 1 FROM stage_results WHERE run_id=? AND platform_id=? AND idempotency_key=?)",
        values: [run, platform, prior.idempotency_key],
      }
    : {
        sql: "EXISTS(SELECT 1 FROM stage_results WHERE run_id=? AND platform_id=?)",
        values: [run, platform],
      };
}
