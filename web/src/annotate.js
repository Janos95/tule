// Annotation mode: the human marks up the view for their AI agent.
//
// Draw: freehand strokes in screen space (a few colors, undo, clear); each
// point remembers what it hit (part, B-rep face, 3D point, or null). While
// there are strokes the camera stays put, since they belong to this view.
// Pick faces: clicks select faces (highlighted); the selection gets a label
// ("fixed", "load 500 N down", free text). A note takes free text.
//
// Save sends req("annotate", {output, camera, viewport, strokes, picks, note,
// png}) to the machine, which writes /workspace/annotations/<id>/
// annotation.png and annotation.json (runtime/annotations.py has the format)
// and tells every viewer. The page then shows the path to tell the agent.
// Earlier annotations (req("annotations")) can be shown again: the camera
// goes back to theirs and their strokes are drawn over the view until it
// moves.

import { color as palette } from "./colors.js";

// pens (named in annotation.json for the agent), the picked-face highlight and pick colors:
// fixed data colors, chosen to stand out on a part, not palette.css roles
const COLORS = [
  { name: "red", hex: "#e5484d" },
  { name: "blue", hex: "#2a78d6" },
  { name: "green", hex: "#1baf7a" },
  { name: "black", hex: "#23344a" }, // the palette's navy (palette.css)
];
const SELECTED = "#9b5de5"; // faces picked but not labeled yet
const PICKS = ["#2a78d6", "#eb6834", "#1baf7a", "#e87ba4", "#008300", "#eda100"];
const WIDTH = 3; // stroke width, CSS pixels

/** ctx: {viewer, req(op, args, ms), machine() -> state, output() -> what is
 * shown (for the record) or null, meshes() -> the shown meshes, select(name)
 * -> Promise (shows an output), on(event fn)}. */
export function initAnnotate(ctx) {
  const { viewer } = ctx;
  const $ = (id) => document.getElementById(id);
  const stage = $("stage");
  const ink = $("ink");
  const panel = $("annotate");
  const g = ink.getContext("2d");

  let open = false;
  let mode = "draw";
  let color = COLORS[0];
  let strokes = []; // {color, hex, width, points: [[x, y]], hits: [{index, part, face, point, tags} | null]}
  let selection = new Set(); // "index:face"
  let picks = []; // {label, color, faces: [{index, face}]}
  let ghost = null; // an earlier annotation drawn over the view: {id, strokes, viewport}
  let drawing = null;
  let wasRunning = false;

  // ------------------------------------------------------------ the ink layer

  function fitInk() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = stage.clientWidth;
    const h = stage.clientHeight;
    if (ink.width !== Math.round(w * dpr) || ink.height !== Math.round(h * dpr)) {
      ink.width = Math.round(w * dpr);
      ink.height = Math.round(h * dpr);
    }
    redraw();
  }
  new ResizeObserver(fitInk).observe(stage);

  function paint(c, list, scale = 1, map = (p) => p) {
    c.lineCap = "round";
    c.lineJoin = "round";
    for (const s of list) {
      c.strokeStyle = s.hex ?? COLORS.find((x) => x.name === s.color)?.hex ?? COLORS[0].hex;
      c.lineWidth = (s.width ?? WIDTH) * scale;
      c.beginPath();
      const pts = s.points.map(map);
      pts.forEach(([x, y], i) => (i ? c.lineTo(x * scale, y * scale) : c.moveTo(x * scale, y * scale)));
      if (pts.length === 1) c.lineTo(pts[0][0] * scale + 0.1, pts[0][1] * scale);
      c.stroke();
    }
  }

  // an earlier annotation's points, from its viewport to this one: the
  // camera's vertical field of view is the same, so the picture scales with
  // the height around the center
  function fromViewport(vp) {
    const w = stage.clientWidth;
    const h = stage.clientHeight;
    const k = h / (vp?.height || h);
    return ([x, y]) => [w / 2 + (x - (vp?.width ?? w) / 2) * k, h / 2 + (y - (vp?.height ?? h) / 2) * k];
  }

  function redraw() {
    const dpr = ink.width / Math.max(1, stage.clientWidth);
    g.clearRect(0, 0, ink.width, ink.height);
    if (ghost) paint(g, ghost.strokes, dpr, fromViewport(ghost.viewport));
    paint(g, strokes, dpr);
  }

  // ------------------------------------------------------------ faces

  function faceStyles() {
    const out = [];
    const add = (faces, c) => {
      const byPart = new Map();
      for (const f of faces) byPart.set(f.index, [...(byPart.get(f.index) ?? []), f.face]);
      for (const [part, ids] of byPart) out.push({ part, faces: ids, color: c });
    };
    for (const p of [...(ghost?.picks ?? []), ...picks]) add(p.faces, p.color);
    add([...selection].map((k) => ({ index: +k.split(":")[0], face: +k.split(":")[1] })), SELECTED);
    return out;
  }

  const restyle = () => viewer.setFaces("annotate", faceStyles());

  function describe({ index, face }) {
    const m = ctx.meshes()[index];
    const tags = m ? Object.entries(m.tags ?? {}).filter(([, ids]) => ids.includes(face)).map(([t]) => t) : [];
    return { part: m?.name ?? `part ${index + 1}`, face, tags };
  }

  // ------------------------------------------------------------ input

  function local(e) {
    const r = stage.getBoundingClientRect();
    return [Math.round((e.clientX - r.left) * 10) / 10, Math.round((e.clientY - r.top) * 10) / 10];
  }

  function hitAt(e) {
    const h = viewer.pick(e.clientX, e.clientY);
    if (!h || h.arrow !== undefined) return null;
    return { index: h.index, ...describe({ index: h.index, face: h.face }), point: h.point.map((v) => +v.toPrecision(6)) };
  }

  ink.addEventListener("pointerdown", (e) => {
    if (!open || mode !== "draw" || e.button !== 0) return;
    e.preventDefault();
    ink.setPointerCapture(e.pointerId);
    hideGhost();
    drawing = { color: color.name, hex: color.hex, width: WIDTH, points: [local(e)], hits: [hitAt(e)] };
    strokes.push(drawing);
    viewer.setLocked(true);
    redraw();
    update();
  });
  ink.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    const p = local(e);
    const last = drawing.points[drawing.points.length - 1];
    if (Math.hypot(p[0] - last[0], p[1] - last[1]) < 3) return;
    drawing.points.push(p);
    drawing.hits.push(hitAt(e));
    redraw();
  });
  const endStroke = () => {
    drawing = null;
  };
  ink.addEventListener("pointerup", endStroke);
  ink.addEventListener("pointercancel", endStroke);

  // pick mode: a click (not a drag, which orbits) toggles the face under it
  let down = null;
  viewer.el.addEventListener("pointerdown", (e) => {
    down = open && mode === "pick" && e.button === 0 ? { x: e.clientX, y: e.clientY, t: performance.now() } : null;
  });
  viewer.el.addEventListener("pointerup", (e) => {
    if (!down || Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5 || performance.now() - down.t > 600) return;
    down = null;
    const h = viewer.pick(e.clientX, e.clientY);
    if (!h || h.arrow !== undefined) return;
    const key = `${h.index}:${h.face}`;
    if (selection.has(key)) selection.delete(key);
    else selection.add(key);
    hideGhost();
    restyle();
    update();
  });

  // ------------------------------------------------------------ the panel

  function setMode(m) {
    mode = m;
    $("annot-draw").setAttribute("aria-pressed", String(m === "draw"));
    $("annot-pick").setAttribute("aria-pressed", String(m === "pick"));
    ink.classList.toggle("drawing", open && m === "draw");
    update();
  }

  function setOpen(on) {
    open = on;
    panel.hidden = !on;
    stage.classList.toggle("annotating", on);
    $("annotate-toggle").classList.toggle("on", on);
    $("annotate-toggle").setAttribute("aria-expanded", String(on));
    ink.classList.toggle("drawing", on && mode === "draw");
    if (on) {
      $("annot-done").hidden = true;
      loadPast();
    } else {
      strokes = [];
      selection.clear();
      picks = [];
      viewer.setLocked(false);
      restyle();
      redraw();
    }
    update();
  }

  function colorButtons() {
    const box = $("annot-colors");
    box.textContent = "";
    for (const c of COLORS) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ink-color";
      b.style.setProperty("--c", c.hex);
      b.title = `Draw in ${c.name}`;
      b.setAttribute("aria-label", c.name);
      b.setAttribute("aria-pressed", String(c === color));
      b.addEventListener("click", () => {
        color = c;
        setMode("draw");
        colorButtons();
      });
      box.append(b);
    }
  }

  function why() {
    if (ctx.machine() !== "running") return "The machine is not running: start it (tule up) to save annotations to /workspace.";
    if (!ctx.output()) return "Nothing is shown to annotate.";
    if (!strokes.length && !picks.length && !selection.size && !$("annot-note").value.trim()) return "Draw, pick faces or write a note first.";
    return "";
  }

  function update() {
    $("annot-undo").disabled = !strokes.length;
    $("annot-clear").disabled = !strokes.length && !selection.size && !picks.length;
    $("annot-colors").hidden = mode !== "draw";
    $("annot-label-form").hidden = !selection.size;
    $("annot-label-add").textContent = `Label ${selection.size} face${selection.size === 1 ? "" : "s"}`;
    $("annot-hint").textContent =
      mode === "draw"
        ? strokes.length
          ? "The view stays put while there are strokes (Clear to move it)."
          : "Drag on the view to draw. Turn the view first in Pick faces."
        : selection.size
          ? "Give the selected faces a label, or click more faces."
          : "Click faces to select them; drag to turn the view.";
    const reason = why();
    $("annot-save").disabled = !!reason;
    $("annot-why").textContent = reason && ctx.machine() !== "running" ? reason : "";
    $("annot-save").title = reason || "Save to /workspace/annotations";
    // picks
    const box = $("annot-picks");
    box.textContent = "";
    picks.forEach((p, i) => {
      const row = document.createElement("div");
      row.className = "annot-pick";
      row.innerHTML = '<span class="swatch"></span><span class="annot-pick-label"></span><small></small><button type="button" class="annot-x" aria-label="Remove">×</button>';
      row.querySelector(".swatch").style.setProperty("--c", p.color);
      row.querySelector(".annot-pick-label").textContent = p.label;
      row.querySelector("small").textContent = p.faces.map((f) => `f${f.face}`).join(", ");
      row.querySelector("button").addEventListener("click", () => {
        picks.splice(i, 1);
        restyle();
        update();
      });
      box.append(row);
    });
  }

  function addLabel() {
    const label = $("annot-label").value.trim();
    if (!label || !selection.size) return;
    const faces = [...selection].map((k) => ({ index: +k.split(":")[0], face: +k.split(":")[1] }));
    picks.push({ label, faces, color: PICKS[picks.length % PICKS.length] });
    selection.clear();
    $("annot-label").value = "";
    restyle();
    update();
  }

  // ------------------------------------------------------------ save

  function picture() {
    const shot = viewer.capture();
    const k = shot.width / Math.max(1, stage.clientWidth);
    const c = shot.getContext("2d");
    paint(c, strokes, k);
    if (picks.length) {
      c.font = `${12 * k}px system-ui, sans-serif`;
      let y = shot.height - 10 * k;
      for (const p of [...picks].reverse()) {
        c.fillStyle = p.color;
        c.fillRect(10 * k, y - 10 * k, 10 * k, 10 * k);
        c.fillStyle = palette("--text");
        c.fillText(`${p.label}: ${p.faces.map((f) => `${describe(f).part} f${f.face}`).join(", ")}`, 26 * k, y);
        y -= 18 * k;
      }
    }
    // at most 2000 pixels on the longer side
    const s = Math.min(1, 2000 / Math.max(shot.width, shot.height));
    if (s === 1) return shot.toDataURL("image/png");
    const out = document.createElement("canvas");
    out.width = Math.round(shot.width * s);
    out.height = Math.round(shot.height * s);
    out.getContext("2d").drawImage(shot, 0, 0, out.width, out.height);
    return out.toDataURL("image/png");
  }

  async function save() {
    if (why()) return;
    const pending = [...selection];
    if (pending.length) {
      // a selection without a label is saved as "selected"
      picks.push({ label: $("annot-label").value.trim() || "selected", color: PICKS[picks.length % PICKS.length], faces: pending.map((k) => ({ index: +k.split(":")[0], face: +k.split(":")[1] })) });
      selection.clear();
      restyle();
    }
    const unique = (hits) => {
      const seen = new Map();
      for (const h of hits) if (h && !seen.has(`${h.index}:${h.face}`)) seen.set(`${h.index}:${h.face}`, { part: h.part, face: h.face, tags: h.tags });
      return [...seen.values()];
    };
    const body = {
      output: ctx.output(),
      camera: viewer.getCamera(),
      viewport: { width: stage.clientWidth, height: stage.clientHeight },
      strokes: strokes.map((s) => ({
        color: s.color,
        width: s.width,
        points: s.points,
        hits: s.hits.map((h) => (h ? { part: h.part, face: h.face, point: h.point } : null)),
        faces: unique(s.hits),
      })),
      picks: picks.map((p) => ({ label: p.label, faces: p.faces.map(describe) })),
      note: $("annot-note").value.trim(),
      png: picture(),
    };
    $("annot-save").disabled = true;
    $("annot-save").textContent = "Saving…";
    try {
      const r = await ctx.req("annotate", body, 60_000);
      strokes = [];
      picks = [];
      $("annot-note").value = "";
      viewer.setLocked(false);
      restyle();
      redraw();
      const done = $("annot-done");
      done.hidden = false;
      done.classList.remove("error");
      done.querySelector("code").textContent = `${r.path}/`;
      done.querySelector(".annot-say").textContent = `I left annotations in ${r.path}`;
      done.querySelector(".annot-summary").textContent = r.summary;
    } catch (e) {
      const done = $("annot-done");
      done.hidden = false;
      done.classList.add("error");
      done.querySelector(".annot-summary").textContent = `Not saved: ${e.message}`;
    } finally {
      $("annot-save").textContent = "Save";
      update();
    }
  }

  // ------------------------------------------------------------ earlier annotations

  async function loadPast() {
    const list = $("annot-past-list");
    if (ctx.machine() !== "running") {
      $("annot-past").hidden = true;
      return;
    }
    try {
      const r = await ctx.req("annotations", { limit: 30 });
      $("annot-past").hidden = !r.annotations.length;
      $("annot-past-count").textContent = String(r.annotations.length);
      list.textContent = "";
      for (const a of r.annotations) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "annot-past-item";
        b.innerHTML = "<b></b><small></small>";
        b.firstChild.textContent = a.id;
        b.lastChild.textContent = a.summary;
        b.title = "Show these marks again (the camera goes back to theirs)";
        b.addEventListener("click", () => showPast(a));
        list.append(b);
      }
    } catch {
      $("annot-past").hidden = true;
    }
  }

  async function showPast(a) {
    try {
      const buf = await ctx.req("read", { path: `${a.path}/annotation.json`, max: 16 << 20 });
      const rec = JSON.parse(new TextDecoder().decode(buf));
      if (rec.output?.name) await ctx.select(rec.output.name);
      const meshes = ctx.meshes();
      const index = (part) => Math.max(0, meshes.findIndex((m) => (m.name ?? "") === part));
      ghost = {
        id: rec.id,
        viewport: rec.viewport,
        strokes: rec.strokes ?? [],
        picks: (rec.picks ?? []).map((p, i) => ({ label: p.label, color: PICKS[i % PICKS.length], faces: p.faces.map((f) => ({ index: index(f.part), face: f.face })) })),
      };
      if (rec.camera) viewer.setCamera(rec.camera);
      $("annot-shown").hidden = false;
      $("annot-shown-text").textContent = `${a.path}: ${rec.summary ?? ""}`;
      restyle();
      redraw();
    } catch (e) {
      $("annot-shown").hidden = false;
      $("annot-shown-text").textContent = `Cannot show ${a.path}: ${e.message}`;
    }
  }

  function hideGhost() {
    if (!ghost && $("annot-shown").hidden) return;
    ghost = null;
    $("annot-shown").hidden = true;
    restyle();
    redraw();
  }
  // the strokes belong to the camera they were drawn with
  viewer.controls.addEventListener("start", hideGhost);

  // ------------------------------------------------------------ wiring

  colorButtons();
  $("annotate-toggle").addEventListener("click", () => setOpen(!open));
  $("annot-close").addEventListener("click", () => setOpen(false));
  $("annot-draw").addEventListener("click", () => setMode("draw"));
  $("annot-pick").addEventListener("click", () => setMode("pick"));
  $("annot-undo").addEventListener("click", () => {
    strokes.pop();
    viewer.setLocked(strokes.length > 0);
    redraw();
    update();
  });
  $("annot-clear").addEventListener("click", () => {
    strokes = [];
    selection.clear();
    picks = [];
    viewer.setLocked(false);
    restyle();
    redraw();
    update();
  });
  $("annot-label-form").addEventListener("submit", (e) => {
    e.preventDefault();
    addLabel();
  });
  $("annot-note").addEventListener("input", update);
  $("annot-save").addEventListener("click", save);
  $("annot-shown-hide").addEventListener("click", hideGhost);
  $("annot-copy").addEventListener("click", async () => {
    const text = $("annot-done").querySelector(".annot-say").textContent;
    try {
      await navigator.clipboard.writeText(text);
      $("annot-copy").textContent = "Copied";
      setTimeout(() => ($("annot-copy").textContent = "Copy"), 1500);
    } catch {
      $("annot-copy").textContent = "Select and copy";
    }
  });
  document.addEventListener("keydown", (e) => {
    if (!open || e.target.closest?.("input, textarea")) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      $("annot-undo").click();
    } else if (e.key === "Escape" && selection.size) {
      selection.clear();
      restyle();
      update();
    }
  });
  ctx.on((m) => {
    if (m.event === "annotation" && open) loadPast();
  });

  return {
    /** The shown output changed: marks on the old one no longer apply. */
    reset() {
      ghost = null; // an earlier annotation shown again was on the old output too
      $("annot-shown").hidden = true;
      if (!strokes.length && !selection.size && !picks.length) return void (restyle(), redraw());
      strokes = [];
      selection.clear();
      picks = [];
      viewer.setLocked(false);
      restyle();
      redraw();
      update();
    },
    /** The machine's state changed (Save needs it running). */
    machineChanged() {
      if (!open) return;
      update();
      const running = ctx.machine() === "running";
      if (running !== wasRunning) loadPast();
      wasRunning = running;
    },

  };
}
