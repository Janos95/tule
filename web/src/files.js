// The files browser: /workspace on the machine, through req("files") (a
// directory listing) and req("read") (a file's bytes; runtime/files.py).
// Folders open in place; a file opens in the preview over the view (text up
// to PREVIEW bytes, else its size), and any file can be downloaded (up to
// the 30 MiB a request carries).

const PREVIEW = 256 << 10;
const TEXT = /\.(txt|md|py|json|toml|ya?ml|csv|tsv|log|ini|cfg|sh|js|ts|html|css|xml|svg|step|stp|stl|obj|gitignore|tuleignore|ipynb|c|h|cpp|rs|go)$/i;

export const size = (n) => (n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KiB` : `${(n / 2 ** 20).toFixed(1)} MiB`);

/** Saves bytes as a file in the browser's downloads. */
export function save(bytes, name) {
  const url = URL.createObjectURL(new Blob([bytes]));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** ctx: {req(op, args, ms), machine() -> state, notify(text)}. */
export function initFiles(ctx) {
  const $ = (id) => document.getElementById(id);
  const list = $("file-list");
  const crumbs = $("file-path");
  const note = $("files-note");
  let cwd = "";
  let loaded = false;

  async function open(path) {
    if (ctx.machine() !== "running") {
      list.textContent = "";
      crumbs.textContent = "";
      note.hidden = false;
      note.textContent = "The machine is not running: start it (tule up) to browse /workspace. History shows saved snapshots without it.";
      loaded = false;
      return;
    }
    note.hidden = true;
    try {
      const r = await ctx.req("files", { path });
      cwd = r.path;
      loaded = true;
      renderCrumbs();
      list.textContent = "";
      if (cwd) list.append(row({ name: "..", type: "dir", up: true }));
      for (const e of r.entries) list.append(row(e));
      if (r.truncated) {
        note.hidden = false;
        note.textContent = `Showing ${r.entries.length} of ${r.truncated} entries.`;
      }
      if (!r.entries.length) list.append(Object.assign(document.createElement("p"), { className: "files-empty", textContent: "Empty folder" }));
    } catch (e) {
      note.hidden = false;
      note.textContent = `Cannot list /workspace/${path}: ${e.message}`;
    }
  }

  function renderCrumbs() {
    crumbs.textContent = "";
    const parts = cwd ? cwd.split("/") : [];
    const link = (label, path) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "crumb";
      b.textContent = label;
      b.addEventListener("click", () => open(path));
      return b;
    };
    crumbs.append(link("workspace", ""));
    parts.forEach((p, i) => {
      crumbs.append("/");
      crumbs.append(link(p, parts.slice(0, i + 1).join("/")));
    });
  }

  function row(e) {
    const path = e.up ? cwd.split("/").slice(0, -1).join("/") : cwd ? `${cwd}/${e.name}` : e.name;
    const b = document.createElement("button");
    b.type = "button";
    b.className = `file ${e.type}`;
    b.innerHTML = `<span class="file-icon"></span><span class="file-name"></span><small></small>`;
    b.querySelector(".file-name").textContent = e.name + (e.type === "dir" && !e.up ? "/" : "");
    b.querySelector("small").textContent = e.type === "file" ? size(e.size) : e.type === "link" ? "link" : "";
    b.title = e.up ? "Up" : e.mtime ? `${path} · ${new Date(e.mtime).toLocaleString()}` : path;
    b.addEventListener("click", () => (e.type === "dir" ? open(path) : e.type === "file" ? preview(path, e) : null));
    return b;
  }

  async function download(path, name = path.split("/").pop(), bytes = null) {
    try {
      save(bytes ?? (await ctx.req("read", { path, max: 30 << 20 }, 300_000)), name);
    } catch (e) {
      ctx.notify(`Cannot download ${path}: ${e.message}`);
    }
  }

  async function preview(path, e) {
    const sheet = $("preview");
    sheet.hidden = false;
    $("preview-name").textContent = path;
    $("preview-size").textContent = size(e.size);
    const body = $("preview-body");
    body.textContent = "Loading…";
    $("preview-download").onclick = () => download(path);
    try {
      const buf = await ctx.req("read", { path, max: PREVIEW });
      const bytes = new Uint8Array(buf);
      const binary = !TEXT.test(path) && bytes.subarray(0, 8192).includes(0);
      if (binary) return (body.textContent = `A binary file (${size(e.size)}). Download it to open it.`);
      let text = new TextDecoder().decode(bytes);
      if (e.size > PREVIEW) text += `\n\n… the first ${size(PREVIEW)} of ${size(e.size)}; download for the rest.`;
      body.textContent = text;
    } catch (err) {
      body.textContent = `Cannot read ${path}: ${err.message}`;
    }
  }

  $("preview-close").addEventListener("click", () => ($("preview").hidden = true));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("preview").hidden) $("preview").hidden = true;
  });

  return {
    /** The Files tab was opened (or the machine changed): list the current folder. */
    show: () => open(cwd),
    shown: () => loaded,
    download,
  };
}
