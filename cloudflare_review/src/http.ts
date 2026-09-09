export class HttpError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(code);
  }
}
export function requireValue(
  condition: unknown,
  status: number,
  code: string,
): asserts condition {
  if (!condition) throw new HttpError(status, code);
}
export const json = (value: unknown, status = 200) =>
  Response.json(value, { status, headers: { "cache-control": "no-store" } });
export async function readLimited(
  request: Request,
  max = 256 * 1024,
): Promise<Uint8Array> {
  const length = request.headers.get("content-length");
  if (length && Number(length) > max)
    throw new HttpError(413, "body_too_large");
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > max) {
        await reader.cancel();
        throw new HttpError(413, "body_too_large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.length;
  }
  return result;
}
export async function body(request: Request): Promise<Record<string, unknown>> {
  requireValue(
    request.headers.get("content-type")?.split(";")[0] === "application/json",
    415,
    "json_required",
  );
  try {
    const value: unknown = JSON.parse(
      new TextDecoder().decode(await readLimited(request)),
    );
    requireValue(
      value && typeof value === "object" && !Array.isArray(value),
      400,
      "invalid_json",
    );
    return value as Record<string, unknown>;
  } catch (e) {
    if (e instanceof HttpError) throw e;
    throw new HttpError(400, "invalid_json");
  }
}
export function text(value: unknown, name: string, max = 256): string {
  requireValue(
    typeof value === "string" && value.length > 0 && value.length <= max,
    400,
    "invalid_" + name,
  );
  return value;
}
export function identifier(value: unknown, name: string): string {
  const v = text(value, name);
  requireValue(/^[a-zA-Z0-9_.:-]+$/.test(v), 400, "invalid_" + name);
  return v;
}
export function version(value: unknown): number {
  requireValue(
    Number.isSafeInteger(value) && Number(value) > 0,
    400,
    "invalid_version",
  );
  return Number(value);
}
