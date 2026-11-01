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
button.primary,.button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.button.small{padding:3px 10px;font-size:13px}button.danger{color:var(--danger)}
input{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);width:100%}
label{display:flex;flex-direction:column;gap:4px;font-size:14px}
.stack{display:flex;flex-direction:column;gap:12px;max-width:520px}.row{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.row>label{flex:1;min-width:200px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:middle}th{color:var(--muted);font-weight:500}
.card{border:1px solid var(--line);border-radius:10px;padding:16px;background:var(--panel)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#b8c0c8;margin-right:6px}.dot.running{background:#3aa45b}.dot.starting{background:#e0a526}
.plist{display:flex;flex-direction:column;border-top:1px solid var(--line)}
.prow{display:grid;grid-template-columns:1fr auto 96px;align-items:center;gap:16px;padding:12px 8px;border-bottom:1px solid var(--line);color:inherit;text-decoration:none}
.prow:hover{background:var(--panel)}
.pname{font-weight:500}.pstate{color:var(--muted);font-size:14px}.pdate{color:var(--muted);font-size:13px;text-align:right}
.newp{border-bottom:1px solid var(--line)}
.newp summary{list-style:none;cursor:pointer;padding:12px 8px;color:var(--muted)}
.newp summary::-webkit-details-marker{display:none}
.newp summary:hover{color:var(--text)}
.newp .plus{display:inline-block;width:14px}
.newp-form{display:flex;gap:8px;padding:0 8px 12px}
.newp-form input{max-width:360px}
.newp .warn{padding:0 8px}
.sect{margin-top:28px}.sect h2{margin:0 0 2px}.hint{color:var(--muted);font-size:13.5px;margin:0 0 8px}
.list{display:flex;flex-direction:column;border-top:1px solid var(--line)}
.item{display:grid;grid-template-columns:minmax(120px,auto) 1fr auto;align-items:center;gap:16px;padding:10px 8px;border-bottom:1px solid var(--line);min-height:44px}
.item-label{font-weight:500;overflow:hidden;text-overflow:ellipsis}.item-meta{color:var(--muted);font-size:14px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item-action form{margin:0}.item-block{grid-template-columns:1fr auto}.item-block pre{grid-column:1/-1;margin:0}
.newkey{background:var(--panel)}
button.small{padding:3px 10px;font-size:13px}
.notice{padding:8px 12px;border-radius:8px;background:var(--panel);border:1px solid var(--line)}
@media (max-width:600px){.item{grid-template-columns:1fr auto}.item-meta{grid-column:1/-1;grid-row:2}}
.hero{padding:48px 0 24px}.hero h1{font-size:30px}.hero p{font-size:17px;color:var(--muted);max-width:620px}
@media (max-width:600px){td:nth-child(3),th:nth-child(3){display:none}.prow{grid-template-columns:1fr auto}.pdate{display:none}}
`;

const LOGO = `<svg width="22" height="22" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#23344a"/><path d="M325.4 222.0 L274.5 253.8" stroke="#e0a45a" stroke-width="13" stroke-linecap="round"/><ellipse cx="234.7" cy="278.7" rx="47.0" ry="26.0" transform="rotate(148 234.7 278.7)" fill="#e0a45a"/><path d="M325.4 222.0 L267.2 207.5" stroke="#e0a45a" stroke-width="13" stroke-linecap="round"/><ellipse cx="221.6" cy="196.1" rx="47.0" ry="26.0" transform="rotate(194 221.6 196.1)" fill="#e0a45a"/><path d="M325.4 222.0 L295.4 170.0" stroke="#e0a45a" stroke-width="13" stroke-linecap="round"/><ellipse cx="271.9" cy="129.3" rx="47.0" ry="26.0" transform="rotate(240 271.9 129.3)" fill="#e0a45a"/><path d="M336 452 Q323.5 257.0 323 62" fill="none" stroke="#a8cf74" stroke-width="32" stroke-linecap="round"/></svg>`;

export function layout(title: string, body: Html | string, user?: User | null): string {
  const nav = user
    ? user.guest
      ? html`<nav><a href="/settings">Settings</a><a class="button primary" href="/auth/login">Sign in</a></nav>`
      : html`<nav><a href="/settings">Settings</a></nav>`
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
export function landing(_origin: string, note = ""): Response {
  return new Response(
    `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tule</title><link rel="icon" href="/favicon.svg"><style>${CSS}
.center{min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:28px;padding:16px}
.mark{display:flex;align-items:center;gap:12px;font-size:30px;font-weight:700;letter-spacing:.2px}
.google{display:inline-flex;align-items:center;gap:10px;padding:10px 18px;font-size:15px;border-radius:9px}</style></head>
<body><div class="center"><div class="mark">${LOGO.replace(/width="22" height="22"/, 'width="40" height="40"')}tule</div>
<a class="button google" href="/auth/login">${GOOGLE}Sign in with Google</a>${note ? `<p class="muted" style="max-width:360px;text-align:center">${esc(note)}</p>` : ""}</div></body></html>`,
    { headers: { "Content-Type": "text/html; charset=utf-8" } },
  );
}

export type ProjectRow = ProjectRef & { state: string; machine: string | null };

/** How an agent connects: the MCP connector (the control plane) and the CLI (the work). */
function connect(origin: string): Html {
  return html`<div class="card">
      <p><b>MCP connector</b> (projects, machines, keys): <code>${origin}/mcp</code><br>
      <span class="muted">Add it as a custom connector in Claude or ChatGPT, or as a remote MCP server in Claude Code, Codex or Cursor.</span></p>
      <p><b>CLI</b> (the work, over SSH):</p>
      <pre>pip install "tule-cli @ git+https://github.com/Janos95/tule#subdirectory=cli"
tule login --url ${origin}
tule ssh -p &lt;project&gt;</pre>
    </div>`;
}

export type GuestLimits = { projects: number; minutesPerDay: number; days: number };

export function projectsPage(user: User, rows: ProjectRow[], origin: string, error?: string, limits?: GuestLimits): Response {
  const state = (p: ProjectRow) => (p.state === "stopped" ? "stopped" : `${p.machine ?? ""} · ${p.state}`);
  const create = html`<details class="newp"${error || !rows.length ? html` open` : ""}>
      <summary><span class="plus">+</span> New project</summary>
      <form method="post" action="/projects/new" class="newp-form">
        <input name="name" pattern="[a-z0-9][a-z0-9-]{0,62}" placeholder="camera-bracket" aria-label="Project name" required autofocus />
        <button class="primary">Create</button>
      </form>
      ${error ? html`<p class="warn">${error}</p>` : ""}
    </details>`;
  const L = limits ?? { projects: 1, minutesPerDay: 30, days: 1 };
  const guest = user.guest
    ? html`<p class="notice">You're trying Tule as a guest: ${L.projects} project, ${L.minutesPerDay} machine minutes a day, deleted ${L.days === 1 ? "a day" : `${L.days} days`} after your last visit. <a href="/auth/login">Sign in</a> to keep your work.</p>`
    : "";
  const body = rows.length
    ? html`${guest}<div class="plist">
        ${rows.map(
          (p) => html`<a class="prow" href="/p/${p.name}">
            <span class="pname">${p.name}</span>
            <span class="pstate"><span class="dot ${p.state}"></span>${state(p)}</span>
            <span class="pdate">${new Date(p.created).toISOString().slice(0, 10)}</span></a>`,
        )}
        ${create}
      </div>`
    : html`${guest}<p class="muted">Start your first project, or ask your agent to ("start a Tule project for a camera bracket").</p>
      <div class="plist">${create}</div>
      <h2>Connect your agent</h2>
      ${connect(origin)}`;
  return page("Your projects", body, 200, user);
}

export type SettingsData = {
  runpod: { connected: boolean; updated: number | null; balance?: string | null; error?: string };
  apiKeys: { id: string; name: string; prefix: string; created: number; last_used: number | null }[];
  sshKeys: { id: string; key: string; name: string; created: number }[];
  hosts: { id: string; name: string; online: unknown; specs: unknown }[];
  usage: { month: string; cpuHours: number; gpuHours: number; gpuCost: number };
  quota: number;
  newKey?: string;
  message?: string;
  origin: string;
};

const day = (t: number | null) => (t ? new Date(t).toISOString().slice(0, 10) : "never");

/** A settings section: a heading, one line of help, then rows. */
const section = (title: string, hint: Html | string, rows: Html | string) =>
  html`<section class="sect"><h2>${title}</h2>${hint ? html`<p class="hint">${hint}</p>` : ""}<div class="list">${rows}</div></section>`;

/** A row: what it is, details, and an action (a small button or form). */
const item = (label: Html | string, meta: Html | string = "", action: Html | string = "") =>
  html`<div class="item"><span class="item-label">${label}</span><span class="item-meta">${meta}</span><span class="item-action">${action}</span></div>`;

/** A post button for one row. */
const rowButton = (actionUrl: string, text: string, id?: string, danger = false) =>
  html`<form method="post" action="${actionUrl}">${id ? html`<input type="hidden" name="id" value="${id}">` : ""}<button class="small${danger ? " danger" : ""}">${text}</button></form>`;

/** "+ Something" that opens into a one-line form. */
const addRow = (label: string, button: string, actionUrl: string, field: Html, open = false, note: Html | string = "") =>
  html`<details class="newp"${open ? html` open` : ""}><summary><span class="plus">+</span> ${label}</summary>
    <form method="post" action="${actionUrl}" class="newp-form" autocomplete="off">${field}<button class="primary">${button}</button></form>
    ${note ? html`<p class="hint" style="padding:0 8px 10px">${note}</p>` : ""}</details>`;

const copyButton = (text: string) => html`<button type="button" class="small" data-copy="${text}">Copy</button>`;

export function settingsPage(user: User, d: SettingsData): Response {
  const cli = `pip install "tule-cli @ git+https://github.com/Janos95/tule#subdirectory=cli"\ntule login --url ${d.origin}\ntule ssh -p <project>`;
  const agent = section(
    "Connect your agent",
    "The connector manages projects and machines; the CLI does the work over SSH.",
    html`${item("MCP connector", html`<code>${d.origin}/mcp</code>`, copyButton(`${d.origin}/mcp`))}
      <div class="item item-block"><span class="item-label">CLI</span><span class="item-action">${copyButton(cli)}</span><pre>${cli}</pre></div>`,
  );

  const rp = d.runpod;
  const runpod = user.guest
    ? section("GPU machines", "GPU machines run on your own RunPod account.", item("RunPod", "sign in to connect it", html`<a class="button small" href="/auth/login">Sign in</a>`))
    : section(
    "GPU machines",
    "GPU machines run on your RunPod account and are paid from its balance. Tule stores the key encrypted and never passes it to a machine.",
    html`${rp.connected
        ? item("RunPod", html`connected${rp.balance ? html` · ${rp.balance} balance` : ""}`, rowButton("/settings/runpod/remove", "Disconnect", undefined, true))
        : item("RunPod", "not connected")}
      ${addRow(
        rp.connected ? "Replace key" : "Connect RunPod",
        rp.connected ? "Replace" : "Connect",
        "/settings/runpod",
        html`<input name="key" type="password" placeholder="rpa_…" aria-label="RunPod API key" required />`,
        !!rp.error,
        rp.error ? html`<span class="warn">${rp.error}</span>` : "A read/write key from runpod.io → Settings → API Keys.",
      )}`,
  );

  const spec = (s: unknown) => {
    const x = (s ?? {}) as { cpus?: number; memoryGb?: number; gpus?: string[]; arch?: string };
    return [x.cpus ? `${x.cpus} cores` : "", x.memoryGb ? `${x.memoryGb} GB` : "", ...(x.gpus ?? []), x.arch ?? ""].filter(Boolean).join(" · ");
  };
  const computers = user.guest
    ? ""
    : section(
        "Your computers",
        "Run machines on your own hardware: Tule starts its container there, using the whole computer.",
        html`${d.hosts.map((h) => item(h.name, html`<span class="dot ${h.online ? "running" : ""}"></span>${h.online ? "online" : "offline"}${spec(h.specs) ? ` · ${spec(h.specs)}` : ""}`, rowButton("/settings/hosts/remove", "Remove", h.id, true)))}
          <details class="newp"><summary><span class="plus">+</span> Add a computer</summary>
            <div style="padding:0 8px 12px"><p class="hint">On the computer (it needs Docker; for NVIDIA GPUs also NVIDIA's container toolkit):</p>
            <pre>pip install "tule-cli @ git+https://github.com/Janos95/tule#subdirectory=cli"
tule host up --url ${d.origin}</pre>
            <p class="hint">It shows a code to approve here, then runs as a background service.</p></div></details>`,
      );

  const u = d.usage;
  const usage = section(
    "Usage",
    `This month (${u.month}).`,
    html`${item("CPU machines", `${u.cpuHours} h of ${d.quota} h`)}
      ${item("GPU machines", u.gpuCost ? `${u.gpuHours} h · about $${u.gpuCost.toFixed(2)} on RunPod` : `${u.gpuHours} h`)}`,
  );

  const ssh = section(
    "SSH keys",
    "They work on every project's machine. tule ssh adds its own.",
    html`${d.sshKeys.map((k) => item(k.name, html`<code>${k.key.slice(0, 24)}…</code> · ${day(k.created)}`, rowButton("/settings/ssh/remove", "Remove", k.id, true)))}
      ${addRow("Add SSH key", "Add", "/settings/ssh/add", html`<input name="key" placeholder="ssh-ed25519 AAAA… you@laptop" aria-label="Public key" required />`)}`,
  );

  const api = section(
    "API keys",
    "For the CLI and scripts. tule login makes one for you.",
    html`${d.newKey ? html`<div class="item item-block newkey"><span class="item-label">New key, shown once</span><span class="item-action">${copyButton(d.newKey)}</span><pre>${d.newKey}</pre></div>` : ""}
      ${d.apiKeys.map((k) => item(k.name, html`<code>${k.prefix}…</code> · used ${day(k.last_used)}`, rowButton("/settings/keys/revoke", "Revoke", k.id, true)))}
      ${addRow("Create API key", "Create", "/settings/keys/new", html`<input name="name" placeholder="CI" aria-label="Key name" required />`)}`,
  );

  const account = user.guest
    ? section("Account", "You're a guest: your project is deleted a day after your last visit. Signing in keeps it.", item("Guest", "", html`<a class="button small primary" href="/auth/login">Sign in</a>`))
    : section("Account", "", item(user.email, "", html`<form method="post" action="/auth/logout"><button class="small">Sign out</button></form>`));

  const body = html`${d.message ? html`<p class="notice">${d.message}</p>` : ""}${agent}${computers}${runpod}${usage}${ssh}${api}${account}
    <script>document.addEventListener("click",(e)=>{const b=e.target.closest("[data-copy]");if(!b)return;navigator.clipboard.writeText(b.dataset.copy);b.textContent="Copied";setTimeout(()=>b.textContent="Copy",1500)})</script>`;
  return page("Settings", body, 200, user);
}
