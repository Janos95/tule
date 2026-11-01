// Local development only (DEV_LOGIN=1 in .dev.vars, never in production):
//   /__dev/version   changes when the Worker reloads or the viewer is rebuilt;
//                    dev pages poll it and reload themselves
//   /auth/dev        signs this browser in as the dev user, without Google
import { accounts, safeNext } from "./auth";

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
