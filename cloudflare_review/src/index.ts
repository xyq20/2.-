import {
  login,
  logout,
  requireDevice,
  requireUser,
  sameOrigin,
  sessionUser,
} from "./auth";
import { reviewPage, uiStyles, uiScript } from "./ui";
import { ingestDeviceEvent } from "./device-events";
import {
  acknowledgeResume,
  adminReview,
  listReviews,
  mutateReview,
  resumeEvents,
} from "./reviews";
import {
  cleanupExpiredOriginals,
  deleteProduct,
  readAsset,
  uploadAsset,
} from "./assets";
import { HttpError, json, requireValue, text } from "./http";
import type { ReviewEnv } from "./types";
export default {
  async fetch(request: Request, env: ReviewEnv): Promise<Response> {
    try {
      const url = new URL(request.url),
        path = url.pathname,
        method = request.method;
      if (method === "GET" && path === "/health")
        return json({ ok: true, service: "kuaimai-review" });
      if (method === "GET" && path === "/")
        return reviewPage(Boolean(await sessionUser(request, env)));
      if (method === "GET" && (path === "/app.css" || path === "/app.js"))
        return new Response(path === "/app.css" ? uiStyles : uiScript, {
          headers: {
            "content-type":
              path === "/app.css"
                ? "text/css; charset=utf-8"
                : "text/javascript; charset=utf-8",
            "cache-control": "no-cache",
            "x-content-type-options": "nosniff",
          },
        });
      if (path.startsWith("/api/device/")) {
        await requireDevice(request, env);
        if (method === "POST" && path === "/api/device/events")
          return await ingestDeviceEvent(request, env);
        if (method === "GET" && path === "/api/device/resume")
          return await resumeEvents(
            env,
            text(url.searchParams.get("device_id"), "device_id"),
          );
        let match = path.match(/^\/api\/device\/resume\/([^/]+)\/ack$/);
        if (match && method === "POST")
          return await acknowledgeResume(request, env, match[1]!);
        match = path.match(/^\/api\/device\/assets\/([^/]+)$/);
        if (match && method === "PUT")
          return await uploadAsset(request, env, match[1]!);
        return json({ error: "not_found" }, 404);
      }
      if (!["GET", "HEAD"].includes(method)) sameOrigin(request);
      if (path === "/api/login" && method === "POST")
        return await login(request, env);
      const user = await requireUser(request, env);
      if (path.startsWith("/api/admin/"))
        requireValue(user.role === "admin", 403, "admin_required");
      if (path === "/api/admin/status" && method === "GET")
        return json({ ok: true });
      if (path === "/api/logout" && method === "POST")
        return await logout(request, env);
      if (path === "/api/reviews" && method === "GET")
        return await listReviews(env, user);
      let match = path.match(
        /^\/api\/reviews\/([^/]+)\/(claim|renew|confirm|skip)$/,
      );
      if (match && method === "POST")
        return await mutateReview(request, env, user, match[1]!, match[2]!);
      match = path.match(
        /^\/api\/admin\/reviews\/([^/]+)\/(invalidate|release)$/,
      );
      if (match && method === "POST")
        return await adminReview(request, env, match[1]!, match[2]!);
      match = path.match(/^\/api\/assets\/([^/]+)$/);
      if (match && method === "GET")
        return await readAsset(env, user, match[1]!);
      match = path.match(/^\/api\/admin\/products\/([^/]+)$/);
      if (match && method === "DELETE")
        return await deleteProduct(env, match[1]!);
      return json({ error: "not_found" }, 404);
    } catch (error) {
      return error instanceof HttpError
        ? json({ error: error.code }, error.status)
        : json({ error: "internal_error" }, 500);
    }
  },
  async scheduled(
    _controller: ScheduledController,
    env: ReviewEnv,
  ): Promise<void> {
    await cleanupExpiredOriginals(env, new Date());
  },
} satisfies ExportedHandler<ReviewEnv>;
