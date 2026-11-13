"""How a part looks: its material (metal, roughness), for the viewer and ``tule screenshot``.

``part.named("hull", material="steel")``, ``part.with_material("polished", color="#e0b45c")``,
``part.with_material(metal=1, roughness=0.3)``; ``part.material`` reads it back. It is written
beside the part's color (``output.json``, a PLY's header); a part without one looks as before.
"""

from __future__ import annotations

from .errors import TuleError


def parse_rgba(c):
    from .io import parse_rgba as f
    return f(c)


def color_hex(c):
    from .io import color_hex as f
    return f(c)


# how a surface looks: name -> (metal 0..1, roughness 0..1, its color when the part has none)
MATERIALS = {
    "steel": (1.0, 0.36, "#cfccc6"),  # stainless, lightly brushed: bright, a little warm, soft reflections
    "brushed-steel": (1.0, 0.3, "#d2d0cb"),
    "polished": (1.0, 0.06, "#d6d6d6"),  # a mirror: chrome, or gold with color="#e0b45c"
    "aluminium": (1.0, 0.4, "#c8ccd0"),
    "satin": (1.0, 0.5, "#55575a"),  # dark satin metal: engines, anodised parts
    "matte": (0.0, 0.9, None),
    "plastic": (0.0, 0.4, None),
    "rubber": (0.0, 0.95, "#222222"),
    "ceramic-tile": (0.0, 0.78, "#1d1d1f"),  # matte black heat-shield tiles
}
_MATERIAL_ALIASES = {"stainless": "steel", "aluminum": "aluminium", "chrome": "polished", "tile": "ceramic-tile"}
DEFAULT_MATERIAL = {"metal": 0.0, "roughness": 0.62}  # what a part without one looks like


class Paint(str):
    """A part's color that also carries its material (``{"metal", "roughness"}``). The material
    rides on the color so every operation that keeps a part's color (booleans, moves, meshing,
    placed copies, files) keeps its material too. ``chosen``: False when the color is the
    material's own (steel's grey), so another material brings its own color."""

    material: dict
    chosen: bool

    def __reduce__(self):
        return (_paint, (str(self), self.material, self.chosen))


def _paint(color: str, material: dict, chosen: bool = True) -> Paint:
    p = Paint(color)
    p.material, p.chosen = material, chosen
    return p


def material_of(color) -> dict | None:
    """The material a part's color carries (``part.material``), or None."""
    return getattr(color, "material", None)


def parse_material(material=None, metal=None, roughness=None, base: dict | None = None) -> tuple[dict, str | None]:
    """``({"metal", "roughness"}, the preset's color or None)`` for a preset name, a dict
    ``{"metal": 1, "roughness": 0.3}``, or ``metal=``/``roughness=`` over ``base``."""
    color = None
    if material is None:
        m = dict(base or DEFAULT_MATERIAL)
    elif isinstance(material, str):
        key = _MATERIAL_ALIASES.get(material.strip().lower(), material.strip().lower())
        if key not in MATERIALS:
            raise TuleError(f"unknown material {material!r}", op="material",
                            hint=f"use one of {', '.join(MATERIALS)}, or metal= and roughness= (0..1)")
        mt, r, color = MATERIALS[key]
        m = {"metal": mt, "roughness": r}
    elif isinstance(material, dict):
        extra = set(material) - {"metal", "roughness", "metalness"}
        if extra:
            raise TuleError(f"a material has metal and roughness, not {', '.join(sorted(extra))}", op="material",
                            hint='{"metal": 1, "roughness": 0.3}')
        m = {"metal": float(material.get("metal", material.get("metalness", DEFAULT_MATERIAL["metal"]))),
             "roughness": float(material.get("roughness", DEFAULT_MATERIAL["roughness"]))}
    else:
        raise TuleError(f"cannot read material {material!r}", op="material",
                        hint=f'a name ({", ".join(MATERIALS)}) or {{"metal": 1, "roughness": 0.3}}')
    if metal is not None:
        m["metal"] = float(metal)
    if roughness is not None:
        m["roughness"] = float(roughness)
    for k, v in m.items():
        if not 0 <= v <= 1:
            raise TuleError(f"{k} {v} is not between 0 and 1", op="material",
                            hint="metal: 0 a paint or plastic, 1 a bare metal; roughness: 0 a mirror, 1 chalk")
    return {k: round(v, 4) for k, v in m.items()}, color


def with_look(old, color=None, material=None, metal=None, roughness=None):
    """A part's new color: ``color`` (else the ``old`` one) with ``material`` (else the old one's).
    Without a color of its own a part takes its material's (steel's warm grey, the tiles' black)."""
    if color is not None:
        parse_rgba(color)  # a wrong color fails here, not when the part is written
    if material is None and metal is None and roughness is None:
        mat = material_of(color) or material_of(old)
        if color is None or mat is None:
            return color if color is not None else old
        return _paint(color if isinstance(color, str) else color_hex(parse_rgba(color)), mat)
    mat, preset = parse_material(material, metal, roughness, base=material_of(old))
    if color is not None:
        return _paint(color if isinstance(color, str) else color_hex(parse_rgba(color)), mat)
    if old is not None and getattr(old, "chosen", True):  # the part's own color, now in this material
        return _paint(old if isinstance(old, str) else color_hex(parse_rgba(old)), mat)
    if preset is None and old is not None:  # metal=/roughness= changed: the color stays
        return _paint(str(old), mat, False)
    return _paint(preset or "#c8ccd0", mat, False)


class Looks:
    """``material`` and ``with_material`` for a part kind; it defines ``_looking(color)``: a copy in that color."""

    __slots__ = ()

    @property
    def material(self) -> dict | None:
        """How the part looks: ``{"metal": 0..1, "roughness": 0..1}``, or None (the default matte paint)."""
        return material_of(self.color)

    def with_material(self, material=None, *, metal=None, roughness=None, color=None):
        """The part in a material, for the viewer and screenshots: a name (``"steel"`` lightly
        brushed stainless, ``"brushed-steel"``, ``"polished"``, ``"aluminium"``, ``"satin"`` dark
        metal, ``"matte"``, ``"plastic"``, ``"rubber"``, ``"ceramic-tile"`` matte black) and/or
        ``metal=`` (0 paint or plastic, 1 bare metal) and ``roughness=`` (0 mirror, 1 chalk).
        A part without a color takes the material's (steel's warm grey); its own color stays,
        or give one (``"polished", color="#e0b45c"``: gold)."""
        return self._looking(with_look(self.color, color, material, metal, roughness))
