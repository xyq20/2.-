import { randomBytes, pbkdf2Sync } from "node:crypto";
if (process.argv.length !== 2 || process.stdin.isTTY) {
  process.stderr.write(
    "Read the password from piped stdin; do not pass it as an argument.\n",
  );
  process.exit(1);
}
try {
  let input = "";
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > 4096) throw Error("invalid");
  }
  const password = input.replace(/\r?\n$/, "");
  if (password.length < 12 || password.length > 1024) throw Error("invalid");
  const salt = randomBytes(16);
  const hash = pbkdf2Sync(password, salt, 310000, 32, "sha256");
  process.stdout.write(
    JSON.stringify({
      salt: salt.toString("base64"),
      hash: hash.toString("base64"),
    }) + "\n",
  );
} catch {
  process.stderr.write("Password must contain 12 to 1024 characters.\n");
  process.exit(1);
}
