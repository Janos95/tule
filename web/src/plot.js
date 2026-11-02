// The sequence plot: the frames' `values` (output.json) as lines, one per
// key, each scaled to its own range (the legend gives the range; one key also
// gets its numbers on the axis), so differently sized values share one chart
// without a second axis. A cursor marks the shown frame; hovering shows a
// frame's values in the legend, clicking (or dragging) jumps to it.

const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]; // fixed order, by key
const H = 64;
const PAD = { l: 8, r: 8, t: 6, b: 6 };
const SVG = "http://www.w3.org/2000/svg";

const fmt = (v) => (Number.isFinite(v) ? +v.toPrecision(4) : "–");

/** el: the plot's container; onPick(frame index). */
export function initPlot(el, onPick) {
  let frames = [];
  let keys = [];
  let current = 0;
  let hover = null;
  let dragging = false;

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
    keys.forEach((k, j) => {
      const { lo, span } = range(k);
      const y = (v) => H - PAD.b - ((v - lo) / span) * (H - PAD.t - PAD.b);
      let d = "";
      let pen = false;
      frames.forEach((f, i) => {
        const v = f.values?.[k];
        if (!Number.isFinite(v)) return (pen = false);
        d += `${pen ? "L" : "M"}${x(i, w).toFixed(1)},${y(v).toFixed(1)}`;
        pen = true;
      });
      add("path", { d, class: "plot-line", stroke: SERIES[j % SERIES.length] });
      const v = frames[current]?.values?.[k];
      if (Number.isFinite(v)) add("circle", { cx: x(current, w), cy: y(v), r: 3.5, fill: SERIES[j % SERIES.length], class: "plot-dot" });
    });
    if (keys.length === 1) {
      const { lo, hi } = range(keys[0]);
      add("text", { x: PAD.l + 2, y: PAD.t + 9, class: "plot-axis" }).textContent = fmt(hi);
      add("text", { x: PAD.l + 2, y: H - PAD.b - 3, class: "plot-axis" }).textContent = fmt(lo);
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
    head.textContent = f?.label ?? `frame ${i}`;
    legend.append(head);
    keys.forEach((k, j) => {
      const { lo, hi } = range(k);
      const item = document.createElement("span");
      item.className = "plot-key";
      item.innerHTML = '<i></i><span class="k"></span> <b></b> <small></small>';
      item.querySelector("i").style.background = SERIES[j % SERIES.length];
      item.querySelector(".k").textContent = k;
      item.querySelector("b").textContent = fmt(f?.values?.[k]);
      item.querySelector("small").textContent = keys.length > 1 ? `(${fmt(lo)} – ${fmt(hi)})` : "";
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
      frames = list;
      current = shown;
      keys = [...new Set(list.flatMap((f) => Object.keys(f.values ?? {}).filter((k) => Number.isFinite(f.values[k]))))];
      // a value that only counts the frames (step 0, 1, 2 …) says nothing the slider doesn't
      const counts = (k) => list.every((f, i) => f.values?.[k] === i || f.values?.[k] === i + 1) && new Set(list.map((f, i) => f.values[k] - i)).size === 1;
      keys = keys.filter((k) => !counts(k));
      el.hidden = !keys.length;
      if (keys.length) render();
    },
  };
}
