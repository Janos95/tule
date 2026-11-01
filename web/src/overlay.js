// What output.json says about the shown part, in the strip under the view:
//
//   "info" (any kind): facts about the part, shown as rows
//     {"info": {"material": "aluminium 6061", "mass": "20.8 g", "volume": 7711.3}}
//
//   kind "overlay": data on the part's B-rep faces (ids as in the viewer: f12)
//     {"version": 1, "kind": "overlay", "file": "part.step", "title": "...",
//      "faces": {"12": {"severity": "error" | "warning" | "info", "label": "..."},
//                "11": {"role": "fixed" | "load" | <any>, "label": "500 N -Z"},
//                "7":  {"value": 1.8, "label": "1.8 mm"}},
//      "scale": {"label": "wall thickness", "unit": "mm", "min": 0, "max": 4},
//      "arrows": [{"at": [x, y, z], "dir": [dx, dy, dz], "label": "500 N"}]}
//
// A face with a severity takes its color (red, amber, blue); with a role, the
// role's (fixed: blue hatch, load: orange, others: the next series color);
// with only a number `value`, a color on the scale (blue low, red high; min and
// max from "scale" or from the values). The rest of the part is greyed. Arrows
// are 3D glyphs whose tip touches `at`. The strip lists it all (hover a row:
// only its faces stay colored); hovering a face or arrow shows its label.
// Extra fields are ignored.

const SEVERITY = { error: "#d93d3d", warning: "#e39a12", info: "#2a78d6" };
const ROLES = { fixed: { color: "#2f6fb3", hatch: true }, load: { color: "#eb6834" } };
const MORE = ["#1baf7a", "#e87ba4", "#008300", "#8a5cd0"]; // further roles, in order of appearance
const ARROW = "#eb6834";
const RANK = { error: 0, warning: 1, info: 2 };
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

const fmt = (v) => (typeof v === "number" ? +v.toPrecision(4) : String(v));

export function initOverlay({ viewer, legend, marks }) {
  let rows = []; // [{kind, label, faces: [ids], color, hatch, severity}]
  let values = []; // [{face, value, label, color}]
  let scale = null; // {label, unit, min, max}
  let arrows = []; // [{at, dir, label}]
  let byFace = new Map(); // face id -> row
  let chips = [];

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
      if (role && !roles.has(role)) roles.set(role, ROLES[role] ?? { color: MORE[roles.size % MORE.length] });
      const label = String(f.label ?? "");
      const key = `${sev ?? "role:" + role}|${label}`;
      if (!groups.has(key)) {
        const style = sev ? { color: SEVERITY[sev] } : roles.get(role);
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
  }

  function paint(only = null) {
    const shown = only ? [only] : rows;
    const valueFaces = only ? [] : values.map((v) => ({ part: null, faces: [v.face], color: v.color }));
    viewer.setFaces("overlay", [...valueFaces, ...shown.map((r) => ({ part: null, faces: r.faces, color: r.color, hatch: r.hatch }))]);
  }

  function render(meta, isOverlay) {
    legend.textContent = "";
    const info = meta.info && typeof meta.info === "object" && !Array.isArray(meta.info) ? Object.entries(meta.info) : [];
    const findings = isOverlay && (rows.length || arrows.length || values.length);
    if (!info.length && !findings) return (legend.hidden = true);
    legend.append(el("div", "legend-head", meta.title || ""));
    if (info.length) {
      const grid = el("dl", "legend-info");
      for (const [k, v] of info) {
        const item = el("div", "legend-fact");
        item.append(el("dt", "", k), el("dd", "", v !== null && typeof v === "object" ? JSON.stringify(v) : fmt(v)));
        grid.append(item);
      }
      legend.append(grid);
    }
    if (isOverlay && scale) {
      const bar = el("div", "legend-scale");
      const unit = scale.unit ? ` ${scale.unit}` : "";
      bar.append(
        el("span", "legend-scale-label", scale.label),
        el("small", "", `${fmt(scale.min)}${unit}`),
        Object.assign(el("span", "legend-ramp"), { style: `background: linear-gradient(90deg, ${RAMP.join(", ")})` }),
        el("small", "", `${fmt(scale.max)}${unit}`),
      );
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
        sw.style.setProperty("--c", ARROW);
        row.append(sw, el("span", "legend-kind role", "arrow"), el("span", "legend-label", a.label || "—"), el("small", "legend-faces", a.at.map((v) => +v.toPrecision(4)).join(", ")));
        list.append(row);
      });
      legend.append(list);
    }
    legend.hidden = false;
  }

  // the arrows' labels, at their tails, follow the view
  function place() {
    chips.forEach((c, i) => {
      const tail = viewer.arrowTail(i);
      const p = tail && viewer.project(tail);
      c.hidden = !p?.front;
      if (p) c.style.transform = `translate(${p.x}px, ${p.y}px) translate(-50%, -130%)`;
    });
  }
  viewer.listeners.add(place);

  return {
    /** Shows what an output's meta says about the shown meshes: info for any kind, face data
     * and arrows for an overlay (`isOverlay`). null clears it. */
    apply(meta, isOverlay = false) {
      parse(isOverlay && meta ? meta : {});
      viewer.setDim(isOverlay && !!meta);
      paint();
      viewer.setArrows(arrows.map((a) => ({ at: a.at, dir: a.dir, color: ARROW })));
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
      if (hit.arrow !== undefined) return [`load arrow${arrows[hit.arrow]?.label ? ": " + arrows[hit.arrow].label : ""}`];
      const v = values.find((x) => x.face === hit.face);
      if (v) return [`${scale.label}: ${v.label ?? `${fmt(v.value)}${scale.unit ? " " + scale.unit : ""}`}`];
      const r = byFace.get(hit.face);
      return r ? [`${r.kind}${r.label ? ": " + r.label : ""}`] : [];
    },
  };
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
