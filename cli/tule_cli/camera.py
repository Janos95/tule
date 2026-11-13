"""The camera both `tule screenshot` and the viewer use (web/src/viewer.js, orbit()/setOrbit()).

A camera is an orbit around a target point, z up:

    azimuth    degrees about +Z, from +X toward +Y (0: the camera on the +X side, looking along -X;
               -90: on the -Y side, the "front" view)
    elevation  degrees above the XY plane (90: from above), kept within ±89.99 so the view keeps its azimuth
    distance   mm from the target to the eye
    target     the point looked at, mm
    fov        the vertical field of view in degrees (a perspective camera; the viewer's is 30)

The eye is target + distance × (cos el cos az, cos el sin az, sin el). Screen right is
(-sin az, cos az, 0), screen up is right × (eye - target)/distance: what three.js's lookAt with
up = +Z gives. A pixel: x = W/2 + f·xc/d, y = H/2 - f·yc/d with f = (H/2)/tan(fov/2), (xc, yc) the
point's right and up offsets from the eye and d its depth along the view.

The viewer link parameter is the same numbers: ?cam=az,el,distance,tx,ty,tz[,fov].
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

FOV = 30.0  # the viewer's (web/src/viewer.js)
MAX_EL = 89.99
FIT_MARGIN = 1.12  # framing: the box fills 1/1.12 of the view


@dataclass(frozen=True)
class Camera:
    azimuth: float
    elevation: float
    distance: float
    target: tuple
    fov: float = FOV

    def basis(self) -> np.ndarray:
        return basis(self.azimuth, self.elevation)

    @property
    def eye(self) -> np.ndarray:
        return np.asarray(self.target, dtype=np.float64) + self.distance * self.basis()[2]

    def param(self) -> str:
        """The viewer's ?cam= value."""
        return ",".join(_num(v) for v in (self.azimuth, self.elevation, self.distance, *self.target, self.fov))

    def as_dict(self) -> dict:
        return {"azimuth": _round(self.azimuth), "elevation": _round(self.elevation), "distance": _round(self.distance),
                "target": [_round(v) for v in self.target], "fov": _round(self.fov), "cam": self.param()}


def _round(v: float) -> float:
    return float(f"{v:.7g}")


def _num(v: float) -> str:
    s = f"{v:.7g}"
    if "e" in s:
        s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "0") else s


def clamp_elevation(el: float) -> float:
    return max(-MAX_EL, min(MAX_EL, float(el)))


def basis(azimuth: float, elevation: float) -> np.ndarray:
    """Rows: screen right, screen up, toward the viewer (from the target to the eye)."""
    a, e = math.radians(azimuth), math.radians(clamp_elevation(elevation))
    back = np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
    right = np.array([-math.sin(a), math.cos(a), 0.0])
    up = np.cross(back, right)
    return np.stack([right, up, back])


def direction_angles(d) -> tuple[float, float]:
    """(azimuth, elevation) of a direction from the target toward the eye."""
    d = np.asarray(d, dtype=np.float64)
    d = d / np.linalg.norm(d)
    el = math.degrees(math.asin(max(-1.0, min(1.0, d[2]))))
    az = math.degrees(math.atan2(d[1], d[0])) if abs(d[2]) < 1 - 1e-12 else -90.0
    return az, el


def from_eye(eye, target, fov: float = FOV) -> Camera:
    eye, target = np.asarray(eye, dtype=np.float64), np.asarray(target, dtype=np.float64)
    az, el = direction_angles(eye - target)
    return Camera(az, el, float(np.linalg.norm(eye - target)), tuple(float(v) for v in target), fov)


def parse_cam(text: str) -> Camera:
    """az,el,distance,tx,ty,tz[,fov] (the viewer's ?cam=)."""
    try:
        v = [float(x) for x in str(text).replace(" ", "").split(",")]
    except ValueError:
        v = []
    if len(v) not in (6, 7) or not all(math.isfinite(x) for x in v) or v[2] <= 0:
        raise ValueError(f"a camera is az,el,distance,tx,ty,tz[,fov] (degrees and mm, distance > 0), not {text!r}")
    return Camera(v[0], clamp_elevation(v[1]), v[2], (v[3], v[4], v[5]), v[6] if len(v) == 7 else FOV)


def parse_vec(text, what: str = "target") -> tuple:
    if isinstance(text, (list, tuple)):
        v = list(text)
    else:
        v = str(text).replace(" ", "").split(",")
    try:
        out = tuple(float(x) for x in v)
    except (TypeError, ValueError):
        out = ()
    if len(out) != 3 or not all(math.isfinite(x) for x in out):
        raise ValueError(f"{what} is X,Y,Z in mm, not {text!r}")
    return out


def parse_size(text, default_aspect: float | None = None) -> tuple[int, int | None]:
    """'1600x1200' -> (1600, 1200); '1200' -> (1200, None); [W, H] too."""
    if isinstance(text, (list, tuple)) and len(text) == 2:
        w, h = text
    elif isinstance(text, int):
        w, h = text, None
    else:
        s = str(text).lower().replace("×", "x").strip()
        w, _, h = s.partition("x")
        h = h or None
    try:
        w = int(w)
        h = int(h) if h is not None else None
    except (TypeError, ValueError):
        raise ValueError(f"a size is W or WxH in pixels, not {text!r}") from None
    if not (16 <= w <= 8192) or (h is not None and not (16 <= h <= 8192)):
        raise ValueError(f"a size is 16 to 8192 pixels a side, not {text!r}")
    return w, h


def fit_distance(lo, hi, cam: Camera, aspect: float, margin: float = FIT_MARGIN) -> float:
    """The distance at which the box [lo, hi] fits in the view (every corner inside, with a margin),
    looking at cam.target from cam's direction, through a perspective of cam.fov and aspect W/H."""
    R = cam.basis()
    tv = math.tan(math.radians(cam.fov) / 2)
    th = tv * aspect
    lo, hi = np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)
    corners = np.array([[(hi if i & 1 else lo)[0], (hi if i & 2 else lo)[1], (hi if i & 4 else lo)[2]] for i in range(8)])
    p = corners - np.asarray(cam.target, dtype=np.float64)
    z = p @ R[2]
    d = np.maximum(z + margin * np.abs(p @ R[0]) / th, z + margin * np.abs(p @ R[1]) / tv)
    r = 0.5 * float(np.linalg.norm(hi - lo))
    return max(float(d.max()), 1e-3, 0.02 * r)


def project(points, cam: Camera, W: int, H: int):
    """Pixel x, y (from the top left) and depth along the view (mm; > 0 in front) of world points."""
    P = np.atleast_2d(np.asarray(points, dtype=np.float64)) - cam.eye
    R = cam.basis()
    x, y, z = P @ R[0], P @ R[1], -(P @ R[2])
    f = (H / 2) / math.tan(math.radians(cam.fov) / 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        return W / 2 + f * x / z, H / 2 - f * y / z, z


# ---------------------------------------------------------------- parts by name


def matches(path: str, name: str) -> bool:
    """Whether a part's path ("ship/heatshield/tiles") is the part or group ``name``: the whole path, a
    group of it ("ship/heatshield"), or a run of its names anywhere ("heatshield", "heatshield/tiles")."""
    name = name.strip().strip("/")
    if not name:
        return False
    return f"/{path}/".find(f"/{name}/") >= 0


def split_copy(name: str) -> tuple[str, int | None]:
    """'tiles[17]' -> ('tiles', 17): one placed copy."""
    if name.endswith("]") and "[" in name:
        stem, _, i = name[:-1].rpartition("[")
        if i.isdigit():
            return stem, int(i)
    return name, None


def with_target(cam: Camera, target) -> Camera:
    return replace(cam, target=tuple(float(v) for v in target))
