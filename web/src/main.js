// The viewer page, /p/<project>: renders the project's /workspace/outputs
// (see DESIGN.md, "Outputs"). It connects to the project's Durable Object
// (wss://<host>/api/projects/<p>/view), which sends the outputs index whenever
// it changes and relays mesh requests to the machine's tule-agent: the page
// asks for a file by path and content hash, the agent tessellates it (cached
// by hash) and answers [rid u32][header length u32][JSON header][arrays]
// (runtime/tule_agent.py, encode_mesh).
//
// New requests use the generic relay: req(op, args) sends {t: "req", rid, op,
// args}; the agent's REQUESTS[op] answers {t: "res", rid, ...} (an object) or
// [rid u32][bytes] (an ArrayBuffer). {t: "event"} messages are the agent's
// broadcasts. Credentials: auth.js, the only place that knows them.
//
// The index has one entry per folder of outputs/, work/ and inputs/. An entry's geometry is
// drawn in 3D; each other file is its own view (files.js: tables, charts, documents,
// images). Under the selected entry the sidebar lists its views. Keeping outputs current
// is the agent's job (tule build): the page says only when a build failed; a row's
// tooltip says when it is out of date (tule build's records) or, for work in work/, that
// what it was based on changed.
//
// Geometry kinds (output.json "kind"): part, assembly, sequence, overlay
// (overlay.js has its format).
//
// Assembly placement: a part entry of an assembly's output.json may carry
// "transform", a 4x4 matrix in row-major order that maps the file's
// coordinates to the assembly's (rotation in the upper left 3x3, the
// translation in the last column, in the file's units):
//
//   {"kind": "assembly", "parts": [{"file": "finger.step", "name": "finger_l",
//     "transform": [[1, 0, 0, 20], [0, 1, 0, 0], [0, 0, 1, 5], [0, 0, 0, 1]]}]}
//
// (4 rows of 4 numbers, numpy's M.tolist(); a flat list of 16 numbers in the
// same order is read the same way). Without it a part stays where its file
// puts it.
import { initAnnotate } from "./annotate.js";
import { auth } from "./auth.js";
import { render as renderFile, size, typeOf, TYPE_ICONS } from "./files.js";
import { initOverlay } from "./overlay.js";
import { initParts } from "./parts.js";
import { initPlot } from "./plot.js";
import { Viewer, sortByFace } from "./viewer.js";

const $ = (id) => document.getElementById(id);
const project = (location.pathname.match(/^\/p\/([a-z0-9][a-z0-9-]{0,62})\/?$/) ?? [])[1];

let viewer;
try {
  viewer = new Viewer($("viewer"));
} catch (e) {
  $("viewer").innerHTML = `<div class="nogl">This browser cannot show 3D (WebGL): ${e.message}</div>`;
}
const parts = viewer ? initParts({ viewer, project: () => project ?? "" }) : null;
const plot = initPlot($("plot"), (i) => {
  play(false);
  state.frame = i;
  show();
});
const overlay = viewer ? initOverlay({ viewer, legend: $("legend"), marks: $("marks") }) : null;
const state = {
  machine: "unknown", // the machine's state: running, starting, stopped, offline
  outputs: [], // the index from the machine
  wanted: new URLSearchParams(location.search).get("o"), // the output asked for (the link, a click)
  selected: null, // the output shown: the wanted one once it exists, else the first
  view: new URLSearchParams(location.search).get("f"), // which of its views: "3d", a file name, "log" (null: the first)
  shown: null, // the key of what is drawn (output name and file hashes)
  frame: 0,
  meshes: [], // what is drawn
  record: null, // what is drawn, for an annotation: {name, kind, title, files, frame}
};
const meshes = new Map(); // hash -> Promise<parts>
const pending = new Map(); // rid -> {resolve, reject, raw: bytes wanted as they are}
let ws = null;
let rid = 0;
const listeners = new Set(); // the agent's broadcasts ({t: "event", event, ...})

const annotate = viewer
  ? initAnnotate({
      viewer,
      req,
      machine: () => state.machine,
      output: () => state.record,
      meshes: () => state.meshes,
      select: (name) => select(name),
      on: (f) => listeners.add(f),
    })
  : null;

function setStatus(text) {
  $("status").textContent = text;
  $("status").hidden = !text;
}

/** A message that goes away by itself. */
function flash(text) {
  setStatus(text);
  setTimeout(() => $("status").textContent === text && setStatus(""), 6000);
}

// ------------------------------------------------------------ connection

async function connect(delay = 500) {
  if (!project) {
    setStatus("Open a project: /p/<name> (tule open -p <name>)");
    return;
  }
  if (!(await auth.signedIn())) return;
  $("project-name").textContent = project;
  document.title = `${project} · Tule`;
  const pickedM = new URLSearchParams(location.search).get("machine");
  ws = new WebSocket(auth.socket(`/api/projects/${project}/view${pickedM ? `?machine=${encodeURIComponent(pickedM)}` : ""}`));
  ws.binaryType = "arraybuffer";
  ws.onopen = () => (delay = 500);
  ws.onmessage = (e) => (typeof e.data === "string" ? onText(JSON.parse(e.data)) : onMesh(e.data));
  ws.onclose = (e) => {
    for (const p of pending.values()) p.reject(new Error("disconnected"));
    pending.clear();
    machine({ state: "offline" });
    if (e.code === 1008 || e.code === 4401) return setStatus("Not authorized for this project: sign in with the account that owns it");
    setTimeout(() => connect(Math.min(delay * 2, 10_000)), delay);
  };
}

function onText(m) {
  if (m.t === "outputs") {
    if (m.status) machine(m.status);
    state.outputs = prepare(m.outputs ?? []);
    renderList();
    show();
  } else if (m.t === "mesh-error" || m.t === "res") {
    const p = pending.get(m.rid);
    pending.delete(m.rid);
    if (!p) return;
    if (m.error) p.reject(new Error(m.error));
    else p.resolve(m);
  } else if (m.t === "event") {
    for (const f of listeners) f(m);
  }
}


/** A request to the machine's agent (REQUESTS in runtime/tule_agent.py):
 * resolves to its answer (an object, or an ArrayBuffer for bytes). */
function req(op, args = {}, timeoutMs = 120_000) {
  return new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return reject(new Error("not connected to the project"));
    const id = ++rid;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`the machine did not answer ${op} within ${timeoutMs / 1000} s`));
    }, timeoutMs);
    const done = (f) => (v) => (clearTimeout(timer), f(v));
    pending.set(id, { resolve: done(resolve), reject: done(reject), raw: true });
    ws.send(JSON.stringify({ t: "req", rid: id, op, args }));
  });
}

function onMesh(buf) {
  const dv = new DataView(buf);
  const id = dv.getUint32(0);
  const p = pending.get(id);
  pending.delete(id);
  if (!p) return;
  try {
    p.resolve(p.raw ? buf.slice(4) : decode(buf, 4));
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
  return header.parts.map((p) =>
    sortByFace({
      name: p.name || null,
      color: p.color,
      tags: p.tags ?? {},
      positions: arr(Float32Array, p.positions),
      normals: arr(Float32Array, p.normals),
      indices: arr(Uint32Array, p.indices),
      faceIds: arr(Uint32Array, p.faceIds),
      edges: arr(Float32Array, p.edges),
    }),
  );
}

function request(file) {
  if (!meshes.has(file.hash)) {
    const promise = new Promise((resolve, reject) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return reject(new Error("not connected"));
      const id = ++rid;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ t: "mesh", rid: id, path: file.wire, hash: file.hash }));
    });
    promise.catch(() => meshes.delete(file.hash)); // retry later
    meshes.set(file.hash, promise);
  }
  return meshes.get(file.hash);
}

/** A machine's name as the projects page shows it. */
function machineName(m) {
  if (m.kind === "cloud") return "Tule cloud";
  if (m.kind === "gpu") return `RunPod ${(m.gpu ?? m.id.slice(4)).replace(/^NVIDIA (GeForce )?/, "")}`;
  if (m.kind === "joined") return "other machine";
  return m.computer ?? m.id;
}

const dirty = (m) => !!m.unsaved && m.unsaved.files + m.unsaved.commits > 0;

/** The footer: the copy shown (a project can be loaded on several machines; then it is a picker). */
function machine(s) {
  const all = s.machines ?? [];
  const m = all.find((x) => x.id === s.viewing);
  const st = s.state === "offline" ? "offline" : (m?.state ?? "stopped");
  state.machine = st;
  annotate?.machineChanged();
  const el = $("machine");
  el.dataset.state = st;
  const n = m && st !== "starting" && dirty(m);
  state.viewing = m?.id ?? null;
  $("save-now").hidden = !(n && st === "running");
  const text = $("machine-text");
  if (st === "offline") text.textContent = "reconnecting…";
  else if (!m) text.textContent = "not loaded on a machine";
  else {
    // the dot says running; the text says what is not obvious
    // unsaved: a running copy offers Save (which says it), a stopped one says so
    const offer = n && st === "running";
    text.textContent = `${machineName(m)}${st === "running" ? "" : ` · ${st}`}${n && !offer ? " · " : ""}`;
    if (n && !offer) text.insertAdjacentHTML("beforeend", `<span class="unsaved">unsaved</span>`);
  }
  el.title = !m ? "Load it with: tule up" : `${machineName(m)}'s copy of /workspace${m.sessions ? ` · ${m.sessions} SSH session(s)` : ""}`;
  const pick = $("machine-pick");
  const several = all.length > 1 && st !== "offline";
  pick.hidden = !several;
  $("machine-more").toggleAttribute("hidden", !several); // an SVG has no .hidden
  if (several) {
    pick.replaceChildren(
      ...all.map((x) => {
        const o = new Option(`${machineName(x)} · ${x.state}${dirty(x) ? " · unsaved" : ""}`, x.id);
        o.selected = x.id === s.viewing;
        return o;
      }),
    );
  }
}

// Save: what tule save does (commit, merge what other machines saved, push), for this copy
$("save-now").addEventListener("click", async () => {
  const b = $("save-now");
  b.disabled = true;
  b.lastChild.textContent = "Saving…";
  try {
    const r = await auth.api(`/api/projects/${project}/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ machine: state.viewing, message: "Save from the viewer" }),
    });
    const body = await r.json();
    const res = body.result ?? body;
    if (res.conflicts?.length) flash(`Not saved: ${res.conflicts.join(", ")} changed here and on another machine. Ask your agent to merge them.`);
    else if (res.error) flash(`Not saved: ${res.error}`);
    else flash("Saved");
  } catch (e) {
    // the API answers 400 with {error, conflicts, hint} on a conflict
    flash(/conflict/.test(e.message) ? "Not saved: this copy and another machine's save changed the same files. Ask your agent to merge them." : `Not saved: ${e.message}`);
  } finally {
    b.disabled = false;
    b.lastChild.textContent = "Save";
  }
});

$("machine-pick").addEventListener("change", (e) => {
  const id = e.target.value;
  const u = new URL(location.href);
  u.searchParams.set("machine", id);
  history.replaceState(null, "", u);
  ws?.readyState === WebSocket.OPEN && ws.send(JSON.stringify({ t: "pick", machine: id }));
});

// ------------------------------------------------------------ outputs

/** The index as the page uses it: id (an output's name, or work/<name>, inputs/<name>), files (geometry,
 * drawn in 3D) and docs (the other files, a view each); paths relative to /workspace. */
function prepare(list) {
  return list.map((o) => {
    const root = o.root ?? "outputs"; // an agent from before work/ and inputs/ were shown sends no root
    const all = (o.files ?? []).map((f) => ({
      ...f,
      wire: f.path, // as the agent named it, for mesh requests
      path: o.root ? f.path : `outputs/${f.path}`,
      type: typeOf(f.file),
    }));
    return { ...o, root, id: root === "outputs" ? o.name : `${root}/${o.name}`, files: all.filter((f) => f.type === "geometry"), docs: all.filter((f) => f.type !== "geometry") };
  });
}

/** What an entry can show: its geometry in 3D, each other file, and a failed build's log. */
function views(o) {
  const v = [];
  if (o.files.length) v.push({ id: "3d", label: "3D", type: "geometry" });
  for (const f of o.docs) v.push({ id: f.file, label: f.file, type: f.type, f });
  if (o.build?.state === "failed" && o.build.log) v.push({ id: "log", label: "build log", type: "log" });
  return v;
}

const viewOf = (o) => {
  const v = views(o);
  return v.find((x) => x.id === state.view) ?? v[0] ?? null;
};

/** part, assembly, sequence or overlay: output.json's kind, else inferred from the files. */
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

const ICONS = {
  part: '<path d="M8 1.5 14 5v6l-6 3.5L2 11V5zM8 8v6.5M8 8l6-3M8 8 2 5"/>',
  assembly: '<path d="M8 2 14 5 8 8 2 5zM2 8l6 3 6-3M2 11l6 3 6-3"/>',
  sequence: '<rect x="2" y="3.5" width="12" height="9" rx="1.5"/><path d="M5 3.5v9M11 3.5v9"/>',
  overlay: '<circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="2.2"/>',
  folder: '<path d="M2 4.5a1 1 0 0 1 1-1h3.5l1.5 1.5h5a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1z"/>',
};

/** An entry's icon: its geometry's kind, else its file's type (a folder of several). */
function iconOf(o) {
  if (o.files.length) return ICONS[kindOf(o)] ?? ICONS.part;
  if (o.docs.length === 1) return TYPE_ICONS[o.docs[0].type] ?? TYPE_ICONS.file;
  const types = new Set(o.docs.map((f) => f.type));
  return types.size === 1 ? TYPE_ICONS[[...types][0]] : o.docs.length ? ICONS.folder : TYPE_ICONS.file;
}

const changed = (o) => o.docs.filter((f) => f.basedOn?.changed?.length);
const building = (o) => o.building || o.build?.state === "building";

/** One short fact for a row, and how to color it: "out of date", "11 errors", "31 frames". */
function fact(o) {
  if (o.error || o.build?.state === "failed") return ["failed", "error"];
  if (building(o)) return ["building", "live"];
  if (!o.files.length) return ["", ""];
  const kind = kindOf(o);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  if (kind === "sequence") return o.meta?.status === "running" ? ["running", "live"] : [plural(frames(o).length, "frame"), ""];
  if (kind === "assembly") return [plural(Array.isArray(o.meta?.parts) ? o.meta.parts.length : o.files.length, "part"), ""];
  if (kind === "overlay") {
    const faces = Object.values(o.meta?.faces ?? {});
    const n = (s) => faces.filter((f) => f?.severity === s).length;
    if (n("error")) return [plural(n("error"), "error"), "error"];
    if (n("warning")) return [plural(n("warning"), "warning"), "warning"];
    const conds = faces.filter((f) => f?.role).length + (o.meta?.arrows?.length ?? 0);
    if (conds) return [plural(conds, "condition"), ""];
    if (faces.some((f) => Number.isFinite(f?.value))) return [o.meta?.scale?.label ?? "values", ""];
    return ["ok", "ok"];
  }
  return ["", ""];
}

const lastLine = (log) => (log ?? "").trim().split("\n").pop()?.trim() || null;

/** Why an entry may not match its sources, for its tooltip (the agent keeps them current). */
function staleness(o) {
  const b = o.build;
  if (b?.state === "failed") return `build failed: ${lastLine(b.log) ?? b.error ?? ""}`;
  if (b?.state === "stale") return b.why === "never built" ? `${b.script} has not run yet` : `out of date: ${b.why}`;
  const work = changed(o);
  return work.length ? work.map((f) => `check again: ${o.docs.length > 1 ? `${f.file}: ` : ""}${f.basedOn.changed.join(", ")} changed`).join("\n") : null;
}

// the parts tree moves into the list under the selected output; the list is rebuilt often,
// which detaches it, so keep the element itself (a lookup by id would find nothing then)
const partsTree = $("parts");
const folded = new Set(); // entries whose views and parts the user folded away (for this visit)
let scrolledTo = null; // the selected entry the list last scrolled to (phones show a short list)

function renderList() {
  // a link's output may not be in the list yet (the machine is starting): show the first meanwhile, keep wanting it
  const all = state.outputs;
  state.selected = all.some((o) => o.id === state.wanted) ? state.wanted : (all.find((o) => o.root === "outputs")?.id ?? all[0]?.id ?? null);
  const outs = all.filter((o) => o.root === "outputs");
  $("outputs-empty").hidden = outs.length > 0;
  fill($("output-list"), outs);
  for (const root of ["work", "inputs"]) {
    const list = all.filter((o) => o.root === root);
    $(`${root}-head`).hidden = !list.length;
    fill($(`${root}-list`), list);
  }
}

function fill(list, entries) {
  list.textContent = "";
  for (const o of entries) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "output";
    b.role = "option";
    const selected = o.id === state.selected;
    b.setAttribute("aria-selected", String(selected));
    const [text, tone] = fact(o);
    const vs = views(o);
    const v = selected ? viewOf(o) : null;
    const tree = o.files.length && kindOf(o) === "assembly";
    const foldable = vs.length > 1 || tree;
    const open = foldable && selected && !folded.has(o.id);
    b.innerHTML = `<svg class="output-icon" viewBox="0 0 16 16" aria-hidden="true">${iconOf(o)}</svg><span class="output-name"></span><small></small>${foldable ? '<svg class="output-fold" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4l4 4-4 4"/></svg>' : ""}`;
    b.children[1].textContent = o.name;
    b.children[2].textContent = text;
    if (tone) b.children[2].dataset.tone = tone;
    if (foldable) {
      b.setAttribute("aria-expanded", String(open));
      b.classList.toggle("open", open);
    }
    const kinds = [o.files.length ? kindOf(o) : null, ...o.docs.map((f) => f.file)].filter(Boolean);
    b.title = [o.meta?.title, `${o.root}/${o.name}${kinds.length ? ` · ${kinds.join(", ")}` : ""}`, o.error, staleness(o)].filter(Boolean).join("\n");
    b.addEventListener("click", () => {
      // shown already: a click folds or unfolds its views and parts (it stays so when you come back)
      if (foldable && selected) {
        if (folded.has(o.id)) folded.delete(o.id);
        else folded.add(o.id);
        return renderList();
      }
      select(o.id);
    });
    list.append(b);
    if (!selected) continue;
    const under = document.createElement("div");
    under.className = "under";
    if (open) {
      for (const x of vs.length > 1 ? vs : []) {
        const r = document.createElement("button");
        r.type = "button";
        r.className = "view";
        r.setAttribute("aria-current", String(x.id === v?.id));
        r.innerHTML = `<svg class="output-icon" viewBox="0 0 16 16" aria-hidden="true">${x.type === "geometry" ? iconOf({ ...o, docs: [] }) : TYPE_ICONS[x.type] ?? TYPE_ICONS.file}</svg><span></span>`;
        r.children[1].textContent = x.label;
        r.addEventListener("click", () => selectView(x.id));
        under.append(r);
        // an assembly's parts open under its 3D view (the tree hides itself for a single part)
        if (x.type === "geometry" && v?.id === "3d" && tree) under.append(partsTree);
      }
      if (vs.length === 1 && tree) under.append(partsTree);
    }
    if (under.childElementCount) list.append(under);
    if (scrolledTo !== o.id) {
      scrolledTo = o.id;
      requestAnimationFrame(() => (under.isConnected ? under : b).scrollIntoView({ block: "nearest" }));
    }
  }
}

/** An assembly part's "transform" as 16 numbers, row-major; throws saying what is wrong. */
function placement(t, file) {
  const flat = Array.isArray(t) && t.length === 4 && t.every(Array.isArray) ? t.flat() : t;
  if (!Array.isArray(flat) || flat.length !== 16 || !flat.every(Number.isFinite))
    throw new Error(`output.json: the transform of ${file} must be a 4x4 matrix, row-major: [[r00, r01, r02, tx], [r10, r11, r12, ty], [r20, r21, r22, tz], [0, 0, 0, 1]]`);
  return flat;
}

/** Shows an output (or input) by id; resolves once it is drawn. */
function select(id) {
  if (state.selected === id && state.shown) return Promise.resolve();
  state.wanted = id;
  state.view = null;
  state.frame = 0;
  remember();
  renderList();
  return show();
}

/** Shows one of the selected entry's views: "3d", one of its files, or "log". */
function selectView(id) {
  state.view = id;
  remember();
  renderList();
  return show();
}

function remember() {
  const u = new URL(location.href);
  u.searchParams.set("o", state.wanted);
  if (state.view) u.searchParams.set("f", state.view);
  else u.searchParams.delete("f");
  history.replaceState(null, "", u);
}

/** Something a table cell, chart point or document link names: a file of this entry, or another entry. */
function linkable(value) {
  if (typeof value !== "string" || !value || value.length > 200) return false;
  const v = value.replace(/^outputs\//, "").replace(/\/$/, "");
  return state.outputs.some((o) => o.id === v && o.id !== state.selected);
}
function open(value) {
  const o = state.outputs.find((x) => x.id === state.selected);
  if (o && views(o).some((x) => x.id === value)) return selectView(value);
  return select(String(value).replace(/^outputs\//, "").replace(/\/$/, ""));
}

function clearStage() {
  $("caption").hidden = true;
  overlay?.apply(null);
  $("strip").hidden = true;
  state.meshes = [];
  state.record = null;
  viewer?.clear();
  $("sequence").hidden = true;
  $("counts").textContent = "";
}

let pageKey = null;
let pageCleanup = null;
const reads = new Map(); // hash -> Promise<ArrayBuffer>: a file is read from the machine once
function readFile(f) {
  if (!reads.has(f.hash)) {
    const p = req("read", { path: f.path, max: 30 << 20 }, 300_000);
    p.catch(() => reads.delete(f.hash));
    if (reads.size > 64) reads.delete(reads.keys().next().value);
    reads.set(f.hash, p);
  }
  return reads.get(f.hash);
}
/** A view that is not 3D: the page over the stage. */
async function showPage(o, v) {
  const key = `${o.id}|${v?.id}|${v?.f?.hash ?? ""}|${v?.type === "log" ? o.build?.log : ""}`;
  $("stage").classList.add("paged");
  if (state.shown !== "page") {
    state.shown = "page";
    annotate?.reset();
    clearStage();
  }
  offerDownload(v?.f ? [v.f] : []);
  if (key === pageKey) return;
  pageKey = key;
  pageCleanup?.();
  pageCleanup = null;
  const page = $("page");
  page.textContent = ""; // not the last view's content while this one loads
  page.scrollTop = 0;
  const body = document.createElement("div");
  body.className = "page-body";
  if (!v) {
    body.append(Object.assign(document.createElement("p"), { className: "page-empty", textContent: o.build ? "Nothing to show yet." : "Empty." }));
  } else if (v.type === "log") {
    body.append(Object.assign(document.createElement("pre"), { className: "page-text", textContent: o.build.log }));
  } else {
    setStatus("Loading…");
    const cleanup = await renderFile(body, v.f, {
      read: readFile,
      files: o.docs,
      link: linkable,
      open,
      download,
    });
    setStatus("");
    if (pageKey !== key) return cleanup();
    pageCleanup = cleanup;
  }
  page.replaceChildren(body);
}

async function show() {
  const o = state.outputs.find((x) => x.id === state.selected);
  if (!o) {
    $("stage").classList.remove("paged");
    pageKey = null;
    $("page").textContent = "";
    clearStage();
    state.shown = null;
    offerDownload([]);
    return;
  }
  const v = viewOf(o);
  if (v?.type !== "geometry") return showPage(o, v);
  if (state.shown === "page") state.shown = null;
  $("stage").classList.remove("paged");
  pageKey = null;
  const kind = kindOf(o);
  $("caption").hidden = kind === "overlay" || !o.meta?.title;
  $("caption").textContent = o.meta?.title ?? "";
  let shownFiles = o.files;
  let frame = null;
  const seq = $("sequence");
  seq.hidden = kind !== "sequence";
  if (kind === "sequence") {
    const fr = frames(o);
    const following = state.frame >= +$("scrub").max; // stay on the newest frame while it grows
    $("scrub").max = String(Math.max(0, fr.length - 1));
    if (following) state.frame = fr.length - 1;
    state.frame = Math.max(0, Math.min(state.frame, fr.length - 1));
    $("scrub").value = String(state.frame);
    const cur = fr[state.frame];
    plot.set(fr, state.frame);
    // the plot's legend names the frame and its values; without values, the label goes here
    $("frame").textContent = `${state.frame + 1} / ${fr.length}${$("plot").hidden && cur?.label ? ` · ${cur.label}` : ""}`;
    $("live").hidden = o.meta?.status !== "running";
    shownFiles = cur ? [cur.f] : [];
    frame = state.frame;
  } else if (kind === "overlay" || kind === "part") {
    const f = o.files.find((x) => x.file === o.meta?.file) ?? o.files[0];
    shownFiles = f ? [f] : [];
  }
  // an assembly's parts: output.json's list when it has one (a file may be placed more than once)
  const entries = kind === "assembly" && Array.isArray(o.meta?.parts) ? o.meta.parts.filter((p) => o.files.some((f) => f.file === p?.file)) : null;
  if (entries?.length) shownFiles = entries.map((p) => o.files.find((f) => f.file === p.file));
  // what is drawn: the files and, but for a growing sequence, output.json (overlay faces, placements)
  const key = o.id + "|" + shownFiles.map((f) => f.hash).join(",") + (kind === "sequence" ? "" : "|" + JSON.stringify(o.meta ?? null));
  if (key === state.shown) return;
  const first = !state.shown || !state.shown.startsWith(o.id + "|");
  state.shown = key;
  if (first) annotate?.reset();
  setStatus(meshes.size === 0 || !shownFiles.every((f) => meshes.has(f.hash)) ? "Loading…" : "");
  try {
    const loaded = await Promise.all(shownFiles.map(request));
    if (state.shown !== key) return;
    const named = (m, f, i) => {
      const entry = entries?.length ? entries[i] : o.meta?.parts?.find((p) => p.file === f.file);
      const matrix = kind === "assembly" && entry?.transform != null ? placement(entry.transform, f.file) : null;
      return { ...m, name: entry?.name ?? (kind === "assembly" ? m.name ?? f.file.replace(/\.[^.]+$/, "") : m.name), color: entry?.color ?? m.color, matrix };
    };
    const all = loaded.flatMap((ps, i) => ps.map((m) => named(m, shownFiles[i], i)));
    const style = parts ? parts.frame(all) : undefined;
    viewer?.setMeshes(all, first ? true : "auto", style);
    overlay?.apply(o.meta ?? null, kind === "overlay");
    $("strip").hidden = $("legend").hidden;
    state.meshes = all;
    state.record = { name: o.name, kind, title: o.meta?.title ?? null, files: shownFiles.map((f) => f.path), frame };
    offerDownload(kind === "assembly" ? o.files : shownFiles);
    const tris = all.reduce((n, m) => n + m.indices.length / 3, 0);
    const grid = viewer?.gridStep ? ` · grid ${+viewer.gridStep.toPrecision(3)} mm` : "";
    $("counts").textContent = `${all.length} part${all.length === 1 ? "" : "s"} · ${tris.toLocaleString()} triangles${grid}`;
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

$("fit").addEventListener("click", () => viewer?.fit());
document.addEventListener("keydown", (e) => {
  if (e.target.closest?.("input, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "f" || e.key === "F") viewer?.fit();
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
    const notes = overlay?.describe(hit) ?? [];
    if (hit.arrow !== undefined) label.textContent = notes.join(" · ");
    else {
      const tags = Object.entries(hit.mesh.tags).filter(([, ids]) => ids.includes(hit.face)).map(([t]) => t);
      label.textContent = [hit.mesh.name, `f${hit.face}`, ...tags, ...notes].filter(Boolean).join(" · ");
    }
    const r = $("stage").getBoundingClientRect();
    label.style.left = `${e.clientX - r.left + 14}px`;
    label.style.top = `${e.clientY - r.top + 14}px`;
    label.hidden = false;
  });
});
$("viewer").addEventListener("pointerleave", () => ($("pick").hidden = true));


// ------------------------------------------------------------ framing around the panels

/** The view frames the part below the toolbar and the title. */
function updateInsets() {
  if (!viewer) return;
  const stage = $("stage").getBoundingClientRect();
  const insets = { left: 0, top: 46, right: 0, bottom: 26 }; // the toolbar row, the counts line
  viewer.setInsets(stage.width ? insets : {});
}
new ResizeObserver(updateInsets).observe($("stage"));

// ------------------------------------------------------------ downloads

/** Saves bytes as a file in the browser's downloads. */
function save(bytes, name) {
  const url = URL.createObjectURL(new Blob([bytes]));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

let downloads = [];
/** The Download button offers the shown output's geometry files, or the file shown. */
function offerDownload(list) {
  downloads = list;
  $("download").hidden = !list.length;
  $("download-menu").hidden = true;
}
async function download(f) {
  $("download-menu").hidden = true;
  try {
    save(await req("read", { path: f.path, max: 30 << 20 }, 300_000), f.file.split("/").pop());
  } catch (e) {
    flash(`Cannot download ${f.file}: ${e.message}`);
  }
}
$("download").addEventListener("click", (e) => {
  e.stopPropagation();
  if (downloads.length === 1) return download(downloads[0]);
  const menu = $("download-menu");
  menu.textContent = "";
  for (const f of downloads) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.innerHTML = "<span></span><small></small>";
    b.firstChild.textContent = f.file;
    b.lastChild.textContent = size(f.size);
    b.addEventListener("click", () => download(f));
    menu.append(b);
  }
  menu.hidden = !menu.hidden;
  $("download").setAttribute("aria-expanded", String(!menu.hidden));
});
document.addEventListener("click", () => ($("download-menu").hidden = true));

connect();
