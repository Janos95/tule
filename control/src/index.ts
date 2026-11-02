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
import { accounts, authorize, authRoutes, deviceApi, isAdmin, devicePage, loginUrl, sameOrigin, SESSION_COOKIE, setCookie, who } from "./auth";
import { devMode, devRoutes, RELOAD_SCRIPT } from "./dev";
import { mcp } from "./mcp";
import * as ops from "./ops";
import { landing, page, projectsPage, settingsPage, type ProjectRow } from "./pages";
import { Host } from "./host";
import { gitKey, lfsKey, Project, snapshotKey } from "./project";
import { stub } from "./rpc";
import { gpuCatalog, runpodBalance } from "./runpod";

export { Project, Accounts, Host };

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

// ------------------------------------------------------------ git and Git LFS (saves)
//
// /api/projects/<p_id>/git: the project's remote for cli/tule_cli/gitremote.py
//   GET  refs                       {refs: {"refs/heads/main": sha}, bundles: [{id, tip}]} (push order)
//   GET  bundles/<id>               a pushed bundle
//   POST push?ref=&old=&new=        the bundle of the new commits (no body: only move or delete the ref)
// /api/projects/<p_id>/lfs: the Git LFS API (batch, basic transfers)
//   POST objects/batch              {operation, objects: [{oid, size}]}
//   PUT/GET objects/<oid>           the object (a PUT is checked against its sha256)
// Either a machine's join token or the owner's API key, as Bearer or Basic (git-lfs) auth.

const OID = /^[0-9a-f]{64}$/;
const BUNDLE = /^[0-9a-z]{6,40}$/;

/** The token from "Bearer x" or "Basic base64(user:x)". */
function tokenOf(req: Request): string {
  const h = req.headers.get("Authorization") ?? "";
  if (h.startsWith("Bearer ")) return h.slice(7);
  if (h.startsWith("Basic ")) {
    try {
      const s = atob(h.slice(6));
      return s.slice(s.indexOf(":") + 1);
    } catch {
      return "";
    }
  }
  return "";
}

async function gitAllowed(req: Request, env: Env, id: string): Promise<boolean> {
  const token = tokenOf(req);
  if (!token) return false;
  if (token.startsWith("tule_")) {
    const user = await accounts(env).apiKeyUser(token);
    return !!user && (await accounts(env).projects(user.id)).some((p) => p.id === id);
  }
  return ops.projectStub(env, id).checkJoin(token);
}

const lfsJson = (b: unknown, status = 200) =>
  new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/vnd.git-lfs+json", "Cache-Control": "no-store" } });

async function gitRoute(req: Request, env: Env, url: URL, id: string, what: string, rest: string[], origin: string): Promise<Response> {
  if (!(await gitAllowed(req, env, id)))
    return new Response("tule: sign in (tule login), or use the machine's own credentials\n", { status: 401, headers: { "WWW-Authenticate": 'Basic realm="tule"' } });
  const project = ops.projectStub(env, id);
  if (what === "git") {
    if (rest[0] === "refs" && req.method === "GET") return json(await project.gitRefs());
    if (rest[0] === "bundles" && rest[1] && BUNDLE.test(rest[1]) && req.method === "GET") {
      const o = await env.WORKSPACES.get(gitKey(id, rest[1]));
      return o ? new Response(o.body, { headers: { "Content-Type": "application/x-git-bundle" } }) : error(`no bundle ${rest[1]}`, 404);
    }
    if (rest[0] === "push" && req.method === "POST") {
      const ref = url.searchParams.get("ref") ?? "";
      const old = url.searchParams.get("old") ?? "";
      const next = url.searchParams.get("new") ?? "";
      const sha = /^([0-9a-f]{40})?$/;
      if (!/^refs\/(heads|tags)\/[A-Za-z0-9._\/-]{1,200}$/.test(ref) || !sha.test(old) || !sha.test(next))
        return error("expected ?ref=refs/heads/<name>&old=<sha or empty>&new=<sha or empty>", 400);
      const size = Number(req.headers.get("Content-Length") ?? 0);
      let bundle: { id: string; tip: string; size: number } | undefined;
      if (size > 0 && req.body) {
        if (!next) return error("a bundle needs a new commit", 400);
        const bid = `${Date.now().toString(36)}${crypto.getRandomValues(new Uint32Array(1))[0].toString(36)}`;
        await env.WORKSPACES.put(gitKey(id, bid), req.body, { httpMetadata: { contentType: "application/x-git-bundle" } });
        bundle = { id: bid, tip: next, size };
      }
      const r = await project.gitPush(ref, old, next, bundle);
      if (r.error && bundle) await env.WORKSPACES.delete(gitKey(id, bundle.id));
      return json(r, r.error ? 409 : 200);
    }
    return error("use GET refs, GET bundles/<id>, POST push", 404);
  }
  // Git LFS
  // the address the client used (a machine and a laptop may reach this Worker by different names in dev)
  const base = req.headers.get("Host") ? `${url.protocol}//${req.headers.get("Host")}` : origin;
  const href = (oid: string) => `${base}/api/projects/${id}/lfs/objects/${oid}`;
  const auth = { Authorization: req.headers.get("Authorization") ?? "" };
  if (rest[0] === "objects" && rest[1] === "batch" && req.method === "POST") {
    const b = (await req.json().catch(() => ({}))) as { operation?: string; objects?: { oid?: unknown; size?: unknown }[] };
    const op = b.operation === "upload" ? "upload" : "download";
    const objects = await Promise.all(
      (b.objects ?? []).slice(0, 1000).map(async (o) => {
        const oid = String(o.oid ?? "");
        const size = Number(o.size ?? 0);
        if (!OID.test(oid)) return { oid, size, error: { code: 422, message: "invalid oid" } };
        const have = await env.WORKSPACES.head(lfsKey(id, oid));
        if (op === "upload") return have ? { oid, size } : { oid, size, authenticated: true, actions: { upload: { href: href(oid), header: auth, expires_in: 3600 } } };
        return have
          ? { oid, size: have.size, authenticated: true, actions: { download: { href: href(oid), header: auth, expires_in: 3600 } } }
          : { oid, size, error: { code: 404, message: "this object was never uploaded" } };
      }),
    );
    return lfsJson({ transfer: "basic", objects, hash_algo: "sha256" });
  }
  if (rest[0] === "objects" && rest[1] && OID.test(rest[1])) {
    const oid = rest[1];
    if (req.method === "GET") {
      const o = await env.WORKSPACES.get(lfsKey(id, oid));
      return o ? new Response(o.body, { headers: { "Content-Type": "application/octet-stream", "Content-Length": String(o.size) } }) : lfsJson({ message: "not found" }, 404);
    }
    if (req.method === "PUT") {
      const len = Number(req.headers.get("Content-Length") ?? -1);
      if (!req.body || !(len >= 0)) return lfsJson({ message: "a Content-Length is required" }, 411);
      // stream into R2 while hashing; an object whose bytes do not match its oid is removed
      const [a, b] = req.body.tee();
      const digest = new crypto.DigestStream("SHA-256");
      const fixed = new FixedLengthStream(len);
      await Promise.all([env.WORKSPACES.put(lfsKey(id, oid), fixed.readable), b.pipeTo(fixed.writable), a.pipeTo(digest)]);
      const got = [...new Uint8Array(await digest.digest)].map((x) => x.toString(16).padStart(2, "0")).join("");
      if (got !== oid) {
        await env.WORKSPACES.delete(lfsKey(id, oid));
        return lfsJson({ message: `the bytes hash to ${got}, not ${oid}` }, 422);
      }
      return new Response(null, { status: 200 });
    }
  }
  return lfsJson({ message: "not found" }, 404);
}

/** A machine, by project id and join token. */
async function machineRoute(req: Request, env: Env, url: URL, id: string, what: string, rest: string[], origin: string): Promise<Response> {
  const project = ops.projectStub(env, id);
  if (what === "agent") return req.headers.get("Upgrade")?.toLowerCase() === "websocket" ? project.fetch(req) : error("expected a WebSocket", 426);
  if (what === "git" || what === "lfs") return gitRoute(req, env, url, id, what, rest, origin);
  const h = req.headers.get("Authorization") ?? "";
  const token = h.startsWith("Bearer ") ? h.slice(7) : "";
  if (!token || !(await project.checkJoin(token))) return error("invalid join token", 401);
  if (what === "blobs") return blobs(req, env, id, rest, true);
  if (what === "snapshots") return snapshotsRoute(req, env, id, rest, true, url);
  return error("machines may only use agent, blobs, snapshots, git and lfs", 403);
}

// ------------------------------------------------------------ the user's API

async function userApi(req: Request, env: Env, url: URL, user: User, origin: string): Promise<Response> {
  const parts = url.pathname.split("/").filter(Boolean).slice(1); // after "api"
  const a = accounts(env);
  const [area, name, what = "", ...rest] = parts;
  if (area === "me") return json({ user, ...(await ops.usage(env, user)) });
  if (area === "usage") return json(await ops.usage(env, user));
  if (area === "hosts") {
    if (req.method === "GET") return json({ hosts: await ops.listHosts(env, user) });
    if (req.method === "DELETE" && name) return reply(await ops.removeHost(env, user, name));
    return error("use GET /api/hosts or DELETE /api/hosts/<id>", 405);
  }
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
  const [ps, hosts] = await Promise.all([accounts(env).projects(user.id), user.guest ? [] : accounts(env).hosts(user.id)]);
  // a computer's OS, for its icon ("Darwin 25.6", "Linux 6.8…-WSL2")
  const os = new Map(hosts.map((h) => [h.name, String((h.specs as { os?: unknown } | null)?.os ?? "")]));
  return Promise.all(
    ps.map(async (p) => {
      const s = await ops.projectStub(env, p.id).status();
      const machine = (s.machine as string | null) ?? null;
      return { ...p, state: String(s.state), machine, os: machine ? os.get(machine) : undefined };
    }),
  );
}

async function settings(req: Request, env: Env, url: URL, user: User): Promise<Response> {
  const a = accounts(env);
  let message: string | undefined;
  let newKey: string | undefined;
  let hostKey: string | undefined;
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
    } else if (action === "hosts/new") {
      hostKey = (await a.createHostKey(user.id)).key;
    } else if (action === "hosts/remove") {
      const r = await ops.removeHost(env, user, f("id"));
      message = r.error ? String(r.error) : `${r.removed} removed.`;
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
    hosts: user.guest ? [] : await ops.listHosts(env, user),
    usage: await a.usage(user.id),
    quota: Number(env.CPU_HOURS_MONTH || 100),
    newKey,
    hostKey,
    message,
    origin: env.PUBLIC_URL || url.origin,
  });
}

// ------------------------------------------------------------ the app

/** In local dev, every HTML page reloads itself when the code changes (dev.ts). */
async function app(req: Request, env: Env): Promise<Response> {
  const res = await route(req, env);
  if (!devMode(env) || !(res.headers.get("Content-Type") ?? "").includes("text/html") || res.status === 304) return res;
  const html = await res.text();
  const headers = new Headers(res.headers);
  headers.delete("Content-Length");
  headers.set("Cache-Control", "no-store"); // the page must always be the latest build
  return new Response(html.includes("/__dev/version") ? html : html.replace("</body>", `${RELOAD_SCRIPT}</body>`), { status: res.status, headers });
}

async function route(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const dev = await devRoutes(req, env, url);
  if (dev) return dev;
  const origin = env.PUBLIC_URL || url.origin;
  const path = url.pathname;

  const m = path.match(/^\/api\/projects\/(p_[^/]+)(?:\/([a-z]+))?(?:\/(.*))?$/);
  if (m) {
    if (!PID.test(m[1])) return error("not found", 404);
    return machineRoute(req, env, url, m[1], m[2] ?? "", (m[3] ?? "").split("/").filter(Boolean), env.MACHINE_URL || origin);
  }
  const auth = await authRoutes(req, env, url);
  if (auth) return auth;
  if (path === "/api/device/code" || path === "/api/device/token") return deviceApi(req, env, path.split("/").pop()!);
  // a computer joins with the one-time key from Settings (install.sh runs `tule host up --key`)
  if (path === "/api/hosts/pair" && req.method === "POST") {
    const b = (await req.json().catch(() => ({}))) as { key?: unknown; name?: unknown; token?: unknown };
    if (typeof b.key !== "string" || !b.key.startsWith("tulep_")) return error("expected {key: \"tulep_…\", name}", 400, "copy the command from Settings → Your computers");
    return reply(await accounts(env).pairHost(b.key, typeof b.name === "string" ? b.name : "computer", typeof b.token === "string" ? b.token : undefined));
  }
  // the one command that adds a computer (curl -fsSL <origin>/install.sh | sh -s -- <key>), and the
  // one that installs and signs in the CLI (curl -fsSL <origin>/cli.sh | sh -s -- <code from connect_info>)
  if (path === "/install.sh" || path === "/cli.sh") {
    const r = await env.ASSETS.fetch(new Request(new URL(`/install${path}`, url)));
    // a missing file comes back as the viewer's page (single-page fallback), not a 404
    if (!r.ok || r.headers.get("Content-Type")?.startsWith("text/html")) return error(`${path.slice(1)} is missing from this deploy`, 500);
    const text = (await r.text()).replaceAll("__TULE_URL__", env.PUBLIC_URL || url.origin);
    return new Response(text, { headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" } });
  }
  // a computer's `tule host run` service, by its host token
  if (path === "/api/hosts/connect") {
    if (req.headers.get("Upgrade")?.toLowerCase() !== "websocket") return error("expected a WebSocket", 426);
    const h = req.headers.get("Authorization") ?? "";
    const host = h.startsWith("Bearer tuleh_") ? await accounts(env).hostByToken(h.slice(7)) : null;
    if (!host) return error("unknown computer", 401, "pair it again: tule host up");
    const fwd = new Request(req);
    fwd.headers.set("X-Tule-Host-Id", host.id);
    fwd.headers.set("X-Tule-Host-Owner", host.userId);
    fwd.headers.set("X-Tule-Host-Name", host.name);
    return env.HOST.getByName(host.id).fetch(fwd);
  }

  // admin, with DEV_TOKEN only: GET /api/admin/users; POST /api/admin/users/delete {"email"} deletes an account
  // with its projects, snapshots, computers, keys and connector sign-ins
  if (path.startsWith("/api/admin/")) {
    if (!isAdmin(req, env)) return error("admin only", 403);
    const a = accounts(env);
    if (path === "/api/admin/users" && req.method === "GET") return reply({ users: await a.allUsers() });
    if (path === "/api/admin/users/delete" && req.method === "POST") {
      const b = (await req.json().catch(() => ({}))) as { email?: unknown };
      const u = (await a.allUsers()).find((x) => !x.guest && x.email === b.email);
      if (!u) return error(`no account ${JSON.stringify(b.email)}`, 404);
      for (const h of await a.hosts(u.id)) await stub<Host>(env.HOST, h.id).forget();
      const grants = await env.OAUTH_PROVIDER.listUserGrants(u.id);
      for (const g of grants.items) await env.OAUTH_PROVIDER.revokeGrant(g.id, u.id);
      const r = await a.removeAccount(u.id);
      return reply({ deleted: !r.failed.length, email: u.email, projects: r.projects, failed: r.failed, connectorSignIns: grants.items.length });
    }
    return error("unknown admin route", 404);
  }

  const user = await who(req, env);
  if (path === "/authorize") return authorize(req, env, url, user);
  if (path.startsWith("/api/")) {
    if (!user) return error("not signed in", 401, "run tule login (or send an API key as 'Authorization: Bearer tule_...')");
    return userApi(req, env, url, user, origin);
  }
  if (path === "/") {
    if (user) return projectsPage(user, await projectRows(env, user), origin, undefined, user.guest ? await accounts(env).limits() : undefined);
    // a first visit: a guest account, so Tule can be tried without signing in
    const g = await accounts(env).createGuest(req.headers.get("CF-Connecting-IP") ?? "local");
    if ("error" in g) return landing(origin, g.error);
    const res = projectsPage(g.user, [], origin, undefined, await accounts(env).limits());
    res.headers.append("Set-Cookie", setCookie(req, SESSION_COOKIE, g.session, 30 * 86400));
    return res;
  }
  if (path === "/projects/new" && req.method === "POST") {
    if (!user) return Response.redirect(`${origin}${loginUrl("/")}`, 302);
    if (!sameOrigin(req, url)) return page("Refused", "<p>That form did not come from Tule.</p>", 403, user);
    const name = String((await req.formData()).get("name") ?? "").trim();
    const r = await ops.createProject(env, user, name, origin);
    if (r.error) return projectsPage(user, await projectRows(env, user), origin, `${r.error}${r.hint ? ` (${r.hint})` : ""}`, user.guest ? await accounts(env).limits() : undefined);
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
