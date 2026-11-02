// What output.json says about the shown part, in the strip under the view:
//
//   "info" (any kind): facts about the part, shown as rows
//     {"info": {"material": "aluminium 6061", "mass": "20.8 g", "volume": 7711.3}}
//
//   kind "overlay": data on the part's B-rep faces (ids as in the viewer: f12)
//     {"version": 1, "kind": "overlay", "file": "part.step", "title": "...",
//      "faces": {"12": {"severity": "error" | "warning" | "info" | "ok", "label": "..."},
//                "11": {"role": "fixed" | "load" | <any>, "label": "500 N -Z"},
//                "7":  {"value": 1.8, "label": "1.8 mm"}},
//      "scale": {"label": "wall thickness", "unit": "mm", "min": 0, "max": 4},
//      "arrows": [{"at": [x, y, z], "dir": [dx, dy, dz], "label": "500 N"}],
//      "fields": [{"label": "von Mises stress", "unit": "MPa", "file": "von-mises-stress.npy",
//                  "origin": [x, y, z], "step": 0.5, "min": 0, "max": 31, "note": "...",
//                  "deform": 200}]}
//
// A face with a severity takes its color (red, amber, blue); with a role, the
// role's (fixed: blue hatch, load: reed, others: the next series color);
// with only a number `value`, a color on the scale (blue low, red high; min and
// max from "scale" or from the values). The rest of the part is greyed. Arrows
// are 3D glyphs whose tip touches `at`. The strip lists it all (hover a row:
// only its faces stay colored); hovering a face or arrow shows its label.
// A field is a value per node of a grid (a .npy file in the output, float32,
// (nx, ny, nz) or a vector (nx, ny, nz, 3); node (i, j, k) at origin + step·(i, j, k)):
// it colors the surface on the scale instead (trilinear, per pixel; a vector by its
// length), with "deform" the shape moves that many times the vector. Buttons
// in the strip switch fields; hover shows the value under the pointer.
// Extra fields are ignored.

import { color } from "./colors.js";

// severities, the two known roles and arrows mean something: palette.css variables, read when drawn
const SEVERITY = { error: "--danger-fill", warning: "--warn-fill", info: "--info-fill", ok: "--ok-fill" };
const ROLES = { fixed: { color: "--info-fill", hatch: true }, load: { color: "--reed" } };
const ARROW = "--reed";
// further roles, in order of appearance: plain series colors (stem green, purple, teal, pink),
// apart from the status and role colors above
const MORE = ["#4f8a3c", "#8a6bb8", "#2f9a95", "#c8689a"];
const RANK = { error: 0, warning: 1, info: 2, ok: 3 };
// a diverging blue-yellow-red ramp, low to high
const RAMP = ["#3b4cc0", "#6f92f3", "#aac7fd", "#f2e5a0", "#f7a889", "#e26952", "#b40426"];

/** The ramp's color at t in [0, 1]. */
function ramp(t) {
  const x = Math.min(1, Math.max(0, t)) * (RAMP.length - 1);
  const i = Math.min(RAMP.length - 2, Math.floor(x));
  const a = RAMP[i].match(/\w\w/g).map((h) => parseInt(h, 16));
  const b = RAMP[i + 1].match(/\w\w/g).map((h) => parseInt(h, 16));
  return "#" + a.map((v, k) => Math.round(v + (b[k] - v) * (x - i)).toString(16).padStart(2, "0")).join("");
}

/** The ramp as sRGB numbers in 0..1, for the field shader. */
const RAMP_RGB = RAMP.map((h) => h.match(/\w\w/g).map((x) => parseInt(x, 16) / 255));

const fmt = (v) => (typeof v === "number" ? +v.toPrecision(4) : String(v));

/** A .npy file's float32 array: {shape, data}. */
function parseNpy(buf) {
  const b = new Uint8Array(buf);
  if (b[0] !== 0x93 || String.fromCharCode(...b.subarray(1, 6)) !== "NUMPY") throw new Error("not a .npy file");
  const v2 = b[6] >= 2;
  const len = v2 ? new DataView(buf).getUint32(8, true) : new DataView(buf).getUint16(8, true);
  const start = (v2 ? 12 : 10) + len;
  const head = new TextDecoder().decode(b.subarray(v2 ? 12 : 10, start));
  if (!/'descr':\s*'<f4'/.test(head) || /'fortran_order':\s*True/.test(head)) throw new Error("a field must be little-endian float32 in C order");
  const shape = (head.match(/'shape':\s*\(([^)]*)\)/)?.[1] ?? "").split(",").filter((s) => s.trim()).map(Number);
  const n = shape.reduce((a, x) => a * x, 1);
  return { shape, data: new Float32Array(buf.slice(start, start + 4 * n)) };
}

/** Trilinear interpolation of a node grid at points (xyz, clamped to the grid), as `tule screenshot` does:
 * {values: per point (a vector's length), vectors: Float32Array (xyz per point) | null}. */
function sampleGrid({ shape, data }, origin, step, points) {
  const [nx, ny, nz] = shape;
  const C = shape.length === 4 ? 3 : 1;
  const n = points.length / 3;
  const values = new Float32Array(n);
  const vectors = C === 3 ? new Float32Array(3 * n) : null;
  const N = [nx, ny, nz];
  const b = [0, 0, 0], t = [0, 0, 0], acc = [0, 0, 0];
  for (let p = 0; p < n; p++) {
    for (let d = 0; d < 3; d++) {
      const q = Math.min(N[d] - 1, Math.max(0, (points[3 * p + d] - origin[d]) / step));
      b[d] = Math.min(Math.floor(q), Math.max(N[d] - 2, 0));
      t[d] = q - b[d];
    }
    acc[0] = acc[1] = acc[2] = 0;
    for (let c = 0; c < 8; c++) {
      const i = b[0] + (c >> 2), j = b[1] + ((c >> 1) & 1), k = b[2] + (c & 1);
      const w = (c >> 2 ? t[0] : 1 - t[0]) * ((c >> 1) & 1 ? t[1] : 1 - t[1]) * (c & 1 ? t[2] : 1 - t[2]);
      const at = ((Math.min(i, nx - 1) * ny + Math.min(j, ny - 1)) * nz + Math.min(k, nz - 1)) * C;
      for (let e = 0; e < C; e++) acc[e] += w * data[at + e];
    }
    if (vectors) {
      vectors.set(acc, 3 * p);
      values[p] = Math.hypot(acc[0], acc[1], acc[2]);
    } else values[p] = acc[0];
  }
  return { values, vectors };
}

/** A grid's value per node (a vector's length). */
function sampleNodes({ shape, data }) {
  if (shape.length === 3) return data;
  const out = new Float32Array(data.length / 3);
  for (let i = 0; i < out.length; i++) out[i] = Math.hypot(data[3 * i], data[3 * i + 1], data[3 * i + 2]);
  return out;
}

export function initOverlay({ viewer, legend, marks }) {
  let rows = []; // [{kind, label, faces: [ids], color, hatch, severity}]
  let values = []; // [{face, value, label, color}]
  let scale = null; // {label, unit, min, max}
  let arrows = []; // [{at, dir, label}]
  let byFace = new Map(); // face id -> row
  let chips = [];
  let fields = []; // [{label, unit, file, origin, step, min, max, note, deform}]
  let active = 0; // the field shown
  let at = null; // the shown field's value at a point [x, y, z]
  let fieldError = "";
  let read = null; // file name in the output -> Promise<ArrayBuffer>
  let grids = new Map(); // file -> Promise<{shape, data}>
  let token = 0; // the field load in flight; a newer one wins
  let fielded = false; // the viewer shows a field
  let current = [null, false]; // the last apply's meta and isOverlay, for redrawing the strip

  function parse(meta) {
    const roles = new Map();
    const groups = new Map();
    values = [];
    for (const [id, f] of Object.entries(meta.faces ?? {})) {
      if (!f || typeof f !== "object" || !/^\d+$/.test(id)) continue;
      const sev = SEVERITY[f.severity] ? f.severity : null;
      if (!sev && f.role == null && Number.isFinite(f.value)) {
        values.push({ face: +id, value: f.value, label: f.label == null ? null : String(f.label) });
        continue;
      }
      const role = sev ? null : String(f.role ?? "note");
      if (role && !roles.has(role)) roles.set(role, ROLES[role] ? { ...ROLES[role], color: color(ROLES[role].color) } : { color: MORE[roles.size % MORE.length] });
      const label = String(f.label ?? "");
      const key = `${sev ?? "role:" + role}|${label}`;
      if (!groups.has(key)) {
        const style = sev ? { color: color(SEVERITY[sev]) } : roles.get(role);
        groups.set(key, { kind: sev ?? role, severity: sev, label, faces: [], ...style });
      }
      groups.get(key).faces.push(+id);
    }
    rows = [...groups.values()].sort((a, b) => (RANK[a.severity] ?? 3) - (RANK[b.severity] ?? 3));
    scale = null;
    if (values.length) {
      const s = meta.scale && typeof meta.scale === "object" ? meta.scale : {};
      const vs = values.map((v) => v.value);
      const min = Number.isFinite(s.min) ? s.min : Math.min(...vs);
      const max = Number.isFinite(s.max) ? s.max : Math.max(...vs);
      scale = { label: s.label ? String(s.label) : "value", unit: s.unit ? String(s.unit) : "", min, max };
      for (const v of values) v.color = ramp(max > min ? (v.value - min) / (max - min) : 0.5);
    }
    arrows = (Array.isArray(meta.arrows) ? meta.arrows : []).filter(
      (a) => a && [a.at, a.dir].every((v) => Array.isArray(v) && v.length === 3 && v.every(Number.isFinite)),
    );
    byFace = new Map(rows.flatMap((r) => r.faces.map((f) => [f, r])));
    const was = fields.map((f) => f.label).join("|");
    fields = (Array.isArray(meta.fields) ? meta.fields : []).filter(
      (f) => f && typeof f.file === "string" && Array.isArray(f.origin) && f.origin.length === 3 && f.origin.every(Number.isFinite) && f.step > 0,
    );
    if (fields.map((f) => f.label).join("|") !== was) active = 0; // the same fields again (a rerun): keep the one shown
  }

  // a field colors the vertices; faces then only light up while their strip row is hovered
  function paint(only = null) {
    const shown = only ? [only] : fields.length ? [] : rows;
    const valueFaces = only || fields.length ? [] : values.map((v) => ({ part: null, faces: [v.face], color: v.color }));
    viewer.setFaces("overlay", [...valueFaces, ...shown.map((r) => ({ part: null, faces: r.faces, color: r.color, hatch: r.hatch }))]);
  }

  /** Colors the parts by the active field (loaded once per apply), on the moved shape if it deforms. */
  async function showField() {
    const run = ++token;
    const f = fields[active];
    at = null;
    fieldError = "";
    if (!f || !read) {
      if (fielded) viewer.setField(null);
      fielded = false;
      return;
    }
    try {
      if (!grids.has(f.file)) grids.set(f.file, read(f.file).then(parseNpy));
      const grid = await grids.get(f.file);
      if (run !== token) return;
      const { shape } = grid;
      if (!(shape.length === 3 || (shape.length === 4 && shape[3] === 3)) || Math.min(...shape.slice(0, 3)) < 2)
        throw new Error(`a field is an (nx, ny, nz) or (nx, ny, nz, 3) grid, not (${shape.join(", ")})`);
      const deform = shape.length === 4 && Number.isFinite(f.deform) ? f.deform : 0;
      const moved = (pts, s) => Float32Array.from(pts, (x, i) => x + deform * s.vectors[i]);
      const lo = Number.isFinite(f.min) ? f.min : 0;
      const hi = Number.isFinite(f.max) ? f.max : 1;
      const node = sampleNodes(grid); // the value at each node (a vector's length)
      const values = Float32Array.from(node, (v) => (hi > lo ? (v - lo) / (hi - lo) : 0.5));
      at = (p) => sampleGrid(grid, f.origin, f.step, p).values[0];
      viewer.setField((mesh) => {
        if (!deform) return {};
        const edges = mesh.edges?.length ? moved(mesh.edges, sampleGrid(grid, f.origin, f.step, mesh.edges)) : null;
        return { positions: moved(mesh.positions, sampleGrid(grid, f.origin, f.step, mesh.positions)), edges };
      }, { values, shape: shape.slice(0, 3), origin: f.origin, step: f.step, ramp: RAMP_RGB });
      fielded = true;
    } catch (e) {
      if (run !== token) return;
      grids.delete(f.file);
      if (fielded) viewer.setField(null);
      fielded = false;
      fieldError = `cannot show ${f.file}: ${e.message}`;
      render(...current);
    }
  }

  function render(meta, isOverlay) {
    legend.textContent = "";
    const info = meta.info && typeof meta.info === "object" && !Array.isArray(meta.info) ? Object.entries(meta.info) : [];
    const findings = isOverlay && (rows.length || arrows.length || values.length || fields.length);
    if (!info.length && !findings) return (legend.hidden = true);
    legend.append(el("div", "legend-head", meta.title || ""));
    if (info.length) {
      const grid = el("dl", "legend-info");
      // a nested value: one row per entry ("motion · status"), lists of plain values joined
      const flat = (k, v, depth) =>
        v !== null && typeof v === "object" && !(Array.isArray(v) && v.every((x) => x === null || typeof x !== "object")) && depth < 2
          ? Object.entries(v).flatMap(([k2, v2]) => flat(`${k} · ${k2}`, v2, depth + 1))
          : [[k, Array.isArray(v) ? v.map(fmt).join(", ") || "none" : v !== null && typeof v === "object" ? JSON.stringify(v) : fmt(v)]];
      for (const [k, v] of info.flatMap(([k, v]) => flat(k, v, 0))) {
        const item = el("div", "legend-fact");
        item.append(el("dt", "", k), el("dd", "", v));
        grid.append(item);
      }
      legend.append(grid);
    }
    const f = isOverlay ? fields[active] : null;
    const s = f ? { label: String(f.label ?? "field"), unit: f.unit ? String(f.unit) : "", min: f.min ?? 0, max: f.max ?? 1 } : isOverlay && scale;
    if (s) {
      const bar = el("div", "legend-scale");
      bar.style.flexWrap = "wrap";
      const unit = s.unit ? ` ${s.unit}` : "";
      if (fields.length > 1) {
        // which field: as `tule screenshot --field`
        const seg = el("div", "seg");
        seg.setAttribute("role", "group");
        seg.setAttribute("aria-label", "Field");
        fields.forEach((x, i) => {
          const b = el("button", "", String(x.label ?? `field ${i + 1}`));
          b.type = "button";
          b.style.cssText = "height: 24px; font-size: 12px; padding: 0 8px";
          b.setAttribute("aria-pressed", String(i === active));
          b.addEventListener("click", () => {
            if (i === active) return;
            active = i;
            render(...current);
            showField();
          });
          seg.append(b);
        });
        bar.append(seg);
      } else bar.append(el("span", "legend-scale-label", s.label));
      bar.append(
        el("small", "", `${fmt(s.min)}${unit}`),
        Object.assign(el("span", "legend-ramp"), { style: `background: linear-gradient(90deg, ${RAMP.join(", ")})` }),
        el("small", "", `${fmt(s.max)}${unit}`),
      );
      const note = f ? [fieldError, f.note, f.deform > 0 ? `shape exaggerated ${fmt(f.deform)} times` : ""].filter(Boolean).join("; ") : "";
      if (note) bar.append(el("small", "", note));
      legend.append(bar);
    }
    if (isOverlay && (rows.length || arrows.length)) {
      const list = el("div", "legend-list");
      for (const r of rows) {
        const row = el("div", "legend-row");
        const sw = el("span", `swatch${r.hatch ? " hatch" : ""}`);
        sw.style.setProperty("--c", r.color);
        row.append(sw, el("span", `legend-kind ${r.severity ?? "role"}`, r.kind), el("span", "legend-label", r.label || "—"), el("small", "legend-faces", r.faces.map((f) => `f${f}`).join(", ")));
        row.title = `${r.kind}: ${r.label} (faces ${r.faces.join(", ")})`;
        row.addEventListener("pointerenter", () => paint(r));
        row.addEventListener("pointerleave", () => paint());
        list.append(row);
      }
      arrows.forEach((a) => {
        const row = el("div", "legend-row");
        const sw = el("span", "swatch arrow");
        sw.style.setProperty("--c", `var(${ARROW})`);
        row.append(sw, el("span", "legend-kind role", "arrow"), el("span", "legend-label", a.label || "—"));
        row.title = `at ${a.at.map((v) => +v.toPrecision(4)).join(", ")}`; // where it points, on hover: not a column of coordinates
        list.append(row);
      });
      legend.append(list);
    }
    legend.hidden = false;
  }

  // the arrows' labels, at their tails, follow the view
  // a label that would cover one placed before it moves up, above it: arrows close together keep readable labels
  function place() {
    const taken = []; // [left, top, right, bottom] of the labels placed so far
    chips.forEach((c, i) => {
      const tail = viewer.arrowTail(i);
      const p = tail && viewer.project(tail);
      c.hidden = !p?.front || !arrows[i]?.label;
      if (!p || c.hidden) return;
      const w = c.offsetWidth, h = c.offsetHeight;
      let top = p.y - 1.3 * h;
      const left = p.x - w / 2;
      for (let k = 0; k < chips.length; k++) {
        const hit = taken.find(([l, t, r, b]) => left < r && left + w > l && top < b && top + h > t);
        if (!hit) break;
        top = hit[1] - h - 2;
      }
      taken.push([left, top, left + w, top + h]);
      c.style.transform = `translate(${left}px, ${top}px)`;
    });
  }
  viewer.listeners.add(place);

  return {
    /** Shows what an output's meta says about the shown meshes: info for any kind, face data,
     * arrows and fields for an overlay (`isOverlay`). null clears it. readFile(name) reads a
     * file of the output (a field's .npy) as an ArrayBuffer. */
    apply(meta, isOverlay = false, readFile = null) {
      parse(isOverlay && meta ? meta : {});
      current = [meta, isOverlay];
      read = readFile;
      grids = new Map();
      fielded = false; // the viewer's meshes are new (setMeshes comes first)
      viewer.setDim(isOverlay && !!meta && !fields.length);
      paint();
      showField();
      viewer.setArrows(arrows.map((a) => ({ at: a.at, dir: a.dir, color: color(ARROW) })));
      for (const c of chips) c.remove();
      chips = arrows.map((a) => {
        const c = el("div", "mark-chip", a.label || "");
        c.hidden = !a.label;
        marks.append(c);
        return c;
      });
      place();
      if (meta) render(meta, isOverlay);
      else legend.hidden = true;
    },
    /** Hover text for a pick: the face's finding, condition or value, or the arrow's label. */
    describe(hit) {
      if (hit.arrow !== undefined) return [arrows[hit.arrow]?.label || "arrow"];
      const r = byFace.get(hit.face);
      const cond = r ? [`${r.kind}${r.label ? ": " + r.label : ""}`] : [];
      const fv = at && hit.rest ? at(hit.rest) : undefined;
      if (fv !== undefined) {
        const f = fields[active];
        return [`${f.label}: ${fmt(fv)}${f.unit ? " " + f.unit : ""}`, ...cond];
      }
      const v = values.find((x) => x.face === hit.face);
      if (v) return [`${scale.label}: ${v.label ?? `${fmt(v.value)}${scale.unit ? " " + scale.unit : ""}`}`];
      return cond;
    },
  };
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
