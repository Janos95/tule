// Tule's control plane Worker (see DESIGN.md, "Architecture").
//
//   GET  /api/projects/<p>          status of the project and its machine
//   POST /api/projects/<p>/up       start (or resize) the machine: {"machine": "cpu-1"}
//   POST /api/projects/<p>/down     stop it
//   POST /api/projects/<p>/join     a join token for a machine started elsewhere
//   POST /api/projects/<p>/keys     add an SSH public key: {"key": "ssh-ed25519 ..."}
//   WSS  /api/projects/<p>/ssh      one SSH session, bytes both ways (`tule proxy`)
//   WSS  /api/projects/<p>/view     the viewer: the outputs index and meshes
//   WSS  /api/projects/<p>/agent    the machine's tule-agent dials in here (join token)
//   GET  /p/<p>                     the viewer page (static, from web/dist)
//
// Until accounts exist (milestone 4) one secret, DEV_TOKEN, authorizes every
// call: as a Bearer header, or ?t= for the viewer's WebSocket. MACHINE_URL
// (optional) is the URL machines dial instead of the request's origin
// (local dev: http://host.docker.internal:8787).
import { Project } from "./project";

export { Project };

const NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body, null, 2) + "\n", {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });

const error = (message: string, status: number, hint?: string) => json({ error: message, ...(hint ? { hint } : {}) }, status);

function same(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  if (x.length !== y.length) return false;
  return crypto.subtle.timingSafeEqual(x, y);
}

function authorized(req: Request, env: Env): boolean {
  if (!env.DEV_TOKEN) return false;
  const h = req.headers.get("Authorization") ?? "";
  const t = h.startsWith("Bearer ") ? h.slice(7) : (new URL(req.url).searchParams.get("t") ?? "");
  return same(t, env.DEV_TOKEN);
}

async function body(req: Request): Promise<Record<string, unknown>> {
  try {
    const b = await req.json();
    return b && typeof b === "object" ? (b as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/api/")
      return json({ tule: "control plane", docs: "https://github.com/Janos95/tule/blob/main/DESIGN.md" });
    const m = url.pathname.match(/^\/api\/projects\/([^/]+)(?:\/([a-z]+))?\/?$/);
    if (!m) return error("not found", 404, "the API is under /api/projects/<project>");
    const [, name, what = ""] = m;
    if (!NAME.test(name))
      return error(`invalid project name ${JSON.stringify(name)}`, 400, "use lowercase letters, digits and '-', up to 63 characters");
    const project = env.PROJECT.getByName(name);
    const ws = req.headers.get("Upgrade")?.toLowerCase() === "websocket";

    // the machine authenticates with its join token, checked by the project
    if (what === "agent") return ws ? project.fetch(req) : error("expected a WebSocket", 426);
    if (!authorized(req, env)) return error("unauthorized", 401, "send the dev token as 'Authorization: Bearer <token>' (tule login)");

    if (what === "ssh" || what === "view") return ws ? project.fetch(req) : error("expected a WebSocket", 426);
    if (what === "" && req.method === "GET") return json(await project.status(name));
    if (req.method !== "POST") return error("method not allowed", 405);
    // the project answers {error, hint} for a request it cannot do
    const reply = (r: Record<string, unknown>) => json(r, r.error ? 400 : 200);
    if (what === "up") return reply(await project.up(name, String((await body(req)).machine ?? "cpu-1"), env.MACHINE_URL || url.origin));
    if (what === "down") return reply(await project.down(name));
    if (what === "join") return reply(await project.join(name));
    if (what === "keys") return reply(await project.addKey(name, String((await body(req)).key ?? "")));
    return error(`unknown action ${JSON.stringify(what)}`, 404, "use up, down, join, keys, ssh, view");
  },
} satisfies ExportedHandler<Env>;
