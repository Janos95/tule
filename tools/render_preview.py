"""Preview renderer: a tiny numpy z-buffer that draws a STEP file shaded, with visible B-rep edges.

Usage: python tools/render_preview.py part.step out.png   (or part.stl; needs numpy, pillow)
A starting point for `tule screenshot`; slow on big meshes."""
import sys, numpy as np
from PIL import Image
from tule import Mesh, import_step

def look(elev, azim):
    e, a = np.radians(elev), np.radians(azim)
    fwd = -np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])  # camera looks along fwd
    right = np.cross(fwd, [0, 0, 1.0]); right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    return np.stack([right, up, -fwd])  # rows: x, y, z(toward viewer)

def raster(shape, elev, azim, W=640, H=480, color=(0.27, 0.67, 0.53)):
    m = shape if isinstance(shape, Mesh) else shape.tessellate(0.05, 0.1)
    R = look(elev, azim)
    P = m.positions @ R.T
    lo, hi = P.min(0), P.max(0); c = (lo + hi) / 2; s = 0.9 * min(W / (hi[0] - lo[0]), H / (hi[1] - lo[1]))
    X = (P[:, 0] - c[0]) * s + W / 2; Y = H / 2 - (P[:, 1] - c[1]) * s; Z = P[:, 2]
    zbuf = np.full((H, W), -np.inf); img = np.ones((H, W, 3))
    N = m.normals @ R.T
    light = np.array([0.4, 0.5, 0.75]); light /= np.linalg.norm(light)
    for t in m.indices:
        xs, ys, zs = X[t], Y[t], Z[t]
        x0, x1 = int(max(0, np.floor(xs.min()))), int(min(W - 1, np.ceil(xs.max())))
        y0, y1 = int(max(0, np.floor(ys.min()))), int(min(H - 1, np.ceil(ys.max())))
        if x1 < x0 or y1 < y0: continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        d = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
        if abs(d) < 1e-12: continue
        w0 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / d
        w1 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / d
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        z = w0 * zs[0] + w1 * zs[1] + w2 * zs[2]
        n = w0[..., None] * N[t[0]] + w1[..., None] * N[t[1]] + w2[..., None] * N[t[2]]
        n /= np.linalg.norm(n, axis=-1, keepdims=True) + 1e-12
        sub = zbuf[y0:y1 + 1, x0:x1 + 1]
        upd = inside & (z > sub)
        sub[upd] = z[upd]
        shade = 0.3 + 0.7 * np.clip(np.abs(n @ light), 0, 1)
        img[y0:y1 + 1, x0:x1 + 1][upd] = np.array(color) * shade[upd][:, None]
    # visible edges: draw where the edge point is not behind the surface
    for _, pl in m.edges:
        Q = pl @ R.T
        ex = (Q[:, 0] - c[0]) * s + W / 2; ey = H / 2 - (Q[:, 1] - c[1]) * s; ez = Q[:, 2]
        for i in range(len(Q) - 1):
            n_ = int(max(abs(ex[i + 1] - ex[i]), abs(ey[i + 1] - ey[i]))) + 1
            for k in np.linspace(0, 1, n_ + 1):
                px, py = int(ex[i] + (ex[i + 1] - ex[i]) * k), int(ey[i] + (ey[i + 1] - ey[i]) * k)
                pz = ez[i] + (ez[i + 1] - ez[i]) * k
                if 0 <= px < W and 0 <= py < H and pz >= zbuf[py, px] - 0.02 * (hi[2] - lo[2]):
                    img[py, px] = (0.07, 0.13, 0.13)
    return (img * 255).astype(np.uint8)

def read_stl(path):
    """A binary STL as a Mesh (flat normals, no edges)."""
    data = open(path, "rb").read()
    n = int(np.frombuffer(data, "<u4", 1, 80)[0])
    rec = np.frombuffer(data, [("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")], n, 84)
    P = rec["v"].reshape(-1, 3)
    N = np.repeat(rec["n"], 3, axis=0)
    return Mesh(P, N, np.arange(3 * n, dtype=np.uint32).reshape(-1, 3), np.zeros(n, np.uint32), [])

shape = read_stl(sys.argv[1]) if sys.argv[1].lower().endswith(".stl") else import_step(sys.argv[1])
views = [(30, -60), (-40, -60)]
Image.fromarray(np.concatenate([raster(shape, e, a) for e, a in views], axis=1)).save(sys.argv[2])
print("wrote", sys.argv[2])
