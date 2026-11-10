// The sequence plot: the frames' `values` (output.json) as lines, one per
// key. Keys in one unit (angle_deg, tension (N)), or without one but of a
// size, share a scale, so they compare; other groups get their own (the legend
// gives each key's range; a group shown alone gets its numbers on the axis), so
// differently sized values share one chart without a second axis. A cursor marks the shown frame; hovering shows a
// frame's values in the legend, clicking (or dragging) jumps to it. Clicking a
// legend entry shows that line alone, with its numbers on the axis.

import { colors } from "./colors.js";

const series = (j) => { const s = colors("--series"); return s[j % s.length]; }; // palette.css, fixed order by key
const H = 64;
const PAD = { l: 8, r: 8, t: 6, b: 6 };
const SVG = "http://www.w3.org/2000/svg";

// to four digits of the series' size, so a value that is zero up to rounding reads 0, not 3.1e-8
const fmt = (v, r) => {
  if (!Number.isFinite(v)) return "–";
  const size = r ? Math.max(Math.abs(r.lo), Math.abs(r.hi)) : Math.abs(v);
  const digits = size ? Math.max(0, Math.min(8, 3 - Math.floor(Math.log10(size)))) : 0;
  return String(+v.toFixed(digits));
};

/** el: the plot's container; onPick(frame index). */
export function initPlot(el, onPick) {
  let frames = [];
  let keys = [];
  let current = 0;
  let hover = null;
  let dragging = false;
  let solo = null; // the one key shown alone (a legend click), or null
  let inputs = new Set(); // what drives the motion (a crank angle rising evenly): read out, not drawn
  let paths = new Map(); // "key|width" -> the line's path, until the frames change
  const shown = () => (solo && keys.includes(solo) ? [solo] : keys.filter((k) => !inputs.has(k)));

  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "plot-svg");
  svg.setAttribute("height", String(H));
  const legend = document.createElement("div");
  legend.className = "plot-legend";
  el.append(svg, legend);

  const width = () => Math.max(60, svg.clientWidth || el.clientWidth || 300);
  const x = (i, w) => (frames.length < 2 ? w / 2 : PAD.l + (i / (frames.length - 1)) * (w - PAD.l - PAD.r));
  const frameAt = (px, w) => (frames.length < 2 ? 0 : Math.max(0, Math.min(frames.length - 1, Math.round(((px - PAD.l) / (w - PAD.l - PAD.r)) * (frames.length - 1)))));

  function range(k) {
    const vs = frames.map((f) => f.values?.[k]).filter(Number.isFinite);
    const lo = Math.min(...vs);
    const hi = Math.max(...vs);
    return { lo, hi, span: hi - lo || Math.abs(hi) || 1 };
  }

  // the scale a key is drawn on: its group's (keys of one unit, or unitless ones of a size)
  let groups = new Map();
  function group() {
    groups = new Map();
    const unit = (k) => k.match(/[\s_([]((?:deg|°|rad|mm|cm|m|N|Nm|N·m|N·mm|Nmm|MPa|kPa|Pa|s|ms|g|kg|%|W|A|V|rpm|mm\/s|deg\/s))[)\]]?$/i)?.[1].toLowerCase();
    const size = (k) => { const r = range(k); return Math.max(Math.abs(r.lo), Math.abs(r.hi)) || 1; };
    const byUnit = new Map();
    const loose = [];
    for (const k of keys) {
      const u = unit(k);
      if (u) byUnit.set(u, [...(byUnit.get(u) ?? []), k]);
      else loose.push(k);
    }
    const sets = [...byUnit.values()];
    loose.sort((a, b) => size(a) - size(b));
    for (const k of loose) {
      const last = sets.at(-1);
      if (last?.unitless && size(k) <= 10 * size(last[0])) last.push(k);
      else sets.push(Object.assign([k], { unitless: true }));
    }
    for (const set of sets) {
      const rs = set.map(range);
      const lo = Math.min(...rs.map((r) => r.lo));
      const hi = Math.max(...rs.map((r) => r.hi));
      const r = { lo, hi, span: hi - lo || Math.abs(hi) || 1, keys: set };
      for (const k of set) groups.set(k, r);
    }
  }
  const scale = (k) => groups.get(k) ?? range(k);

  function render() {
    const w = width();
    svg.setAttribute("viewBox", `0 0 ${w} ${H}`);
    svg.textContent = "";
    const add = (tag, attrs) => {
      const e = document.createElementNS(SVG, tag);
      for (const [a, v] of Object.entries(attrs)) e.setAttribute(a, String(v));
      svg.append(e);
      return e;
    };
    add("line", { x1: PAD.l, x2: w - PAD.r, y1: H - PAD.b, y2: H - PAD.b, class: "plot-base" });
    const drawn = new Set(); // a line on top of one drawn before (servos turning together) is dashed, so both show
    shown().forEach((k) => {
      const j = keys.indexOf(k);
      const { lo, span } = scale(k);
      const y = (v) => H - PAD.b - ((v - lo) / span) * (H - PAD.t - PAD.b);
      let d = paths.get(`${k}|${w}`);
      if (d == null) {
        d = "";
        let pen = false;
        frames.forEach((f, i) => {
          const v = f.values?.[k];
          if (!Number.isFinite(v)) return (pen = false);
          d += `${pen ? "L" : "M"}${x(i, w).toFixed(1)},${y(v).toFixed(1)}`;
          pen = true;
        });
        paths.set(`${k}|${w}`, d);
      }
      add("path", { d, class: "plot-line", stroke: series(j), ...(drawn.has(d) ? { "stroke-dasharray": "5 4" } : {}) });
      drawn.add(d);
      const v = frames[current]?.values?.[k];
      if (Number.isFinite(v)) add("circle", { cx: x(current, w), cy: y(v), r: 3.5, fill: series(j), class: "plot-dot" });
    });
    if (shown().every((k) => scale(k) === scale(shown()[0]))) { // one scale: its numbers on the axis
      const r = scale(shown()[0]);
      add("text", { x: PAD.l + 2, y: PAD.t + 9, class: "plot-axis" }).textContent = fmt(r.hi, r);
      add("text", { x: PAD.l + 2, y: H - PAD.b - 3, class: "plot-axis" }).textContent = fmt(r.lo, r);
    }
    add("line", { x1: x(current, w), x2: x(current, w), y1: 0, y2: H, class: "plot-cursor" });
    if (hover !== null && hover !== current) add("line", { x1: x(hover, w), x2: x(hover, w), y1: 0, y2: H, class: "plot-hover" });
    renderLegend();
  }

  function renderLegend() {
    const i = hover ?? current;
    const f = frames[i];
    legend.textContent = "";
    const head = document.createElement("span");
    head.className = "plot-frame";
    head.textContent = f?.label ?? `frame ${i + 1}`;
    legend.append(head);
    for (const k of inputs) {
      const item = document.createElement("span");
      item.className = "plot-frame";
      item.textContent = `${k} ${fmt(f?.values?.[k], range(k))}`;
      legend.append(item);
    }
    keys.filter((k) => !inputs.has(k)).forEach((k) => {
      const j = keys.indexOf(k);
      const r = range(k);
      const item = document.createElement("span");
      const many = keys.length - inputs.size > 1;
      item.className = "plot-key" + (many && solo ? (solo === k ? " solo" : " off") : "");
      if (many) {
        item.title = solo === k ? "Show all" : `Show ${k} alone, with its axis`;
        item.addEventListener("click", () => {
          solo = solo === k ? null : k;
          render();
        });
      }
      item.innerHTML = '<i></i><span class="k"></span> <b></b> <small></small>';
      item.querySelector("i").style.background = series(j);
      item.querySelector(".k").textContent = k;
      item.querySelector("b").textContent = fmt(f?.values?.[k], r);
      item.querySelector("small").textContent = shown().length > 1 ? `(${fmt(r.lo, r)} – ${fmt(r.hi, r)})` : "";
      legend.append(item);
    });
  }

  const pick = (e) => {
    const r = svg.getBoundingClientRect();
    return frameAt(e.clientX - r.left, r.width);
  };
  svg.addEventListener("pointerdown", (e) => {
    dragging = true;
    svg.setPointerCapture(e.pointerId);
    hover = pick(e);
    onPick(hover);
  });
  svg.addEventListener("pointermove", (e) => {
    const i = pick(e);
    if (dragging && i !== current) onPick(i);
    if (i !== hover) {
      hover = i;
      render();
    }
  });
  svg.addEventListener("pointerup", () => (dragging = false));
  svg.addEventListener("pointerleave", () => {
    hover = null;
    render();
  });
  new ResizeObserver(() => frames.length && render()).observe(el);

  return {
    /** The sequence's frames ({label, values}) and the shown one; hidden without values. */
    set(list, shown) {
      // the same frames (a step to another frame): only the cursor, dots and legend change
      const same = list.length === frames.length && list.every((f, i) => f.values === frames[i].values && f.label === frames[i].label);
      frames = list;
      current = shown;
      if (same) {
        if (keys.length) render();
        return;
      }
      paths = new Map();
      keys = [...new Set(list.flatMap((f) => Object.keys(f.values ?? {}).filter((k) => Number.isFinite(f.values[k]))))];
      // a value that only counts the frames (step 0, 1, 2 …) says nothing the slider doesn't
      const counts = (k) => list.every((f, i) => f.values?.[k] === i || f.values?.[k] === i + 1) && new Set(list.map((f, i) => f.values[k] - i)).size === 1;
      // nor does one that never changes (it is in the legend of no frame worth comparing)
      const constant = (k) => new Set(list.map((f) => f.values?.[k])).size === 1 && list.length > 1;
      keys = keys.filter((k) => !counts(k) && !constant(k));
      // a value rising evenly with the frames is the input (crank_deg 0, 5, 10 …): beside other values it is
      // the slider's axis, a diagonal line that says nothing; shown in the legend only
      const even = (k) => {
        const v = list.map((f) => f.values?.[k]);
        const step = (v[v.length - 1] - v[0]) / (v.length - 1);
        return v.length > 2 && step !== 0 && v.every((x, i) => Math.abs(x - v[0] - i * step) <= 1e-6 * Math.abs(step) * v.length);
      };
      inputs = new Set(keys.filter(even));
      if (inputs.size === keys.length) inputs = new Set();
      el.hidden = !keys.length;
      group();
      if (keys.length) render();
    },
  };
}
