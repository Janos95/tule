"""pack_mesh (runtime/tule_agent.py): the viewer's packed meshes read back as encode_mesh's, within the grid."""

import json
import os
import struct
import sys
import zlib

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "runtime"))

import tule_agent  # noqa: E402
from tule import box, cylinder, export_step  # noqa: E402


def unpack_v1(data):
    (hl,) = struct.unpack_from("<I", data)
    head, base = json.loads(data[4:4 + hl]), 4 + hl
    out = []
    for p in head["parts"]:
        get = lambda k, dt: np.frombuffer(data, dt, p[k][1] // np.dtype(dt).itemsize, base + p[k][0])  # noqa: E731
        out.append({"name": p["name"], "tags": p["tags"], "P": get("positions", np.float32).reshape(-1, 3),
                    "N": get("normals", np.float32).reshape(-1, 3), "I": get("indices", np.uint32).reshape(-1, 3),
                    "F": get("faceIds", np.uint32), "E": get("edges", np.float32).reshape(-1, 3)})
    return out


def unpack_v2(data):
    """What web/src/main.js decodePacked does."""
    assert data[:4] == tule_agent.MESH_MAGIC
    raw = zlib.decompress(data[4:])
    (hl,) = struct.unpack_from("<I", raw)
    head, base = json.loads(raw[4:4 + hl]), 4 + hl
    assert head["v"] == 2
    out = []
    for p in head["parts"]:
        it = np.uint32 if p["wide"] else np.uint16
        get = lambda k, dt, n: np.frombuffer(raw, dt, n, base + p[k][0])  # noqa: E731
        grid = lambda k, n: (np.cumsum(get(k, np.uint16, 3 * n).reshape(-1, 3).astype(np.int64), 0) & 0xFFFF) * p["step"] + p["lo"]  # noqa: E731
        o = get("normals", np.int16, 2 * p["nv"]).reshape(-1, 2) / 1023
        z = 1 - np.abs(o).sum(1)
        x = np.where(z < 0, (1 - np.abs(o[:, 1])) * np.where(o[:, 0] >= 0, 1, -1), o[:, 0])
        y = np.where(z < 0, (1 - np.abs(o[:, 0])) * np.where(o[:, 1] >= 0, 1, -1), o[:, 1])
        N = np.stack([x, y, z], 1)
        codes = get("indices", it, 3 * p["nt"]).astype(np.int64)
        idx, nxt = np.empty(len(codes), np.int64), 0
        for i, c in enumerate(codes):
            idx[i] = nxt - c
            nxt += c == 0
        out.append({"name": p["name"], "tags": p["tags"], "P": grid("positions", p["nv"]), "N": N / np.linalg.norm(N, axis=1, keepdims=True),
                    "I": idx.reshape(-1, 3), "F": get("faceIds", it, p["nt"]), "E": grid("edges", p["ne"])})
    return out


def triangles(m):
    """The triangles' face ids, centroids and mean normals."""
    n = m["N"][m["I"]].mean(1)
    return m["F"], m["P"][m["I"]].mean(1), n / np.linalg.norm(n, axis=1, keepdims=True)


def test_packed_mesh_reads_back(tmp_path):
    part = box(80, 40, 12) - cylinder(6, 40, center=True).at(40, 20, 0)
    part = part.fillet(part.edges.parallel((0, 0, 1)), 5)
    part = part.tag("top", part.faces.top()).named("plate", "#4a8")
    path = str(tmp_path / "plate.step")
    export_step(part, path)
    v1 = tule_agent.encode_mesh(path)
    v2 = tule_agent.pack_mesh(v1)
    assert len(v2) < len(v1) / 3
    (a,), (b,) = unpack_v1(v1), unpack_v2(v2)
    assert (a["name"], a["tags"]) == (b["name"], b["tags"]) and a["tags"]["top"]
    assert len(a["I"]) == len(b["I"]) and len(a["E"]) == len(b["E"])
    fa, ca, na = triangles(a)
    fb, cb, nb = triangles(b)
    assert sorted(fa) == sorted(fb)
    for f in set(fa.tolist()):  # each triangle has its twin on the same face: as near as the grid, as turned as the normals' coding
        A, B = fa == f, fb == f
        d = np.linalg.norm(ca[A][:, None] - cb[B][None], axis=2)
        near = d.argmin(1)
        assert d.min(1).max() < 0.01
        assert (np.einsum("ij,ij->i", na[A], nb[B][near]) > 0.999).all()
    assert np.abs(a["E"] - b["E"]).max() <= 0.0051
    assert (np.abs(a["P"].min(0) - b["P"].min(0)) < 0.006).all() and (np.abs(a["P"].max(0) - b["P"].max(0)) < 0.006).all()
