// Overlay outputs (output.json kind "overlay"): findings and boundary
// conditions on a part's B-rep faces, drawn over the part.
//
//   {"version": 1, "kind": "overlay", "file": "part.step", "title": "...",
//    "faces": {"12": {"severity": "error" | "warning" | "info", "label": "..."},
//              "11": {"role": "fixed" | "load" | <any>, "label": "500 N -Z"}},
//    "arrows": [{"at": [x, y, z], "dir": [dx, dy, dz], "label": "500 N"}]}
//
// Listed faces take the color of their severity (red, amber, blue) or role
// (fixed: blue hatch, load: orange, other roles: the next series color); the
// rest of the part is greyed. Arrows are 3D glyphs whose tip touches `at`.
// The legend lists the findings and conditions (hover a row: only its faces
// stay colored); hovering a face or arrow in the view shows its label.
// Extra fields are ignored.

const SEVERITY = { error: "#d93d3d", warning: "#e39a12", info: "#2a78d6" };
const ROLES = { fixed: { color: "#2f6fb3", hatch: true }, load: { color: "#eb6834" } };
const MORE = ["#1baf7a", "#e87ba4", "#008300", "#8a5cd0"]; // further roles, in order of appearance
const ARROW = "#eb6834";
const RANK = { error: 0, warning: 1, info: 2 };

export function initOverlay({ viewer, legend, marks }) {
  let rows = []; // [{kind, label, faces: [ids], color, hatch, severity}]
  let arrows = []; // [{at, dir, label}]
  let byFace = new Map(); // face id -> row
  let chips = [];
  let collapsed = matchMedia("(max-width: 640px)").matches; // phones: the view first

  function parse(meta) {
    const roles = new Map();
    const groups = new Map();
    for (const [id, f] of Object.entries(meta.faces ?? {})) {
      if (!f || typeof f !== "object" || !/^\d+$/.test(id)) continue;
      const sev = SEVERITY[f.severity] ? f.severity : null;
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
    arrows = (Array.isArray(meta.arrows) ? meta.arrows : []).filter(
      (a) => a && [a.at, a.dir].every((v) => Array.isArray(v) && v.length === 3 && v.every(Number.isFinite)),
    );
    byFace = new Map(rows.flatMap((r) => r.faces.map((f) => [f, r])));
  }

  function paint(only = null) {
    const shown = only ? [only] : rows;
    viewer.setFaces("overlay", shown.map((r) => ({ part: null, faces: r.faces, color: r.color, hatch: r.hatch })));
  }

  function render(meta) {
    legend.textContent = "";
    if (!rows.length && !arrows.length) return (legend.hidden = true);
    const count = (s) => rows.filter((r) => r.severity === s).reduce((n, r) => n + r.faces.length, 0);
    const sums = ["error", "warning", "info"].map((s) => [count(s), s]).filter(([n]) => n).map(([n, s]) => `${n} ${s}${n === 1 || s === "info" ? "" : "s"}`);
    const conds = rows.filter((r) => !r.severity).length + arrows.length;
    if (conds) sums.push(`${conds} condition${conds === 1 ? "" : "s"}`);
    const head = el("div", "legend-head");
    const title = el("button", "legend-title");
    title.type = "button";
    title.title = "Show or hide the list";
    title.append(el("b", "", meta.title || "Overlay"), el("small", "", sums.join(" · ")));
    title.addEventListener("click", () => {
      collapsed = !collapsed;
      list.hidden = collapsed;
      legend.classList.toggle("collapsed", collapsed);
    });
    head.append(title);
    const list = el("div", "legend-list");
    list.hidden = collapsed;
    legend.classList.toggle("collapsed", collapsed);
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
    legend.append(head, list);
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
    /** Shows an overlay output's meta over the shown meshes (null clears it). */
    apply(meta) {
      parse(meta ?? {});
      viewer.setDim(!!meta);
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
      if (meta) render(meta);
      else legend.hidden = true;
    },
    /** Hover text for a pick: the face's finding or condition, or the arrow's label. */
    describe(hit) {
      if (hit.arrow !== undefined) return [`load arrow${arrows[hit.arrow]?.label ? ": " + arrows[hit.arrow].label : ""}`];
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
