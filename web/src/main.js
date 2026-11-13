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
let project = (location.pathname.match(/^\/p\/([a-z0-9][a-z0-9-]{0,62})\/?$/) ?? [])[1];
// /s/<token>: a view-only link; the project's name comes from the link, and nothing needs an account
const shareToken = (location.pathname.match(/^\/s\/(tules_[A-Za-z0-9_-]{8,64})\/?$/) ?? [])[1];
if (shareToken) document.body.classList.add("shared-view");
const LINK_OFF = "This link was turned off, or the project was deleted. Ask for a new link.";

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
  state.pinned = false;
  remember();
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
  frame: Math.max(0, (parseInt(new URLSearchParams(location.search).get("frame") ?? "", 10) || 1) - 1), // a sequence's frame (the link counts from 1)
  pinned: new URLSearchParams(location.search).has("frame"), // a frame asked for (a link, an annotation): a running sequence stays on it
  meshes: [], // what is drawn
  drawn: null, // the key of the last drawing that finished (state.shown while loading is the next one)
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
      select: (name, frame) => select(name, { frame, view: null }),
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
  if (shareToken) return openShared(delay);
  if (!project) {
    setStatus("Open a project: /p/<name> (tule open -p <name>)");
    return;
  }
  if (!(await auth.signedIn())) return;
  $("project-name").textContent = project;
  document.title = `${project} · Tule`;
  void initShare();
  const pickedM = new URLSearchParams(location.search).get("machine");
  ws = new WebSocket(auth.socket(`/api/projects/${project}/view${pickedM ? `?machine=${encodeURIComponent(pickedM)}` : ""}`));
  ws.binaryType = "arraybuffer";
  ws.onopen = () => (delay = 500);
  ws.onmessage = (e) => (typeof e.data === "string" ? onText(JSON.parse(e.data)) : onMesh(e.data));
  ws.onclose = (e) => {
    for (const p of pending.values()) p.reject(new Error("disconnected"));
    pending.clear();
    chunks.clear();
    machine({ state: "offline" });
    if (e.code === 1008 || e.code === 4401) return setStatus("Not authorized for this project: sign in with the account that owns it");
    setTimeout(() => connect(Math.min(delay * 2, 10_000)), delay);
  };
}

/** A view-only link: the outputs list and files as frozen when it was made, from storage; models are
 * meshed in this browser (stepmesh.js). No machine and no account. */
async function openShared(delay) {
  let r;
  try {
    r = await fetch(`/api/share/${shareToken}`);
  } catch {
    return void setTimeout(() => openShared(Math.min(delay * 2, 10_000)), delay);
  }
  if (!r.ok) {
    if (r.status !== 404 && r.status !== 410) return void setTimeout(() => openShared(Math.min(delay * 2, 10_000)), delay); // the server, not the link
    $("machine-text").textContent = "link turned off";
    return setStatus(LINK_OFF);
  }
  const s = await r.json();
  project = s.project;
  $("project-name").textContent = project;
  document.title = `${project} · Tule`;
  $("machine").dataset.state = "shared";
  $("machine-text").textContent = `as of ${new Date(s.at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
  $("machine").title = "A view-only copy, frozen when the link was made";
  onText({ t: "outputs", outputs: s.outputs });
}

/** A file of the shared snapshot, as an ArrayBuffer (the first `max` bytes). */
async function sharedFile(path, max) {
  const r = await fetch(`/api/share/${shareToken}/file?path=${encodeURIComponent(path)}`);
  if (!r.ok) {
    let msg = `${r.status}`;
    try {
      msg = (await r.json()).error ?? msg;
    } catch {
      // not JSON
    }
    throw new Error(msg);
  }
  const buf = await r.arrayBuffer();
  return max && buf.byteLength > max ? buf.slice(0, max) : buf;
}

let stepWorker;
const stepJobs = new Map();
/** A model of the shared snapshot as the machine's mesh message would decode, meshed in a worker. */
async function sharedMesh(file) {
  // the machine's mesh, kept when the project was viewed or shared; else this browser meshes the model
  if (file.hash) {
    const r = await fetch(`/api/share/${shareToken}/mesh?hash=${file.hash}`).catch(() => null);
    if (r?.ok) return unpack(await r.arrayBuffer(), 0);
  }
  const bytes = await sharedFile(file.path ?? file.wire);
  if (!stepWorker) {
    stepWorker = new Worker(new URL("./stepmesh.js", import.meta.url));
    stepWorker.onmessage = ({ data }) => {
      const job = stepJobs.get(data.id);
      stepJobs.delete(data.id);
      if (data.error) job?.reject(new Error(data.error));
      else job?.resolve(unpack(data.buf, 0));
    };
    stepWorker.onerror = (e) => { // it could not start: fail what waits, and start a new one next time
      for (const job of stepJobs.values()) job.reject(new Error(e.message || "could not start the mesher"));
      stepJobs.clear();
      stepWorker = null;
    };
  }
  const id = ++rid;
  return new Promise((resolve, reject) => {
    stepJobs.set(id, { resolve, reject });
    stepWorker.postMessage({ id, name: file.path ?? file.wire, bytes }, [bytes]);
  });
}

function onText(m) {
  if (m.t === "outputs") {
    if (m.status) machine(m.status);
    state.outputs = prepare(m.outputs ?? []);
    // meshes of files that are gone (rebuilt, deleted) are dropped: a long session would keep them all
    const live = new Set(state.outputs.flatMap((o) => o.files.map((f) => f.hash)));
    for (const h of meshes.keys()) if (!live.has(h)) meshes.delete(h);
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
  if (shareToken) return op === "read" ? sharedFile(args.path, args.max) : Promise.reject(new Error("not available from a view-only link"));
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

const chunks = new Map(); // rid -> the parts of a big binary answer so far (they come with the rid's top bit set)
function onMesh(buf) {
  const dv = new DataView(buf);
  let id = dv.getUint32(0);
  if (id & 0x80000000) {
    id &= 0x7fffffff;
    if (pending.has(id)) chunks.set(id, [...(chunks.get(id) ?? []), buf.slice(4)]);
    return;
  }
  const p = pending.get(id);
  pending.delete(id);
  const before = chunks.get(id);
  chunks.delete(id);
  if (!p) return;
  if (before) {
    const all = [...before, buf.slice(4)];
    const whole = new Uint8Array(4 + all.reduce((n, c) => n + c.byteLength, 0));
    let at = 4;
    for (const c of all) whole.set(new Uint8Array(c), at), (at += c.byteLength);
    buf = whole.buffer;
  }
  try {
    p.resolve(p.raw ? buf.slice(4) : unpack(buf, 4));
  } catch (e) {
    p.reject(e);
  }
}

/** A mesh message from the machine or the mesh cache: pack_mesh's (it starts with "TMZ2") or encode_mesh's. */
async function unpack(buf, at) {
  const b = new Uint8Array(buf, at);
  if (!(b[0] === 84 && b[1] === 77 && b[2] === 90 && b[3] === 50)) return decode(buf, at);
  const raw = await new Response(new Blob([b.subarray(4)]).stream().pipeThrough(new DecompressionStream("deflate"))).arrayBuffer();
  return decodePacked(raw);
}

/** pack_mesh's arrays (runtime/tule_agent.py) back into encode_mesh's. */
function decodePacked(buf) {
  const dv = new DataView(buf);
  const hl = dv.getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, hl)));
  const base = 4 + hl;
  const arr = (Type, r, n) => new Type(buf.slice(base + r[0], base + r[0] + n * Type.BYTES_PER_ELEMENT));
  const grid = (p, r, n) => {
    const q = arr(Uint16Array, r, 3 * n);
    const out = new Float32Array(n * 3);
    let x = 0, y = 0, z = 0;
    for (let i = 0; i < n; i++) {
      x = (x + q[3 * i]) & 0xffff;
      y = (y + q[3 * i + 1]) & 0xffff;
      z = (z + q[3 * i + 2]) & 0xffff;
      out[3 * i] = p.lo[0] + x * p.step[0];
      out[3 * i + 1] = p.lo[1] + y * p.step[1];
      out[3 * i + 2] = p.lo[2] + z * p.step[2];
    }
    return out;
  };
  return header.parts.map((p) => {
    const I = p.wide ? Uint32Array : Uint16Array;
    const nv = p.nv;
    const oct = arr(Int16Array, p.normals, 2 * nv);
    const normals = new Float32Array(nv * 3);
    for (let i = 0; i < nv; i++) {
      let x = oct[2 * i] / 1023, y = oct[2 * i + 1] / 1023;
      const z = 1 - Math.abs(x) - Math.abs(y);
      if (z < 0) [x, y] = [(1 - Math.abs(y)) * (x >= 0 ? 1 : -1), (1 - Math.abs(x)) * (y >= 0 ? 1 : -1)];
      const l = Math.hypot(x, y, z) || 1;
      normals[3 * i] = x / l, normals[3 * i + 1] = y / l, normals[3 * i + 2] = z / l;
    }
    const codes = arr(I, p.indices, 3 * p.nt);
    const indices = new Uint32Array(codes.length);
    for (let i = 0, next = 0; i < codes.length; i++) {
      indices[i] = next - codes[i];
      if (codes[i] === 0) next++;
    }
    return {
      name: p.name || null,
      color: p.color,
      tags: p.tags ?? {},
      positions: grid(p, p.positions, nv),
      normals,
      indices,
      faceIds: Uint32Array.from(arr(I, p.faceIds, p.nt)),
      edges: grid(p, p.edges, p.ne),
      sorted: true, // by face, as sortByFace
    };
  });
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
    const promise = shareToken ? sharedMesh(file) : new Promise((resolve, reject) => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return reject(new Error("not connected"));
      const id = ++rid;
      // the machine gives up after 15 minutes; past that the answer was lost on the way
      const timer = setTimeout(() => (pending.delete(id), reject(new Error("no answer from the machine; reload to try again"))), 960_000);
      const done = (f) => (v) => (clearTimeout(timer), f(v));
      pending.set(id, { resolve: done(resolve), reject: done(reject) });
      ws.send(JSON.stringify({ t: "mesh", rid: id, path: file.wire, hash: file.hash, v: 2 }));
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
  if (m.kind === "joined") return "joined machine";
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

const framesOf = new Map(); // output.json hash -> frames (null while loading)
/** A large output.json's frames, once read (the index leaves them out); reading starts here. */
function bigFrames(ff) {
  if (!framesOf.has(ff.hash)) {
    framesOf.set(ff.hash, null);
    readFile(ff)
      .then((buf) => {
        framesOf.set(ff.hash, JSON.parse(new TextDecoder().decode(buf)).frames ?? []);
        for (const k of framesOf.keys()) if (framesOf.size > 8 && k !== ff.hash) framesOf.delete(k);
        state.outputs = state.outputs.map((o) => (o.meta?.framesFile?.hash === ff.hash ? { ...o, meta: { ...o.meta, frames: framesOf.get(ff.hash) } } : o));
        renderList();
        show();
      })
      .catch(() => framesOf.delete(ff.hash));
  }
  return framesOf.get(ff.hash);
}

/** A long study whose frames are still being read from its output.json. */
const framesLoading = (o) => !!o.meta?.framesFile && framesOf.get(o.meta.framesFile.hash) == null;

/** The index as the page uses it: id (an output's name, or work/<name>, inputs/<name>), files (geometry,
 * drawn in 3D) and docs (the other files, a view each); paths relative to /workspace. */
function prepare(list) {
  return list.map((o) => {
    const ff = o.meta?.framesFile; // a long motion study: its frames are read from output.json
    if (ff) o = { ...o, meta: { ...o.meta, frames: bigFrames(ff) ?? [] } };
    const root = o.root ?? "outputs"; // an agent from before work/ and inputs/ were shown sends no root
    const all = (o.files ?? []).map((f) => ({
      ...f,
      wire: f.path, // as the agent named it, for mesh requests
      path: o.root ? f.path : `outputs/${f.path}`,
      type: typeOf(f.file),
    }));
    // an overlay's field grids and surface meshes: drawn on the part, not a view
    const field = new Set((Array.isArray(o.meta?.fields) ? o.meta.fields : []).flatMap((x) => [x?.file, ...Object.values(x?.mesh && typeof x.mesh === "object" ? x.mesh : {})]));
    // a tule.Mesh's name, color and tags beside its STL (x.stl.json): read with the mesh, not a view
    for (const f of all) if (/\.stl\.json$/i.test(f.file) && all.some((g) => g.file === f.file.slice(0, -5))) field.add(f.file);
    return { ...o, root, id: root === "outputs" ? o.name : `${root}/${o.name}`, files: all.filter((f) => f.type === "geometry"), docs: all.filter((f) => f.type !== "geometry" && !field.has(f.file)), fields: all.filter((f) => field.has(f.file)) };
  });
}

/** What an entry can show: its geometry in 3D, each other file, and a failed build's log. */
function views(o) {
  const v = [];
  // a document named after the output is what it is for (shopping-list/shopping-list.md): it opens first, the 3D after it
  // (a set of parts with their drawings opens on the first drawing); data beside a model (motion.csv, plate.gcode)
  // and motions and plates open on the 3D
  const moving = o.meta?.kind === "sequence" || o.meta?.frames || o.meta?.plate;
  // a part, assembly or overlay is its model: a README beside it comes after the 3D
  const model = ["part", "assembly", "overlay"].includes(o.meta?.kind);
  const named = o.files.length && !moving && (o.docs.find((f) => (readme(f) && !model) || (["document", "pdf", "dxf"].includes(f.type) && base(f.file) === o.name.split("/").pop())) ?? drawn(o)[0]);
  if (o.files.length && !named) v.push({ id: "3d", label: "3D", type: "geometry" });
  // a README first; beside a model, files with no view of their own (an STL, a .3mf) are only in Download
  // then by type, a document, a chart before a table; the one named after the output first among its type
  const order = ["document", "pdf", "dxf", "chart", "image", "table", "data", "text"]; // a chart says more than its numbers
  const rank = (f) => (readme(f) ? 0 : 2 * ((order.indexOf(f.type) + 1) || 10) + (base(f.file) === o.name.split("/").pop() ? 0 : 1));
  const docs = o.docs.filter((f) => !sheetSvg(o, f)).sort((a, b) => rank(a) - rank(b));
  for (const f of o.files.length ? docs.filter((f) => f.type !== "file") : docs) {
    v.push({ id: f.file, label: f.file, type: f.type, f });
    if (f === named) v.push({ id: "3d", label: "3D", type: "geometry" });
  }
  if (o.build?.state === "failed" && o.build.log) v.push({ id: "log", label: "build log", type: "log" });
  return v;
}

const viewOf = (o) => {
  const v = views(o);
  return v.find((x) => x.id === state.view) ?? v[0] ?? null;
};

const base = (file) => file.replace(/\.[^.]*$/, "");
/** The page to read first: README.md, START-HERE.md, 00-upload-these.md. */
const readme = (f) => f.type === "document" && /^((\d+[-_ ]*)?(readme|start[-_ ]?here)\b|0+[-_ ])[^/]*\.md$/i.test(f.file);
/** The drawings (PDFs) named like the entry's models: a set of parts, each with its drawing. */
const drawn = (o) => o.docs.filter((d) => d.type === "pdf" && o.files.some((f) => base(f.file) === base(d.file)));

/** part, assembly, sequence or overlay: output.json's kind, else inferred from the files. */
function kindOf(o) {
  if (o.meta?.kind) return o.meta.kind;
  if (o.files.length === 1) return "part";
  // frames written by hand: numbered files (plate-01.step …), unless each has its drawing (R11-001.step and .pdf: parts)
  if (o.files.length > 2 && o.files.every((f) => /\d+\.[a-z]+$/i.test(f.file)) && !drawn(o).length) return "sequence";
  return "assembly";
}

function frames(o) {
  const byFile = new Map(o.files.map((f) => [f.file, f]));
  // a frame is one file, or parts (each file written once) with placements: a motion study
  if (o.meta?.frames)
    return o.meta.frames
      .map((fr) => ({ ...fr, f: byFile.get(fr.file), parts: Array.isArray(fr.parts) ? fr.parts.map((p) => ({ ...p, f: byFile.get(p?.file) })) : null }))
      .filter((fr) => fr.f || (fr.parts?.length && fr.parts.every((p) => p.f)));
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

/** One short fact for a row, and how to color it: "failed", "11 errors", "31 frames". */
function fact(o) {
  if (o.error || o.build?.state === "failed") return ["failed", "error"];
  if (building(o)) return ["building", "live"];
  // what it reads changed since it was built: it may not show the design any more (the tooltip says why)
  if (o.build?.state === "stale" && o.build.why !== "never built" && (o.files.length || o.docs.length)) return ["out of date", "warning"];
  if (o.build?.state === "stale" && o.build.why === "never built") return ["not built", ""];
  if (changed(o).length) return ["check again", "warning"];
  if (!o.files.length) return ["", ""];
  const kind = kindOf(o);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  if (kind === "sequence") return o.meta?.status === "running" ? ["running", "live"] : framesLoading(o) ? ["loading", ""] : [plural(frames(o).length, "frame"), ""];
  if (kind === "assembly" && o.meta?.fact) return [String(o.meta.fact), ""]; // a print plate: "2 h 10 min · 31 g"
  if (kind === "assembly") return [plural(Array.isArray(o.meta?.parts) ? o.meta.parts.length : o.files.length, "part"), ""];
  if (kind === "overlay") {
    const faces = Object.values(o.meta?.faces ?? {});
    const n = (s) => faces.filter((f) => f?.severity === s).length;
    // the result in a few words, when the writer gave one: "gap 0.13 mm", "safety 1.5"
    if (o.meta?.fact) return [String(o.meta.fact), n("error") ? "error" : n("warning") ? "warning" : ""];
    if (n("error")) return [plural(n("error"), "error"), "error"];
    if (n("warning")) return [plural(n("warning"), "warning"), "warning"];
    // the conditions by role: "4 fixed · 4 load", not "10 conditions"
    const roles = new Map();
    for (const f of faces) if (f?.role) roles.set(String(f.role), (roles.get(String(f.role)) ?? 0) + 1);
    if (roles.size) return [[...roles].map(([r, n]) => `${n} ${r}`).join(" · "), ""];
    if (o.meta?.arrows?.length) return [plural(o.meta.arrows.length, "arrow"), ""];
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
  // without a link to one: the model that changed last (else the output): a check's report is rebuilt after the model
  const recent = all.filter((o) => o.root === "outputs").sort((a, b) => (b.changed ?? 0) - (a.changed ?? 0));
  const newest = recent.find((o) => o.files.length) ?? recent[0];
  if (state.wanted == null && newest) state.wanted = newest.id; // once: the view doesn't jump while the agent works
  // a link may name work/ or inputs/ entries without their folder (?o=shopping for work/shopping)
  let wanted = [state.wanted, `work/${state.wanted}`, `inputs/${state.wanted}`].find((id) => all.some((o) => o.id === id));
  // a link to a file in an output (?o=motion/height.vl.json): that output, on that file
  const cut = state.wanted?.lastIndexOf("/") ?? -1;
  if (!wanted && cut > 0 && all.some((o) => o.id === state.wanted.slice(0, cut))) {
    [wanted, state.view] = [state.wanted.slice(0, cut), state.wanted.slice(cut + 1)];
    state.wanted = wanted;
  }
  state.selected = wanted ?? newest?.id ?? all[0]?.id ?? null;
  const outs = all.filter((o) => o.root === "outputs");
  partsTree.remove(); // shown only under the entry that has parts (below)
  $("outputs-empty").hidden = outs.length > 0;
  fill($("output-list"), outs);
  for (const root of ["work", "inputs"]) {
    const list = all.filter((o) => o.root === root);
    $(`${root}-head`).hidden = !list.length;
    fill($(`${root}-list`), list);
  }
}

// groups (src/checks/fit.py makes outputs/checks/fit): folded unless they hold the selected output or the user opened them
const groupOpen = new Map();
const groupOf = (o) => (o.root === "outputs" && o.name.includes("/") ? o.name.split("/")[0] : null);

// "01-printing" sorts before "02-checks" and shows as "printing": a number in front orders, it is not a name
const shown = (n) => n.replace(/^(\d+|\d{2,}[a-z])[-_. ]+(?=\S)/, ""); // 01-print, 02a-fit: the order, unshown (3d-print stays)

function fill(list, entries) {
  list.textContent = "";
  entries = [...entries].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  // each group's members right after its row, where the group first appears
  const order = [];
  for (const o of entries) {
    const g = groupOf(o);
    if (!g) order.push(o);
    else if (!order.some((x) => x.group === g)) order.push({ group: g, members: entries.filter((x) => groupOf(x) === g) });
  }
  for (const row of order) {
    if (!row.group) {
      entry(list, row);
      continue;
    }
    const { group, members } = row;
    const open = groupOpen.get(group) ?? members.some((o) => o.id === state.selected);
    const h = document.createElement("button");
    h.type = "button";
    h.className = "output group" + (open ? " open" : "");
    h.setAttribute("aria-expanded", String(open));
    h.innerHTML = `<svg class="output-icon" viewBox="0 0 16 16" aria-hidden="true">${ICONS.folder}</svg><span class="output-name"></span><small></small><svg class="output-fold" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4l4 4-4 4"/></svg>`;
    h.children[1].textContent = shown(group);
    const bad = members.filter((o) => ["error", "warning"].includes(fact(o)[1])).length;
    h.children[2].textContent = bad ? `${bad} to check` : String(members.length);
    if (bad) h.children[2].dataset.tone = "warning";
    h.title = `src/${group}/: ${members.length} output${members.length === 1 ? "" : "s"}`;
    h.addEventListener("click", () => {
      groupOpen.set(group, !open);
      renderList();
    });
    list.append(h);
    if (open) for (const o of members) entry(list, o, group);
  }
}

function entry(list, o, group = null) {
  {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "output";
    b.role = "option";
    const selected = o.id === state.selected;
    b.setAttribute("aria-selected", String(selected));
    const [text, tone] = fact(o);
    const vs = views(o);
    const v = selected ? viewOf(o) : null;
    // parts: an assembly's, or a sequence's whose frames are assemblies (STEP files, or a frame lists its parts)
    const tree = o.files.length && (kindOf(o) === "assembly" || (kindOf(o) === "sequence" && (/\.(step|stp)$/i.test(o.files[0].file) || !!o.meta?.frames?.[0]?.parts || !!o.meta?.framesFile)));
    const foldable = vs.length > 1 || tree;
    const open = foldable && selected && !folded.has(o.id);
    b.innerHTML = `<svg class="output-icon" viewBox="0 0 16 16" aria-hidden="true">${iconOf(o)}</svg><span class="output-name"></span><small></small>${foldable ? '<svg class="output-fold" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4l4 4-4 4"/></svg>' : ""}`;
    b.children[1].textContent = shown(group ? o.name.slice(group.length + 1) : o.name);
    if (group) b.classList.add("in-group");
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
    if (!selected) return;
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
        r.title = x.label; // a long file name is cut short in the row
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

/** A motion study's box around every frame ({min, max}), from the boxes of the
 * shown files (loaded: their meshes) moved by each frame's placements. */
function studyBox(o, files, loaded) {
  const boxes = new Map();
  files.forEach((f, i) => {
    const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
    for (const m of loaded[i])
      for (let j = 0; j < m.positions.length; j++) {
        lo[j % 3] = Math.min(lo[j % 3], m.positions[j]);
        hi[j % 3] = Math.max(hi[j % 3], m.positions[j]);
      }
    if (lo[0] <= hi[0]) boxes.set(f.file, [lo, hi]);
  });
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (const fr of frames(o))
    for (const p of fr.parts ?? []) {
      const b = boxes.get(p.file);
      if (!b) continue;
      const t = p.transform == null ? [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0] : placement(p.transform, p.file);
      for (let k = 0; k < 8; k++) {
        const c = [b[k & 1 ? 1 : 0][0], b[k & 2 ? 1 : 0][1], b[k & 4 ? 1 : 0][2]];
        for (let r = 0; r < 3; r++) {
          const v = t[4 * r] * c[0] + t[4 * r + 1] * c[1] + t[4 * r + 2] * c[2] + t[4 * r + 3];
          lo[r] = Math.min(lo[r], v);
          hi[r] = Math.max(hi[r], v);
        }
      }
    }
  return lo[0] <= hi[0] ? { min: lo, max: hi } : null;
}

/** Shows an output (or input) by id; resolves once it is drawn. ``at``: {frame, view} to show (an
 * annotation's: its frame in the 3D view), even when the output is already the one shown. */
function select(id, at) {
  const frame = Number.isInteger(at?.frame) ? at.frame : null;
  if (state.selected === id && state.shown && !at) return Promise.resolve();
  if (at && state.selected === id && state.view == null && (frame == null || frame === state.frame)) return Promise.resolve();
  play(false);
  const g = groupOf(state.outputs.find((x) => x.id === id) ?? { root: "", name: "" });
  if (g) groupOpen.set(g, true); // a link or an annotation opening a member of a folded group unfolds it
  state.wanted = id;
  state.view = null;
  state.frame = frame ?? 0;
  state.pinned = frame != null;
  $("scrub").max = "0";
  remember();
  renderList();
  return show();
}

/** Shows one of the selected entry's views: "3d", one of its files, or "log". */
function selectView(id) {
  play(false);
  state.wanted = state.selected; // the URL names the entry with the view
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
  if (state.frame > 0) u.searchParams.set("frame", String(state.frame + 1));
  else u.searchParams.delete("frame");
  history.replaceState(null, "", u);
}

/** Something a table cell, chart point or document link names: a file of this entry, or another entry. */
function linkable(value) {
  if (typeof value !== "string" || !value || value.length > 200) return false;
  if (ownFile(value)) return true;
  const v = value.replace(/^outputs\//, "").replace(/\/$/, "");
  return state.outputs.some((o) => o.id === v && o.id !== state.selected);
}
/** A file of the shown entry that a value names (its name, or its path in the entry). */
function ownFile(value) {
  const o = state.outputs.find((x) => x.id === state.selected);
  if (!o || !value.includes(".")) return null;
  const v = value.replace(/^\.\//, "");
  return [...o.docs, ...o.files].find((f) => f.file === v || f.file.split("/").pop() === v) ?? null;
}
function open(value) {
  const o = state.outputs.find((x) => x.id === state.selected);
  if (o && views(o).some((x) => x.id === value)) return selectView(value);
  const f = ownFile(String(value));
  if (f) return views(o).some((x) => x.id === f.file) ? selectView(f.file) : download(f); // a file to print: download it
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

const WHOLE = 1 << 30; // read the whole file: the agent refuses one larger than it can send rather than cut it short
let pageKey = null; // what the page shows (entry, view, content)
let pageRun = null; // the render in flight or done; a newer one replaces it
let pageCleanup = null;
const reads = new Map(); // hash -> Promise<ArrayBuffer>: a small file is read from the machine once
function readFile(f) {
  if (!reads.has(f.hash)) {
    const p = req("read", { path: f.path, max: WHOLE }, 300_000).then((buf) => {
      if (buf.byteLength > 8 << 20) reads.delete(f.hash); // a big one each time it is shown
      return buf;
    });
    p.catch(() => reads.delete(f.hash));
    if (reads.size > 64) reads.delete(reads.keys().next().value);
    reads.set(f.hash, p);
  }
  return reads.get(f.hash);
}
/** Leaves the page: its chart views and object URLs go with it. */
function closePage() {
  $("stage").classList.remove("paged");
  pageKey = pageRun = null;
  pageCleanup?.();
  pageCleanup = null;
  $("page").textContent = "";
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
  if (key === pageKey) return;
  offerDownload(v?.f ? [v.f, ...extras(o).filter((f) => f !== v.f)] : extras(o));
  pageKey = key;
  setStatus(""); // a 3D view's "Loading…" or error is not this page's
  const run = (pageRun = {});
  pageCleanup?.();
  pageCleanup = null;
  const page = $("page");
  page.textContent = ""; // not the last view's content while this one loads
  page.scrollTop = 0;
  const body = document.createElement("div");
  body.className = "page-body";
  if (!v) {
    body.append(Object.assign(document.createElement("p"), { className: "page-empty", textContent: o.build?.state === "stale" && o.build.why === "never built" ? `Not built yet: tule build ${o.name}` : o.build ? "Nothing to show yet." : "Empty." }));
  } else if (v.type === "log") {
    // the error and the script line first, in plain words; the traceback folded under it (without its ^^^^ marker lines)
    const log = o.build.log.split("\n").filter((l) => !/^\s*[~^]+\s*$/.test(l)).join("\n");
    const at = [...log.matchAll(/File "(?:\/workspace\/)?((?:src|lib)\/[^"]+)", line (\d+)/g)].pop();
    const error = o.build.error ?? lastLine(log);
    if (error) body.append(Object.assign(document.createElement("p"), { className: "page-error", textContent: error + (at ? `  (${at[1]}, line ${at[2]})` : "") }));
    const more = document.createElement("details");
    more.open = !error;
    more.append(Object.assign(document.createElement("summary"), { textContent: "Build log" }), Object.assign(document.createElement("pre"), { className: "page-text", textContent: log }));
    body.append(more);
  } else {
    setStatus("Loading…");
    const cleanup = await renderFile(body, v.f, {
      read: readFile,
      files: o.docs,
      link: linkable,
      open,
      download,
    });
    if (pageRun !== run) return cleanup();
    setStatus("");
    pageCleanup = cleanup;
  }
  page.replaceChildren(body);
}

async function show() {
  const o = state.outputs.find((x) => x.id === state.selected);
  if (!o) {
    closePage();
    clearStage();
    state.shown = null;
    offerDownload([]);
    return;
  }
  const v = viewOf(o);
  if (v?.type !== "geometry") return showPage(o, v);
  if (state.shown === "page") state.shown = null;
  closePage();
  const kind = kindOf(o);
  $("caption").hidden = kind === "overlay" || !o.meta?.title;
  $("caption").textContent = o.meta?.title ?? "";
  let shownFiles = o.files;
  let frame = null;
  let placed = null; // a motion frame's parts: [{file, name, color, transform}]
  const seq = $("sequence");
  seq.hidden = kind !== "sequence";
  if (kind === "sequence") {
    const fr = frames(o);
    // a running sequence: stay on the newest frame while it grows; a finished one opens on its first
    const following = o.meta?.status === "running" && !state.pinned && state.frame >= +$("scrub").max;
    $("scrub").max = String(Math.max(0, fr.length - 1));
    if (following) state.frame = fr.length - 1;
    if (fr.length) state.frame = Math.max(0, Math.min(state.frame, fr.length - 1)); // none yet: a long study's frames are still loading, keep the link's
    $("scrub").value = String(state.frame);
    const cur = fr[state.frame];
    plot.set(fr, state.frame);
    // the plot's legend names the frame and its values; without values, the label goes here
    $("frame").textContent = !fr.length && framesLoading(o) ? "Loading frames…" : `${state.frame + 1} / ${fr.length}${$("plot").hidden && cur?.label ? ` · ${cur.label}` : ""}`;
    $("live").hidden = o.meta?.status !== "running";
    placed = cur?.parts ?? null;
    shownFiles = placed ? placed.map((p) => p.f) : cur ? [cur.f] : [];
    frame = state.frame;
  } else if (kind === "overlay" || kind === "part") {
    const f = o.files.find((x) => x.file === o.meta?.file) ?? o.files[0];
    shownFiles = f ? [f] : [];
  }
  // an assembly's parts: output.json's list when it has one (a file may be placed more than once)
  const entries = placed ?? (kind === "assembly" && Array.isArray(o.meta?.parts) ? o.meta.parts.filter((p) => o.files.some((f) => f.file === p?.file)) : null);
  if (entries?.length && !placed) shownFiles = entries.map((p) => o.files.find((f) => f.file === p.file));
  // what is drawn: the files and, but for a growing sequence, output.json (overlay faces, placements)
  const key = o.id + "|" + [...shownFiles, ...o.fields].map((f) => f.hash).join(",") + (kind === "sequence" ? (placed ? "|" + JSON.stringify(placed.map((p) => p.transform ?? null)) : "") : "|" + JSON.stringify(o.meta ?? null));
  // a part's or assembly's folder may hold more than the model (an STL to print, a 3MF): offer them all, the shown ones first
  const offerFiles = () => offerDownload(kind === "sequence" ? [...shownFiles, ...extras(o)] : [...shownFiles, ...o.files.filter((f) => !shownFiles.includes(f) && f.file !== "output.json"), ...extras(o)]);
  if (key === state.shown) return offerFiles(); // the drawing is as it was; the folder's other files may not be
  // scrubbing a motion study onto a frame with the same files as the drawn one: only placements differ, so the
  // drawn parts move (no rebuild). The key is "id|file hashes|placements"; playback's blend handles its own frames.
  const same = (a, b) => a.slice(0, a.lastIndexOf("|")) === b.slice(0, b.lastIndexOf("|"));
  if (placed && viewer && !viewer.blend && state.shown && state.drawn === state.shown && same(state.shown, key)) {
    try {
      const at = placed.map((p) => (p.transform != null ? placement(p.transform, p.file) : null));
      viewer.placeParts((m) => at[m.entry]);
      for (const m of state.meshes) m.matrix = at[m.entry];
      state.shown = state.drawn = key;
      state.record = { ...state.record, frame };
      return offerFiles();
    } catch {
      // a bad placement: the full path below says so
    }
  }
  const first = !state.shown || !state.shown.startsWith(o.id + "|");
  state.shown = key;
  if (first) annotate?.reset();
  setStatus(meshes.size === 0 || !shownFiles.every((f) => meshes.has(f.hash)) ? "Loading…" : "");
  try {
    // many files to mesh: count them in, with their size, so a big assembly does not look stuck
    const todo = [...new Map(shownFiles.filter((f) => !meshes.has(f.hash)).map((f) => [f.hash, f])).values()];
    let done = 0;
    const mb = todo.reduce((n, f) => n + (f.size ?? 0), 0) / 2 ** 20;
    const progress = () => state.shown === key && todo.length > 3 && setStatus(`Loading ${done} / ${todo.length} files${mb >= 10 ? ` (${Math.round(mb)} MB)` : ""}…`);
    progress();
    for (const f of todo) request(f).then(() => (done++, progress()), () => {});
    const named = (m, f, i) => {
      const entry = entries?.length ? entries[i] : o.meta?.parts?.find((p) => p.file === f.file);
      const matrix = (kind === "assembly" || placed) && entry?.transform != null ? placement(entry.transform, f.file) : null;
      return { ...m, name: entry?.name ?? (kind === "assembly" ? m.name ?? f.file.replace(/\.[^.]+$/, "") : m.name), color: entry?.color ?? (kind === "part" ? o.meta?.color : null) ?? m.color, matrix, entry: i };
    };
    // a big assembly draws its parts as they come in (a few times a second), not after the last one
    const got = new Array(shownFiles.length);
    let drawn = false;
    let timer = null;
    const drawSoFar = () => {
      timer = null;
      if (state.shown !== key) return;
      viewer?.setMeshes(got.flatMap((ps, i) => (ps ? ps.map((m) => named(m, shownFiles[i], i)) : [])), first && !drawn ? "fresh" : "auto", {});
      drawn = true;
    };
    // an assembly shows the parts it could read and names the rest; a part or frame fails as a whole
    const failed = [];
    const loaded = await Promise.all(shownFiles.map((f, i) => request(f).then((ps) => {
      got[i] = ps;
      if (kind === "assembly" && todo.length > 8 && !timer) timer = setTimeout(drawSoFar, drawn ? 500 : 150);
      return ps;
    }, (e) => {
      if (kind !== "assembly") throw e;
      failed.push(`${f.file}: ${e.message}`);
      return [];
    })));
    clearTimeout(timer);
    if (state.shown !== key) return;
    const all = loaded.flatMap((ps, i) => ps.map((m) => named(m, shownFiles[i], i)));
    const style = parts ? parts.frame(all) : undefined;
    viewer?.setMeshes(all, first && !drawn ? "fresh" : "auto", style, placed ? studyBox(o, shownFiles, loaded) : null);
    const field = (name) => {
      const f = o.fields.find((x) => x.file === name);
      return f ? readFile(f) : Promise.reject(new Error("not in the output"));
    };
    overlay?.apply(o.meta ?? null, kind === "overlay", field);
    $("strip").hidden = $("legend").hidden;
    state.meshes = all;
    state.drawn = key; // drawn completely (a motion study's next frame may only move it)
    state.record = { name: o.name, kind, title: o.meta?.title ?? null, files: shownFiles.map((f) => f.path), frame };
    offerFiles();
    const grid = viewer?.gridStep ? ` · grid ${+viewer.gridStep.toPrecision(3)} mm` : "";
    $("counts").textContent = `${all.length} part${all.length === 1 ? "" : "s"}${grid}`;
    setStatus(failed.length ? `Could not show ${failed.length} of ${shownFiles.length} files: ${failed[0]}` : "");
  } catch (e) {
    if (state.shown === key) {
      state.shown = null;
      if (first) clearStage(); // not the output shown before, as if it were this one
      setStatus(`Cannot show ${o.name}: ${e.message}`);
    }
  }
}

// ------------------------------------------------------------ controls

// Playing: at the sequence's own pace (fps, else 4 a second). A motion study (the same parts in every frame,
// each moved) plays smoothly: the drawn parts move between one frame's placements and the next on every
// screen refresh, and the counter and plot step through the real frames. Paused, it shows only real frames.
let playing = null;
function play(on) {
  if (playing) {
    clearInterval(playing.timer);
    cancelAnimationFrame(playing.raf);
    const blended = playing.blend;
    playing = null;
    if (blended) {
      viewer.movePlacements(null);
      // draw the frame it stopped on as it is: the same output still (the camera and an annotation stay)
      state.shown = state.shown?.startsWith(`${state.selected}|`) ? `${state.selected}|` : null;
      show();
    }
  }
  $("play").classList.toggle("on", !!on);
  if (!on) return;
  state.pinned = false;
  const o = state.outputs.find((x) => x.id === state.selected);
  const fps = Number(o?.meta?.fps); // the sequence's own pace (real time)
  const step = fps > 0 && fps <= 60 ? 1000 / fps : 250;
  const fr = o ? frames(o) : [];
  const blend = smooth(fr);
  if (!blend) {
    playing = {
      timer: setInterval(() => {
        const max = +$("scrub").max;
        state.frame = state.frame >= max ? 0 : state.frame + 1;
        show();
      }, step),
    };
    return;
  }
  // placements per frame, per part: read once
  const at = fr.map((f) => f.parts.map((p) => (p.transform != null ? placement(p.transform, p.file) : null)));
  let pos = state.frame >= fr.length - 1 ? 0 : state.frame;
  let drawnFrame = Math.min(state.frame, fr.length - 1); // whose files are drawn (a frame from a longer sequence: its last)
  let last = null; // the first tick's time (a frame's timestamp may be before now)
  playing = { blend: true };
  const tick = (now) => {
    if (state.selected !== o.id || !state.shown?.startsWith(o.id + "|")) return play(false); // another output, or a page
    // its frame still loading (the output shown before is still drawn): wait for it, so nothing else moves
    if (!drawnHere()) {
      last = now;
      playing.raf = requestAnimationFrame(tick);
      return;
    }
    pos += (now - (last ?? now)) / step;
    last = now;
    if (pos >= fr.length - 1) pos = 0; // loop: back to the first frame
    const i = Math.floor(pos);
    if (i !== state.frame) {
      state.frame = i;
      $("scrub").value = String(i);
      plot.set(fr, i);
      $("frame").textContent = `${i + 1} / ${fr.length}${$("plot").hidden && fr[i]?.label ? ` · ${fr[i].label}` : ""}`;
      // a part made anew in this frame (another file): draw this frame's files
      if (fr[i].parts.some((p, e) => p.file !== fr[drawnFrame].parts[e].file)) {
        drawnFrame = i;
        show();
      }
    }
    // every part glides from this frame's placement to the next one's (one made anew too: its file swaps at frames)
    viewer.movePlacements((m) => [at[i][m.entry], at[i + 1][m.entry]], pos - i);
    playing.raf = requestAnimationFrame(tick);
  };
  playing.raf = requestAnimationFrame(tick);
}

/** Whether a sequence can play smoothly: a motion study (every frame lists its parts, as many each time). A part
 * may be another file from frame to frame (made anew): it moves at real frames only. */
function smooth(fr) {
  return !!viewer && fr.length >= 2 && !!fr[0].parts?.length && fr.every((f) => f.parts?.length === fr[0].parts.length);
}

/** Whether the parts on screen are the shown frame's, drawn completely (not still the output shown before). */
function drawnHere() {
  const drawn = viewer.group.children;
  return state.drawn === state.shown && drawn.length > 0 && drawn.every((part) => Number.isInteger(part.userData.mesh?.entry));
}
$("play").addEventListener("click", () => play(!playing));
$("scrub").addEventListener("input", (e) => {
  play(false);
  state.frame = +e.target.value;
  state.pinned = false; // back at the end, a running sequence is followed again
  remember(); // a link to this frame
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
function closeDownload() {
  $("download-menu").hidden = true;
  $("download").setAttribute("aria-expanded", "false");
}
/** An output's files with no view of their own (.3mf, .zip): offered in Download from every view. */
const extras = (o) => o.docs.filter((f) => f.type === "file" || sheetSvg(o, f));
/** A drawing's sheet as an SVG (bracket.svg, bracket-2.svg beside bracket.pdf): in Download, not a view of its own. */
const sheetSvg = (o, f) => /\.svg$/i.test(f.file) && o.docs.some((d) => d.type === "pdf" && base(d.file) === base(f.file).replace(/-\d+$/, ""));

// a file to print (3MF, STL) first: it is what most people come for
const printable = (f) => (/\.3mf$/i.test(f.file) ? 0 : /\.stl$/i.test(f.file) ? 1 : 2);
/** The Download button offers the shown output's geometry files, or the file shown. */
function offerDownload(list) {
  const next = [...list].sort((a, b) => printable(a) - printable(b));
  const same = next.length === downloads.length && next.every((f, i) => f.path === downloads[i].path && f.hash === downloads[i].hash);
  downloads = next;
  $("download").hidden = !list.length;
  if (!same) closeDownload(); // an open menu stays while its files stay the same
}
async function download(f) {
  closeDownload();
  try {
    save(await req("read", { path: f.path, max: WHOLE }, 300_000), f.file.split("/").pop());
  } catch (e) {
    flash(`Cannot download ${f.file}: ${e.message}`);
  }
}
$("download").addEventListener("click", (e) => {
  e.stopPropagation();
  shareMenu(false);
  if (downloads.length === 1) return download(downloads[0]);
  const menu = $("download-menu");
  menu.textContent = "";
  for (const f of downloads) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.innerHTML = "<span></span><small></small>";
    b.firstChild.textContent = f.file;
    b.title = f.file;
    b.lastChild.textContent = size(f.size);
    b.addEventListener("click", () => download(f));
    menu.append(b);
  }
  menu.hidden = !menu.hidden;
  $("download").setAttribute("aria-expanded", String(!menu.hidden));
});
document.addEventListener("click", closeDownload);

// ------------------------------------------------------------ share: a view-only link (owner)

let shareReady = false;
/** The Share button, for a signed-in owner (not a guest): once per page. */
async function initShare() {
  if (shareReady) return;
  shareReady = true;
  try {
    const me = await (await auth.api("/api/me")).json();
    if (me.user?.guest || me.user?.id === "u_support") return; // the support account's projects are users' work
    renderShare(await (await auth.api(`/api/projects/${project}/share`)).json());
    $("share").hidden = false;
  } catch {
    shareReady = false; // try again on the next connection
  }
}

// Share: one click makes the view-only link and copies it. Once a link exists the button reads "Shared" and
// opens a small menu: copy it, update it (a new link to the project as it is now; the old one stops), stop.
const LINK_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.8 9.2a2.6 2.6 0 0 0 3.7 0l2.1-2.1a2.6 2.6 0 0 0-3.7-3.7l-.8.8M9.2 6.8a2.6 2.6 0 0 0-3.7 0L3.4 8.9a2.6 2.6 0 0 0 3.7 3.7l.8-.8"/></svg>';
let shareState = { shared: false };
let shareFlash = null;
function shareLabel(text) { // the button, or a passing word on it ("Link copied")
  clearTimeout(shareFlash);
  const s = shareState;
  $("share").innerHTML = text ? (s.shared ? LINK_ICON : "") + text : s.shared ? LINK_ICON + "Shared" : "Share";
  $("share").classList.toggle("linked", !!s.shared);
  $("share").title = s.shared ? "A view-only link to this project exists" : "Copy a view-only link to this project as it is now: its outputs, work and inputs, not sources";
  if (text && !text.endsWith("…")) shareFlash = setTimeout(() => shareLabel(), 1600);
}
function shareMenu(open) {
  const menu = $("share-menu");
  const r = $("share").getBoundingClientRect();
  menu.style.top = `${r.bottom + 6}px`;
  menu.style.left = `${Math.max(8, Math.min(r.left, innerWidth - 208))}px`;
  menu.hidden = !open;
  $("share").setAttribute("aria-expanded", String(open));
}
function renderShare(s, err) {
  shareState = s;
  const menu = $("share-menu");
  const p = (text, cls) => Object.assign(document.createElement("p"), { textContent: text, className: cls });
  const item = (text, fn) => Object.assign(document.createElement("button"), { type: "button", textContent: text, onclick: fn });
  const kids = [];
  if (s.shared) {
    const when = new Date(s.created).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    kids.push(
      p(`View only · as of ${when}`, "menu-note"),
      item("Copy link", () => copyShare(s.url)),
      item("Update link", () => share("POST")),
      item("Use this view as preview", () => previewShare()),
      item("Stop sharing", () => share("DELETE")),
    );
  }
  if (err) kids.push(p(err, "menu-error"));
  menu.replaceChildren(...kids);
  if (!kids.length) shareMenu(false);
  shareLabel();
}
/** The link's preview, the picture sites show when the link is posted: the 3D view from its direction, 1200×630.
 * Only a drawn 3D view (not a page or a loading one); false when there is none. */
async function sendPreview() {
  if (!viewer || !state.shown || state.shown === "page" || state.drawn !== state.shown) return false;
  const out = viewer.preview(1200, 630); // from the view's direction, the model filling the picture
  if (!out) return false;
  const png = await new Promise((done) => out.toBlob(done, "image/png"));
  const r = await auth.api(`/api/projects/${project}/share/preview`, { method: "PUT", headers: { "content-type": "image/png" }, body: png });
  return r.ok;
}
async function previewShare() {
  shareMenu(false);
  shareLabel("Saving preview…");
  const ok = await sendPreview().catch(() => false);
  shareLabel(ok ? "Preview saved" : "Open a 3D view first");
}
async function copyShare(url) {
  try {
    await navigator.clipboard.writeText(url);
    shareMenu(false);
    shareLabel("Link copied");
  } catch {
    prompt("The view-only link:", url); // no clipboard here: to copy by hand
  }
}
async function share(method) {
  const before = shareState;
  shareMenu(false);
  if (method === "POST") shareLabel(before.shared ? "Updating…" : "Sharing…");
  const init = method === "POST" ? { method, headers: { "content-type": "application/json" }, body: JSON.stringify({ machine: state.viewing ?? undefined }) } : { method };
  const made = auth.api(`/api/projects/${project}/share`, init).then((r) => r.json());
  // the copy is asked for now, within the click, and gets the link once it is made (a browser may refuse a later one)
  const copied = method === "POST" && window.ClipboardItem
    ? navigator.clipboard.write([new ClipboardItem({ "text/plain": made.then((s) => new Blob([s.url ?? ""], { type: "text/plain" })) })]).then(() => true, () => false)
    : Promise.resolve(false);
  try {
    const s = await made;
    renderShare(s);
    if (method !== "POST" || !s.shared) return;
    sendPreview().catch(() => {}); // the picture a posted link shows; the link works without it
    if (await copied) shareLabel("Link copied");
    else shareMenu(true); // not copied: the menu's Copy link
  } catch (e) {
    renderShare(before, e.message);
    shareMenu(true);
  }
}

$("share").addEventListener("click", (e) => {
  e.stopPropagation();
  closeDownload();
  if (!shareState.shared) return void share("POST");
  shareMenu($("share-menu").hidden);
});
$("share-menu").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => {
  $("share-menu").hidden = true;
  $("share").setAttribute("aria-expanded", "false");
});

connect();
