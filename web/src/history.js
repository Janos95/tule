// Snapshot history: the project's saved states of /workspace (REST, through
// auth.js), so any output can be seen as it was.
//
// Picking a snapshot fetches its manifest (per file: chunk hashes, size),
// turns outputs/ into an outputs index like the machine's (output.json is
// read from its chunks over REST) and hands it to the page; geometry is then
// tessellated by the machine from the chunks (req mesh_chunks), cached by
// hash like the live meshes. Restore (after a confirm) makes /workspace match
// the snapshot; the machine saves the current state first.

import { auth } from "./auth.js";
import { size } from "./files.js";

const GEOMETRY = /\.(step|stp|stl)$/i;

export const when = (ms) => new Date(ms).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** ctx: {project, view(snapshot, outputs), live(), notify(text), machine()}. */
export function initHistory(ctx) {
  const $ = (id) => document.getElementById(id);
  const api = (path, init) => auth.api(`/api/projects/${ctx.project}${path}`, init);
  const list = $("snapshot-list");
  const note = $("history-note");
  let current = null; // the snapshot being viewed

  async function load() {
    note.hidden = true;
    try {
      const { snapshots } = await (await api("/snapshots?limit=100")).json();
      list.textContent = "";
      if (!snapshots.length) {
        note.hidden = false;
        note.textContent = "No snapshots yet: the machine saves /workspace a few seconds after it changes.";
      }
      for (const s of snapshots) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "snap";
        b.setAttribute("aria-selected", String(s.id === current?.id));
        b.innerHTML = "<span></span><small></small>";
        b.firstChild.textContent = when(s.at);
        b.lastChild.textContent = `${s.reason} · ${s.files} files · ${size(s.bytes)}`;
        b.title = `snapshot ${s.id}`;
        b.addEventListener("click", () => view(s));
        list.append(b);
      }
    } catch (e) {
      note.hidden = false;
      note.textContent = `Cannot list snapshots: ${e.message}`;
    }
  }

  async function bytes(chunks) {
    const parts = await Promise.all(chunks.map(async (h) => new Uint8Array(await (await api(`/blobs/${h}`)).arrayBuffer())));
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let at = 0;
    for (const p of parts) {
      out.set(p, at);
      at += p.length;
    }
    return out;
  }

  /** The outputs index of a manifest, shaped like the machine's (runtime/tule_agent.py, Outputs.index). */
  async function outputsOf(manifest) {
    const groups = new Map();
    const rels = Object.keys(manifest.files ?? {}).filter((r) => r.startsWith("outputs/")).sort();
    for (const rel of rels) {
      const f = manifest.files[rel];
      const inner = rel.slice("outputs/".length);
      const [top, ...rest] = inner.split("/");
      const name = rest.length ? top : top.replace(/\.[^.]+$/, "");
      if (!groups.has(name)) groups.set(name, { name, files: [], meta: null });
      const g = groups.get(name);
      if (rest.join("/") === "output.json") {
        try {
          g.meta = JSON.parse(new TextDecoder().decode(await bytes(f.h)));
        } catch (e) {
          g.error = `output.json: ${e.message}`;
        }
      } else if (GEOMETRY.test(inner)) {
        g.files.push({ path: inner, file: rest.join("/") || top, hash: `chunks:${f.h.join(",")}`, chunks: f.h, size: f.s });
      }
    }
    return [...groups.values()].filter((g) => g.files.length || g.error).sort((a, b) => (a.name < b.name ? -1 : 1));
  }

  async function view(s) {
    try {
      const manifest = await (await api(`/snapshots/${s.id}`)).json();
      current = s;
      ctx.view(s, await outputsOf(manifest));
      for (const b of list.querySelectorAll(".snap")) b.setAttribute("aria-selected", String(b.title === `snapshot ${s.id}`));
      $("snapshot-bar").hidden = false;
      $("snapshot-text").textContent = `Snapshot of ${when(s.at)} (${s.reason}): outputs as they were`;
    } catch (e) {
      ctx.notify(`Cannot open snapshot ${s.id}: ${e.message}`);
    }
  }

  function live() {
    current = null;
    $("snapshot-bar").hidden = true;
    for (const b of list.querySelectorAll(".snap")) b.setAttribute("aria-selected", "false");
    ctx.live();
  }

  async function restore() {
    if (!current) return;
    if (ctx.machine() !== "running") return ctx.notify("Restoring needs the machine: start it (tule up), then restore.");
    const ok = confirm(`Restore /workspace to the snapshot of ${when(current.at)}?\n\nFiles changed since then are replaced; the current state is saved as a snapshot first, so you can come back to it.`);
    if (!ok) return;
    $("snapshot-restore").disabled = true;
    $("snapshot-restore").textContent = "Restoring…";
    try {
      const r = await (await api("/restore", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: current.id }) })).json();
      if (r.error) throw new Error([r.error, r.hint].filter(Boolean).join(": "));
      ctx.notify(`Restored the snapshot of ${when(current.at)} (${r.files ?? 0} files written)`);
      live();
      load();
    } catch (e) {
      ctx.notify(`Restore failed: ${e.message}`);
    } finally {
      $("snapshot-restore").disabled = false;
      $("snapshot-restore").textContent = "Restore…";
    }
  }

  $("snapshot-live").addEventListener("click", live);
  $("snapshot-restore").addEventListener("click", restore);

  return {
    /** The History tab was opened. */
    show: load,
    /** A snapshot file's bytes (for Download). */
    bytes,
    viewing: () => current,
  };
}
