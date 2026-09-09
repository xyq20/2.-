import { pbkdf2 } from "node:crypto";
import { body, HttpError, json, requireValue, text } from "./http";
import type { ReviewEnv, User } from "./types";
export const hex = (bytes: Uint8Array) =>
  Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
export async function sha256(value: string | Uint8Array): Promise<string> {
  return hex(
    new Uint8Array(
      await crypto.subtle.digest(
        "SHA-256",
        typeof value === "string" ? new TextEncoder().encode(value) : value,
      ),
    ),
  );
}
export async function constantTimeEqual(
  a: string,
  b: string,
): Promise<boolean> {
  const [left, right] = await Promise.all([
    crypto.subtle.digest("SHA-256", new TextEncoder().encode(a)),
    crypto.subtle.digest("SHA-256", new TextEncoder().encode(b)),
  ]);
  return crypto.subtle.timingSafeEqual(left, right);
}
export async function requireDevice(request: Request, env: ReviewEnv) {
  requireValue(
    env.DEVICE_TOKEN &&
      (await constantTimeEqual(
        request.headers.get("authorization") ?? "",
        `Device ${env.DEVICE_TOKEN}`,
      )),
    401,
    "unauthorized",
  );
}
export async function derivePassword(
  password: string,
  salt: string,
): Promise<string> {
  // Workers Web Crypto caps PBKDF2 iterations at 100,000. nodejs_compat crypto supports the required 310,000.
  return new Promise((resolve, reject) =>
    pbkdf2(
      password,
      Buffer.from(salt, "base64"),
      310000,
      32,
      "sha256",
      (error, result) =>
        error ? reject(error) : resolve(Buffer.from(result).toString("base64")),
    ),
  );
}
function token(request: Request) {
  return (
    request.headers
      .get("cookie")
      ?.split(";")
      .map((x) => x.trim())
      .find((x) => x.startsWith("km_session="))
      ?.slice(11) ?? ""
  );
}
export async function sessionUser(
  request: Request,
  env: ReviewEnv,
): Promise<User | null> {
  const raw = token(request);
  if (!raw) return null;
  return env.DB.prepare(
    "SELECT u.id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND s.revoked_at IS NULL AND u.active=1",
  )
    .bind(await sha256(raw), new Date().toISOString())
    .first<User>();
}
export async function requireUser(
  request: Request,
  env: ReviewEnv,
): Promise<User> {
  const user = await sessionUser(request, env);
  if (!user) throw new HttpError(401, "unauthorized");
  return user;
}
export function sameOrigin(request: Request) {
  requireValue(
    request.headers.get("origin") === new URL(request.url).origin,
    403,
    "same_origin_required",
  );
}
export async function login(
  request: Request,
  env: ReviewEnv,
): Promise<Response> {
  const data = await body(request);
  const username = text(data.username, "username", 128);
  const password = text(data.password, "password", 1024);
  const user = await env.DB.prepare(
    "SELECT * FROM users WHERE username=? AND active=1",
  )
    .bind(username)
    .first<User & { password_salt: string; password_hash: string }>();
  // Always perform the same KDF, including unknown accounts.
  const derived = await derivePassword(
    password,
    user?.password_salt ?? "AAAAAAAAAAAAAAAAAAAAAA==",
  );
  requireValue(
    user && (await constantTimeEqual(derived, user.password_hash)),
    401,
    "invalid_credentials",
  );
  const raw = hex(crypto.getRandomValues(new Uint8Array(32)));
  const now = new Date();
  await env.DB.prepare(
    "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
  )
    .bind(
      await sha256(raw),
      user.id,
      new Date(now.getTime() + 28800000).toISOString(),
      now.toISOString(),
    )
    .run();
  return Response.json(
    { ok: true },
    {
      headers: {
        "set-cookie": `km_session=${raw}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=28800`,
        "cache-control": "no-store",
      },
    },
  );
}
export async function logout(request: Request, env: ReviewEnv) {
  await env.DB.prepare("UPDATE sessions SET revoked_at=? WHERE token_hash=?")
    .bind(new Date().toISOString(), await sha256(token(request)))
    .run();
  const result = json({ ok: true });
  result.headers.set(
    "set-cookie",
    "km_session=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0",
  );
  return result;
}
