import {
  body,
  HttpError,
  identifier,
  json,
  requireValue,
  text,
  version,
} from "./http";
import type {
  DeviceEventInput,
  DeviceEventType,
  Json,
  ReviewEnv,
} from "./types";
import {
  stableJson,
  validateCheckpoint,
  validateStage,
  type StateGuard,
} from "./event-state";
const fields: Record<
  DeviceEventType,
  { required: string[]; optional: string[] }
> = {
  "product.upsert": {
    required: ["product_version", "style_code", "title"],
    optional: ["category_json"],
  },
  "snapshot.created": {
    required: [
      "snapshot_version",
      "platform_id",
      "category_leaf_id",
      "field_id",
      "field_label",
      "schema_version",
      "custom_allowed",
      "options",
    ],
    optional: ["canonical_field", "control_type"],
  },
  "review.created": {
    required: [
      "run_id",
      "device_id",
      "product_version",
      "platform_id",
      "category_leaf_id",
      "field_id",
      "field_label",
      "snapshot_version",
      "reason_code",
      "evidence_json",
    ],
    optional: ["id", "canonical_field", "suggested_value_id"],
  },
  "checkpoint.updated": {
    required: [
      "run_id",
      "product_version",
      "device_id",
      "execution_mode",
      "platform_order",
      "current_index",
      "status",
      "pending_review_id",
      "version",
    ],
    optional: ["image_version"],
  },
  "stage.completed": {
    required: [
      "run_id",
      "platform_id",
      "status",
      "expected_json",
      "readback_json",
      "verified",
    ],
    optional: ["product_version"],
  },
  "readback.recorded": {
    required: [
      "run_id",
      "product_version",
      "platform_id",
      "category_leaf_id",
      "field_id",
      "snapshot_version",
      "verified",
      "payload_json",
    ],
    optional: ["actual_value_id", "actual_label"],
  },
  "text_facts.created": {
    required: ["product_version", "source", "payload_json"],
    optional: ["id"],
  },
};
function safeObject(value: unknown, depth = 0): void {
  requireValue(depth < 16, 400, "payload_too_deep");
  if (value && typeof value === "object")
    for (const [key, nested] of Object.entries(value)) {
      requireValue(
        !/(password|passwd|token|cookie|authorization|secret|api.?key|chrome.?profile)/i.test(
          key,
        ),
        400,
        "sensitive_key_rejected",
      );
      safeObject(nested, depth + 1);
    }
}
export function validateDeviceEvent(
  data: Record<string, unknown>,
): DeviceEventInput {
  requireValue(
    Object.keys(data).every((k) =>
      ["idempotency_key", "event_type", "payload"].includes(k),
    ),
    400,
    "unknown_envelope_key",
  );
  const key = text(data.idempotency_key, "idempotency_key");
  const type = text(data.event_type, "event_type");
  requireValue(Object.hasOwn(fields, type), 400, "unknown_event_type");
  requireValue(
    data.payload &&
      typeof data.payload === "object" &&
      !Array.isArray(data.payload),
    400,
    "invalid_payload",
  );
  safeObject(data.payload);
  const p = data.payload as Record<string, Json>;
  const spec = fields[type as DeviceEventType];
  for (const required of spec.required)
    requireValue(Object.hasOwn(p, required), 400, "missing_" + required);
  requireValue(
    Object.keys(p).every((k) =>
      [...spec.required, ...spec.optional].includes(k),
    ),
    400,
    "invalid_payload_keys",
  );
  const structured = new Set([
    "category_json",
    "evidence_json",
    "expected_json",
    "readback_json",
    "payload_json",
    "options",
    "platform_order",
  ]);
  for (const [key, value] of Object.entries(p)) {
    if (
      (type === "readback.recorded" && key === "actual_value_id") ||
      (type === "review.created" &&
        key === "suggested_value_id" &&
        value === "")
    )
      continue;
    if (structured.has(key)) {
      requireValue(
        value !== null && typeof value === "object",
        400,
        "invalid_" + key,
      );
      continue;
    }
    if (["custom_allowed", "verified"].includes(key)) {
      requireValue(typeof value === "boolean", 400, "invalid_" + key);
      continue;
    }
    if (key === "current_index") {
      requireValue(
        Number.isSafeInteger(value) && Number(value) >= 0,
        400,
        "invalid_index",
      );
      continue;
    }
    if (key === "version") {
      version(value);
      continue;
    }
    if (
      value === null &&
      [
        "pending_review_id",
        "canonical_field",
        "suggested_value_id",
        "snapshot_version",
        "actual_value_id",
        "actual_label",
      ].includes(key)
    )
      continue;
    text(value, key, key === "title" ? 1000 : 256);
  }
  if (p.product_version) identifier(p.product_version, "product_version");
  if (type === "snapshot.created") {
    requireValue(
      Array.isArray(p.options) &&
        p.options.length > 0 &&
        p.options.length <= 500,
      400,
      "invalid_options",
    );
    for (const option of p.options) {
      requireValue(
        option && typeof option === "object" && !Array.isArray(option),
        400,
        "invalid_option",
      );
      requireValue(
        Object.keys(option).every((k) =>
          ["value_id", "label", "position"].includes(k),
        ),
        400,
        "invalid_option",
      );
      text(option.value_id, "value_id");
      text(option.label, "label");
      requireValue(
        Number.isSafeInteger(option.position) && Number(option.position) >= 0,
        400,
        "invalid_position",
      );
    }
  }
  if (type === "checkpoint.updated") {
    requireValue(
      Array.isArray(p.platform_order) &&
        p.platform_order.length > 0 &&
        p.platform_order.every((x) => typeof x === "string"),
      400,
      "invalid_platform_order",
    );
    requireValue(
      Number(p.current_index) <= p.platform_order.length,
      400,
      "invalid_index",
    );
  }
  return {
    idempotency_key: key,
    event_type: type as DeviceEventType,
    payload: p,
  };
}
export async function ingestDeviceEvent(
  request: Request,
  env: ReviewEnv,
): Promise<Response> {
  const event = validateDeviceEvent(await body(request));
  const p = { ...event.payload };
  let stateGuard: StateGuard | undefined;
  let eventProduct = p.product_version ?? null;
  const existing = await env.DB.prepare(
    "SELECT id,event_type,payload_json FROM device_events WHERE idempotency_key=?",
  )
    .bind(event.idempotency_key)
    .first<{ id: string; event_type: string; payload_json: string }>();
  if (existing) {
    requireValue(
      existing.event_type === event.event_type &&
        stableJson(JSON.parse(existing.payload_json)) ===
          stableJson(event.payload),
      409,
      "idempotency_payload_conflict",
    );
    return json({ event_id: existing.id }, 409);
  }
  if (p.product_version) {
    const prod = await env.DB.prepare(
      "SELECT deleting FROM products WHERE product_version=?",
    )
      .bind(p.product_version)
      .first<{ deleting: number }>();
    requireValue(!prod?.deleting, 409, "product_deleting");
  }
  if (
    event.event_type === "review.created" ||
    event.event_type === "readback.recorded"
  ) {
    const snapshot = await env.DB.prepare(
      "SELECT * FROM option_snapshots WHERE snapshot_version=? AND platform_id=? AND category_leaf_id=? AND field_id=?",
    )
      .bind(p.snapshot_version, p.platform_id, p.category_leaf_id, p.field_id)
      .first<{
        field_label: string;
        schema_version: string;
        options_json: string;
        custom_allowed: number;
      }>();
    requireValue(snapshot, 422, "snapshot_mismatch");
    const options = JSON.parse(snapshot.options_json) as { value_id: string }[];
    if (event.event_type === "review.created") {
      requireValue(
        p.field_label === snapshot.field_label,
        422,
        "snapshot_label_mismatch",
      );
      const mapping = await env.DB.prepare(
        "SELECT canonical_field FROM platform_fields WHERE platform_id=? AND category_leaf_id=? AND source_field_id=? AND schema_version=?",
      )
        .bind(
          p.platform_id,
          p.category_leaf_id,
          p.field_id,
          snapshot.schema_version,
        )
        .first<{ canonical_field: string | null }>();
      const canonical = mapping?.canonical_field ?? null;
      requireValue(
        p.canonical_field === undefined ||
          p.canonical_field === null ||
          p.canonical_field === canonical,
        422,
        "canonical_field_mismatch",
      );
      p.canonical_field = canonical;
      if (canonical === null) p.reason_code = "field_mapping_required";
      p.suggested_value_id = p.suggested_value_id || null;
      requireValue(
        p.suggested_value_id === null ||
          options.filter((option) => option.value_id === p.suggested_value_id)
            .length === 1,
        422,
        "suggested_candidate_not_unique",
      );
    } else {
      requireValue(
        typeof p.actual_value_id === "string" &&
          p.actual_value_id.length > 0 &&
          p.actual_value_id.length <= 256,
        422,
        "actual_value_required",
      );
      requireValue(
        snapshot.custom_allowed === 1 ||
          options.filter((option) => option.value_id === p.actual_value_id)
            .length === 1,
        422,
        "actual_candidate_not_unique",
      );
    }
  }
  if (
    event.event_type === "readback.recorded" ||
    event.event_type === "stage.completed"
  ) {
    const checkpoint = await env.DB.prepare(
      "SELECT product_version,platform_order_json FROM run_checkpoints WHERE run_id=?",
    )
      .bind(p.run_id)
      .first<{ product_version: string; platform_order_json: string }>();
    requireValue(
      checkpoint &&
        (!p.product_version ||
          checkpoint.product_version === p.product_version) &&
        JSON.parse(checkpoint.platform_order_json).includes(p.platform_id),
      422,
      "checkpoint_mismatch",
    );
    eventProduct = checkpoint.product_version;
  }
  if (event.event_type === "checkpoint.updated")
    stateGuard = await validateCheckpoint(env, p);
  if (event.event_type === "stage.completed")
    stateGuard = await validateStage(env, p);
  const id = crypto.randomUUID(),
    now = new Date().toISOString();
  const q = (sql: string, ...values: (Json | undefined)[]) =>
    env.DB.prepare(sql).bind(
      ...values.map((v) =>
        v === undefined
          ? null
          : typeof v === "object" && v !== null
            ? JSON.stringify(v)
            : v,
      ),
    );
  const statements = [
    q(
      "INSERT OR IGNORE INTO device_events(id,idempotency_key,event_type,payload_json,created_at,device_id) VALUES(?,?,?,?,?,?)",
      id,
      event.idempotency_key,
      event.event_type,
      event.payload,
      now,
      p.device_id,
    ),
  ];
  // NOT NULL violation aborts the entire batch if a state changed after validation.
  // The predicate is gated by our newly inserted event ID so an idempotent race is harmless.
  if (stateGuard)
    statements.push(
      q(
        `UPDATE device_events SET idempotency_key=NULL WHERE id=? AND (${stateGuard.sql})`,
        id,
        ...stateGuard.values,
      ),
    );
  const guard = " WHERE EXISTS(SELECT 1 FROM device_events WHERE id=?)";
  const insert = (
    table: string,
    columns: string[],
    values: (Json | undefined)[],
    suffix = "",
  ) =>
    statements.push(
      q(
        `INSERT INTO ${table}(${columns.join(",")}) SELECT ${columns.map(() => "?").join(",")}${guard} ${suffix}`,
        ...values,
        id,
      ),
    );
  switch (event.event_type) {
    case "product.upsert":
      insert(
        "products",
        [
          "product_version",
          "style_code",
          "title",
          "category_json",
          "created_at",
          "updated_at",
        ],
        [
          p.product_version,
          p.style_code,
          p.title,
          p.category_json ?? {},
          now,
          now,
        ],
        "ON CONFLICT(product_version) DO UPDATE SET style_code=excluded.style_code,title=excluded.title,category_json=excluded.category_json,updated_at=excluded.updated_at WHERE products.deleting=0",
      );
      break;
    case "snapshot.created": {
      // Snapshots are immutable. A reused version with different candidates is rejected below.
      const old = await env.DB.prepare(
        "SELECT options_json,platform_id,category_leaf_id,field_id,schema_version FROM option_snapshots WHERE snapshot_version=?",
      )
        .bind(p.snapshot_version)
        .first<Record<string, string>>();
      requireValue(
        !old ||
          (old.options_json === JSON.stringify(p.options) &&
            [
              "platform_id",
              "category_leaf_id",
              "field_id",
              "schema_version",
            ].every((k) => old[k] === p[k])),
        409,
        "snapshot_version_conflict",
      );
      insert(
        "option_snapshots",
        [
          "snapshot_version",
          "platform_id",
          "category_leaf_id",
          "field_id",
          "field_label",
          "schema_version",
          "custom_allowed",
          "options_json",
          "created_at",
        ],
        [
          p.snapshot_version,
          p.platform_id,
          p.category_leaf_id,
          p.field_id,
          p.field_label,
          p.schema_version,
          p.custom_allowed ? 1 : 0,
          p.options,
          now,
        ],
        // Enforce immutability again inside the transaction, including concurrent first creation.
        "ON CONFLICT(snapshot_version) DO UPDATE SET options_json=CASE WHEN option_snapshots.options_json=excluded.options_json AND option_snapshots.platform_id=excluded.platform_id AND option_snapshots.category_leaf_id=excluded.category_leaf_id AND option_snapshots.field_id=excluded.field_id AND option_snapshots.schema_version=excluded.schema_version AND option_snapshots.field_label=excluded.field_label AND option_snapshots.custom_allowed=excluded.custom_allowed THEN option_snapshots.options_json ELSE NULL END",
      );
      insert(
        "platform_fields",
        [
          "id",
          "platform_id",
          "category_leaf_id",
          "source_field_id",
          "label",
          "canonical_field",
          "control_type",
          "custom_allowed",
          "schema_version",
          "created_at",
        ],
        [
          `${p.platform_id}:${p.category_leaf_id}:${p.field_id}:${p.schema_version}`,
          p.platform_id,
          p.category_leaf_id,
          p.field_id,
          p.field_label,
          p.canonical_field,
          p.control_type ?? "select",
          p.custom_allowed ? 1 : 0,
          p.schema_version,
          now,
        ],
        "ON CONFLICT DO NOTHING",
      );
      break;
    }
    case "review.created":
      insert(
        "review_tasks",
        [
          "id",
          "idempotency_key",
          "run_id",
          "device_id",
          "product_version",
          "platform_id",
          "category_leaf_id",
          "field_id",
          "field_label",
          "canonical_field",
          "snapshot_version",
          "suggested_value_id",
          "status",
          "reason_code",
          "evidence_json",
          "created_at",
          "updated_at",
        ],
        [
          p.id ?? id,
          event.idempotency_key,
          p.run_id,
          p.device_id,
          p.product_version,
          p.platform_id,
          p.category_leaf_id,
          p.field_id,
          p.field_label,
          p.canonical_field,
          p.snapshot_version,
          p.suggested_value_id,
          "pending",
          p.reason_code,
          p.evidence_json,
          now,
          now,
        ],
      );
      break;
    case "checkpoint.updated":
      insert(
        "run_checkpoints",
        [
          "run_id",
          "product_version",
          "device_id",
          "execution_mode",
          "platform_order_json",
          "current_index",
          "status",
          "pending_review_id",
          "version",
          "image_version",
          "updated_at",
        ],
        [
          p.run_id,
          p.product_version,
          p.device_id,
          p.execution_mode,
          p.platform_order,
          p.current_index,
          p.status,
          p.pending_review_id,
          p.version,
          p.image_version ?? "",
          now,
        ],
        "ON CONFLICT(run_id) DO UPDATE SET current_index=excluded.current_index,status=excluded.status,pending_review_id=excluded.pending_review_id,version=excluded.version,updated_at=excluded.updated_at WHERE excluded.version>run_checkpoints.version",
      );
      break;
    case "stage.completed":
      insert(
        "stage_results",
        [
          "idempotency_key",
          "run_id",
          "platform_id",
          "status",
          "expected_json",
          "readback_json",
          "verified",
          "updated_at",
        ],
        [
          event.idempotency_key,
          p.run_id,
          p.platform_id,
          p.status,
          p.expected_json,
          p.readback_json,
          p.verified ? 1 : 0,
          now,
        ],
        "ON CONFLICT(run_id,platform_id) DO UPDATE SET idempotency_key=excluded.idempotency_key,status=excluded.status,expected_json=excluded.expected_json,readback_json=excluded.readback_json,verified=excluded.verified,updated_at=excluded.updated_at WHERE excluded.status<>stage_results.status",
      );
      break;
    case "readback.recorded":
      insert(
        "persisted_readbacks",
        [
          "idempotency_key",
          "run_id",
          "product_version",
          "platform_id",
          "field_id",
          "snapshot_version",
          "actual_value_id",
          "actual_label",
          "verified",
          "payload_json",
          "created_at",
        ],
        [
          event.idempotency_key,
          p.run_id,
          p.product_version,
          p.platform_id,
          p.field_id,
          p.snapshot_version,
          p.actual_value_id,
          p.actual_label,
          p.verified ? 1 : 0,
          p.payload_json,
          now,
        ],
      );
      break;
    case "text_facts.created":
      insert(
        "text_facts",
        ["id", "product_version", "source", "payload_json", "created_at"],
        [p.id ?? id, p.product_version, p.source, p.payload_json, now],
      );
      break;
  }
  statements.push(
    q(
      "UPDATE device_events SET product_version=?,processed_at=? WHERE id=?",
      eventProduct,
      now,
      id,
    ),
  );
  try {
    const results = await env.DB.batch(statements);
    if (!results[0]!.meta.changes) {
      const raced = await env.DB.prepare(
        "SELECT id,event_type,payload_json FROM device_events WHERE idempotency_key=?",
      )
        .bind(event.idempotency_key)
        .first<{ id: string; event_type: string; payload_json: string }>();
      requireValue(
        raced &&
          raced.event_type === event.event_type &&
          stableJson(JSON.parse(raced.payload_json)) ===
            stableJson(event.payload),
        409,
        "idempotency_payload_conflict",
      );
      return json({ event_id: raced!.id }, 409);
    }
  } catch (error) {
    if (error instanceof HttpError) throw error;
    if (
      stateGuard &&
      error instanceof Error &&
      error.message.includes("device_events.idempotency_key")
    )
      throw new HttpError(409, "concurrent_state_conflict");
    throw new HttpError(422, "event_dependency_or_conflict");
  }
  return json({ event_id: id }, 201);
}
