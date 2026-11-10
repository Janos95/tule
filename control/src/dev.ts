// Local development only (DEV_LOGIN=1 in .dev.vars, never in production):
//   /__dev/version   changes when the Worker reloads or the viewer is rebuilt;
//                    dev pages poll it and reload themselves
//   /auth/dev        signs this browser in as the dev user, without Google
//   /__dev/sweep     runs the guest sweep now; /__dev/merge merges this guest into the dev user
//   /__dev/as?key=   signs this browser in as the API key's user (qa/: screenshots as a test guest)
//   /__dev/import    a production project's autosave, into a new project of the dev user (tule admin pull)
import { accounts, safeNext } from "./auth";
import * as ops from "./ops";
import { snapshotKey } from "./project";
import { sha256hex } from "./util";

let boot = ""; // set on the first request (the clock is frozen while the module loads); new on every reload

export const RELOAD_SCRIPT = `<script>(()=>{let v;setInterval(async()=>{try{const r=await fetch("/__dev/version",{cache:"no-store"});const t=await r.text();if(v&&t!==v)location.reload();v=t}catch{}},700)})()</script>`;

export const devMode = (env: Env) => env.DEV_LOGIN === "1";

export async function devRoutes(req: Request, env: Env, url: URL): Promise<Response | null> {
  if (!devMode(env)) return null;
  if (url.pathname === "/__dev/version") {
    const web = await env.ASSETS.fetch(new Request(new URL("/assets-dev/version.txt", url))).then((r) => (r.ok ? r.text() : ""));
    boot ||= String(Date.now());
    return new Response(`${boot}:${web}`, { headers: { "Cache-Control": "no-store" } });
  }
  // the hourly guest sweep, now (GUEST_DAYS from .dev.vars decides who is old)
  if (url.pathname === "/__dev/sweep") {
    return new Response(`deleted ${await accounts(env).sweep()} guest(s)\n`);
  }
  // a guest signing in, without Google: this browser's guest merges into the dev user
  if (url.pathname === "/__dev/merge") {
    const m = (req.headers.get("Cookie") ?? "").match(/tule_session=([^;]+)/);
    const g = m ? await accounts(env).sessionUser(decodeURIComponent(m[1])) : null;
    if (!g?.guest) return new Response("not a guest session\n", { status: 400 });
    const n = await accounts(env).mergeGuest(g.id, (await accounts(env).devUser()).id);
    return new Response(`moved ${n} project(s) to the dev user\n`);
  }
  if (url.pathname === "/__dev/as") {
    const key = url.searchParams.get("key") ?? "";
    const user = key.startsWith("tule_") ? await accounts(env).apiKeyUser(key) : null;
    if (!user) return new Response("expected ?key=<an API key>\n", { status: 400 });
    const session = await accounts(env).createSession(user.id);
    return new Response(null, {
      status: 302,
      headers: {
        Location: safeNext(url.searchParams.get("next")),
        "Set-Cookie": `tule_session=${encodeURIComponent(session)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400`,
      },
    });
  }
  if (url.pathname.startsWith("/__dev/import")) return devImport(req, env, url);
  if (url.pathname === "/auth/dev") {
    const a = accounts(env);
    const user = await a.devUser();
    const session = await a.createSession(user.id);
    return new Response(null, {
      status: 302,
      headers: {
        Location: safeNext(url.searchParams.get("next")),
        "Set-Cookie": `tule_session=${encodeURIComponent(session)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000`,
      },
    });
  }
  return null;
}

// tule admin pull: POST /__dev/import {name, manifest} makes the project (name, name-2, … if taken) and returns its id;
// PUT /__dev/import/<id>/<sha256> stores each piece; POST /__dev/import/<id>/done {snapshot, at, files, bytes} makes the
// autosave the project's newest, so its first machine starts from it.
async function devImport(req: Request, env: Env, url: URL): Promise<Response> {
  const json = (r: unknown, status = 200) => Response.json(r, { status });
  const [, , , id, rest] = url.pathname.split("/");
  const a = accounts(env);
  const user = await a.devUser();
  if (!id && req.method === "POST") {
    const b = (await req.json().catch(() => ({}))) as { name?: string; manifest?: unknown };
    if (!b.name || !b.manifest || typeof b.manifest !== "object") return json({ error: "expected {name, manifest}" }, 400);
    const base = b.name.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+/, "").slice(0, 56) || "pulled";
    let name = base;
    for (let n = 2; await a.project(user.id, name); n++) name = `${base}-${n}`;
    const made = await ops.createProject(env, user, name, url.origin);
    if ("error" in made) return json(made, 400);
    const p = await a.project(user.id, name);
    if (!p) return json({ error: "the project was not created" }, 500);
    await env.WORKSPACES.put(`${snapshotKey(p.id, "pending")}`, JSON.stringify(b.manifest));
    return json({ id: p.id, name, viewer: ops.viewerUrl(url.origin, name) });
  }
  const p = id ? await a.projectById(id) : null;
  if (!p || p.userId !== user.id) return json({ error: "no such project of the dev user" }, 404);
  if (rest && /^[0-9a-f]{64}$/.test(rest) && req.method === "PUT") {
    const data = await req.arrayBuffer();
    const h = await sha256hex(data);
    if (h !== rest) return json({ error: "the piece does not match its hash" }, 400);
    await env.WORKSPACES.put(`projects/${p.id}/blobs/${h}`, data);
    return json({ ok: true });
  }
  if (rest === "done" && req.method === "POST") {
    const b = (await req.json().catch(() => ({}))) as { snapshot?: string; at?: number; files?: number; bytes?: number };
    if (!b.snapshot || !/^[0-9a-z-]{8,40}$/.test(b.snapshot)) return json({ error: "expected {snapshot, at, files, bytes}" }, 400);
    const man = await env.WORKSPACES.get(snapshotKey(p.id, "pending"));
    if (!man) return json({ error: "start with POST /__dev/import" }, 400);
    await env.WORKSPACES.put(snapshotKey(p.id, b.snapshot), await man.arrayBuffer(), { httpMetadata: { contentType: "application/json" } });
    await env.WORKSPACES.delete(snapshotKey(p.id, "pending"));
    await ops.projectStub(env, p.id).importSnapshot(b.snapshot, { at: Number(b.at) || Date.now(), files: Number(b.files) || 0, bytes: Number(b.bytes) || 0, reason: "pulled from production" });
    return json({ ok: true });
  }
  return json({ error: "unknown import route" }, 404);
}
