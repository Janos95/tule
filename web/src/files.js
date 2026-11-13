// Views of files that are not geometry (DESIGN.md, "Output types"): tables (CSV, a JSON list
// of records), charts (Vega-Lite, *.vl.json), documents (Markdown, PDF, text), images, and a
// download for anything else. Files come from the machine (ctx.read), never the network: a
// chart's data and a document's images are files next to it. A value that names an output
// (a BOM's part column, a chart point's field) links to it (ctx.link / ctx.open).
//
// ctx = {
//   read(file) -> Promise<ArrayBuffer>,   a file of this output ({path, file, hash, size})
//   files: [file],                        the output's files, for relative names
//   link(value) -> bool,                  whether a value names something ctx.open can show
//   open(value),                          show it (another output, or a file of this one)
//   download(file),
// }

export const GEOMETRY = /\.(step|stp|stl|ply|obj)$/i;

/** geometry, chart, table, data (a table when it is a list of records), document, pdf, dxf, image, text, or file. */
export function typeOf(file) {
  const f = file.toLowerCase();
  if (GEOMETRY.test(f)) return "geometry";
  if (f.endsWith(".vl.json")) return "chart";
  if (/\.(csv|tsv)$/.test(f)) return "table";
  if (f.endsWith(".json")) return "data";
  if (/\.(md|markdown)$/.test(f)) return "document";
  if (f.endsWith(".pdf")) return "pdf";
  if (f.endsWith(".dxf")) return "dxf";
  if (/\.(png|jpe?g|gif|webp|svg)$/.test(f)) return "image";
  if (/\.(txt|log|py|toml|ya?ml|ini|cfg|xml|js|ts|sh|ino|h|hpp|c|cpp|scad|gcode|nc|ngc|m|rs|go|java|html|css|tex|bat|ps1)$/.test(f)) return "text";
  return "file";
}

// 16×16 outline icons, the outputs list's style
export const TYPE_ICONS = {
  geometry: '<path d="M8 1.5 14 5v6l-6 3.5L2 11V5zM8 8v6.5M8 8l6-3M8 8 2 5"/>',
  table: '<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M2 6.5h12M2 9.75h12M6.5 6.5V13"/>',
  data: '<rect x="2" y="3" width="12" height="10" rx="1.5"/><path d="M2 6.5h12M2 9.75h12M6.5 6.5V13"/>',
  chart: '<path d="M2.5 13.5h11M4.5 11V8M8 11V4.5M11.5 11V6.5"/>',
  document: '<path d="M4 1.5h5.5L12.5 4.5v10h-8.5zM9.5 1.5v3h3M6 8h4.5M6 10.5h4.5"/>',
  pdf: '<path d="M4 1.5h5.5L12.5 4.5v10h-8.5zM9.5 1.5v3h3M6 8h4.5M6 10.5h4.5"/>',
  text: '<path d="M4 1.5h5.5L12.5 4.5v10h-8.5zM9.5 1.5v3h3M6 8h4.5M6 10.5h3"/>',
  image: '<rect x="2" y="3" width="12" height="10" rx="1.5"/><circle cx="5.75" cy="6.5" r="1.1"/><path d="m2.5 12 3.5-3.5 2.5 2.5 1.75-1.75L13.5 12"/>',
  file: '<path d="M4 1.5h5.5L12.5 4.5v10h-8.5zM9.5 1.5v3h3"/>',
  dxf: '<rect x="2" y="2" width="12" height="12" rx="1.5"/><circle cx="10" cy="6" r="1.8"/><path d="M4.5 11.5h4v-4"/>',
  log: '<path d="M4 1.5h5.5L12.5 4.5v10h-8.5zM9.5 1.5v3h3M6 8h4.5M6 10.5h4.5"/>',
};

const MIME = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", svg: "image/svg+xml", pdf: "application/pdf" };
const MAX_ROWS = 2000;
const text = (buf) => new TextDecoder().decode(buf);
const el = (tag, cls, content) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (content != null) e.textContent = content;
  return e;
};
export const size = (n) => (n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 2 ** 20).toFixed(1)} MiB`);

/** Draws a file into `into` (emptied first). Returns a cleanup function (object URLs, chart views). */
export async function render(into, file, ctx) {
  const urls = [];
  const views = [];
  let done = false; // a chart may still ask for an image after its page is gone
  const c = { ...ctx, url: async (f) => {
    const u = URL.createObjectURL(new Blob([await ctx.read(f)], { type: MIME[f.file.split(".").pop().toLowerCase()] ?? "" }));
    if (done) URL.revokeObjectURL(u);
    else urls.push(u);
    return u;
  }, views };
  const cleanup = () => {
    done = true;
    urls.forEach((u) => URL.revokeObjectURL(u));
    views.forEach((v) => v.finalize());
  };
  into.textContent = "";
  try {
    await draw(into, file, c);
  } catch (e) {
    into.textContent = "";
    into.append(el("p", "page-error", `Cannot show ${file.file}: ${e.message}`));
  }
  return cleanup;
}

/** Text in a pre; a huge file shows its first megabyte so the page stays responsive. */
const MAX_TEXT = 1 << 20;
function plain(into, s) {
  into.append(el("pre", "page-text", s.length > MAX_TEXT ? s.slice(0, MAX_TEXT) : s));
  if (s.length > MAX_TEXT) into.append(el("p", "page-note", "The first megabyte; download the file for the rest."));
}

async function draw(into, f, c) {
  const type = typeOf(f.file);
  if (type === "table") return table(into, parseCsv(text(await c.read(f)), /\.tsv$/i.test(f.file) ? "\t" : undefined), c);
  if (type === "data") {
    const data = JSON.parse(text(await c.read(f)));
    if (records(data)) return table(into, fromRecords(data), c);
    return plain(into, JSON.stringify(data, null, 2));
  }
  if (type === "chart") return chart(into, JSON.parse(text(await c.read(f))), c);
  if (type === "document") return markdown(into, text(await c.read(f)), c);
  if (type === "text") return plain(into, text(await c.read(f)));
  if (type === "image") {
    const img = el("img", "page-image");
    img.alt = f.file;
    img.src = await c.url(f);
    return into.append(img);
  }
  if (type === "dxf") return into.append(dxf(text(await c.read(f))));
  if (type === "pdf") {
    const frame = el("iframe", "page-pdf");
    frame.title = f.file;
    frame.src = (await c.url(f)) + "#view=Fit&navpanes=0"; // a whole sheet at first sight, no thumbnails
    return into.append(frame);
  }
  const card = el("div", "page-file");
  card.append(el("span", null, f.file), el("small", null, size(f.size)));
  const b = el("button", null, "Download");
  b.type = "button";
  b.addEventListener("click", () => c.download(f));
  card.append(b);
  into.append(card);
}

// ---------------------------------------------------------------- DXF

// AutoCAD color index -> palette variable; anything else draws in the text color
const ACI = { 1: "--danger", 2: "--warn-fill", 3: "--ok", 4: "--info-fill", 5: "--info", 6: "--link", 8: "--muted", 9: "--faint" };
const DASHED = { DASHDOT: "9 3 1.5 3", CENTER: "12 3 3 3", DASHED: "6 3", HIDDEN: "4 2", DOT: "1.5 3", PHANTOM: "12 3 3 3 3 3" };

/** A DXF's linework (lines, circles, arcs, polylines with bulges, splines through their
 * points) and texts as an SVG, y up, with its size: a preview of a flat for laser or waterjet
 * cutting. Entities take their own or their layer's color and linetype (a sheet metal flat:
 * CUT solid, BEND_UP / BEND_DOWN dash-dot in green / red); a legend names the layers. */
function dxf(src) {
  const lines = src.split(/\r?\n/);
  const pairs = [];
  for (let i = 0; i + 1 < lines.length; i += 2) pairs.push([+lines[i].trim(), lines[i + 1].trim()]);
  const paths = []; // [points, layer, color]
  const texts = [];
  // the layer table: name -> {color, ltype}
  const layers = new Map();
  for (let i = 0; i < pairs.length; i++) {
    if (pairs[i][0] !== 0 || pairs[i][1] !== "LAYER" || pairs[i + 1]?.[0] !== 2) continue;
    const L = { color: 7, ltype: "CONTINUOUS" };
    let name = "";
    for (let j = i + 1; j < pairs.length && pairs[j][0] !== 0; j++) {
      const [c, v] = pairs[j];
      if (c === 2) name = v;
      else if (c === 62) L.color = Math.abs(+v);
      else if (c === 6) L.ltype = v.toUpperCase();
    }
    layers.set(name, L);
  }
  let lo = [Infinity, Infinity], hi = [-Infinity, -Infinity];
  const see = (x, y) => { lo = [Math.min(lo[0], x), Math.min(lo[1], y)]; hi = [Math.max(hi[0], x), Math.max(hi[1], y)]; };
  const arcPts = (cx, cy, r, a0, a1) => {
    if (a1 <= a0) a1 += 360;
    const n = Math.max(8, Math.ceil((a1 - a0) / 6));
    return Array.from({ length: n + 1 }, (_, k) => {
      const a = ((a0 + ((a1 - a0) * k) / n) * Math.PI) / 180;
      return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
    });
  };
  const bulged = (pts, bulges, closed) => {
    const out = [];
    const m = closed ? pts.length : pts.length - 1;
    for (let k = 0; k < m; k++) {
      const [p, q, b] = [pts[k], pts[(k + 1) % pts.length], bulges[k] || 0];
      out.push(p);
      if (!b) continue;
      const th = 4 * Math.atan(b), d = Math.hypot(q[0] - p[0], q[1] - p[1]), r = d / (2 * Math.sin(th / 2));
      const mx = (p[0] + q[0]) / 2, my = (p[1] + q[1]) / 2, h = r * Math.cos(th / 2);
      const nx = -(q[1] - p[1]) / d, ny = (q[0] - p[0]) / d;
      const cx = mx + nx * h, cy = my + ny * h;
      const a0 = Math.atan2(p[1] - cy, p[0] - cx), steps = Math.max(4, Math.ceil(Math.abs(th) / 0.1));
      for (let j = 1; j < steps; j++) {
        const a = a0 + (th * j) / steps;
        out.push([cx + Math.abs(r) * Math.cos(a), cy + Math.abs(r) * Math.sin(a)]);
      }
    }
    if (!closed) out.push(pts[pts.length - 1]);
    else out.push(pts[0]);
    return out;
  };
  // entities: group code 0 starts one; collect its codes
  let inEntities = false;
  const ents = [];
  for (const [code, v] of pairs) {
    if (code === 2 && v === "ENTITIES") inEntities = true;
    else if (code === 0 && v === "ENDSEC") inEntities = false;
    if (!inEntities) continue;
    if (code === 0) ents.push({ type: v, c: [] });
    else ents.at(-1)?.c.push([code, v]);
  }
  let poly = null; // an old-style POLYLINE collects its VERTEXes
  for (const e of ents) {
    const g = (k) => +(e.c.find(([c]) => c === k)?.[1] ?? 0);
    const s = (k) => e.c.find(([c]) => c === k)?.[1];
    const all = (k) => e.c.filter(([c]) => c === k).map(([, v]) => +v);
    const layer = s(8) ?? "0";
    const own = s(62) === undefined ? 256 : +s(62); // 256: by layer
    const style = { layer, color: own > 0 && own < 256 ? own : undefined, ltype: s(6)?.toUpperCase() };
    const push = (pts) => paths.push(Object.assign(pts, style));
    if (e.type === "LINE") push([[g(10), g(20)], [g(11), g(21)]]);
    else if (e.type === "CIRCLE") push(arcPts(g(10), g(20), g(40), 0, 360));
    else if (e.type === "ARC") push(arcPts(g(10), g(20), g(40), g(50), g(51)));
    else if (e.type === "TEXT" && s(1)) texts.push({ ...style, x: g(10), y: g(20), h: g(40) || 2.5, a: g(50), t: s(1).replace(/%%d/gi, "°").replace(/%%p/gi, "±").replace(/%%c/gi, "⌀") });
    else if (e.type === "LWPOLYLINE") {
      const xs = all(10), ys = all(20), pts = xs.map((x, k) => [x, ys[k]]);
      const bulges = [];
      let k = -1;
      for (const [c, v] of e.c) if (c === 10) k++; else if (c === 42) bulges[k] = +v;
      if (pts.length) push(bulged(pts, bulges, (g(70) & 1) === 1));
    } else if (e.type === "SPLINE") {
      const fx = all(11), fy = all(21);
      const [xs, ys] = fx.length ? [fx, fy] : [all(10), all(20)];
      if (xs.length) push(xs.map((x, k) => [x, ys[k]]));
    } else if (e.type === "POLYLINE") poly = { pts: [], bulges: [], closed: (g(70) & 1) === 1, style };
    else if (e.type === "VERTEX" && poly) { poly.pts.push([g(10), g(20)]); poly.bulges.push(g(42)); }
    else if (e.type === "SEQEND" && poly) {
      if (poly.pts.length) paths.push(Object.assign(bulged(poly.pts, poly.bulges, poly.closed), poly.style));
      poly = null;
    }
  }
  for (const p of paths) for (const [x, y] of p) see(x, y);
  const wrap = el("div", "page-dxf");
  if (!paths.length) {
    wrap.append(el("p", "page-note", "No lines, arcs or polylines to show; download the file."));
    return wrap;
  }
  const w = hi[0] - lo[0] || 1, h = hi[1] - lo[1] || 1, pad = 0.04 * Math.max(w, h);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `${lo[0] - pad} ${-hi[1] - pad} ${w + 2 * pad} ${h + 2 * pad}`);
  // one path per layer, color and linetype; texts after the lines
  const look = (e) => {
    const L = layers.get(e.layer) ?? { color: 7, ltype: "CONTINUOUS" };
    const ltype = e.ltype && e.ltype !== "BYLAYER" ? e.ltype : L.ltype;
    return { color: `var(${ACI[e.color ?? L.color] ?? "--text"})`, dash: DASHED[ltype] ?? "" };
  };
  const groups = new Map();
  for (const p of paths) {
    const { color, dash } = look(p);
    const key = `${p.layer}\u0000${color}\u0000${dash}`;
    if (!groups.has(key)) groups.set(key, { layer: p.layer, color, dash, paths: [] });
    groups.get(key).paths.push(p);
  }
  const num = (v) => +v.toFixed(4);
  for (const gr of groups.values()) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", gr.paths.map((p) => "M" + p.map(([x, y]) => `${num(x)} ${num(-y)}`).join("L")).join(""));
    path.setAttribute("vector-effect", "non-scaling-stroke");
    path.style.stroke = gr.color;
    if (gr.dash) path.setAttribute("stroke-dasharray", gr.dash);
    svg.append(path);
  }
  for (const tx of texts) {
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", num(tx.x));
    t.setAttribute("y", num(-tx.y));
    t.setAttribute("font-size", num(tx.h));
    if (tx.a) t.setAttribute("transform", `rotate(${num(-tx.a)} ${num(tx.x)} ${num(-tx.y)})`);
    t.style.fill = look(tx).color;
    t.textContent = tx.t;
    svg.append(t);
  }
  const at = pairs.findIndex(([c, v]) => c === 9 && v === "$INSUNITS");
  const unit = { 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m" }[at >= 0 ? +pairs[at + 1]?.[1] : 0] ?? "units";
  wrap.append(svg);
  // a legend when there is more than one layer: a sample of each layer's line, its name and count
  const used = [...new Set([...paths, ...texts].map((e) => e.layer))];
  if (used.length > 1) {
    const legend = el("div", "dxf-legend");
    for (const name of used) {
      const n = paths.filter((p) => p.layer === name).length + texts.filter((t) => t.layer === name).length;
      const { color, dash } = look({ layer: name });
      const item = el("span", "dxf-layer");
      const sw = document.createElementNS(ns, "svg");
      sw.setAttribute("viewBox", "0 0 28 6");
      sw.setAttribute("aria-hidden", "true");
      const ln = document.createElementNS(ns, "line");
      Object.entries({ x1: 1, y1: 3, x2: 27, y2: 3 }).forEach(([k, v]) => ln.setAttribute(k, v));
      ln.style.stroke = color;
      if (dash) ln.setAttribute("stroke-dasharray", dash.split(" ").map((x) => x / 2).join(" "));
      sw.append(ln);
      item.append(sw, el("span", null, name), el("small", null, String(n)));
      legend.append(item);
    }
    wrap.append(legend);
  }
  wrap.append(el("p", "page-note", `${+w.toFixed(2)} × ${+h.toFixed(2)} ${unit} · ${paths.length + texts.length} entities`));
  return wrap;
}

// ---------------------------------------------------------------- tables

/** RFC 4180: quoted fields may hold the separator, quotes ("") and line breaks. */
/** The separator of a CSV's header row: comma, or the semicolon (or tab) a European Excel writes. */
function separator(s) {
  const head = s.slice(0, s.search(/\r?\n|$/)).replace(/"[^"]*"/g, "");
  const count = (c) => head.split(c).length - 1;
  return [",", ";", "\t"].reduce((a, b) => (count(b) > count(a) ? b : a));
}

export function parseCsv(s, sep = separator(s)) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (quoted) {
      if (ch === '"' && s[i + 1] === '"') (field += '"'), i++;
      else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"' && field === "") quoted = true;
    else if (ch === sep) row.push(field), (field = "");
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && s[i + 1] === "\n") i++;
      row.push(field), rows.push(row), (row = []), (field = "");
    } else field += ch;
  }
  if (field !== "" || row.length) row.push(field), rows.push(row);
  const [head = [], ...body] = rows.filter((r) => r.length > 1 || r[0] !== "");
  return { head, body };
}

const records = (d) => Array.isArray(d) && d.length > 0 && d.every((r) => r && typeof r === "object" && !Array.isArray(r));

function fromRecords(list) {
  const head = [...new Set(list.flatMap((r) => Object.keys(r)))];
  const cell = (v) => (v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));
  return { head, body: list.map((r) => head.map((k) => cell(r[k]))) };
}

const isNum = (v) => v !== "" && Number.isFinite(Number(v));
// float noise from computed tables (3.999999999999999 → 4); the exact value is in the cell's tooltip
// float noise shortened for reading (0.30000000000000004, 1.017777498e-13); the file keeps every digit
const tidy = (v) => (/\.\d{10,}$/.test(v) ? String(Number(Number(v).toPrecision(10))) : /^-?\d\.\d{6,}e[-+]\d+$/.test(v) ? Number(v).toPrecision(4) : v);

function table(into, { head, body }, c) {
  const numeric = head.map((_, i) => body.some((r) => (r[i] ?? "") !== "") && body.every((r) => (r[i] ?? "") === "" || isNum(r[i])));
  let order = null; // [column, 1 | -1]
  const wrap = el("div", "table-wrap");
  const t = el("table", "data");
  const tr = el("tr");
  head.forEach((h, i) => {
    const th = el("th");
    if (numeric[i]) th.className = "num";
    const b = el("button", null, h);
    b.type = "button";
    b.title = "Sort";
    b.addEventListener("click", () => {
      order = order?.[0] === i ? [i, -order[1]] : [i, 1];
      tr.querySelectorAll("th").forEach((x, j) => (x.dataset.sort = j === i ? (order[1] > 0 ? "up" : "down") : ""));
      rows();
    });
    th.append(b);
    tr.append(th);
  });
  const thead = el("thead");
  thead.append(tr);
  const tbody = el("tbody");
  t.append(thead, tbody);
  wrap.append(t);
  into.append(wrap);
  const note = el("p", "page-note");
  into.append(note);
  function rows() {
    let list = body;
    if (order) {
      const [i, dir] = order;
      list = [...body].sort((a, b) => dir * (numeric[i] ? (Number(a[i]) || 0) - (Number(b[i]) || 0) : String(a[i] ?? "").localeCompare(String(b[i] ?? ""), undefined, { numeric: true })));
    }
    tbody.textContent = "";
    for (const r of list.slice(0, MAX_ROWS)) {
      const row = el("tr");
      head.forEach((_, i) => {
        const v = r[i] ?? "";
        const td = el("td");
        if (numeric[i]) td.className = "num";
        else if (v.length > 40) td.className = "long"; // long text wraps instead of widening the table
        if (c.link(v)) {
          const a = el("button", "link", v);
          a.type = "button";
          a.addEventListener("click", () => c.open(v));
          td.append(a);
        } else if (/^https?:\/\/[^\s]+$/.test(v)) {
          // a web link (a supplier's page): opens in a new tab, shown as its site
          const a = el("a", "link", v.replace(/^https?:\/\/(www\.)?/, "").replace(/^([^/]+)\/.+$/, "$1/…"));
          a.href = v;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          td.className = "";
          td.append(a);
        } else if (td.className === "long") {
          td.append(el("div", "clamp", v)); // three lines at most, so a wide table stays readable; all of it on hover
          td.title = v;
        } else td.textContent = numeric[i] ? tidy(v) : v;
        if (td.textContent !== v) td.title = v;
        row.append(td);
      });
      tbody.append(row);
    }
    note.textContent = body.length > MAX_ROWS ? `${MAX_ROWS.toLocaleString()} of ${body.length.toLocaleString()} rows` : "";
    note.hidden = !note.textContent;
  }
  rows();
}

// ---------------------------------------------------------------- charts

/** Colors and type from the page's palette, so every chart looks like Tule. */
function theme() {
  const css = getComputedStyle(document.documentElement);
  const v = (n) => css.getPropertyValue(n).trim();
  const px = (n) => parseFloat(v(n)); // a text role (palette.css), in pixels
  const font = getComputedStyle(document.body).fontFamily;
  return {
    background: null,
    font,
    range: { category: [v("--reed"), v("--navy"), v("--stem"), v("--info-fill"), v("--danger-fill"), v("--warn-fill"), v("--faint")] },
    axis: { domainColor: v("--line-strong"), gridColor: v("--line"), tickColor: v("--line-strong"), labelColor: v("--muted"), titleColor: v("--muted"), labelFontSize: px("--fs-label"), titleFontSize: px("--fs-small"), titleFontWeight: 500 },
    legend: { labelColor: v("--muted"), titleColor: v("--muted"), labelFontSize: px("--fs-label"), titleFontSize: px("--fs-small"), titleFontWeight: 500 },
    title: { color: v("--text"), fontSize: px("--fs-body"), fontWeight: 600, anchor: "start" },
    view: { stroke: null },
    mark: { color: v("--reed"), tooltip: true },
    bar: { color: v("--reed") },
    line: { color: v("--reed") },
    point: { color: v("--reed") },
  };
}

/** A file next to this one, by the name a spec or document uses ("bom.csv", "./bom.csv"); null otherwise. */
function sibling(c, name) {
  if (typeof name !== "string" || /^[a-z][a-z0-9+.-]*:|^\/|\.\./i.test(name)) return null;
  let n = name.replace(/^\.\//, "");
  try {
    n = decodeURIComponent(n); // Markdown writes "my part.png" as my%20part.png
  } catch {}
  return c.files.find((f) => f.file === n) ?? null;
}

async function chart(into, spec, c) {
  const [vega, vl, { expressionInterpreter }] = await Promise.all([import("vega"), import("vega-lite"), import("vega-interpreter")]);
  const box = el("div", "chart");
  into.append(box);
  const isVega = /\/vega\/v\d/.test(spec.$schema ?? "");
  if (!isVega) {
    spec = { width: "container", height: 320, ...spec };
    spec.config = { ...theme(), ...(spec.config ?? {}) };
  }
  const parsed = vega.parse(isVega ? spec : vl.compile(spec).spec, isVega ? theme() : undefined, { ast: true });
  // data and images come only from files next to the chart
  const loader = vega.loader();
  const refused = []; // vega logs a failed load and draws the chart without it: say so instead
  const only = (uri) => {
    const f = sibling(c, uri);
    if (f) return f;
    refused.push(new Error(`${uri}: a chart reads only files next to it in this output (${c.files.map((x) => x.file).join(", ")})`));
    throw refused.at(-1);
  };
  loader.load = async (uri) => text(await c.read(only(uri)));
  // an image mark's picture; never an href (a page of the viewer's origin): marks link by the names in their data
  loader.sanitize = async (uri, opt) => {
    const f = only(uri);
    if (opt?.context === "href" || typeOf(f.file) !== "image") throw new Error(`${uri}: a chart shows only images next to it`);
    return { href: await c.url(f) };
  };
  const view = new vega.View(parsed, { loader, renderer: "svg", container: box, hover: true, expr: expressionInterpreter });
  c.views.push(view);
  view.addEventListener("click", (_, item) => {
    const hit = Object.values(item?.datum ?? {}).find((v) => c.link(v));
    if (hit !== undefined) c.open(hit);
  });
  await view.runAsync();
  if (refused.length) throw refused[0];
}

// ---------------------------------------------------------------- documents

let md;
async function markdown(into, src, c) {
  if (!md) {
    const { default: MarkdownIt } = await import("markdown-it");
    md = new MarkdownIt({ html: false, linkify: true }); // no HTML: a document is text, links and pictures
  }
  // parsed inert, and no picture keeps its src: nothing is fetched from the network
  const tpl = document.createElement("template");
  tpl.innerHTML = md.render(src);
  const pics = [...tpl.content.querySelectorAll("img")].map((img) => [img, img.getAttribute("src")]);
  pics.forEach(([img]) => img.removeAttribute("src"));
  // a cell of running text keeps a readable width: the table scrolls sideways instead of crushing it
  for (const td of tpl.content.querySelectorAll("td")) if (td.textContent.length > 40) td.classList.add("prose");
  const doc = el("article", "page-doc");
  doc.append(tpl.content);
  into.append(doc);
  // a picture of a file next to it: an image, a chart or table drawn in place, else a link to its view
  for (const [img, name] of pics) {
    const f = sibling(c, name);
    if (!f) {
      img.replaceWith(el("span", "page-missing", `[${img.alt || name}]`));
      continue;
    }
    const type = typeOf(f.file);
    if (type === "image") img.src = await c.url(f);
    else if (type === "chart" || type === "table" || type === "data") {
      const box = el("div", "page-embed");
      img.replaceWith(box);
      try {
        await draw(box, f, c);
      } catch (e) {
        box.replaceChildren(el("p", "page-error", `Cannot show ${f.file}: ${e.message}`));
      }
    } else {
      const b = el("button", "link", img.alt || f.file); // a document is not drawn inside another (or itself)
      b.type = "button";
      b.addEventListener("click", () => c.open(f.file));
      img.replaceWith(b);
    }
  }
  for (const a of doc.querySelectorAll("a[href]")) {
    const href = a.getAttribute("href");
    const f = sibling(c, href);
    const target = f?.file ?? href.replace(/^(\.\.\/)+/, "").replace(/\/$/, "");
    if (/^(https?:|mailto:)/i.test(href)) Object.assign(a, { target: "_blank", rel: "noopener noreferrer" });
    else if (f || c.link(target))
      a.addEventListener("click", (e) => {
        e.preventDefault();
        c.open(target);
      });
    else a.removeAttribute("href");
  }
}
