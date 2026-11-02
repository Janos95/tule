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

export const GEOMETRY = /\.(step|stp|stl)$/i;

/** geometry, chart, table, data (a table when it is a list of records), document, pdf, image, text, or file. */
export function typeOf(file) {
  const f = file.toLowerCase();
  if (GEOMETRY.test(f)) return "geometry";
  if (f.endsWith(".vl.json")) return "chart";
  if (/\.(csv|tsv)$/.test(f)) return "table";
  if (f.endsWith(".json")) return "data";
  if (/\.(md|markdown)$/.test(f)) return "document";
  if (f.endsWith(".pdf")) return "pdf";
  if (/\.(png|jpe?g|gif|webp|svg)$/.test(f)) return "image";
  if (/\.(txt|log|py|toml|ya?ml|ini|cfg|xml|js|ts|sh)$/.test(f)) return "text";
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
  if (type === "table") return table(into, parseCsv(text(await c.read(f)), /\.tsv$/i.test(f.file) ? "\t" : ","), c);
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
  if (type === "pdf") {
    const frame = el("iframe", "page-pdf");
    frame.title = f.file;
    frame.src = await c.url(f);
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

// ---------------------------------------------------------------- tables

/** RFC 4180: quoted fields may hold the separator, quotes ("") and line breaks. */
export function parseCsv(s, sep = ",") {
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
const tidy = (v) => (/\.\d{10,}/.test(v) ? String(Number(Number(v).toPrecision(10))) : v);

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
  const font = getComputedStyle(document.body).fontFamily;
  return {
    background: null,
    font,
    range: { category: [v("--reed"), v("--navy"), v("--stem"), v("--info-fill"), v("--danger-fill"), v("--warn-fill"), v("--faint")] },
    axis: { domainColor: v("--line-strong"), gridColor: v("--line"), tickColor: v("--line-strong"), labelColor: v("--muted"), titleColor: v("--muted"), labelFontSize: 11, titleFontSize: 11.5, titleFontWeight: 500 },
    legend: { labelColor: v("--muted"), titleColor: v("--muted"), labelFontSize: 11, titleFontSize: 11.5, titleFontWeight: 500 },
    title: { color: v("--text"), fontSize: 13, fontWeight: 600, anchor: "start" },
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
