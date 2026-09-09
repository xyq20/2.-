CREATE TABLE rule_outcomes(
  idempotency_key TEXT PRIMARY KEY,
  product_version TEXT NOT NULL REFERENCES products(product_version) ON DELETE CASCADE,
  platform_id TEXT NOT NULL,
  category_leaf_id TEXT NOT NULL,
  canonical_field TEXT NOT NULL,
  condition_signature TEXT NOT NULL,
  snapshot_version TEXT NOT NULL REFERENCES option_snapshots(snapshot_version),
  target_value_id TEXT NOT NULL,
  proposed_value_id TEXT,
  accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
  created_at TEXT NOT NULL
);
CREATE INDEX rule_outcome_lookup_idx ON rule_outcomes(platform_id,category_leaf_id,canonical_field,condition_signature);
