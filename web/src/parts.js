// The parts panel: the shown frame's parts (their names) as a small tree.
// Paths ("gripper/left jaw/finger") make the groups (a root that every part
// shares is left out). Rows: the name; the eye hides the part (or group),
// "only" (or Alt-click) shows just it, and hovering or tapping a row
// highlights it in the view (the other parts turn translucent). Unnamed
// shapes are "shape 1", "shape 2", ... Hidden parts are kept by name per
// project for the session: they stay hidden across an animation's frames
// and across runs, and are neither drawn nor fitted. With a single part
// there is no panel (and nothing is hidden).

const EYE = '<svg viewBox="0 0 16 16"><path d="M1.5 8S3.9 3.5 8 3.5 14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 16 16"><path d="M1.5 8S3.9 3.5 8 3.5 14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/><path d="M2.5 13.5l11-11"/></svg>';
const CARET = '<svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg>';

// the keys of a frame's meshes: the part name, "shape <n>" for the n-th unnamed shape
function partKeys(meshes) {
  let unnamed = 0;
  return meshes.map((m) => m.name ?? `shape ${++unnamed}`);
}

/** viewer: the Viewer; project: () => the open project's id. */
export function initParts({ viewer, project }) {
  const $ = (id) => document.getElementById(id);
  const panel = $("parts");
  const byProject = new Map(); // project id -> Set of hidden keys
  const expanded = new Set(); // open groups (ids)
  let keys = []; // the shown frame's keys
  let rows = new Map(); // row id -> {id, label, keys: [...], kind: "part" | "group"}
  let shape = ""; // the frame's parts: the list is rebuilt when they change
  let offered = false;
  let hover = null; // row ids
  let selected = null;

  const hiddenSet = () => {
    const id = project();
    if (!byProject.has(id)) byProject.set(id, new Set());
    return byProject.get(id);
  };
  const isHidden = (key) => hiddenSet().has(key);
  const setHidden = (key, on) => (on ? hiddenSet().add(key) : hiddenSet().delete(key));
  const modelKeys = () => [...new Set(keys)];
  const anyHidden = () => keys.some((k) => isHidden(k));

  /** The viewer style for the shown frame: {hidden, lit} as mesh indices. */
  function style() {
    if (!offered) return { hidden: null, lit: null };
    const hidden = new Set();
    keys.forEach((k, i) => isHidden(k) && hidden.add(i));
    const row = rows.get(hover ?? selected);
    let on = null;
    if (row) {
      on = new Set();
      keys.forEach((k, i) => row.keys.includes(k) && !hidden.has(i) && on.add(i));
      if (!on.size) on = null;
    }
    return { hidden, lit: on };
  }

  const restyle = () => {
    viewer.setStyle(style());
    paint();
  };

  // ---------------------------------------------------------------- the list

  function build() {
    rows = new Map();
    const model = modelKeys();
    let paths = model.map((k) => k.split("/"));
    const root = [];
    while (paths.length && paths.every((p) => p.length > 1 && p[0] === paths[0][0])) {
      root.push(paths[0][0]);
      paths = paths.map((p) => p.slice(1));
    }
    const top = [];
    const groups = new Map();
    model.forEach((key, i) => {
      let list = top;
      let depth = 0;
      for (let j = 0; j < paths[i].length - 1; j++, depth++) {
        const id = `g:${[...root, ...paths[i].slice(0, j + 1)].join("/")}`;
        let g = groups.get(id);
        if (!g) {
          g = { id, label: paths[i][j], kind: "group", keys: [], children: [], depth };
          groups.set(id, g);
          rows.set(id, g);
          list.push(g);
        }
        g.keys.push(key);
        list = g.children;
      }
      const leaf = { id: `p:${key}`, label: paths[i][paths[i].length - 1], kind: "part", keys: [key], depth };
      rows.set(leaf.id, leaf);
      list.push(leaf);
    });
    // numbered siblings fold into one row ("ring pin ×16"), opened like a group
    const fold = (list, parent) => {
      const stem = (n) => (n.kind === "part" ? n.label.replace(/[\s_#-]*\(?\d+\)?$/, "") : null);
      const counts = new Map();
      for (const n of list) if (stem(n) && stem(n) !== n.label) counts.set(stem(n), (counts.get(stem(n)) ?? 0) + 1);
      const made = new Map();
      for (let i = 0; i < list.length; i++) {
        const n = list[i];
        if (n.kind === "group") fold(n.children, n.id);
        const st = stem(n);
        if (!st || st === n.label || counts.get(st) < 3) continue;
        let g = made.get(st);
        if (!g) {
          g = { id: `g:${parent}/${st} ×`, label: `${st} ×${counts.get(st)}`, kind: "group", keys: [], children: [], depth: n.depth };
          made.set(st, g);
          rows.set(g.id, g);
          list.splice(i, 0, g);
          i++;
        }
        list.splice(i--, 1);
        n.depth += 1;
        g.keys.push(...n.keys);
        g.children.push(n);
      }
    };
    fold(top, root.join("/"));
    if (!rows.has(hover)) hover = null;
    if (!rows.has(selected)) selected = null;

    panel.textContent = "";
    const head = document.createElement("div");
    head.className = "parts-head";
    head.innerHTML = '<button class="parts-all" type="button">Show all</button>';
    head.firstChild.addEventListener("click", showAll);
    panel.appendChild(head);
    const list = document.createElement("div");
    list.className = "parts-list";
    const add = (node, into) => {
      into.appendChild(row(node));
      if (node.kind !== "group") return;
      const kids = document.createElement("div");
      kids.setAttribute("role", "group");
      kids.dataset.group = node.id;
      for (const c of node.children) add(c, kids);
      into.appendChild(kids);
    };
    for (const n of top) add(n, list);
    panel.appendChild(list);
  }

  function row(node) {
    const el = document.createElement("div");
    el.className = `part ${node.kind}`;
    el.dataset.id = node.id;
    el.setAttribute("role", "treeitem");
    el.style.setProperty("--depth", node.depth);
    const lead =
      node.kind === "group"
        ? `<button class="caret" type="button" aria-label="Expand">${CARET}</button>`
        : '<span class="lead"></span>'; // names line up under a group's
    el.innerHTML = `${lead}<span class="name"></span><button class="only" type="button" title="Show only this (Alt-click)">only</button><button class="eye" type="button"></button>`;
    el.querySelector(".name").textContent = node.label;
    el.title = node.keys.length === 1 ? node.keys[0] : `${node.label}: ${node.keys.length} parts`;
    return el;
  }

  // row states: hidden, partly hidden (groups), highlighted, expanded; the
  // DOM changes only where a state changed (frames arrive at 30 per second)
  function paint() {
    const some = offered && anyHidden();
    panel.classList.toggle("has-hidden", some);
    for (const el of panel.querySelectorAll(".part")) {
      const node = rows.get(el.dataset.id);
      const n = node.keys.filter((k) => isHidden(k)).length;
      const off = n === node.keys.length;
      el.classList.toggle("partly", n > 0 && !off);
      el.classList.toggle("lit", el.dataset.id === (hover ?? selected));
      el.classList.toggle("selected", el.dataset.id === selected);
      if (el.classList.contains("off") !== off || !el.querySelector(".eye svg")) {
        el.classList.toggle("off", off);
        const eye = el.querySelector(".eye");
        eye.innerHTML = off ? EYE_OFF : EYE;
        eye.title = off ? "Show" : "Hide";
        eye.setAttribute("aria-label", `${off ? "Show" : "Hide"} ${node.label}`);
        eye.setAttribute("aria-pressed", String(!off));
      }
      if (node.kind === "group") {
        const exp = expanded.has(node.id);
        if (el.classList.contains("open") === exp && el.hasAttribute("aria-expanded")) continue;
        el.classList.toggle("open", exp);
        el.setAttribute("aria-expanded", String(exp));
        el.querySelector(".caret").setAttribute("aria-label", exp ? "Collapse" : "Expand");
        panel.querySelector(`[data-group="${CSS.escape(node.id)}"]`).hidden = !exp;
      }
    }
  }

  // ---------------------------------------------------------------- actions

  function toggle(node) {
    const show = node.keys.every((k) => isHidden(k));
    for (const k of node.keys) setHidden(k, !show);
    restyle();
  }

  // only this row's parts; again: show all.
  // Both end a highlight that a click or tap kept.
  function solo(node) {
    const model = modelKeys();
    const already = model.every((k) => isHidden(k) === !node.keys.includes(k));
    for (const k of model) setHidden(k, already ? false : !node.keys.includes(k));
    selected = null;
    restyle();
  }

  function showAll() {
    for (const k of new Set(keys)) setHidden(k, false);
    selected = null;
    restyle();
  }

  // the tree lives in the sidebar under the selected output, shown whenever it has parts
  function layout() {
    panel.hidden = !offered;
  }

  panel.addEventListener("click", (e) => {
    const el = e.target.closest(".part");
    const node = el && rows.get(el.dataset.id);
    if (!node) return;
    if (e.target.closest(".caret")) {
      if (expanded.has(node.id)) expanded.delete(node.id);
      else expanded.add(node.id);
      return paint();
    }
    if (e.altKey || e.target.closest(".only")) return solo(node);
    if (e.target.closest(".eye")) return toggle(node);
    selected = selected === node.id ? null : node.id;
    restyle();
  });
  // a mouse highlights the row under it; touch highlights on tap (selected)
  panel.addEventListener("pointerover", (e) => {
    if (e.pointerType !== "mouse") return;
    const id = e.target.closest(".part")?.dataset.id ?? null;
    if (id !== hover) {
      hover = id;
      restyle();
    }
  });
  panel.addEventListener("pointerleave", () => {
    if (hover === null) return;
    hover = null;
    restyle();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !selected || (e.target.closest && e.target.closest(".cm-editor, dialog"))) return;
    selected = null;
    restyle();
  });

  return {
    /** A new frame (or a replayed one): returns its viewer style. The tree is offered for several parts. */
    frame(meshes) {
      keys = partKeys(meshes);
      offered = keys.length > 1;
      const next = offered ? JSON.stringify(keys) : "";
      if (next !== shape) {
        shape = next;
        if (offered) build();
        else panel.textContent = "";
      }
      layout();
      if (offered) paint();
      return style();
    },
  };
}
