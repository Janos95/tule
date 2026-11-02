"""Errors that tell an agent what failed, where, and what would work."""

from __future__ import annotations

import json
from typing import Any


class TuleError(Exception):
    """A modeling operation failed.

    Fields are structured so agents can act on them: ``op`` (the operation),
    ``faces`` / ``edges`` (ids of the sub-shapes involved, in the shape's
    stable order), ``hint`` (what would likely work) and ``data`` (values such
    as ``max_radius``).
    """

    def __init__(
        self,
        message: str,
        *,
        op: str | None = None,
        faces: list[int] | None = None,
        edges: list[int] | None = None,
        hint: str | None = None,
        **data: Any,
    ):
        self.message = message
        self.op = op
        self.faces = faces or []
        self.edges = edges or []
        self.hint = hint
        self.data = data
        super().__init__(self.__str__())

    def __str__(self) -> str:
        parts = [f"{self.op} failed: {self.message}" if self.op else self.message]
        if self.edges:
            parts.append(f"edges {', '.join(f'e{i}' for i in self.edges)}")
        if self.faces:
            parts.append(f"faces {', '.join(f'f{i}' for i in self.faces)}")
        if self.hint:
            parts.append(self.hint)
        return "; ".join(parts)

    def to_json(self) -> str:
        return json.dumps(
            {
                "error": self.message,
                "op": self.op,
                "faces": self.faces,
                "edges": self.edges,
                "hint": self.hint,
                **self.data,
            }
        )


def _occ_errors() -> tuple:
    """Every exception class OpenCascade raises through OCP: they do not share a base class
    (StdFail_NotDone is not a Standard_Failure), so `except OCC_ERRORS` catches them all."""
    import importlib

    out = []
    for mod in ("Standard", "StdFail", "gp", "Geom", "Geom2d", "TopoDS", "BRepExtrema", "Precision"):
        try:
            m = importlib.import_module(f"OCP.{mod}")
        except ImportError:
            continue
        out += [v for v in vars(m).values() if isinstance(v, type) and issubclass(v, Exception)]
    return tuple(dict.fromkeys(out))


OCC_ERRORS = _occ_errors()
