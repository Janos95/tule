"""Reading files under /workspace for the viewer's Download (request "read").

Paths from the page are relative to /workspace ("outputs/bracket"); absolute
paths, ".." and symlinks that lead outside /workspace are refused.

    read  {path, max}  -> the file's first `max` bytes (default 1 MiB, at most READ_MAX)
"""

from __future__ import annotations

import os

READ_MAX = 30 << 20  # one WebSocket message through the control plane


def resolve(root: str, rel) -> str:
    """The absolute path of `rel` inside `root`, or ValueError saying why not."""
    if not isinstance(rel, str):
        raise ValueError("path must be a string relative to /workspace, like 'outputs/bracket'")
    if rel.startswith("/") or "\\" in rel or "\0" in rel:
        raise ValueError(f"{rel!r}: give a path relative to /workspace, like 'outputs/bracket'")
    if any(p == ".." for p in rel.split("/")):
        raise ValueError(f"{rel!r}: '..' is not allowed; paths stay inside /workspace")
    rel = rel.rstrip("/")
    path = os.path.join(root, rel) if rel not in ("", ".") else root
    real, base = os.path.realpath(path), os.path.realpath(root)
    if real != base and not real.startswith(base + os.sep):
        raise ValueError(f"{rel!r} is a symlink that leads outside /workspace")
    return path


def read(agent, args: dict) -> bytes:
    rel = args.get("path") or ""
    path = resolve(agent.workspace.root, rel)
    if not os.path.isfile(path):
        raise ValueError(f"{rel!r} is not a file" if os.path.exists(path) else f"no such file {rel!r}")
    want = int(args.get("max") or (1 << 20))
    size = os.path.getsize(path)
    if want > READ_MAX and size > READ_MAX:
        raise ValueError(f"{rel} is {size / 2**20:.1f} MiB; the viewer reads at most {READ_MAX >> 20} MiB "
                         f"(copy it with scp or tule ssh -- cat {rel} > file)")
    with open(path, "rb") as f:
        return f.read(max(0, min(want, READ_MAX)))


REQUESTS = {"read": read}
