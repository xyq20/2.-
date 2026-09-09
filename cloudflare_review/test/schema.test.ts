import { expect, it } from "vitest";
import { env } from "cloudflare:test";
import { setup } from "./helpers";
setup();
it("creates every authoritative table and foreign key enforcement", async () => {
  const db = env.DB;
  const rows = await db
    .prepare("SELECT name FROM sqlite_master WHERE type='table'")
    .all<{ name: string }>();
  for (const table of [
    "users",
    "sessions",
    "device_events",
    "products",
    "assets",
    "visual_facts",
    "text_facts",
    "platform_fields",
    "option_snapshots",
    "attribute_decisions",
    "conditional_rules",
    "review_tasks",
    "review_actions",
    "run_checkpoints",
    "stage_results",
    "persisted_readbacks",
  ])
    expect(rows.results.map((r) => r.name)).toContain(table);
});
