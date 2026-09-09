import { sha256 } from "./auth";
import { identifier, json, readLimited, requireValue } from "./http";
import type { ReviewEnv, User } from "./types";
type AssetEnv = Pick<ReviewEnv, "DB" | "ASSETS">;
export const MAX_ASSET_BYTES = 10 * 1024 * 1024;
export async function uploadAsset(
  request: Request,
  env: AssetEnv,
  hash: string,
) {
  requireValue(/^[a-f0-9]{64}$/.test(hash), 400, "invalid_sha256");
  const product = identifier(
    request.headers.get("x-product-version"),
    "product_version",
  );
  const kind = request.headers.get("x-asset-kind");
  const contentType = request.headers.get("content-type");
  requireValue(
    kind === "original" || kind === "learning_thumbnail",
    400,
    "invalid_asset_kind",
  );
  requireValue(
    contentType &&
      ["image/jpeg", "image/png", "image/webp"].includes(contentType),
    415,
    "unsupported_image_type",
  );
  requireValue(
    await env.DB.prepare(
      "SELECT product_version FROM products WHERE product_version=? AND deleting=0",
    )
      .bind(product)
      .first(),
    422,
    "product_missing",
  );
  const bytes = await readLimited(request, MAX_ASSET_BYTES);
  requireValue(
    bytes.length > 0 && (await sha256(bytes)) === hash,
    422,
    "asset_hash_mismatch",
  );
  const key = `products/${product}/${hash}/${kind}`;
  const prior = await env.DB.prepare("SELECT id FROM assets WHERE r2_key=?")
    .bind(key)
    .first<{ id: string }>();
  if (prior) return json({ asset_id: prior.id });
  await env.ASSETS.put(key, bytes, { httpMetadata: { contentType } });
  const id = crypto.randomUUID(),
    now = new Date();
  const result = await env.DB.prepare(
    "INSERT OR IGNORE INTO assets(id,product_version,r2_key,sha256,kind,content_type,byte_size,delete_after,created_at) SELECT ?,?,?,?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM products WHERE product_version=? AND deleting=0)",
  )
    .bind(
      id,
      product,
      key,
      hash,
      kind,
      contentType,
      bytes.length,
      kind === "original"
        ? new Date(now.getTime() + 30 * 86400000).toISOString()
        : null,
      now.toISOString(),
      product,
    )
    .run();
  const stored = await env.DB.prepare("SELECT id FROM assets WHERE r2_key=?")
    .bind(key)
    .first<{ id: string }>();
  if (!stored) {
    await env.ASSETS.delete(key);
    requireValue(false, 409, "product_deleting");
  }
  return json({ asset_id: stored.id }, result.meta.changes ? 201 : 200);
}
export async function readAsset(env: AssetEnv, user: User, id: string) {
  const row = await env.DB.prepare(
    "SELECT a.* FROM assets a JOIN products p USING(product_version) WHERE a.id=? AND p.deleting=0",
  )
    .bind(id)
    .first<{ product_version: string; r2_key: string; content_type: string }>();
  requireValue(row, 404, "asset_missing");
  if (user.role !== "admin")
    requireValue(
      await env.DB.prepare(
        "SELECT id FROM review_tasks WHERE product_version=? AND claimed_by=? AND lease_until>? AND status='claimed' LIMIT 1",
      )
        .bind(row.product_version, user.id, new Date().toISOString())
        .first(),
      403,
      "asset_forbidden",
    );
  const object = await env.ASSETS.get(row.r2_key);
  requireValue(object, 404, "asset_expired");
  return new Response(object.body, {
    headers: {
      "content-type": row.content_type,
      "cache-control": "private, no-store",
      "x-content-type-options": "nosniff",
    },
  });
}
export async function deleteProduct(env: AssetEnv, product: string) {
  // Tombstone first: retries continue deletion and concurrent writes are rejected.
  await env.DB.prepare("UPDATE products SET deleting=1 WHERE product_version=?")
    .bind(product)
    .run();
  const keys = await env.DB.prepare(
    "SELECT r2_key FROM assets WHERE product_version=?",
  )
    .bind(product)
    .all<{ r2_key: string }>();
  for (let i = 0; i < keys.results.length; i += 100)
    await env.ASSETS.delete(
      keys.results.slice(i, i + 100).map((x) => x.r2_key),
    );
  // Rules may aggregate this product's learning. Conservatively disable matching rules and reset counts.
  await env.DB.batch([
    env.DB.prepare(
      "UPDATE conditional_rules SET status='disabled',consecutive_confirmations=0,accepted_count=0,total_count=0,updated_at=? WHERE EXISTS(SELECT 1 FROM attribute_decisions d WHERE d.product_version=? AND d.platform_id=conditional_rules.platform_id AND d.category_leaf_id=conditional_rules.category_leaf_id AND d.canonical_field=conditional_rules.canonical_field)",
    ).bind(new Date().toISOString(), product),
    env.DB.prepare("DELETE FROM products WHERE product_version=?").bind(
      product,
    ),
  ]);
  return json({ ok: true });
}
export async function cleanupExpiredOriginals(
  env: AssetEnv,
  now: Date,
): Promise<number> {
  const rows = await env.DB.prepare(
    "SELECT id,r2_key FROM assets WHERE kind='original' AND delete_after<=? ORDER BY delete_after LIMIT 100",
  )
    .bind(now.toISOString())
    .all<{ id: string; r2_key: string }>();
  if (!rows.results.length) return 0;
  await env.ASSETS.delete(rows.results.map((row) => row.r2_key));
  await env.DB.batch(
    rows.results.map((row) =>
      env.DB.prepare(
        "DELETE FROM assets WHERE id=? AND kind='original' AND delete_after<=?",
      ).bind(row.id, now.toISOString()),
    ),
  );
  return rows.results.length;
}
