import { body, json, requireValue, text, version } from "./http";
import type { ReviewEnv, ReviewTask, User } from "./types";
const leaseMs = 10 * 60 * 1000;
export async function listReviews(env: ReviewEnv, user: User) {
  const rows = await env.DB.prepare(
    "SELECT r.*,p.title,s.options_json,s.custom_allowed FROM review_tasks r JOIN products p USING(product_version) JOIN option_snapshots s USING(snapshot_version) WHERE p.deleting=0 AND r.status IN ('pending','claimed') AND NOT EXISTS(SELECT 1 FROM run_checkpoints c WHERE c.run_id=r.run_id AND c.status IN ('failed','completed')) AND (r.claimed_by IS NULL OR r.claimed_by=? OR r.lease_until<=?) ORDER BY r.created_at,r.id LIMIT 50",
  )
    .bind(user.id, new Date().toISOString())
    .all<
      ReviewTask & {
        title: string;
        options_json: string;
        custom_allowed: number;
      }
    >();
  const tasks = [];
  for (const row of rows.results) {
    const assets = await env.DB.prepare(
      "SELECT id FROM assets WHERE product_version=? ORDER BY CASE WHEN kind='learning_thumbnail' THEN 0 ELSE 1 END,created_at,id LIMIT 8",
    )
      .bind(row.product_version)
      .all<{ id: string }>();
    const { evidence_json, options_json, ...task } = row;
    tasks.push({
      ...task,
      options: JSON.parse(options_json),
      evidence: JSON.parse(evidence_json),
      asset_ids: assets.results.map((a) => a.id),
    });
  }
  return json({ tasks });
}
export async function mutateReview(
  request: Request,
  env: ReviewEnv,
  user: User,
  id: string,
  action: string,
): Promise<Response> {
  const data = await body(request),
    v = version(data.version),
    now = new Date().toISOString(),
    lease = new Date(Date.now() + leaseMs).toISOString();
  const q = (sql: string, ...values: (string | number | null)[]) =>
    env.DB.prepare(sql).bind(...values);
  if (action === "claim" || action === "renew" || action === "skip") {
    const condition =
      action === "claim"
        ? "status IN ('pending','claimed') AND (claimed_by IS NULL OR claimed_by=? OR lease_until<=?)"
        : "status='claimed' AND claimed_by=? AND lease_until>?";
    const result = await q(
      `UPDATE review_tasks SET status=?,claimed_by=?,lease_until=?,version=version+1,updated_at=? WHERE id=? AND version=? AND ${condition} AND EXISTS(SELECT 1 FROM products WHERE product_version=review_tasks.product_version AND deleting=0)`,
      action === "skip" ? "pending" : "claimed",
      action === "skip" ? null : user.id,
      action === "skip" ? null : lease,
      now,
      id,
      v,
      user.id,
      now,
    ).run();
    requireValue(result.meta.changes, 409, "task_locked_or_updated");
    return json({
      id,
      version: v + 1,
      status: action === "skip" ? "pending" : "claimed",
      lease_until: action === "skip" ? null : lease,
    });
  }
  requireValue(action === "confirm", 404, "not_found");
  const final = text(data.final_value_id, "final_value_id");
  const reason =
    data.correction_reason === undefined
      ? null
      : text(data.correction_reason, "correction_reason", 1000);
  const task = await q(
    "SELECT * FROM review_tasks WHERE id=?",
    id,
  ).first<ReviewTask>();
  requireValue(
    task &&
      task.status === "claimed" &&
      task.claimed_by === user.id &&
      task.version === v &&
      task.lease_until! > now,
    409,
    "task_locked_or_updated",
  );
  const selection = await q(
    "SELECT s.custom_allowed,(SELECT count(*) FROM json_each(s.options_json) o WHERE json_extract(o.value,'$.value_id')=?) candidate_count FROM option_snapshots s WHERE s.snapshot_version=?",
    final,
    task.snapshot_version,
  ).first<{ custom_allowed: number; candidate_count: number }>();
  requireValue(
    Boolean(selection?.custom_allowed) || selection?.candidate_count === 1,
    422,
    "candidate_not_unique",
  );
  requireValue(
    final === task.suggested_value_id || reason,
    422,
    "correction_reason_required",
  );
  const actionId = crypto.randomUUID(),
    eventId = crypto.randomUUID();
  const payload = {
    review_id: id,
    run_id: task.run_id,
    device_id: task.device_id,
    product_version: task.product_version,
    platform_id: task.platform_id,
    category_leaf_id: task.category_leaf_id,
    field_id: task.field_id,
    snapshot_version: task.snapshot_version,
    final_value_id: final,
    version: v + 1,
  };
  const results = await env.DB.batch([
    q(
      "UPDATE review_tasks SET status='confirmed',version=version+1,updated_at=? WHERE id=? AND version=? AND status='claimed' AND claimed_by=? AND lease_until>? AND EXISTS(SELECT 1 FROM products WHERE product_version=review_tasks.product_version AND deleting=0) AND EXISTS(SELECT 1 FROM option_snapshots s WHERE s.snapshot_version=review_tasks.snapshot_version AND (s.custom_allowed=1 OR (SELECT count(*) FROM json_each(s.options_json) o WHERE json_extract(o.value,'$.value_id')=?)=1))",
      now,
      id,
      v,
      user.id,
      now,
      final,
    ),
    q(
      "INSERT INTO review_actions(id,idempotency_key,review_id,user_id,suggested_value_id,final_value_id,correction_reason,snapshot_version,task_version,created_at) SELECT ?,?,?,?,?,?,?,?,?,? WHERE changes()=1",
      actionId,
      `confirm:${id}:${v}`,
      id,
      user.id,
      task.suggested_value_id,
      final,
      reason,
      task.snapshot_version,
      v,
      now,
    ),
    q(
      "INSERT INTO attribute_decisions(id,product_version,platform_id,category_leaf_id,field_id,canonical_field,snapshot_version,proposed_value_id,final_value_id,source,status,reason_code,evidence_json,created_at,updated_at) SELECT ?,?,?,?,?,?,?,?,?, 'human','confirmed',?,?,?,? WHERE EXISTS(SELECT 1 FROM review_actions WHERE id=?)",
      actionId,
      task.product_version,
      task.platform_id,
      task.category_leaf_id,
      task.field_id,
      task.canonical_field,
      task.snapshot_version,
      task.suggested_value_id,
      final,
      task.reason_code,
      task.evidence_json,
      now,
      now,
      actionId,
    ),
    q(
      "INSERT INTO device_events(id,idempotency_key,event_type,product_version,device_id,review_id,payload_json,created_at) SELECT ?,?,'resume_ready',?,?,?,?,? WHERE EXISTS(SELECT 1 FROM review_actions WHERE id=?)",
      eventId,
      `resume_ready:${id}:${v + 1}`,
      task.product_version,
      task.device_id,
      id,
      JSON.stringify(payload),
      now,
      actionId,
    ),
    q(
      "UPDATE review_tasks SET status='resume_ready' WHERE id=? AND EXISTS(SELECT 1 FROM review_actions WHERE id=?)",
      id,
      actionId,
    ),
  ]);
  requireValue(results[0]!.meta.changes, 409, "task_locked_or_updated");
  return json({ id, version: v + 1, status: "resume_ready" });
}
export async function resumeEvents(env: ReviewEnv, deviceId: string) {
  const rows = await env.DB.prepare(
    "SELECT e.id,e.payload_json FROM device_events e JOIN review_tasks r ON r.id=e.review_id JOIN products p ON p.product_version=e.product_version WHERE e.device_id=? AND e.event_type='resume_ready' AND e.processed_at IS NULL AND r.status='resume_ready' AND p.deleting=0 ORDER BY e.created_at LIMIT 100",
  )
    .bind(deviceId)
    .all<{ id: string; payload_json: string }>();
  return json({
    events: rows.results.map((row) => ({
      event_id: row.id,
      payload: JSON.parse(row.payload_json),
    })),
  });
}
export async function acknowledgeResume(
  request: Request,
  env: ReviewEnv,
  id: string,
) {
  const data = await body(request);
  requireValue(
    data.checkpoint_persisted === true &&
      typeof data.checkpoint_id === "string" &&
      data.checkpoint_id.length > 0,
    422,
    "persist_checkpoint_before_ack",
  );
  const deviceId = text(data.device_id, "device_id"),
    checkpointId = text(data.checkpoint_id, "checkpoint_id");
  const event = await env.DB.prepare(
    "SELECT e.*,r.status task_status FROM device_events e JOIN review_tasks r ON r.id=e.review_id WHERE e.id=? AND e.device_id=? AND e.event_type='resume_ready'",
  )
    .bind(id, deviceId)
    .first<{
      processed_at: string | null;
      checkpoint_id: string | null;
      review_id: string;
      task_status: string;
    }>();
  requireValue(
    event && ["resume_ready", "consumed"].includes(event.task_status),
    409,
    "resume_unavailable",
  );
  if (event.processed_at) {
    requireValue(
      event.checkpoint_id === checkpointId,
      409,
      "checkpoint_conflict",
    );
    return json({ ok: true });
  }
  const now = new Date().toISOString();
  const result = await env.DB.batch([
    env.DB.prepare(
      "UPDATE device_events SET processed_at=?,checkpoint_id=? WHERE id=? AND processed_at IS NULL AND EXISTS(SELECT 1 FROM review_tasks r WHERE r.id=review_id AND r.status='resume_ready')",
    ).bind(now, checkpointId, id),
    env.DB.prepare(
      "UPDATE review_tasks SET status='consumed',version=version+1,updated_at=? WHERE id=? AND status='resume_ready' AND changes()=1",
    ).bind(now, event.review_id),
  ]);
  requireValue(result[0]!.meta.changes, 409, "resume_updated");
  return json({ ok: true });
}
export async function adminReview(
  request: Request,
  env: ReviewEnv,
  id: string,
  action: string,
) {
  const data = await body(request),
    v = version(data.version);
  requireValue(
    action === "invalidate" || action === "release",
    404,
    "not_found",
  );
  const allowed =
    action === "invalidate"
      ? "('pending','claimed','confirmed','resume_ready')"
      : "('claimed')";
  const status = action === "invalidate" ? "invalidated" : "pending";
  const update = env.DB.prepare(
    `UPDATE review_tasks SET status=?,claimed_by=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=? AND version=? AND status IN ${allowed}`,
  ).bind(status, new Date().toISOString(), id, v);
  const statements = [update];
  if (action === "invalidate")
    statements.push(
      env.DB.prepare(
        "UPDATE attribute_decisions SET status='invalidated',updated_at=? WHERE changes()=1 AND id IN (SELECT id FROM review_actions WHERE review_id=?)",
      ).bind(new Date().toISOString(), id),
    );
  const results = await env.DB.batch(statements);
  requireValue(results[0]!.meta.changes, 409, "task_updated");
  return json({ id, version: v + 1, status });
}
