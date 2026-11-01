"""Draws the tule mark (one stem, three seed heads fanned from one node): tools/logo.py writes mark.svg, mark-light.svg and preview.html in the current folder; the site uses web/src/favicon.svg."""
import math, sys

def spikelet(x, y, ang, length, width):
    """A teardrop: blunt near the stalk at (x, y), pointed at the far end, pointing along ang (degrees)."""
    a = math.radians(ang)
    ux, uy = math.cos(a), math.sin(a)
    nx, ny = -uy, ux
    tip = (x + ux * length, y + uy * length)
    w = width / 2
    # control points: widest at ~40% of the length
    c1 = (x + ux * length * 0.15 + nx * w * 1.25, y + uy * length * 0.15 + ny * w * 1.25)
    c2 = (x + ux * length * 0.75 + nx * w * 0.9, y + uy * length * 0.75 + ny * w * 0.9)
    c3 = (x + ux * length * 0.75 - nx * w * 0.9, y + uy * length * 0.75 - ny * w * 0.9)
    c4 = (x + ux * length * 0.15 - nx * w * 1.25, y + uy * length * 0.15 - ny * w * 1.25)
    f = lambda p: f"{p[0]:.1f} {p[1]:.1f}"
    return f"M{f((x, y))} C{f(c1)} {f(c2)} {f(tip)} C{f(c3)} {f(c4)} {f((x, y))}Z"

# colors from web/src/palette.css: navy, reed-light and stem-light on the tile; reed and stem without it
def mark(P, tile=True, bg="#23344a", stem="#8fc46a", seed="#e0a45a"):
    s = []
    if tile:
        s.append(f'<rect width="512" height="512" rx="{P["rx"]}" fill="{bg}"/>')
    bx, by, tx, ty = P["base"] + P["top"]
    nodey = P["node"]
    # the stem: a gentle curve, base to top
    cx = (bx + tx) / 2 + P["bend"]
    stem_path = f'<path d="M{bx} {by} Q{cx} {(by+ty)/2} {tx} {ty}" fill="none" stroke="{stem}" stroke-width="{P["sw"]}" stroke-linecap="round"/>'
    # point on the stem at nodey (quadratic: solve t from y)
    t = (by - nodey) / (by - ty)
    nx = (1-t)**2*bx + 2*(1-t)*t*cx + t*t*tx
    for ang in P["angles"]:
        a = math.radians(ang)
        sx, sy = nx + math.cos(a) * P["stalk"], nodey + math.sin(a) * P["stalk"]
        s.append(f'<path d="M{nx:.1f} {nodey:.1f} L{sx:.1f} {sy:.1f}" stroke="{seed}" stroke-width="{P["ssw"]}" stroke-linecap="round"/>')
        ex, ey = sx + math.cos(a) * P["len"] / 2, sy + math.sin(a) * P["len"] / 2
        s.append(f'<ellipse cx="{ex:.1f}" cy="{ey:.1f}" rx="{P["len"]/2:.1f}" ry="{P["wid"]/2:.1f}" transform="rotate({ang} {ex:.1f} {ey:.1f})" fill="{seed}"/>')
    s.append(stem_path)
    return "".join(s)

P = dict(rx=112, base=(336, 452), top=(323, 62), bend=-6, sw=32, node=222,
         angles=(148, 194, 240), stalk=60, ssw=13, len=94, wid=52)
for kv in sys.argv[1:]:
    k, v = kv.split("=")
    P[k] = eval(v)

svg = lambda inner, size: f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 512 512">{inner}</svg>'
dark = mark(P)
light = mark(P, tile=False, stem="#4f8a3c", seed="#c98a3a")
open("mark.svg", "w").write(svg(dark, 512))
open("mark-light.svg", "w").write(svg(light, 512))
row = "".join(f'<div style="display:inline-block;margin:8px;vertical-align:bottom">{svg(dark, n)}</div>' for n in (256, 96, 48, 32, 16))
rowl = "".join(f'<div style="display:inline-block;margin:8px;vertical-align:bottom">{svg(light, n)}</div>' for n in (256, 96, 48, 32, 16))
head = f'<div style="display:flex;align-items:center;gap:8px;font:600 20px system-ui;color:#23344a;margin:16px">{svg(dark, 28)}tule</div>'
open("preview.html", "w").write(f'<!doctype html><body style="margin:0;background:#fff">{head}<div>{row}</div><div style="background:#faf8f3">{rowl}</div><div style="background:#14171c;padding:8px">{row}</div></body>')
print(P)
