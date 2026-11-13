// A worker that turns a STEP, PLY or STL file into the viewer's mesh message, in the browser: a view-only
// link has no machine to do it. The message is what runtime/tule_agent.py's encode_mesh sends:
// [u32 header length][JSON header][arrays], per part positions, normals, indices, a B-rep face id per
// triangle (OpenCascade's face order, as Tule numbers faces) and edge segments where faces meet.
/* global occtimportjs */
importScripts("__OCCT__occt-import-js.js");

let occt;
const ready = () => (occt ??= occtimportjs({ locateFile: (f) => `__OCCT__${f}` }).catch((e) => {
  occt = undefined; // not kept: the next model tries again
  throw e;
}));

onmessage = async ({ data: { id, name, bytes } }) => {
  try {
    const b = new Uint8Array(bytes);
    const parts = /\.stl$/i.test(name) ? [stl(b, stem(name))] : /\.ply$/i.test(name) ? [ply(b, stem(name))] : await step(b, stem(name));
    const buf = encode(parts);
    postMessage({ id, buf }, [buf]);
  } catch (e) {
    postMessage({ id, error: String(e?.message ?? e) });
  }
};

const stem = (name) => name.split("/").pop().replace(/\.[^.]+$/, "");

async function step(bytes, name) {
  const r = (await ready()).ReadStepFile(bytes, {
    linearUnit: "millimeter",
    linearDeflectionType: "bounding_box_ratio",
    linearDeflection: 0.0008,
    angularDeflection: 0.35,
  });
  if (!r.success) throw new Error("this STEP file could not be read");
  return r.meshes.map((m) => {
    const positions = new Float32Array(m.attributes.position.array);
    const indices = new Uint32Array(m.index.array);
    const faceIds = new Uint32Array(indices.length / 3);
    m.brep_faces.forEach((f, id) => faceIds.fill(id, f.first, f.last + 1));
    const normals = m.attributes.normal ? new Float32Array(m.attributes.normal.array) : flatNormals(positions, indices);
    return { name: m.name || name, color: hex(m.color), positions, normals, indices, faceIds, edges: featureEdges(positions, indices, faceIds) };
  });
}

function stl(b, name) {
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  let pts;
  const n = b.byteLength >= 84 ? dv.getUint32(80, true) : 0;
  if (b.byteLength === 84 + 50 * n) {
    pts = new Float32Array(9 * n);
    for (let t = 0; t < n; t++) for (let k = 0; k < 9; k++) pts[9 * t + k] = dv.getFloat32(84 + 50 * t + 12 + 4 * k, true);
  } else {
    const v = [...new TextDecoder().decode(b).matchAll(/vertex\s+(\S+)\s+(\S+)\s+(\S+)/g)].flatMap((m) => [+m[1], +m[2], +m[3]]);
    pts = new Float32Array(v);
  }
  const indices = Uint32Array.from({ length: pts.length / 3 }, (_, i) => i);
  return { name, color: null, positions: pts, normals: flatNormals(pts, indices), indices, faceIds: new Uint32Array(indices.length / 3), edges: new Float32Array(0) };
}

// PLY as tule.Mesh writes it (binary little-endian or ASCII; a "region" per face is the face id, and a
// "comment tule {json}" header line carries name and color); polygons become fans.
const PLY_SIZE = { char: 1, int8: 1, uchar: 1, uint8: 1, short: 2, int16: 2, ushort: 2, uint16: 2, int: 4, int32: 4, uint: 4, uint32: 4, float: 4, float32: 4, double: 8, float64: 8 };

function ply(b, name) {
  const headEnd = new TextDecoder("latin1").decode(b.subarray(0, Math.min(b.length, 65536))).indexOf("end_header");
  if (headEnd < 0) throw new Error("not a PLY file");
  const lines = new TextDecoder("latin1").decode(b.subarray(0, headEnd)).split(/\r?\n/);
  let fmt = "ascii", meta = {};
  const els = [];
  for (const line of lines) {
    const w = line.trim().split(/\s+/);
    if (w[0] === "format") fmt = w[1];
    else if (w[0] === "comment" && w[1] === "tule") try { meta = JSON.parse(line.slice(line.indexOf("tule") + 4)); } catch { /* not ours */ }
    else if (w[0] === "element") els.push({ name: w[1], n: +w[2], props: [] });
    else if (w[0] === "property") els.at(-1).props.push(w[1] === "list" ? { list: true, count: w[2], type: w[3], name: w[4] } : { type: w[1], name: w[2] });
  }
  if (fmt === "binary_big_endian") throw new Error("big-endian PLY is not supported");
  let off = b.indexOf(10, headEnd) + 1;
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  const tokens = fmt === "ascii" ? new TextDecoder().decode(b.subarray(off)).trim().split(/\s+/) : null;
  let ti = 0;
  const read = (type) => {
    if (tokens) return +tokens[ti++];
    const t = type, o = off;
    off += PLY_SIZE[type];
    if (/^(float|float32)$/.test(t)) return dv.getFloat32(o, true);
    if (/^(double|float64)$/.test(t)) return dv.getFloat64(o, true);
    if (/^(uchar|uint8)$/.test(t)) return dv.getUint8(o);
    if (/^(char|int8)$/.test(t)) return dv.getInt8(o);
    if (/^(ushort|uint16)$/.test(t)) return dv.getUint16(o, true);
    if (/^(short|int16)$/.test(t)) return dv.getInt16(o, true);
    if (/^(uint|uint32)$/.test(t)) return dv.getUint32(o, true);
    return dv.getInt32(o, true);
  };
  let positions = new Float32Array(0);
  const idx = [], fid = [];
  for (const el of els) {
    if (el.name === "vertex") positions = new Float32Array(3 * el.n);
    for (let i = 0; i < el.n; i++) {
      let face = null, region = 0;
      for (const p of el.props) {
        if (p.list) {
          const c = read(p.count), v = [];
          for (let k = 0; k < c; k++) v.push(read(p.type));
          if (p.name === "vertex_indices" || p.name === "vertex_index") face = v;
        } else {
          const v = read(p.type);
          if (el.name === "vertex" && "xyz".includes(p.name) && p.name.length === 1) positions[3 * i + "xyz".indexOf(p.name)] = v;
          if (el.name === "face" && p.name === "region") region = v;
        }
      }
      if (face) for (let k = 1; k + 1 < face.length; k++) idx.push(face[0], face[k], face[k + 1]), fid.push(region);
    }
  }
  const indices = Uint32Array.from(idx), faceIds = Uint32Array.from(fid);
  return { name: meta.name || name, color: meta.color ?? null, positions, normals: flatNormals(positions, indices), indices, faceIds, edges: featureEdges(positions, indices, faceIds) };
}

const hex = (c) => (Array.isArray(c) ? `#${c.map((x) => Math.round(255 * x).toString(16).padStart(2, "0")).join("")}` : null);

function flatNormals(p, idx) {
  const n = new Float32Array(p.length);
  for (let t = 0; t < idx.length; t += 3) {
    const [a, b, c] = [idx[t], idx[t + 1], idx[t + 2]].map((i) => 3 * i);
    const ux = p[b] - p[a], uy = p[b + 1] - p[a + 1], uz = p[b + 2] - p[a + 2];
    const vx = p[c] - p[a], vy = p[c + 1] - p[a + 1], vz = p[c + 2] - p[a + 2];
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    for (const i of [a, b, c]) (n[i] += nx), (n[i + 1] += ny), (n[i + 2] += nz);
  }
  for (let i = 0; i < n.length; i += 3) {
    const l = Math.hypot(n[i], n[i + 1], n[i + 2]) || 1;
    (n[i] /= l), (n[i + 1] /= l), (n[i + 2] /= l);
  }
  return n;
}

/** Lines where two B-rep faces meet (and open borders): the edges the machine would send. */
function featureEdges(p, idx, faceIds) {
  // vertices at the same place (to 0.1 µm) are one: numbered once per vertex, so an edge is a number pair
  const weld = new Map();
  const id = new Uint32Array(p.length / 3);
  for (let i = 0; i < id.length; i++) {
    const k = `${Math.round(p[3 * i] * 1e4)},${Math.round(p[3 * i + 1] * 1e4)},${Math.round(p[3 * i + 2] * 1e4)}`;
    let w = weld.get(k);
    if (w === undefined) weld.set(k, (w = weld.size));
    id[i] = w;
  }
  const W = weld.size;
  const seen = new Map(); // edge (lower id × W + higher id) -> [face, vertex a, vertex b, count, other faces differ]
  for (let t = 0; t < idx.length / 3; t++) {
    for (let k = 0; k < 3; k++) {
      const a = idx[3 * t + k], b = idx[3 * t + ((k + 1) % 3)];
      const wa = id[a], wb = id[b];
      const e = wa < wb ? wa * W + wb : wb * W + wa;
      const s = seen.get(e);
      if (!s) seen.set(e, [faceIds[t], a, b, 1, false]);
      else (s[3] += 1), (s[4] ||= s[0] !== faceIds[t]);
    }
  }
  const out = [];
  for (const [, a, b, count, differ] of seen.values()) {
    if (count === 1 || differ) out.push(p[3 * a], p[3 * a + 1], p[3 * a + 2], p[3 * b], p[3 * b + 1], p[3 * b + 2]);
  }
  return new Float32Array(out);
}

function encode(parts) {
  const arrays = [];
  let offset = 0;
  const put = (a) => {
    const at = [offset, a.byteLength];
    arrays.push(new Uint8Array(a.buffer, a.byteOffset, a.byteLength));
    offset += a.byteLength;
    return at;
  };
  const header = {
    parts: parts.map((q) => ({
      name: q.name, color: q.color, tags: {},
      positions: put(q.positions), normals: put(q.normals), indices: put(q.indices), faceIds: put(q.faceIds), edges: put(q.edges),
    })),
  };
  const h = new TextEncoder().encode(JSON.stringify(header));
  const buf = new Uint8Array(4 + h.length + offset);
  new DataView(buf.buffer).setUint32(0, h.length, true);
  buf.set(h, 4);
  let at = 4 + h.length;
  for (const a of arrays) buf.set(a, at), (at += a.byteLength);
  return buf.buffer;
}
