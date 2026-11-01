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
// Output kinds (output.json "kind"): part, assembly, sequence, overlay
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
  $("project-title").textContent = project;
  document.title = `${project} · Tule`;
  ws = new WebSocket(auth.socket(`/api/projects/${project}/view`));
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
    state.outputs = m.outputs ?? [];
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
      ws.send(JSON.stringify({ t: "mesh", rid: id, path: file.path, hash: file.hash }));
    });
    promise.catch(() => meshes.delete(file.hash)); // retry later
    meshes.set(file.hash, promise);
  }
  return meshes.get(file.hash);
}

function machine(s) {
  const changed = state.machine !== s.state;
  state.machine = s.state;
  annotate?.machineChanged();
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
};

/** One short fact for a row, and how to color it: "11 errors", "31 frames", "3 parts". */
function fact(o, kind) {
  if (o.error) return ["error", "error"];
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  if (kind === "sequence") return o.meta?.status === "running" ? ["running", "live"] : [plural(frames(o).length, "frame"), ""];
  if (kind === "assembly") return [plural(Array.isArray(o.meta?.parts) ? o.meta.parts.length : o.files.length, "part"), ""];
  if (kind === "overlay") {
    const faces = Object.values(o.meta?.faces ?? {});
    const n = (s) => faces.filter((f) => f?.severity === s).length;
    if (n("error")) return [plural(n("error"), "error"), "error"];
    if (n("warning")) return [plural(n("warning"), "warning"), "warning"];
    const conds = faces.filter((f) => f?.role).length + (o.meta?.arrows?.length ?? 0);
    return conds ? [plural(conds, "condition"), ""] : ["ok", "ok"];
  }
  return ["", ""];
}

function renderList() {
  const list = $("output-list");
  list.textContent = "";
  $("outputs-empty").hidden = state.outputs.length > 0;
  // a link's output may not be in the list yet (the machine is starting): show the first meanwhile, keep wanting it
  state.selected = state.outputs.some((o) => o.name === state.wanted) ? state.wanted : (state.outputs[0]?.name ?? null);
  for (const o of state.outputs) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "output";
    b.role = "option";
    b.setAttribute("aria-selected", String(o.name === state.selected));
    const kind = kindOf(o);
    const [text, tone] = fact(o, kind);
    b.innerHTML = `<svg class="output-icon" viewBox="0 0 16 16" aria-hidden="true">${ICONS[kind] ?? ICONS.part}</svg><span class="output-name"></span><small></small>`;
    b.children[1].textContent = o.name;
    b.children[2].textContent = text;
    if (tone) b.children[2].dataset.tone = tone;
    b.title = [o.meta?.title, `${kind}${o.files.length ? ` · ${o.files.map((f) => f.file).join(", ")}` : ""}`, o.error].filter(Boolean).join("\n");
    b.addEventListener("click", () => select(o.name));
    list.append(b);
  }
}

/** An assembly part's "transform" as 16 numbers, row-major; throws saying what is wrong. */
function placement(t, file) {
  const flat = Array.isArray(t) && t.length === 4 && t.every(Array.isArray) ? t.flat() : t;
  if (!Array.isArray(flat) || flat.length !== 16 || !flat.every(Number.isFinite))
    throw new Error(`output.json: the transform of ${file} must be a 4x4 matrix, row-major: [[r00, r01, r02, tx], [r10, r11, r12, ty], [r20, r21, r22, tz], [0, 0, 0, 1]]`);
  return flat;
}

/** Shows an output by name; resolves once it is drawn. */
function select(name) {
  if (state.selected === name && state.shown) return Promise.resolve();
  state.wanted = name;
  state.frame = 0;
  const u = new URL(location.href);
  u.searchParams.set("o", name);
  history.replaceState(null, "", u);
  renderList();
  return show();
}

async function show() {
  const o = state.outputs.find((x) => x.name === state.selected);
  if (!o) {
    $("caption").hidden = true;
    state.shown = null;
    state.meshes = [];
    state.record = null;
    viewer?.clear();
    offerDownload([]);
    $("sequence").hidden = true;
    $("counts").textContent = "";
    return;
  }
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
  const key = o.name + "|" + shownFiles.map((f) => f.hash).join(",") + (kind === "sequence" ? "" : "|" + JSON.stringify(o.meta ?? null));
  if (key === state.shown) return;
  const first = !state.shown || !state.shown.startsWith(o.name + "|");
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
    overlay?.apply(kind === "overlay" ? o.meta : null);
    state.meshes = all;
    state.record = { name: o.name, kind, title: o.meta?.title ?? null, files: shownFiles.map((f) => f.path), frame };
    offerDownload(kind === "assembly" ? o.files : shownFiles);
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

$("fit").addEventListener("click", () => viewer?.fit());
document.addEventListener("keydown", (e) => {
  if (e.target.closest?.("input, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "f" || e.key === "F") viewer?.fit();
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


// ------------------------------------------------------------ downloads

const size = (n) => (n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 2 ** 20).toFixed(1)} MiB`);

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
/** The Download button offers the shown output's geometry files. */
function offerDownload(list) {
  downloads = list;
  $("download").hidden = !list.length;
  $("download-menu").hidden = true;
}
async function download(f) {
  $("download-menu").hidden = true;
  try {
    save(await req("read", { path: `outputs/${f.path}`, max: 30 << 20 }, 300_000), f.file.split("/").pop());
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
