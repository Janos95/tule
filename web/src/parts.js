// The parts panel: the shown frame's parts (show() names) as a small tree.
// Paths ("gripper/left jaw/finger") make the groups; a root that every
// part shares is the panel's title. Rows: a color swatch and the name; the
// eye hides the part (or group), "only" (or Alt-click) shows just it, and
// hovering or tapping a row highlights it in the view (the other parts turn
// translucent). Unnamed shapes are "shape 1", "shape 2", ...; references
// (show(reference=...)) are one row. Hidden parts are kept by name per
// project for the session: they stay hidden across an animation's frames
// and across runs. They are neither drawn nor fitted, and the agent's
// screenshot shows what the page shows. With one unnamed shape and no
// reference there is no panel (and nothing is hidden).

const EYE = '<svg viewBox="0 0 16 16"><path d="M1.5 8S3.9 3.5 8 3.5 14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>';
const EYE_OFF = '<svg viewBox="0 0 16 16"><path d="M1.5 8S3.9 3.5 8 3.5 14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/><path d="M2.5 13.5l11-11"/></svg>';
const CARET = '<svg viewBox="0 0 16 16"><path d="M6 4l4 4-4 4"/></svg>';

// the keys of a frame's meshes: the part name, "shape <n>" for the n-th
// unnamed shape, null for references
function partKeys(meshes) {
  let unnamed = 0;
  return meshes.map((m) => (m.role === "reference" ? null : (m.name ?? `shape ${++unnamed}`)));
}

// a name picks the parts whose path contains its segments in a row ("left
// jaw" picks "gripper/left jaw/finger"); "reference" picks the references
const segs = (s) => s.split("/").map((x) => x.trim().toLowerCase());
function picks(key, name) {
  if (key === null) return name.trim().toLowerCase() === "reference";
  const k = segs(key);
  const n = segs(name);
  for (let i = 0; i + n.length <= k.length; i++) if (n.every((x, j) => x === k[i + j])) return true;
  return false;
}

/** viewer: the Viewer; project: () => the open project's id. */
export function initParts({ viewer, project }) {
  const $ = (id) => document.getElementById(id);
  const panel = $("parts");
  const byProject = new Map(); // project id -> {parts: Set of hidden keys, reference: hidden}
  const expanded = new Set(); // open groups (ids)
  let shown = []; // the shown frame's meshes
  let keys = []; // and their keys
  let rows = new Map(); // row id -> {id, label, keys: [...], kind: "part" | "group" | "reference"}
  let shape = ""; // the frame's parts and colors: the list is rebuilt when they change
  let offered = false;
  let hover = null; // row ids
  let selected = null;

  const hiddenSet = () => {
    const id = project();
    if (!byProject.has(id)) byProject.set(id, { parts: new Set(), reference: false });
    return byProject.get(id);
  };
  const isHidden = (key) => (key === null ? hiddenSet().reference : hiddenSet().parts.has(key));
  const setHidden = (key, on) => {
    const h = hiddenSet();
    if (key === null) h.reference = on;
    else if (on) h.parts.add(key);
    else h.parts.delete(key);
  };
  const modelKeys = () => [...new Set(keys.filter((k) => k !== null))];
  const anyHidden = () => keys.some((k) => isHidden(k));

  /** The viewer style for the shown frame: {hidden, lit} as mesh indices.
   * hide, only: part names (picks) for an agent's screenshot; lit: false
   * leaves out the highlight. */
  function style({ hide = [], only = null, lit = true } = {}) {
    if (!offered) return { hidden: null, lit: null };
    const hidden = new Set();
    keys.forEach((k, i) => {
      if (only ? !only.some((n) => picks(k, n)) : isHidden(k)) hidden.add(i);
      if (hide.some((n) => picks(k, n))) hidden.add(i);
    });
    const row = lit && rows.get(hover ?? selected);
    let on = null;
    if (row && row.kind !== "reference") {
      on = new Set();
      keys.forEach((k, i) => row.keys.includes(k) && !hidden.has(i) && on.add(i));
      if (!on.size) on = null;
    }
    return { hidden, lit: on };
  }

  // while a part is lit the panel turns see-through: the part may be behind it
  const restyle = () => {
    const st = style();
    viewer.setStyle(st);
    panel.classList.toggle("see-through", !!st.lit);
    paint();
  };

  // ---------------------------------------------------------------- the list

  function build(colors) {
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
      const leaf = { id: `p:${key}`, label: paths[i][paths[i].length - 1], kind: "part", keys: [key], color: colors.get(key), depth };
      rows.set(leaf.id, leaf);
      list.push(leaf);
    });
    if (keys.includes(null)) rows.set("r", { id: "r", label: "Reference", kind: "reference", keys: [null], color: colors.get(null), depth: 0 });
    if (!rows.has(hover)) hover = null;
    if (!rows.has(selected)) selected = null;

    panel.textContent = "";
    const head = document.createElement("div");
    head.className = "parts-head";
    head.innerHTML = '<span class="parts-title"></span><button class="parts-all" type="button">Show all</button>';
    head.firstChild.textContent = root.join(" / ");
    head.lastChild.addEventListener("click", showAll);
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
    if (rows.has("r")) {
      if (top.length) list.appendChild(Object.assign(document.createElement("div"), { className: "parts-sep" }));
      list.appendChild(row(rows.get("r")));
    }
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
        : `<span class="swatch" style="--c: ${node.color}"></span>`;
    el.innerHTML = `${lead}<span class="name"></span>${node.kind === "reference" ? "" : '<button class="only" type="button" title="Show only this (Alt-click)">only</button>'}<button class="eye" type="button"></button>`;
    el.querySelector(".name").textContent = node.label;
    el.title = node.kind === "reference" ? "Reference shapes (show(reference=...)), drawn translucent" : node.keys.length === 1 ? node.keys[0] : `${node.label}: ${node.keys.length} parts`;
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

  // only this row's parts (references keep their state); again: show all.
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
    if (node.kind !== "reference" && (e.altKey || e.target.closest(".only"))) return solo(node);
    if (e.target.closest(".eye")) return toggle(node);
    if (node.kind === "reference") return;
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

  // a frame offers the tree when there is something to choose: several parts, or references
  const offers = (meshes, k) => k.length > 1 || k.some((x) => x === null);

  return {
    /** A new frame (or a replayed one): returns its viewer style. */
    frame(meshes) {
      shown = meshes;
      keys = partKeys(meshes);
      offered = offers(meshes, keys);
      const colors = new Map(keys.map((k, i) => [k, meshes[i].color]));
      const next = offered ? JSON.stringify([...colors]) : "";
      if (next !== shape) {
        shape = next;
        if (offered) build(colors);
        else panel.textContent = "";
      }
      layout();
      if (offered) paint();
      return style();
    },
    style,
    /** A frame's (default: the shown one's) part names, the hidden ones and
     * the number of references. */
    names(meshes = shown) {
      const k = partKeys(meshes);
      const model = [...new Set(k.filter((x) => x !== null))];
      return { parts: model, hidden: offers(meshes, k) ? model.filter((x) => isHidden(x)) : [], references: k.filter((x) => x === null).length };
    },
    /** The names of the parts a style hides ("reference" for references). */
    hiddenNames: (st) => [...new Set(keys.filter((k, i) => st.hidden?.has(i)).map((k) => k ?? "reference"))],
    /** Names (of hide / only) that pick no part of the shown frame. */
    unknown: (names) => names.filter((n) => !keys.some((k) => picks(k, n))),
    /** Indices of the visible model shapes of a frame's meshes, or null when
     * none is hidden (exports). */
    keep(meshes) {
      const k = partKeys(meshes);
      if (!offers(meshes, k)) return null;
      const keep = [];
      k.forEach((key, i) => key !== null && !isHidden(key) && keep.push(i));
      return keep.length === k.filter((x) => x !== null).length ? null : keep;
    },
  };
}
