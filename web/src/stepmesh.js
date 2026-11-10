// A worker that turns a STEP or STL file into the viewer's mesh message, in the browser: a view-only
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
    const parts = /\.stl$/i.test(name) ? [stl(new Uint8Array(bytes), stem(name))] : await step(new Uint8Array(bytes), stem(name));
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
  const key = (i) => `${Math.round(p[3 * i] * 1e4)},${Math.round(p[3 * i + 1] * 1e4)},${Math.round(p[3 * i + 2] * 1e4)}`;
  const seen = new Map(); // edge key -> [face, vertex a, vertex b, count, other faces differ]
  for (let t = 0; t < idx.length / 3; t++) {
    for (let k = 0; k < 3; k++) {
      const a = idx[3 * t + k], b = idx[3 * t + ((k + 1) % 3)];
      const ka = key(a), kb = key(b);
      const e = ka < kb ? `${ka}|${kb}` : `${kb}|${ka}`;
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
