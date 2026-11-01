// Tule's control plane Worker (see DESIGN.md, "Architecture", "Interfaces").
//
// Pages (pages.ts): /  /settings  /device  /authorize  /auth/*  and the
// viewer /p/<name> (static, web/dist).
//
// API for the CLI and the pages (a session cookie or an API key; auth.ts):
//   GET  /api/me                              the user, usage, RunPod connected
//   GET  /api/projects                        the user's projects;  POST {name} makes one
//   GET  /api/projects/<name>                 status of the project and its machine
//   DELETE /api/projects/<name>               delete it (machine, /workspace, snapshots)
//   POST /api/projects/<name>/rename          {"to": "new-name"}
//   POST /api/projects/<name>/up              start (or resize) the machine: {"machine": "cpu-1"}
//   POST /api/projects/<name>/down            save /workspace and stop it
//   POST /api/projects/<name>/join            a join token for a machine started elsewhere
//   POST /api/projects/<name>/snapshot        save /workspace now
//   POST /api/projects/<name>/restore         {"id": "..."}
//   GET  /api/projects/<name>/snapshots       newest first; /snapshots/<id> is a manifest
//   GET  /api/projects/<name>/blobs/<sha256>  a chunk of a snapshot (the viewer's history)
//   GET  /api/projects/<name>/outputs         the outputs index
//   WSS  /api/projects/<name>/ssh             one SSH session (`tule proxy`)
//   WSS  /api/projects/<name>/view            the viewer
//   GET/POST /api/keys/ssh, DELETE /api/keys/ssh/<id>; GET/POST /api/keys/api, DELETE /api/keys/api/<id>
//   POST /api/device/code, /api/device/token  `tule login` (device code; no auth)
//
// Machines (join token), by project id: /api/projects/<p_id>/agent (WSS),
//   /blobs/missing, /blobs/<sha256> (PUT/GET), /snapshots (POST, GET).
//
// MCP: /mcp, behind the OAuth provider (tokens from /authorize, or an API key).
import { OAuthProvider, type OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import { Accounts, type User } from "./accounts";
import { accounts, authorize, authRoutes, deviceApi, devicePage, loginUrl, sameOrigin, who } from "./auth";
import { mcp } from "./mcp";
import * as ops from "./ops";
import { landing, page, projectsPage, settingsPage, type ProjectRow } from "./pages";
import { Project, snapshotKey } from "./project";
import { gpuCatalog, runpodBalance } from "./runpod";

export { Project, Accounts };

declare global {
  interface Env {
    OAUTH_PROVIDER: OAuthHelpers;
  }
}

const PID = /^p_[a-z0-9]{8,32}$/;
const HASH = /^[0-9a-f]{64}$/;
const SNAP = /^[0-9a-z-]{8,40}$/;
const MAX_CHUNK = 16 << 20;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body, null, 2) + "\n", {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
const error = (message: string, status: number, hint?: string) => json({ error: message, ...(hint ? { hint } : {}) }, status);
const reply = (r: Record<string, unknown>) => json(r, r.error ? 400 : 200);

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

const blobKey = (id: string, h: string) => `projects/${id}/blobs/${h}`;

// ------------------------------------------------------------ chunks and snapshots

async function blobs(req: Request, env: Env, id: string, rest: string[], machine: boolean): Promise<Response> {
  if (machine && rest[0] === "missing" && req.method === "POST") {
    const hashes = (await body(req)).hashes;
    if (!Array.isArray(hashes) || hashes.length > 1000 || !hashes.every((h) => typeof h === "string" && HASH.test(h)))
      return error("expected {hashes: [sha256 hex, ...]} (at most 1000)", 400);
    const missing: string[] = [];
    for (let i = 0; i < hashes.length; i += 50) {
      const batch = hashes.slice(i, i + 50) as string[];
      const heads = await Promise.all(batch.map((h) => env.WORKSPACES.head(blobKey(id, h))));
      heads.forEach((o, j) => o || missing.push(batch[j]));
    }
    return json({ missing });
  }
  const h = rest[0];
  if (!h || !HASH.test(h)) return error("expected /blobs/<sha256 hex>", 404);
  if (req.method === "GET") {
    const o = await env.WORKSPACES.get(blobKey(id, h));
    return o
      ? new Response(o.body, { headers: { "Content-Type": "application/octet-stream", "Cache-Control": "private, max-age=31536000, immutable" } })
      : error("no such chunk", 404);
  }
  if (machine && req.method === "PUT") {
    if (Number(req.headers.get("Content-Length") ?? 0) > MAX_CHUNK) return error(`a chunk is at most ${MAX_CHUNK} bytes`, 413);
    const data = await req.arrayBuffer();
    if (data.byteLength > MAX_CHUNK) return error(`a chunk is at most ${MAX_CHUNK} bytes`, 413);
    if ((await sha256(data)) !== h) return error("the bytes do not match the hash", 400);
    await env.WORKSPACES.put(blobKey(id, h), data);
    return json({ stored: h });
  }
  return error("method not allowed", 405);
}

async function snapshotsRoute(req: Request, env: Env, id: string, rest: string[], machine: boolean, url: URL): Promise<Response> {
  const project = ops.projectStub(env, id);
  if (machine && req.method === "POST" && rest.length === 0) {
    const b = await body(req);
    if (!b.manifest || typeof b.manifest !== "object") return error("expected {reason, manifest}", 400);
    return json(await project.addSnapshot(String(b.reason ?? ""), b.manifest as Record<string, unknown>));
  }
  if (req.method !== "GET") return error("method not allowed", 405);
  if (rest.length === 0) return json(await project.snapshots(Number(url.searchParams.get("limit") ?? 50)));
  const sid = rest[0] === "latest" ? await project.latestSnapshot() : rest[0];
  if (!sid || !SNAP.test(sid)) return error("no snapshot yet", 404);
  const o = await env.WORKSPACES.get(snapshotKey(id, sid));
  return o ? new Response(o.body, { headers: { "Content-Type": "application/json" } }) : error(`no snapshot ${sid}`, 404);
}

/** A machine, by project id and join token. */
async function machineRoute(req: Request, env: Env, url: URL, id: string, what: string, rest: string[]): Promise<Response> {
  const project = ops.projectStub(env, id);
  if (what === "agent") return req.headers.get("Upgrade")?.toLowerCase() === "websocket" ? project.fetch(req) : error("expected a WebSocket", 426);
  const h = req.headers.get("Authorization") ?? "";
  const token = h.startsWith("Bearer ") ? h.slice(7) : "";
  if (!token || !(await project.checkJoin(token))) return error("invalid join token", 401);
  if (what === "blobs") return blobs(req, env, id, rest, true);
  if (what === "snapshots") return snapshotsRoute(req, env, id, rest, true, url);
  return error("machines may only use agent, blobs and snapshots", 403);
}

// ------------------------------------------------------------ the user's API

async function userApi(req: Request, env: Env, url: URL, user: User, origin: string): Promise<Response> {
  const parts = url.pathname.split("/").filter(Boolean).slice(1); // after "api"
  const a = accounts(env);
  const [area, name, what = "", ...rest] = parts;
  if (area === "me") return json({ user, ...(await ops.usage(env, user)) });
  if (area === "usage") return json(await ops.usage(env, user));
  if (area === "gpus") {
    const key = await a.runpodKey(user.id);
    if (!key) return error("RunPod is not connected", 400, "paste a RunPod API key in Settings");
    return json({ gpus: await gpuCatalog(key) });
  }
  if (area === "keys") {
    const kind = name;
    const id = what;
    if (kind === "ssh") {
      if (req.method === "GET") return json({ keys: await a.sshKeys(user.id) });
      if (req.method === "POST") {
        const b = await body(req);
        return reply(await ops.addSshKey(env, user, b.key, b.name));
      }
      if (req.method === "DELETE" && id) return reply(await ops.removeSshKey(env, user, id));
    }
    if (kind === "api") {
      if (req.method === "GET") return json({ keys: await a.apiKeys(user.id) });
      if (req.method === "POST") return json(await a.createApiKey(user.id, String((await body(req)).name ?? "API key")));
      if (req.method === "DELETE" && id) return (await a.revokeApiKey(user.id, id)) ? json({ revoked: id }) : error(`no API key ${id}`, 404);
    }
    return error("use /api/keys/ssh or /api/keys/api", 404);
  }
  if (area !== "projects") return error("not found", 404, "see the API list at the top of control/src/index.ts");
  if (!name) {
    if (req.method === "GET") return json(await ops.listProjects(env, user, origin));
    if (req.method === "POST") return reply(await ops.createProject(env, user, (await body(req)).name, origin));
    return error("method not allowed", 405);
  }
  if (what === "" && req.method === "GET") return reply(await ops.status(env, user, name, origin));
  if (what === "" && req.method === "DELETE") return reply(await ops.deleteProject(env, user, name));
  const ref = await ops.resolve(env, user, name);
  if (!ops.isRef(ref)) return json(ref, 404);
  const project = ops.projectStub(env, ref.id);
  const ws = req.headers.get("Upgrade")?.toLowerCase() === "websocket";
  if (what === "ssh" || what === "view") return ws ? project.fetch(req) : error("expected a WebSocket", 426);
  if (what === "snapshots") return snapshotsRoute(req, env, ref.id, rest, false, url);
  if (what === "blobs") return blobs(req, env, ref.id, rest, false);
  if (what === "outputs") return reply(await ops.outputs(env, user, name, origin));
  if (req.method !== "POST") return error("method not allowed", 405);
  const b = what === "down" || what === "snapshot" || what === "join" ? {} : await body(req);
  if (what === "up") return reply(await project.up(String(b.machine ?? "cpu-1"), env.MACHINE_URL || origin));
  if (what === "down") return reply(await project.down());
  if (what === "join") return reply(await ops.join(env, user, name, env.MACHINE_URL || origin));
  if (what === "rename") return reply(await ops.renameProject(env, user, name, b.to));
  if (what === "snapshot") return reply(await project.command({ t: "snapshot", reason: "manual" }));
  if (what === "restore") return reply(await project.command({ t: "restore", id: String(b.id ?? "") }));
  return error(`unknown action ${JSON.stringify(what)}`, 404, "use up, down, join, rename, snapshot, restore, snapshots, outputs, ssh, view");
}

// ------------------------------------------------------------ pages

async function projectRows(env: Env, user: User): Promise<ProjectRow[]> {
  const ps = await accounts(env).projects(user.id);
  return Promise.all(
    ps.map(async (p) => {
      const s = await ops.projectStub(env, p.id).status();
      return { ...p, state: String(s.state), machine: (s.machine as string | null) ?? null };
    }),
  );
}

async function settings(req: Request, env: Env, url: URL, user: User): Promise<Response> {
  const a = accounts(env);
  let message: string | undefined;
  let newKey: string | undefined;
  let rpError: string | undefined;
  if (req.method === "POST") {
    if (!sameOrigin(req, url)) return page("Refused", "<p>That form did not come from Tule.</p>", 403, user);
    const form = await req.formData();
    const f = (k: string) => String(form.get(k) ?? "").trim();
    const action = url.pathname.slice("/settings/".length);
    if (action === "runpod") {
      const key = f("key");
      const check = await runpodBalance(key);
      if (check.error) rpError = `RunPod refused this key: ${check.error}`;
      else {
        await a.setRunpodKey(user.id, key);
        message = "RunPod connected.";
      }
    } else if (action === "runpod/remove") {
      await a.removeRunpod(user.id);
      message = "RunPod disconnected.";
    } else if (action === "keys/new") {
      newKey = (await a.createApiKey(user.id, f("name") || "API key")).key;
    } else if (action === "keys/revoke") {
      await a.revokeApiKey(user.id, f("id"));
      message = "API key revoked.";
    } else if (action === "ssh/add") {
      const r = await ops.addSshKey(env, user, f("key"));
      message = r.error ? undefined : "SSH key added.";
      if (r.error) rpError = undefined, (message = `${r.error}${r.hint ? ` (${r.hint})` : ""}`);
    } else if (action === "ssh/remove") {
      await ops.removeSshKey(env, user, f("id"));
      message = "SSH key removed.";
    }
  }
  const rp = await a.hasRunpod(user.id);
  let balance: string | null = null;
  if (rp.connected) {
    const key = await a.runpodKey(user.id);
    const b = key ? await runpodBalance(key) : null;
    balance = b && !b.error ? `$${Number(b.balance).toFixed(2)}` : null;
  }
  return settingsPage(user, {
    runpod: { ...rp, balance, error: rpError },
    apiKeys: await a.apiKeys(user.id),
    sshKeys: await a.sshKeys(user.id),
    usage: await a.usage(user.id),
    quota: Number(env.CPU_HOURS_MONTH || 100),
    newKey,
    message,
  });
}

// ------------------------------------------------------------ the app

async function app(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const origin = env.PUBLIC_URL || url.origin;
  const path = url.pathname;

  const m = path.match(/^\/api\/projects\/(p_[^/]+)(?:\/([a-z]+))?(?:\/(.*))?$/);
  if (m) {
    if (!PID.test(m[1])) return error("not found", 404);
    return machineRoute(req, env, url, m[1], m[2] ?? "", (m[3] ?? "").split("/").filter(Boolean));
  }
  const auth = await authRoutes(req, env, url);
  if (auth) return auth;
  if (path === "/api/device/code" || path === "/api/device/token") return deviceApi(req, env, path.split("/").pop()!);

  const user = await who(req, env);
  if (path === "/authorize") return authorize(req, env, url, user);
  if (path.startsWith("/api/")) {
    if (!user) return error("not signed in", 401, "run tule login (or send an API key as 'Authorization: Bearer tule_...')");
    return userApi(req, env, url, user, origin);
  }
  if (path === "/") {
    if (!user) return landing(origin);
    return projectsPage(user, await projectRows(env, user), origin);
  }
  if (path === "/projects/new" && req.method === "POST") {
    if (!user) return Response.redirect(`${origin}${loginUrl("/")}`, 302);
    if (!sameOrigin(req, url)) return page("Refused", "<p>That form did not come from Tule.</p>", 403, user);
    const name = String((await req.formData()).get("name") ?? "").trim();
    const r = await ops.createProject(env, user, name, origin);
    if (r.error) return projectsPage(user, await projectRows(env, user), origin, `${r.error}${r.hint ? ` (${r.hint})` : ""}`);
    return new Response(null, { status: 303, headers: { Location: `/p/${name}` } });
  }
  if (path === "/settings" || path.startsWith("/settings/")) {
    if (!user) return new Response(null, { status: 302, headers: { Location: loginUrl("/settings") } });
    return settings(req, env, url, user);
  }
  if (path === "/device") {
    if (!user) return new Response(null, { status: 302, headers: { Location: loginUrl(path + url.search) } });
    return devicePage(req, env, url, user);
  }
  // /p/<name> (the viewer) checks /api/me itself and sends a signed-out visitor to sign in
  return env.ASSETS.fetch(req);
}

let provider: OAuthProvider<Env> | null = null;
let providerFor = "";

/** The OAuth provider for this deployment's public URL (static per deployment; local dev differs). */
function oauth(env: Env): OAuthProvider<Env> {
  const base = env.PUBLIC_URL;
  if (provider && providerFor === base) return provider;
  providerFor = base;
  provider = new OAuthProvider<Env>({
    apiRoute: "/mcp",
    apiHandler: {
      fetch: (req, env, ctx) => mcp(req, env as Env, String((ctx as unknown as { props: { userId: string } }).props.userId)),
    },
    defaultHandler: { fetch: (req, env) => app(req as Request, env as Env) },
    authorizeEndpoint: "/authorize",
    tokenEndpoint: "/oauth/token",
    clientRegistrationEndpoint: "/oauth/register",
    scopesSupported: ["tule"],
    requiredScopes: ["tule"],
    resourceMetadata: { resource: `${base}/mcp`, authorization_servers: [base], resource_name: "Tule" },
    // a Tule API key also works at /mcp (Claude Code, Codex: a header instead of OAuth)
    resolveExternalToken: async ({ token, env }) => {
      if (!token.startsWith("tule_")) return null;
      const user = await accounts(env as Env).apiKeyUser(token);
      return user ? { props: { userId: user.id }, audience: `${base}/mcp` } : null;
    },
  });
  return provider;
}

export default {
  fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (!env.PUBLIC_URL) return Promise.resolve(error("PUBLIC_URL is not set", 500, "set it in wrangler.jsonc vars"));
    return oauth(env).fetch(req, env, ctx);
  },
} satisfies ExportedHandler<Env>;
