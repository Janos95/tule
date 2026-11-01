// The viewer page, /p/<project>: renders the project's /workspace/outputs
// (see DESIGN.md, "Outputs"). It connects to the project's Durable Object
// (wss://<host>/api/projects/<p>/view), which sends the outputs index whenever
// it changes and relays mesh requests to the machine's tule-agent: the page
// asks for a file by path and content hash, the agent tessellates it (cached
// by hash) and answers [rid u32][header length u32][JSON header][arrays]
// (runtime/tule_agent.py, encode_mesh).
//
// The dev token (until accounts exist) comes once in the link's fragment,
// #t=<token> (tule open), and is kept in localStorage.
import { initParts } from "./parts.js";
import { store } from "./store.js";
import { Viewer } from "./viewer.js";

const $ = (id) => document.getElementById(id);
const project = (location.pathname.match(/^\/p\/([a-z0-9][a-z0-9-]{0,62})\/?$/) ?? [])[1];
const frag = new URLSearchParams(location.hash.slice(1));
if (frag.get("t")) {
  store.setItem("tule/token", frag.get("t"));
  history.replaceState(null, "", location.pathname + location.search);
}
const token = store.getItem("tule/token");

let viewer;
try {
  viewer = new Viewer($("viewer"));
} catch (e) {
  $("viewer").innerHTML = `<div class="nogl">This browser cannot show 3D (WebGL): ${e.message}</div>`;
}
const parts = viewer ? initParts({ viewer, project: () => project ?? "" }) : null;

const state = {
  outputs: [], // the index from the machine
  selected: new URLSearchParams(location.search).get("o"),
  shown: null, // the key of what is drawn (output name and file hashes)
  frame: 0,
};
const meshes = new Map(); // hash -> Promise<parts>
const pending = new Map(); // rid -> {resolve, reject}
let ws = null;
let rid = 0;

function setStatus(text) {
  $("status").textContent = text;
  $("status").hidden = !text;
}

// ------------------------------------------------------------ connection

function connect(delay = 500) {
  if (!project) {
    setStatus("Open a project: /p/<name> (tule open -p <name>)");
    return;
  }
  if (!token) {
    setStatus("No access token: open this page with tule open");
    return;
  }
  $("project-title").textContent = project;
  document.title = `${project} · Tule`;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/api/projects/${project}/view?t=${encodeURIComponent(token)}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => (delay = 500);
  ws.onmessage = (e) => (typeof e.data === "string" ? onText(JSON.parse(e.data)) : onMesh(e.data));
  ws.onclose = (e) => {
    for (const p of pending.values()) p.reject(new Error("disconnected"));
    pending.clear();
    machine({ state: "offline" });
    if (e.code === 1008 || e.code === 4401) return setStatus("Not authorized: open this page with tule open");
    setTimeout(() => connect(Math.min(delay * 2, 10_000)), delay);
  };
}

function onText(m) {
  if (m.t === "outputs") {
    if (m.status) machine(m.status);
    state.outputs = m.outputs ?? [];
    renderList();
    show();
  } else if (m.t === "mesh-error") {
    const p = pending.get(m.rid);
    pending.delete(m.rid);
    p?.reject(new Error(m.error));
  }
}

function onMesh(buf) {
  const dv = new DataView(buf);
  const id = dv.getUint32(0);
  const p = pending.get(id);
  pending.delete(id);
  if (!p) return;
  try {
    p.resolve(decode(buf, 4));
  } catch (e) {
    p.reject(e);
  }
}

function decode(buf, at) {
  const dv = new DataView(buf);
  const hl = dv.getUint32(at, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, at + 4, hl)));
  const base = at + 4 + hl;
  const arr = (Type, r) => (r ? new Type(buf.slice(base + r[0], base + r[0] + r[1])) : null);
  return header.parts.map((p) => ({
    name: p.name || null,
    color: p.color,
    tags: p.tags ?? {},
    positions: arr(Float32Array, p.positions),
    normals: arr(Float32Array, p.normals),
    indices: arr(Uint32Array, p.indices),
    faceIds: arr(Uint32Array, p.faceIds),
    edges: arr(Float32Array, p.edges),
  }));
}

function request(file) {
  if (!meshes.has(file.hash)) {
    const promise = new Promise((resolve, reject) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return reject(new Error("not connected"));
      const id = ++rid;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ t: "mesh", rid: id, path: file.path, hash: file.hash }));
    });
    promise.catch(() => meshes.delete(file.hash)); // retry later
    meshes.set(file.hash, promise);
  }
  return meshes.get(file.hash);
}

function machine(s) {
  const el = $("machine");
  el.dataset.state = s.state;
  const text = {
    running: `${s.machine ?? "machine"} · running`,
    starting: `${s.machine ?? "machine"} · starting`,
    stopped: "machine stopped",
    offline: "reconnecting…",
  }[s.state] ?? s.state;
  $("machine-text").textContent = text;
  el.title = s.state === "stopped" ? "Start it with: tule up" : `The project's machine${s.sessions ? ` · ${s.sessions} SSH session(s)` : ""}`;
}

// ------------------------------------------------------------ outputs

/** part, assembly or sequence: output.json's kind, else inferred from the files. */
function kindOf(o) {
  if (o.meta?.kind) return o.meta.kind;
  if (o.files.length === 1) return "part";
  if (o.files.length > 2 && o.files.every((f) => /\d+\.[a-z]+$/i.test(f.file))) return "sequence";
  return "assembly";
}

function frames(o) {
  const byFile = new Map(o.files.map((f) => [f.file, f]));
  if (o.meta?.frames) return o.meta.frames.map((fr) => ({ ...fr, f: byFile.get(fr.file) })).filter((fr) => fr.f);
  return [...o.files].sort((a, b) => a.file.localeCompare(b.file, undefined, { numeric: true })).map((f) => ({ file: f.file, f }));
}

function renderList() {
  const list = $("output-list");
  list.textContent = "";
  $("outputs-empty").hidden = state.outputs.length > 0;
  if (!state.outputs.some((o) => o.name === state.selected)) state.selected = state.outputs[0]?.name ?? null;
  for (const o of state.outputs) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "output";
    b.role = "option";
    b.setAttribute("aria-selected", String(o.name === state.selected));
    const kind = kindOf(o);
    const n = kind === "sequence" ? `${frames(o).length} frames${o.meta?.status === "running" ? " · running" : ""}` : kind === "assembly" ? `${o.files.length} parts` : o.files[0]?.file ?? "";
    b.innerHTML = `<span class="output-name"></span><small></small>`;
    b.firstChild.textContent = o.meta?.title || o.name;
    b.lastChild.textContent = o.error ? o.error : `${kind} · ${n}`;
    b.addEventListener("click", () => {
      state.selected = o.name;
      state.frame = 0;
      const u = new URL(location.href);
      u.searchParams.set("o", o.name);
      history.replaceState(null, "", u);
      renderList();
      show();
    });
    list.append(b);
  }
}

async function show() {
  const o = state.outputs.find((x) => x.name === state.selected);
  if (!o) {
    state.shown = null;
    viewer?.clear();
    $("sequence").hidden = true;
    $("counts").textContent = "";
    return;
  }
  const kind = kindOf(o);
  let files = o.files;
  const seq = $("sequence");
  seq.hidden = kind !== "sequence";
  if (kind === "sequence") {
    const fr = frames(o);
    const following = state.frame >= +$("scrub").max; // stay on the newest frame while it grows
    $("scrub").max = String(Math.max(0, fr.length - 1));
    if (following) state.frame = fr.length - 1;
    state.frame = Math.min(state.frame, fr.length - 1);
    $("scrub").value = String(state.frame);
    const cur = fr[state.frame];
    const values = cur?.values ? Object.entries(cur.values).map(([k, v]) => `${k} ${+v.toPrecision(4)}`).join(" · ") : "";
    $("frame").textContent = cur ? [cur.label ?? `frame ${state.frame}`, values].filter(Boolean).join(" · ") : "";
    files = cur ? [cur.f] : [];
  }
  const key = o.name + "|" + files.map((f) => f.hash).join(",");
  if (key === state.shown) return;
  const first = !state.shown || !state.shown.startsWith(o.name + "|");
  state.shown = key;
  setStatus(meshes.size === 0 || !files.every((f) => meshes.has(f.hash)) ? "Loading…" : "");
  try {
    const loaded = await Promise.all(files.map(request));
    if (state.shown !== key) return;
    const named = (m, f) => {
      const entry = o.meta?.parts?.find((p) => p.file === f.file);
      return { ...m, name: entry?.name ?? (kind === "assembly" ? m.name ?? f.file.replace(/\.[^.]+$/, "") : m.name), color: entry?.color ?? m.color };
    };
    const all = loaded.flatMap((ps, i) => ps.map((m) => named(m, files[i])));
    const style = parts ? parts.frame(all) : undefined;
    viewer?.setMeshes(all, first ? true : "auto", style);
    const tris = all.reduce((n, m) => n + m.indices.length / 3, 0);
    $("counts").textContent = `${all.length} part${all.length === 1 ? "" : "s"} · ${tris.toLocaleString()} triangles`;
    setStatus("");
  } catch (e) {
    if (state.shown === key) {
      state.shown = null;
      setStatus(`Cannot show ${o.name}: ${e.message}`);
    }
  }
}

// ------------------------------------------------------------ controls

let playing = null;
function play(on) {
  clearInterval(playing);
  playing = on ? setInterval(() => {
    const max = +$("scrub").max;
    state.frame = state.frame >= max ? 0 : state.frame + 1;
    show();
  }, 250) : null;
  $("play").classList.toggle("on", !!on);
}
$("play").addEventListener("click", () => play(!playing));
$("scrub").addEventListener("input", (e) => {
  play(false);
  state.frame = +e.target.value;
  show();
});

function setWire(on) {
  viewer?.setWireframe(on);
  $("wire").classList.toggle("on", on);
  $("wire").setAttribute("aria-pressed", String(on));
}
$("wire").addEventListener("click", () => setWire(!viewer?.wire));
$("fit").addEventListener("click", () => viewer?.fit());
document.addEventListener("keydown", (e) => {
  if (e.target.closest?.("input, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "w" || e.key === "W") setWire(!viewer?.wire);
  else if (e.key === "f" || e.key === "F") viewer?.fit();
  else if (e.key === "p" || e.key === "P") parts?.toggle();
});

// hover: the B-rep face under the pointer, its id and tags (f12 · load)
let raf = 0;
$("viewer").addEventListener("pointermove", (e) => {
  if (e.buttons) return ($("pick").hidden = true);
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(() => {
    const hit = viewer?.pick(e.clientX, e.clientY);
    const label = $("pick");
    if (!hit) return (label.hidden = true);
    const tags = Object.entries(hit.mesh.tags).filter(([, ids]) => ids.includes(hit.face)).map(([t]) => t);
    label.textContent = [hit.mesh.name, `f${hit.face}`, ...tags].filter(Boolean).join(" · ");
    const r = $("stage").getBoundingClientRect();
    label.style.left = `${e.clientX - r.left + 14}px`;
    label.style.top = `${e.clientY - r.top + 14}px`;
    label.hidden = false;
  });
});
$("viewer").addEventListener("pointerleave", () => ($("pick").hidden = true));

connect();
