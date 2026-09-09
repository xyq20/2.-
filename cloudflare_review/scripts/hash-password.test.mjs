import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { pbkdf2Sync } from "node:crypto";
test("stdin bootstrap emits only salted 310000-iteration password material", () => {
  const result = spawnSync(process.execPath, ["scripts/hash-password.mjs"], {
    input: "local-test-password\n",
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const output = JSON.parse(result.stdout);
  assert.deepEqual(Object.keys(output).sort(), ["hash", "salt"]);
  assert.equal(
    output.hash,
    pbkdf2Sync(
      "local-test-password",
      Buffer.from(output.salt, "base64"),
      310000,
      32,
      "sha256",
    ).toString("base64"),
  );
  assert.ok(!result.stdout.includes("local-test-password"));
});
test("rejects command line passwords and empty stdin", () => {
  const arg = spawnSync(
    process.execPath,
    ["scripts/hash-password.mjs", "not-a-real-password"],
    { encoding: "utf8" },
  );
  assert.equal(arg.status, 1);
  assert.equal(arg.stdout, "");
  assert.ok(!arg.stderr.includes("not-a-real-password"));
  const empty = spawnSync(process.execPath, ["scripts/hash-password.mjs"], {
    input: "",
    encoding: "utf8",
  });
  assert.equal(empty.status, 1);
});
