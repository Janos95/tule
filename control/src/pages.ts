// The site's own pages, rendered here (the viewer, /p/<project>, is the
// static app in web/): the landing page, the projects list, Settings (RunPod,
// API keys, SSH keys, usage), and the pages auth.ts shows.
import { GPUS } from "./runpod";
import type { ProjectRef, User } from "./accounts";
import PALETTE from "../../web/src/palette.css"; // Tule's colors, shared with the viewer

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

const CSS = `${PALETTE}
:root{--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;color-scheme:light dark}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 var(--sans);color:var(--text);background:var(--bg)}
header{display:flex;align-items:center;gap:14px;height:52px;padding:0 16px;border-bottom:1px solid var(--line)}
header .brand{display:flex;gap:8px;align-items:center;color:inherit;text-decoration:none;font-weight:700;font-size:17px}
header nav{display:flex;gap:14px;margin-left:auto;align-items:center}header nav a{color:var(--muted);text-decoration:none}header nav a:not(.button):hover{color:var(--text)}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}h1{font-size:18px;font-weight:600;letter-spacing:-0.01em;margin:8px 0 12px}h2{font-size:15px;font-weight:600;margin:32px 0 10px}
a{color:var(--link)}code{font:13px var(--mono);background:var(--panel);padding:1px 5px;border-radius:4px}
pre{font:13px/1.45 var(--mono);background:var(--panel);padding:12px;border-radius:8px;overflow-x:auto;white-space:pre-wrap;word-break:break-all}
.muted{color:var(--muted)}.warn{color:var(--warn)}.ok{color:var(--ok)}
button,.button{font:inherit;font-size:14px;padding:6px 14px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);cursor:pointer;text-decoration:none;display:inline-block}
button.primary,.button.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:500}
button.primary:hover,.button.primary:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
button:focus-visible,.button:focus-visible,input:focus-visible,a:focus-visible{outline:none;box-shadow:0 0 0 3px var(--focus)}
input:focus{border-color:var(--accent)}
.button.small{padding:3px 10px;font-size:13px}button.danger{color:var(--danger)}
input{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--text);width:100%}
label{display:flex;flex-direction:column;gap:4px;font-size:14px}
.stack{display:flex;flex-direction:column;gap:12px;max-width:520px}.row{display:flex;gap:8px;align-items:end;flex-wrap:wrap}.row>label{flex:1;min-width:200px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:middle}th{color:var(--muted);font-weight:500}
.card{border:1px solid var(--line);border-radius:10px;padding:16px;background:var(--panel)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--idle);margin-right:6px}.dot.running{background:var(--ok-fill)}.dot.starting{background:var(--warn-fill)}
.plist{display:flex;flex-direction:column;border-top:1px solid var(--line)}
.prow{display:grid;grid-template-columns:1fr auto 96px;align-items:center;gap:16px;padding:12px 8px;border-bottom:1px solid var(--line);color:inherit;text-decoration:none}
.prow:hover{background:var(--hover)}
.pname{font-weight:500}.pstate{color:var(--muted);font-size:14px}.pdate{color:var(--muted);font-size:13px;text-align:right}
.newp{border-bottom:1px solid var(--line)}
.newp summary,.newp-go{all:unset;box-sizing:border-box;display:block;width:100%;cursor:pointer;padding:12px 8px;color:var(--muted)}
.newp summary::-webkit-details-marker{display:none}
.newp summary:hover,.newp-go:hover{color:var(--text);background:var(--hover)}
.newp[open] summary{display:none}
.newp .plus{display:inline-block;width:20px}
.newp-form{display:flex;align-items:center;padding:12px 8px;margin:0}
.newp-form .plus{color:var(--muted)}
.newp-form input,.newp-form input:focus,.newp-form input:focus-visible{flex:1;min-width:0;border:0;border-radius:0;padding:0;font:inherit;line-height:inherit;background:transparent;outline:none;box-shadow:none;caret-color:var(--accent)}
.newp-form input::placeholder{color:var(--faint)}
.newp-note{margin:0;padding:0 8px 12px 28px;color:var(--muted);font-size:13.5px}
.sect{margin-top:28px}.sect h2{margin:0 0 2px}.hint{color:var(--muted);font-size:13.5px;margin:0 0 8px}
.list{display:flex;flex-direction:column;border-top:1px solid var(--line)}
.item{display:grid;grid-template-columns:minmax(120px,auto) 1fr auto;align-items:center;gap:16px;padding:10px 8px;border-bottom:1px solid var(--line);min-height:44px}
.item-label{font-weight:500;overflow:hidden;text-overflow:ellipsis}.item-meta{color:var(--muted);font-size:14px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item-action form{margin:0}.item-block{grid-template-columns:1fr auto}.item-block pre,.item-block .copyfield,.item-block .hint{grid-column:1/-1;margin:0}
.newkey{background:var(--selected)}
button.small{padding:3px 10px;font-size:13px}
.guestbar{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);padding:6px 14px;border-radius:999px;background:var(--warn-bg);border:1px solid var(--warn-fill);color:var(--warn);font-size:13px;font-weight:500;white-space:nowrap;box-shadow:0 2px 8px var(--shadow)}
body:has(.guestbar) main{padding-bottom:64px}
.agentbar{background:var(--panel);border-bottom:1px solid var(--line)}
.agentbar>div{max-width:860px;margin:0 auto;padding:8px 16px;display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.agentbar b{font-weight:600;font-size:14px;white-space:nowrap}
.agenthint{order:9;flex-basis:100%;margin:0;font-size:13px;color:var(--muted)}.agenthint b{font-weight:600;color:var(--text)}
.agentbtns{display:inline-flex;gap:6px;white-space:nowrap}.item-action .agenthint{text-align:right;margin-top:6px}
.copyfield{all:unset;box-sizing:border-box;display:inline-flex;align-items:center;gap:8px;min-width:0;max-width:100%;padding:5px 10px;border:1px solid var(--line-strong);border-radius:7px;background:var(--bg);color:var(--text);font:13px var(--mono);cursor:pointer}
.copyfield:hover{border-color:var(--faint)}.copyfield:focus-visible{box-shadow:0 0 0 3px var(--focus)}
.copyfield span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.copyfield.wrap{justify-content:space-between}.copyfield.wrap span{white-space:normal;word-break:break-all}
.copyfield svg{flex:none;width:14px;height:14px;fill:none;stroke:var(--muted);stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.copyfield .ck,.copyfield.copied .cp{display:none}.copyfield.copied .ck{display:block;stroke:var(--ok)}
.notice{padding:8px 12px;border-radius:8px;background:var(--panel);border:1px solid var(--line)}
@media (max-width:600px){.item{grid-template-columns:1fr auto}.item-meta{grid-column:1/-1;grid-row:2}}
.hero{padding:48px 0 24px}.hero h1{font-size:30px}.hero p{font-size:17px;color:var(--muted);max-width:620px}
@media (max-width:600px){td:nth-child(3),th:nth-child(3){display:none}.prow{grid-template-columns:1fr auto}.pdate{display:none}}
`;

const LOGO = `<svg width="22" height="22" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#23344a"/><path d="M325.4 222.0 L274.5 253.8" stroke="#e0a45a" stroke-width="13" stroke-linecap="round"/><ellipse cx="234.7" cy="278.7" rx="47.0" ry="26.0" transform="rotate(148 234.7 278.7)" fill="#e0a45a"/><path d="M325.4 222.0 L267.2 207.5" stroke="#e0a45a" stroke-width="13" stroke-linecap="round"/><ellipse cx="221.6" cy="196.1" rx="47.0" ry="26.0" transform="rotate(194 221.6 196.1)" fill="#e0a45a"/><path d="M325.4 222.0 L295.4 170.0" stroke="#e0a45a" stroke-width="13" stroke-linecap="round"/><ellipse cx="271.9" cy="129.3" rx="47.0" ry="26.0" transform="rotate(240 271.9 129.3)" fill="#e0a45a"/><path d="M336 452 Q323.5 257.0 323 62" fill="none" stroke="#8fc46a" stroke-width="32" stroke-linecap="round"/></svg>`;

export function layout(title: string, body: Html | string, user?: User | null, strip: Html | string = ""): string {
  const nav = user
    ? user.guest
      ? html`<nav><a href="/settings">Settings</a><a class="button primary" href="/auth/login">Sign in</a></nav>`
      : html`<nav><a href="/settings">Settings</a></nav>`
    : html`<nav><a class="button" href="/auth/login">Sign in</a></nav>`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)} · Tule</title><link rel="icon" href="/favicon.svg"><style>${CSS}</style></head>
<body><header><a class="brand" href="/">${LOGO}tule</a>${nav}</header>${strip}<main><h1>${esc(title)}</h1>${body}</main>${
    user?.guest ? `<div class="guestbar">Guest projects are deleted after a day.</div>` : ""
  }<script>${NEWP_JS}</script></body></html>`;
}

/** Copy buttons; "+ New …" rows: opening focuses the field, Esc closes it again. */
const NEWP_JS = `document.addEventListener("click",(e)=>{const g=e.target.closest?.("[data-chatgpt]");if(g){navigator.clipboard.writeText(g.dataset.chatgpt);const h=g.closest(".agentbar,.item")?.querySelector(".agenthint");if(h)h.hidden=false}});
document.addEventListener("click",(e)=>{const b=e.target.closest?.("[data-copy]");if(!b)return;navigator.clipboard.writeText(b.dataset.copy);b.classList.add("copied");setTimeout(()=>b.classList.remove("copied"),1500)});
document.addEventListener("click",(e)=>{const s=e.target.closest?.(".newp summary");if(s){e.preventDefault();s.parentElement.open=true;s.parentElement.querySelector("input")?.focus()}});
document.addEventListener("keydown",(e)=>{const d=e.key==="Escape"&&e.target.closest?.(".newp");if(d){e.target.value="";d.open=false;d.querySelector("summary").focus()}})`;

export function page(title: string, body: Html | string, status = 200, user?: User | null, strip: Html | string = ""): Response {
  return new Response(layout(title, body, user, strip), {
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
/** The last row of a list: "+ New …", which turns into a one-line field in place.
    Enter submits, Esc closes. */
function addRow(label: string, actionUrl: string, field: Html, open = false, note: Html | string = "") {
  return html`<details class="newp"${open ? html` open` : ""}><summary><span class="plus">+</span>${label}</summary>
    <form method="post" action="${actionUrl}" class="newp-form" autocomplete="off"><span class="plus">+</span>${field}</form>
    ${note ? html`<p class="newp-note">${note}</p>` : ""}</details>`;
}

export type GuestLimits = { projects: number; minutesPerDay: number; days: number };

export function projectsPage(user: User, rows: ProjectRow[], origin: string, error?: string, limits?: GuestLimits): Response {
  // where it runs, in words: cpu-* are Tule's, gpu-* are RunPod pods, "external" joined with a token, anything else is a computer's name
  const where = (m: string | null) =>
    !m ? "" : m.startsWith("cpu-") ? "Tule cloud" : m.startsWith("gpu-") ? `RunPod ${(GPUS[m.slice(4)] ?? m.slice(4)).replace(/^NVIDIA (GeForce )?/, "")}` : m === "external" ? "other machine" : m;
  const state = (p: ProjectRow) => (p.state === "stopped" ? "stopped" : [where(p.machine), p.state].filter(Boolean).join(" · "));
  const create = addRow(
    "New project",
    "/projects/new",
    html`<input name="name" pattern="[a-z0-9][a-z0-9-]{0,62}" placeholder="camera-bracket" aria-label="Project name" required${error ? html` autofocus` : ""} />`,
    !!error,
    error ? html`<span class="warn">${error}</span>` : "",
  );
  const body = html`<div class="plist">
        ${rows.map(
          (p) => html`<a class="prow" href="/p/${p.name}">
            <span class="pname">${p.name}</span>
            <span class="pstate"><span class="dot ${p.state}"></span>${state(p)}</span>
            <span class="pdate">${new Date(p.created).toISOString().slice(0, 10)}</span></a>`,
        )}
        ${create}
      </div>`;
  return page("Projects", body, 200, user, user.agent ? "" : agentStrip(origin));
}

export type SettingsData = {
  runpod: { connected: boolean; updated: number | null; balance?: string | null; error?: string };
  apiKeys: { id: string; name: string; prefix: string; created: number; last_used: number | null }[];
  sshKeys: { id: string; key: string; name: string; created: number }[];
  hosts: { id: string; name: string; online: unknown; specs: unknown }[];
  usage: { month: string; cpuHours: number; gpuHours: number; gpuCost: number };
  quota: number;
  newKey?: string;
  hostKey?: string;
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



/** A value shown as a field: clicking anywhere copies it (the icon turns into a check). */
const copyField = (text: string) =>
  html`<button type="button" class="copyfield" data-copy="${text}" title="Copy"><span>${text}</span><svg class="cp" viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V3.5a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/></svg><svg class="ck" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5"/></svg></button>`;

/** Until the account's agent has used the connector: a strip under the header with its address. */
/** Claude's own link that opens "Add custom connector" with Tule's name and address filled in
    (claude.com/docs/connectors/custom/add-unlisted). Connectors live on the Claude account, so this covers
    claude.ai, the desktop and mobile apps, and Claude Code signed in with that account. */
const addToClaude = (origin: string) =>
  html`<a class="button small" target="_blank" rel="noopener" href="https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=Tule&connectorUrl=${encodeURIComponent(`${origin}/mcp`)}">Add to Claude</a>`;

/** ChatGPT has no such link: the button copies the address, opens ChatGPT's plugins page and says what to click. */
const addToChatGPT = (origin: string) =>
  html`<a class="button small" target="_blank" rel="noopener" href="https://chatgpt.com/plugins" data-chatgpt="${origin}/mcp">Add to ChatGPT</a>`;

const agentButtons = (origin: string) => html`<span class="agentbtns">${addToClaude(origin)}${addToChatGPT(origin)}</span>`;
const agentHint = html`<p class="agenthint" hidden>Address copied. In ChatGPT, click <b>+</b> → <b>Add custom MCP server</b> and paste it.</p>`;

const agentStrip = (origin: string) =>
  html`<div class="agentbar"><div><b>Connect your agent</b>${agentButtons(origin)}${copyField(`${origin}/mcp`)}${agentHint}</div></div>`;

/** How to connect an agent. The connector is all a user needs: the agent sets up the CLI itself (connect_info).
    Settings also shows the CLI command, for working on a machine by hand. */
/** A value to copy, shown in full under its label (an install command, a new key). */
const copyBlock = (label: string, text: string, note: Html | string = "", id = "") =>
  html`<div class="item item-block newkey"${id ? html` id="${id}"` : ""}><span class="item-label">${label}</span><button type="button" class="copyfield wrap" data-copy="${text}" title="Copy"><span>${text}</span><svg class="cp" viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V3.5a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/></svg><svg class="ck" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5"/></svg></button>${note ? html`<p class="hint">${note}</p>` : ""}</div>`;

export function settingsPage(user: User, d: SettingsData): Response {
  const agent = section("Connect your agent", "", item("Connector", copyField(`${d.origin}/mcp`), html`${agentButtons(d.origin)}${agentHint}`));

  // Machines: Tule's own, your computers, RunPod; with this month's hours on each
  const u = d.usage;
  const rp = d.runpod;
  const spec = (s: unknown) => {
    const x = (s ?? {}) as { cpus?: number; memoryGb?: number; gpus?: string[]; arch?: string };
    return [x.cpus ? `${x.cpus} cores` : "", x.memoryGb ? `${x.memoryGb} GB` : "", ...(x.gpus ?? []), x.arch ?? ""].filter(Boolean).join(" · ");
  };
  const install = (key: string) => `curl -fsSL ${d.origin}/install.sh | sh -s -- ${key}`;
  const gpu = u.gpuHours ? ` · ${u.gpuHours} h this month${u.gpuCost ? ` (about $${u.gpuCost.toFixed(2)})` : ""}` : "";
  const machines = section(
    "Machines",
    "",
    html`${item("Tule cloud", `${u.cpuHours} h of ${d.quota} h this month`)}
      ${user.guest ? "" : html`${d.hosts.map((h) => item(h.name, html`<span class="dot ${h.online ? "running" : ""}"></span>${h.online ? "online" : "offline"}${spec(h.specs) ? ` · ${spec(h.specs)}` : ""}`, rowButton("/settings/hosts/remove", "Remove", h.id, true)))}
      ${rp.connected ? item("RunPod", html`connected${rp.balance ? html` · ${rp.balance} balance` : ""}${gpu}`, rowButton("/settings/runpod/remove", "Disconnect", undefined, true)) : ""}
      ${d.hostKey
        ? copyBlock("Paste this into a terminal on the computer", install(d.hostKey), "Works on Linux, macOS and Windows (WSL). Expires in an hour.", "add-computer")
        : html`<form method="post" action="/settings/hosts/new" class="newp"><button class="newp-go"><span class="plus">+</span>Add a computer</button></form>`}
      ${addRow(
        rp.connected ? "Replace RunPod key" : "Connect RunPod",
        "/settings/runpod",
        html`<input name="key" type="password" placeholder="RunPod API key, rpa_…" aria-label="RunPod API key" required />`,
        !!rp.error,
        rp.error ? html`<span class="warn">${rp.error}</span>` : "A read/write key from runpod.io → Settings → API Keys. Billed to your RunPod balance.",
      )}`}`,
  );

  // Keys: agents and the CLI add their own; this is mostly for removing them
  const keys = section(
    "Keys",
    "",
    html`${d.newKey ? copyBlock("New API key, shown once", d.newKey) : ""}
      ${d.sshKeys.map((k) => item(k.name, html`SSH · <code>${k.key.slice(0, 20)}…</code> · ${day(k.created)}`, rowButton("/settings/ssh/remove", "Remove", k.id, true)))}
      ${d.apiKeys.map((k) => item(k.name, html`API · <code>${k.prefix}…</code> · used ${day(k.last_used)}`, rowButton("/settings/keys/revoke", "Revoke", k.id, true)))}
      ${addRow("Add SSH key", "/settings/ssh/add", html`<input name="key" placeholder="ssh-ed25519 AAAA… you@laptop" aria-label="Public key" required />`)}
      ${addRow("Create API key", "/settings/keys/new", html`<input name="name" placeholder="Name, like CI" aria-label="Key name" required />`)}`,
  );

  const account = user.guest
    ? section("Account", "", item("Guest", "", html`<a class="button small primary" href="/auth/login">Sign in</a>`))
    : section("Account", "", item(user.email, "", html`<form method="post" action="/auth/logout"><button class="small">Sign out</button></form>`));

  const body = html`${d.message ? html`<p class="notice">${d.message}</p>` : ""}${agent}${machines}${keys}${account}`;
  return page("Settings", body, 200, user);
}
