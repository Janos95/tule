// Who is asking, and signing in (see DESIGN.md, "User journey").
//
//   a browser       the tule_session cookie, set after Google sign-in
//   the CLI, MCP    an API key (tule_...), made by `tule login` (device code)
//                   or in Settings; MCP clients also get OAuth tokens
//                   (/authorize: sign in with Google, then consent)
//   tests, admin    DEV_TOKEN, which acts as the "dev" user
//
// ALLOWED_EMAILS (comma-separated, "*" for anyone) says who may sign up.
import type { Accounts, User } from "./accounts";
import { stub } from "./rpc";
import { esc, html, layout, page } from "./pages";

export const SESSION_COOKIE = "tule_session";
const LOGIN_COOKIE = "tule_login";

export const accounts = (env: Env) => stub<Accounts>(env.ACCOUNTS, "global");

function cookie(req: Request, name: string): string | null {
  const m = (req.headers.get("Cookie") ?? "").match(new RegExp(`(?:^|;\\s*)${name}=([^;]+)`));
  return m ? decodeURIComponent(m[1]) : null;
}

export function setCookie(req: Request, name: string, value: string, maxAge: number): string {
  const secure = new URL(req.url).protocol === "https:" ? "; Secure" : "";
  return `${name}=${encodeURIComponent(value)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure}`;
}

function same(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  return x.length === y.length && crypto.subtle.timingSafeEqual(x, y);
}

export function bearer(req: Request): string {
  const h = req.headers.get("Authorization") ?? "";
  return h.startsWith("Bearer ") ? h.slice(7).trim() : "";
}

/** The signed-in user, from the session cookie, an API key or the dev token. */
export async function who(req: Request, env: Env): Promise<User | null> {
  const a = accounts(env);
  const token = bearer(req) || new URL(req.url).searchParams.get("t") || "";
  if (token) {
    if (env.DEV_TOKEN && same(token, env.DEV_TOKEN)) return a.devUser();
    if (token.startsWith("tule_")) return a.apiKeyUser(token);
    return null;
  }
  const s = cookie(req, SESSION_COOKIE);
  const user = s ? await a.sessionUser(s) : null;
  if (user?.guest) await a.seen(user.id);
  return user;
}

/** The URL to sign in and come back to `next`. */
export const loginUrl = (next: string) => `/auth/login?next=${encodeURIComponent(next)}`;

function allowed(env: Env, email: string): boolean {
  const list = (env.ALLOWED_EMAILS ?? "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  return list.includes("*") || list.includes(email.toLowerCase());
}

const b64url = (b: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

/** /auth/login, /auth/google/callback, /auth/logout. */
export async function authRoutes(req: Request, env: Env, url: URL): Promise<Response | null> {
  const origin = env.PUBLIC_URL || url.origin;
  if (url.pathname === "/auth/login") {
    if (!env.GOOGLE_CLIENT_ID) return page("Sign-in is not set up", "<p>GOOGLE_CLIENT_ID is missing on this deployment.</p>", 500);
    const next = safeNext(url.searchParams.get("next"));
    const { state, verifier } = await accounts(env).startLogin(next);
    const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
    const g = new URL("https://accounts.google.com/o/oauth2/v2/auth");
    g.search = new URLSearchParams({
      client_id: env.GOOGLE_CLIENT_ID,
      redirect_uri: `${origin}/auth/google/callback`,
      response_type: "code",
      scope: "openid email profile",
      state,
      code_challenge: challenge,
      code_challenge_method: "S256",
      prompt: "select_account",
    }).toString();
    return new Response(null, { status: 302, headers: { Location: g.toString(), "Set-Cookie": setCookie(req, LOGIN_COOKIE, state, 600) } });
  }
  if (url.pathname === "/auth/google/callback") {
    const state = url.searchParams.get("state") ?? "";
    const code = url.searchParams.get("code") ?? "";
    if (url.searchParams.get("error")) return page("Sign-in cancelled", `<p>Google said: ${esc(url.searchParams.get("error")!)}. <a href="/">Try again</a>.</p>`, 400);
    if (!state || cookie(req, LOGIN_COOKIE) !== state) return page("Sign-in failed", `<p>This sign-in link is not from this browser or has expired. <a href="/auth/login">Start again</a>.</p>`, 400);
    const login = await accounts(env).finishLogin(state);
    if (!login) return page("Sign-in failed", `<p>The sign-in expired. <a href="/auth/login">Start again</a>.</p>`, 400);
    const r = await fetch("https://oauth2.googleapis.com/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        code,
        client_id: env.GOOGLE_CLIENT_ID,
        client_secret: env.GOOGLE_CLIENT_SECRET,
        redirect_uri: `${origin}/auth/google/callback`,
        grant_type: "authorization_code",
        code_verifier: login.verifier,
      }),
    });
    const tok = (await r.json()) as { id_token?: string; error?: string; error_description?: string };
    if (!tok.id_token) return page("Sign-in failed", `<p>Google refused the code: ${esc(tok.error_description ?? tok.error ?? String(r.status))}.</p>`, 400);
    // straight from Google over TLS, so its claims need no signature check here
    const claims = JSON.parse(atob(tok.id_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))) as {
      sub: string; email: string; email_verified?: boolean; name?: string; aud: string;
    };
    if (claims.aud !== env.GOOGLE_CLIENT_ID || !claims.email_verified)
      return page("Sign-in failed", "<p>Google did not confirm this email address.</p>", 400);
    if (!allowed(env, claims.email))
      return page("Not invited yet", `<p>Tule is invite-only for now, and ${esc(claims.email)} is not on the list.</p>`, 403);
    const user = await accounts(env).googleUser(claims.sub, claims.email, claims.name ?? claims.email);
    // signing in from a guest session: the guest's projects come along
    const before = cookie(req, SESSION_COOKIE);
    const guest = before ? await accounts(env).sessionUser(before) : null;
    if (guest?.guest) await accounts(env).mergeGuest(guest.id, user.id);
    const session = await accounts(env).createSession(user.id);
    const h = new Headers({ Location: login.next });
    h.append("Set-Cookie", setCookie(req, SESSION_COOKIE, session, 30 * 86400));
    h.append("Set-Cookie", setCookie(req, LOGIN_COOKIE, "", 0));
    return new Response(null, { status: 302, headers: h });
  }
  if (url.pathname === "/auth/logout" && req.method === "POST") {
    const s = cookie(req, SESSION_COOKIE);
    if (s) await accounts(env).endSession(s);
    return new Response(null, { status: 303, headers: { Location: "/", "Set-Cookie": setCookie(req, SESSION_COOKIE, "", 0) } });
  }
  return null;
}

/** Only same-site paths: never redirect elsewhere after sign-in. */
export function safeNext(next: string | null): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/";
}

/** Forms post only from our own pages. */
export function sameOrigin(req: Request, url: URL): boolean {
  const o = req.headers.get("Origin");
  return !o || o === url.origin;
}

// ------------------------------------------------------------ device codes (tule login)

export async function deviceApi(req: Request, env: Env, what: string): Promise<Response> {
  const origin = env.PUBLIC_URL || new URL(req.url).origin;
  let b: Record<string, unknown> = {};
  try {
    b = (await req.json()) as Record<string, unknown>;
  } catch {
    // empty body
  }
  if (what === "code") {
    const host = typeof b.host === "string" && b.host.trim() ? b.host.trim().slice(0, 40) : undefined;
    const d = await accounts(env).deviceStart(String(b.client ?? req.headers.get("User-Agent") ?? "tule CLI"), host);
    return Response.json({
      device_code: d.deviceCode,
      user_code: d.userCode,
      verification_uri: `${origin}/device`,
      verification_uri_complete: `${origin}/device?code=${d.userCode}`,
      expires_in: d.expiresIn,
      interval: 2,
    });
  }
  if (what === "token") return Response.json(await accounts(env).devicePoll(String(b.device_code ?? "")));
  return Response.json({ error: "unknown device endpoint" }, { status: 404 });
}

/** /device: a signed-in user approves a `tule login`. */
export async function devicePage(req: Request, env: Env, url: URL, user: User): Promise<Response> {
  const a = accounts(env);
  if (req.method === "POST") {
    if (!sameOrigin(req, url)) return page("Refused", "<p>That form did not come from Tule.</p>", 403);
    const form = await req.formData();
    const code = String(form.get("code") ?? "").trim().toUpperCase();
    const pending = await a.deviceInfo(code);
    if (pending?.host && user.guest) return page("Sign in first", `<p>Guests cannot add computers. <a href="/auth/login">Sign in</a>, then run <code>tule host up</code> again.</p>`, 403, user);
    const ok = await a.deviceApprove(code, user.id);
    if (!ok) return page("Code not found", `<p>The code <code>${esc(code)}</code> is wrong or expired. Run the command again.</p>`, 400, user);
    return pending?.host
      ? page("Computer added", `<p>Done: <b>${esc(pending.host)}</b> is now one of your computers. Its service connects in a moment; see <a href="/settings">Settings</a>.</p>`, 200, user)
      : page("CLI signed in", `<p>Done: the <code>tule</code> CLI is signed in as ${esc(user.email)}. You can close this tab.</p>`, 200, user);
  }
  const code = (url.searchParams.get("code") ?? "").toUpperCase();
  const info = code ? await a.deviceInfo(code) : null;
  return page(
    "Sign in the tule CLI",
    html`<p>Signed in as <b>${user.email}</b>.</p>
      ${code && !info ? html`<p class="warn">The code ${code} is wrong or expired.</p>` : ""}
      <form method="post" class="stack">
        <label>Code shown by <code>tule login</code><input name="code" value="${code}" autocomplete="off" placeholder="ABCD-EFGH" required /></label>
        ${info?.host ? html`<p>This adds the computer <b>${info.host}</b> to your account: Tule may start its machine image there for your projects.</p>` : info ? html`<p class="muted">Requested by: ${info.client}</p>` : ""}
        <button class="primary">${info?.host ? "Add computer" : "Approve"}</button>
      </form>
      <p class="muted">Approving gives that CLI an API key for your account (revoke it in <a href="/settings">Settings</a>).</p>`,
  );
}

// ------------------------------------------------------------ MCP connector consent (/authorize)

export async function authorize(req: Request, env: Env, url: URL, user: User | null): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  if (req.method === "GET") {
    if (!user) return new Response(null, { status: 302, headers: { Location: loginUrl(url.pathname + url.search) } });
    let request;
    try {
      request = await oauth.parseAuthRequest(req);
    } catch (e) {
      const err = e as { redirectUri?: string; message: string };
      return page("Cannot connect this app", `<p>${esc(err.message)}</p>`, 400);
    }
    const d = await oauth.describeConsent(request);
    const consent = await oauth.beginConsent(request);
    consent.headers.set("Content-Type", "text/html; charset=utf-8");
    const body = html`<p>Signed in as <b>${user.email}</b>.</p>
      <p><b>${d.clientName}</b> wants to manage your Tule projects: create and delete projects, start and stop
      machines, manage your SSH keys and read your usage.</p>
      <p class="muted">${d.clientDomain ? html`Published by <b>${d.clientDomain}</b>.` : "This app registered itself; its name is not verified."}
      Access goes to <b>${d.redirectHost}</b>.</p>
      ${d.redirectIsLoopback ? html`<p class="warn">This sends access to an app on your computer. Continue only if you just started connecting it.</p>` : ""}
      <form method="post" class="row">
        <input type="hidden" name="handle" value="${consent.handle}" />
        <button class="primary" name="decision" value="approve">Allow</button>
        <button name="decision" value="deny">Deny</button>
      </form>`;
    return new Response(layout(`Connect ${d.clientName}`, body), { headers: consent.headers });
  }
  if (!user) return page("Signed out", "<p>Sign in again and retry from your app.</p>", 401);
  const form = await req.formData();
  const handle = String(form.get("handle") ?? "");
  if (form.get("decision") !== "approve") {
    const denied = await oauth.denyConsent(req, handle);
    return new Response(null, { status: 302, headers: denied.headers });
  }
  const approved = await oauth.approveConsent(req, handle, { scope: ["tule"] });
  const { redirectTo } = await oauth.completeAuthorization({
    request: approved.request,
    userId: user.id,
    metadata: { email: user.email },
    scope: ["tule"],
    props: { userId: user.id },
  });
  approved.headers.set("Location", redirectTo);
  return new Response(null, { status: 302, headers: approved.headers });
}

