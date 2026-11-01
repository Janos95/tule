// Tule's control plane Worker (see DESIGN.md, "Architecture").
//
//   GET  /api/projects/<p>                  status of the project and its machine
//   POST /api/projects/<p>/up               start (or resize) the machine: {"machine": "cpu-1"}
//   POST /api/projects/<p>/down             stop it (after a final snapshot)
//   POST /api/projects/<p>/join             a join token for a machine started elsewhere
//   POST /api/projects/<p>/keys             add an SSH public key: {"key": "ssh-ed25519 ..."}
//   POST /api/projects/<p>/snapshot         save /workspace now
//   POST /api/projects/<p>/restore          make /workspace match a snapshot: {"id": "..."}
//   GET  /api/projects/<p>/snapshots        the snapshots, newest first
//   GET  /api/projects/<p>/snapshots/<id>   a snapshot's manifest ("latest" for the newest)
//   POST /api/projects/<p>/snapshots        (machine) record a snapshot: {"reason", "manifest"}
//   POST /api/projects/<p>/blobs/missing    (machine) which chunk hashes R2 lacks
//   PUT  /api/projects/<p>/blobs/<sha256>   (machine) store a chunk; GET reads it
//   WSS  /api/projects/<p>/ssh              one SSH session, bytes both ways (`tule proxy`)
//   WSS  /api/projects/<p>/view             the viewer: the outputs index and meshes
//   WSS  /api/projects/<p>/agent            the machine's tule-agent dials in here (join token)
//   GET  /p/<p>                             the viewer page (static, from web/dist)
//
// Until accounts exist (milestone 4) one secret, DEV_TOKEN, authorizes every
// call: as a Bearer header, or ?t= for the viewer's WebSocket. Machines use
// their join token. MACHINE_URL (optional) is the URL machines dial instead
// of the request's origin (local dev: http://host.docker.internal:8787).
import { Project, snapshotKey } from "./project";

export { Project };

const NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;
const HASH = /^[0-9a-f]{64}$/;
const SNAP = /^[0-9a-z-]{8,40}$/;
const MAX_CHUNK = 16 << 20;

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

function bearer(req: Request): string {
  const h = req.headers.get("Authorization") ?? "";
  return h.startsWith("Bearer ") ? h.slice(7) : (new URL(req.url).searchParams.get("t") ?? "");
}

async function body(req: Request): Promise<Record<string, unknown>> {
  try {
    const b = await req.json();
    return b && typeof b === "object" ? (b as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function sha256(data: ArrayBuffer): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(d)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

/** The project's chunks and manifests in R2. */
const blobKey = (p: string, h: string) => `projects/${p}/blobs/${h}`;

async function blobs(req: Request, env: Env, p: string, rest: string[]): Promise<Response> {
  if (rest[0] === "missing" && req.method === "POST") {
    const hashes = (await body(req)).hashes;
    if (!Array.isArray(hashes) || hashes.length > 1000 || !hashes.every((h) => typeof h === "string" && HASH.test(h)))
      return error("expected {hashes: [sha256 hex, ...]} (at most 1000)", 400);
    const missing: string[] = [];
    for (let i = 0; i < hashes.length; i += 50) {
      const batch = hashes.slice(i, i + 50) as string[];
      const heads = await Promise.all(batch.map((h) => env.WORKSPACES.head(blobKey(p, h))));
      heads.forEach((o, j) => o || missing.push(batch[j]));
    }
    return json({ missing });
  }
  const h = rest[0];
  if (!h || !HASH.test(h)) return error("expected /blobs/<sha256 hex>", 404);
  if (req.method === "GET") {
    const o = await env.WORKSPACES.get(blobKey(p, h));
    return o ? new Response(o.body, { headers: { "Content-Type": "application/octet-stream" } }) : error("no such chunk", 404);
  }
  if (req.method === "PUT") {
    if (Number(req.headers.get("Content-Length") ?? 0) > MAX_CHUNK) return error(`a chunk is at most ${MAX_CHUNK} bytes`, 413);
    const data = await req.arrayBuffer();
    if (data.byteLength > MAX_CHUNK) return error(`a chunk is at most ${MAX_CHUNK} bytes`, 413);
    if ((await sha256(data)) !== h) return error("the bytes do not match the hash", 400);
    await env.WORKSPACES.put(blobKey(p, h), data);
    return json({ stored: h });
  }
  return error("method not allowed", 405);
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/api/") return json({ tule: "control plane", docs: "https://github.com/Janos95/tule/blob/main/DESIGN.md" });
    const m = url.pathname.match(/^\/api\/projects\/([^/]+)(?:\/(.*?))?\/?$/);
    if (!m) return error("not found", 404, "the API is under /api/projects/<project>");
    const name = m[1];
    const [what = "", ...rest] = (m[2] ?? "").split("/");
    if (!NAME.test(name))
      return error(`invalid project name ${JSON.stringify(name)}`, 400, "use lowercase letters, digits and '-', up to 63 characters");
    const project = env.PROJECT.getByName(name);
    const ws = req.headers.get("Upgrade")?.toLowerCase() === "websocket";

    // the machine authenticates with its join token, checked by the project
    if (what === "agent") return ws ? project.fetch(req) : error("expected a WebSocket", 426);
    const token = bearer(req);
    const user = !!env.DEV_TOKEN && same(token, env.DEV_TOKEN);
    const machine = !user && !!token && (await project.checkJoin(token));
    if (!user && !machine) return error("unauthorized", 401, "send the dev token as 'Authorization: Bearer <token>' (tule login)");

    if (what === "blobs") return blobs(req, env, name, rest);
    if (what === "snapshots") {
      if (req.method === "POST" && rest.length === 0) {
        const b = await body(req);
        if (!b.manifest || typeof b.manifest !== "object") return error("expected {reason, manifest}", 400);
        return json(await project.addSnapshot(name, String(b.reason ?? ""), b.manifest as Record<string, unknown>));
      }
      if (req.method !== "GET") return error("method not allowed", 405);
      if (rest.length === 0) return json(await project.snapshots(Number(url.searchParams.get("limit") ?? 50)));
      const id = rest[0] === "latest" ? await project.latestSnapshot() : rest[0];
      if (!id || !SNAP.test(id)) return error("no snapshot yet", 404);
      const o = await env.WORKSPACES.get(snapshotKey(name, id));
      return o ? new Response(o.body, { headers: { "Content-Type": "application/json" } }) : error(`no snapshot ${id}`, 404);
    }
    if (!user) return error("machines may only use blobs and snapshots", 403);

    if (what === "ssh" || what === "view") return ws ? project.fetch(req) : error("expected a WebSocket", 426);
    if (what === "" && req.method === "GET") return json(await project.status(name));
    if (req.method !== "POST") return error("method not allowed", 405);
    // the project answers {error, hint} for a request it cannot do
    const reply = (r: Record<string, unknown>) => json(r, r.error ? 400 : 200);
    if (what === "up") return reply(await project.up(name, String((await body(req)).machine ?? "cpu-1"), env.MACHINE_URL || url.origin));
    if (what === "down") return reply(await project.down(name));
    if (what === "join") return reply(await project.join(name));
    if (what === "keys") return reply(await project.addKey(name, String((await body(req)).key ?? "")));
    if (what === "snapshot") return reply(await project.command(name, { t: "snapshot", reason: "manual" }));
    if (what === "restore") return reply(await project.command(name, { t: "restore", id: String((await body(req)).id ?? "") }));
    return error(`unknown action ${JSON.stringify(what)}`, 404, "use up, down, join, keys, snapshot, restore, snapshots, ssh, view");
  },
} satisfies ExportedHandler<Env>;
