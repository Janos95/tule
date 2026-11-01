// The site's own pages, rendered here (the viewer, /p/<project>, is the
// static app in web/): the landing page, the projects list, Settings (RunPod,
// API keys, SSH keys, usage), and the pages auth.ts shows.
import type { ProjectRef, User } from "./accounts";

export const esc = (v: unknown) => String(v ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/** A trusted HTML fragment. */
class Html {
  constructor(readonly s: string) {}
  toString() {
    return this.s;
  }
}

/** html`...${x}...`: interpolated values are escaped unless they are html`` fragments (or arrays of them). */
export function html(strings: TemplateStringsArray, ...values: unknown[]): Html {
  const part = (v: unknown): string =>
    v instanceof Html ? v.s : Array.isArray(v) ? v.map(part).join("") : v === false || v == null ? "" : esc(v);
  return new Html(strings.reduce((out, s, i) => out + s + (i < values.length ? part(values[i]) : ""), ""));
}

const CSS = `
:root{--bg:#fff;--panel:#f6f7f9;--line:#e3e6ea;--text:#1f2933;--muted:#6b7682;--accent:#2f6fb3;--warn:#a3610b;--danger:#c2453a;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#14181d;--panel:#1b2027;--line:#2b323b;--text:#e4e8ec;--muted:#98a2ad;--accent:#6fa6e0}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 var(--sans);color:var(--text);background:var(--bg)}
header{display:flex;align-items:center;gap:14px;height:52px;padding:0 16px;border-bottom:1px solid var(--line)}
header .brand{display:flex;gap:8px;align-items:center;color:inherit;text-decoration:none;font-weight:700;font-size:17px}
header nav{display:flex;gap:14px;margin-left:auto;align-items:center}header nav a{color:var(--muted);text-decoration:none}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 16px}h2{font-size:17px;margin:32px 0 10px}
a{color:var(--accent)}code{font:13px var(--mono);background:var(--panel);padding:1px 5px;border-radius:4px}
pre{font:13px/1.45 var(--mono);background:var(--panel);padding:12px;border-radius:8px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}
.muted{color:var(--muted)}.warn{color:var(--warn)}.ok{color:#2f8a4c}
button,.button{font:inherit;font-size:14px;padding:6px 14px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);cursor:pointer;text-decoration:none;display:inline-block}
button.primary,.button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--danger)}
input{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);width:100%}
label{display:flex;flex-direction:column;gap:4px;font-size:14px}
.stack{display:flex;flex-direction:column;gap:12px;max-width:520px}.row{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.row>label{flex:1;min-width:200px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:middle}th{color:var(--muted);font-weight:500}
.card{border:1px solid var(--line);border-radius:10px;padding:16px;background:var(--panel)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#b8c0c8;margin-right:6px}.dot.running{background:#3aa45b}.dot.starting{background:#e0a526}
.hero{padding:48px 0 24px}.hero h1{font-size:30px}.hero p{font-size:17px;color:var(--muted);max-width:620px}
@media (max-width:600px){td:nth-child(3),th:nth-child(3){display:none}}
`;

const LOGO = `<svg width="22" height="22" viewBox="0 0 32 32"><rect x="3" y="3" width="26" height="26" rx="6" fill="#23344a"/><path d="M9 11h14M16 11v12" stroke="#fff" stroke-width="3" stroke-linecap="round"/></svg>`;

export function layout(title: string, body: Html | string, user?: User | null): string {
  const nav = user
    ? html`<nav><a href="/">Projects</a><a href="/settings">Settings</a>
        <form method="post" action="/auth/logout" style="margin:0"><button>Sign out</button></form></nav>`
    : html`<nav><a class="button" href="/auth/login">Sign in</a></nav>`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)} · Tule</title><link rel="icon" href="/favicon.svg"><style>${CSS}</style></head>
<body><header><a class="brand" href="/">${LOGO}tule</a>${nav}</header><main><h1>${esc(title)}</h1>${body}</main></body></html>`;
}

export function page(title: string, body: Html | string, status = 200, user?: User | null): Response {
  return new Response(layout(title, body, user), {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Frame-Options": "DENY" },
  });
}

const GOOGLE = `<svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.6 5.4 2.6 13.3l7.9 6.1C12.4 13.7 17.7 9.5 24 9.5z"/><path fill="#4285F4" d="M46.1 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.4c-.5 2.9-2.2 5.3-4.6 7l7.4 5.7c4.3-4 6.9-9.9 6.9-17.2z"/><path fill="#FBBC05" d="M10.5 28.6A14.5 14.5 0 0 1 9.5 24c0-1.6.3-3.2.8-4.6l-7.9-6.1A24 24 0 0 0 0 24c0 3.9.9 7.5 2.6 10.7l7.9-6.1z"/><path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.7c-2.1 1.4-4.8 2.3-8.5 2.3-6.3 0-11.6-4.2-13.5-10l-7.9 6.1C6.6 42.6 14.6 48 24 48z"/></svg>`;

/** The signed-out front page: the name and one way in. */
export function landing(_origin: string): Response {
  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tule</title><link rel="icon" href="/favicon.svg"><style>${CSS}
.center{min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:28px;padding:16px}
.mark{display:flex;align-items:center;gap:12px;font-size:30px;font-weight:700;letter-spacing:.2px}
.google{display:inline-flex;align-items:center;gap:10px;padding:10px 18px;font-size:15px;border-radius:9px}</style></head>
<body><div class="center"><div class="mark">${LOGO.replace(/width="22" height="22"/, 'width="40" height="40"')}tule</div>
<a class="button google" href="/auth/login">${GOOGLE}Sign in with Google</a></div></body></html>`,
    { headers: { "Content-Type": "text/html; charset=utf-8" } },
  );
}

export type ProjectRow = ProjectRef & { state: string; machine: string | null };

export function projectsPage(user: User, rows: ProjectRow[], origin: string, error?: string): Response {
  const list = rows.length
    ? html`<table><tr><th>Project</th><th>Machine</th><th>Created</th><th></th></tr>
        ${rows.map(
          (p) => html`<tr><td><a href="/p/${p.name}"><b>${p.name}</b></a></td>
            <td><span class="dot ${p.state}"></span>${p.state === "stopped" ? "stopped" : `${p.machine ?? ""} · ${p.state}`}</td>
            <td class="muted">${new Date(p.created).toISOString().slice(0, 10)}</td>
            <td style="text-align:right"><a class="button" href="/p/${p.name}">Open viewer</a></td></tr>`,
        )}</table>`
    : html`<p class="muted">No projects yet. Create one here, or ask your agent to ("start a Tule project for a camera bracket").</p>`;
  const body = html`${list}
    <h2>New project</h2>
    ${error ? html`<p class="warn">${error}</p>` : ""}
    <form method="post" action="/projects/new" class="row">
      <label>Name<input name="name" pattern="[a-z0-9][a-z0-9-]{0,62}" placeholder="camera-bracket" required /></label>
      <button class="primary">Create</button>
    </form>
    <h2>Connect your agent</h2>
    <div class="card">
      <p><b>MCP connector</b> (projects, machines, keys): <code>${origin}/mcp</code></p>
      <p><b>CLI</b> (the work, over SSH):</p>
      <pre>pip install "tule-cli @ git+https://github.com/Janos95/tule#subdirectory=cli"
tule login --url ${origin}
tule ssh -p &lt;project&gt;</pre>
    </div>`;
  return page("Projects", body, 200, user);
}

export type SettingsData = {
  runpod: { connected: boolean; updated: number | null; balance?: string | null; error?: string };
  apiKeys: { id: string; name: string; prefix: string; created: number; last_used: number | null }[];
  sshKeys: { id: string; key: string; name: string; created: number }[];
  usage: { month: string; cpuHours: number; gpuHours: number; gpuCost: number };
  quota: number;
  newKey?: string;
  message?: string;
};

const day = (t: number | null) => (t ? new Date(t).toISOString().slice(0, 10) : "never");

export function settingsPage(user: User, d: SettingsData): Response {
  const rp = d.runpod.connected
    ? html`<p><span class="ok">Connected</span> since ${day(d.runpod.updated)}${d.runpod.balance ? html` · balance <b>${d.runpod.balance}</b>` : ""}.</p>
        <form method="post" action="/settings/runpod/remove"><button class="danger">Disconnect</button></form>`
    : html`<p class="muted">Not connected. GPU machines run on your RunPod account and are paid from its balance;
        RunPod's own limits (prepaid balance, spend limit) are the budget.</p>`;
  const body = html`
    ${d.message ? html`<p class="ok">${d.message}</p>` : ""}
    <p class="muted">Signed in as ${user.email}.</p>
    <h2>RunPod (GPU machines)</h2>
    ${rp}
    <form method="post" action="/settings/runpod" class="row" autocomplete="off">
      <label>API key<input name="key" type="password" placeholder="rpa_..." required /></label>
      <button class="primary">${d.runpod.connected ? "Replace key" : "Connect"}</button>
    </form>
    <p class="muted">Create a key with read/write access at runpod.io → Settings → API Keys (a dedicated key is best).
      Tule encrypts it and uses it only to create, check and stop your pods; it never enters a machine.
      ${d.runpod.error ? html`<br><span class="warn">${d.runpod.error}</span>` : ""}</p>

    <h2>Usage this month (${d.usage.month})</h2>
    <p>CPU machines: <b>${d.usage.cpuHours} h</b> of ${d.quota} h · GPU machines: <b>${d.usage.gpuHours} h</b>${d.usage.gpuCost ? html` (about $${d.usage.gpuCost.toFixed(2)} on RunPod)` : ""}</p>

    <h2>SSH keys</h2>
    ${d.sshKeys.length
      ? html`<table>${d.sshKeys.map(
          (k) => html`<tr><td>${k.name}</td><td class="muted"><code>${k.key.slice(0, 28)}…</code></td><td class="muted">${day(k.created)}</td>
            <td style="text-align:right"><form method="post" action="/settings/ssh/remove"><input type="hidden" name="id" value="${k.id}"><button class="danger">Remove</button></form></td></tr>`,
        )}</table>`
      : html`<p class="muted">None yet: <code>tule ssh</code> adds its own key.</p>`}
    <form method="post" action="/settings/ssh/add" class="row">
      <label>Public key<input name="key" placeholder="ssh-ed25519 AAAA... you@laptop" required /></label>
      <button>Add</button>
    </form>

    <h2>API keys</h2>
    ${d.newKey ? html`<div class="card"><p><b>Your new key</b> (shown once): </p><pre>${d.newKey}</pre></div>` : ""}
    ${d.apiKeys.length
      ? html`<table><tr><th>Name</th><th>Key</th><th>Last used</th><th></th></tr>${d.apiKeys.map(
          (k) => html`<tr><td>${k.name}</td><td class="muted"><code>${k.prefix}…</code></td><td class="muted">${day(k.last_used)}</td>
            <td style="text-align:right"><form method="post" action="/settings/keys/revoke"><input type="hidden" name="id" value="${k.id}"><button class="danger">Revoke</button></form></td></tr>`,
        )}</table>`
      : html`<p class="muted">None yet. <code>tule login</code> makes one for the CLI.</p>`}
    <form method="post" action="/settings/keys/new" class="row">
      <label>Name<input name="name" placeholder="CI" required /></label>
      <button>Create key</button>
    </form>`;
  return page("Settings", body, 200, user);
}
