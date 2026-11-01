"""The viewer's files browser: list and read files under /workspace (requests "files" and "read").

Paths from the page are relative to /workspace ("outputs/bracket"); absolute
paths, ".." and symlinks that lead outside /workspace are refused.

    files {path}       -> {path, entries: [{name, type: "dir"|"file"|"link", size, mtime (ms)}]}
    read  {path, max}  -> the file's first `max` bytes (default 1 MiB, at most READ_MAX)
"""

from __future__ import annotations

import os
import stat

READ_MAX = 30 << 20  # one WebSocket message through the control plane
LIST_MAX = 5000


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


def files(agent, args: dict) -> dict:
    root = agent.workspace.root
    rel = args.get("path") or ""
    path = resolve(root, rel)
    if not os.path.isdir(path):
        raise ValueError(f"{rel!r} is not a directory" if os.path.exists(path) else f"no such directory {rel!r}")
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            kind = "link" if stat.S_ISLNK(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode) else "file"
            if kind == "link" and os.path.isdir(e.path):
                try:
                    resolve(root, os.path.relpath(e.path, root))
                    kind = "dir"  # a link to a directory inside /workspace is browsable
                except ValueError:
                    pass
            entries.append({"name": e.name, "type": kind, "size": st.st_size, "mtime": st.st_mtime_ns // 1_000_000})
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    shown = os.path.relpath(path, root).replace(os.sep, "/")
    out = {"path": "" if shown == "." else shown, "entries": entries[:LIST_MAX]}
    if len(entries) > LIST_MAX:
        out["truncated"] = len(entries)
    return out


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


REQUESTS = {"files": files, "read": read}
