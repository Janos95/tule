"""Saving and restoring /workspace (see DESIGN.md, "The workspace").

A snapshot is a manifest of every file under /workspace: its content as a
list of chunk hashes (sha256 of up to 16 MiB each), size, executable bit and
mtime; symlinks by target; empty directories by name. Chunks are stored once
per project in R2 (content-addressed), so a snapshot only uploads new bytes.
The machine has no storage credentials: it moves chunks through the control
plane with its join token.

    /api/projects/<p>/blobs/missing   POST {"hashes": [...]} -> {"missing": [...]}
    /api/projects/<p>/blobs/<hash>    PUT the bytes / GET them
    /api/projects/<p>/snapshots       POST {"reason", "manifest"} -> {"id"}
    /api/projects/<p>/snapshots/<id>  GET the manifest ("latest" for the newest)

Ignored: __pycache__, .pytest_cache, and the patterns in /workspace/.tuleignore
(fnmatch, one per line, matched against the relative path and the name).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CHUNK = 16 << 20
IGNORE = ["__pycache__", ".pytest_cache", "*.tule-tmp"]


class Api:
    """The control plane's workspace endpoints, authenticated with the join token."""

    def __init__(self, url: str, project: str, token: str):
        self.base = f"{url.rstrip('/')}/api/projects/{project}"
        self.token = token

    def call(self, method: str, path: str, data: bytes | None = None, ctype="application/json", timeout=120):
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": ctype, "User-Agent": "tule-agent",
        })
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code < 500 or attempt == 3:
                    raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:200]!r}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 3:
                    raise
            time.sleep(0.5 * 2**attempt)

    def missing(self, hashes: list[str]) -> set[str]:
        out: set[str] = set()
        for i in range(0, len(hashes), 500):
            r = self.call("POST", "/blobs/missing", json.dumps({"hashes": hashes[i:i + 500]}).encode())
            out |= set(json.loads(r)["missing"])
        return out

    def put(self, h: str, data: bytes):
        self.call("PUT", f"/blobs/{h}", data, "application/octet-stream", timeout=300)

    def get(self, h: str) -> bytes:
        data = self.call("GET", f"/blobs/{h}", timeout=300)
        if data is None or hashlib.sha256(data).hexdigest() != h:
            raise RuntimeError(f"chunk {h[:12]} is missing or corrupt")
        return data

    def snapshot(self, manifest: dict, reason: str) -> str:
        r = self.call("POST", "/snapshots", json.dumps({"reason": reason, "manifest": manifest}).encode())
        return json.loads(r)["id"]

    def manifest(self, sid: str = "latest") -> dict | None:
        r = self.call("GET", f"/snapshots/{sid}")
        return json.loads(r) if r else None


class Workspace:
    def __init__(self, root: str, api: Api, log=print):
        self.root = root
        self.api = api
        self.log = log
        self.cache: dict[str, tuple[tuple, list[str]]] = {}  # rel -> ((size, mtime_ns, ino), chunk hashes)
        self.last: dict | None = None  # the manifest of the last snapshot
        self.last_id: str | None = None
        self.pool = ThreadPoolExecutor(8)

    # ------------------------------------------------------------ scanning

    def _patterns(self) -> list[str]:
        pats = list(IGNORE)
        try:
            with open(os.path.join(self.root, ".tuleignore")) as f:
                pats += [x.strip() for x in f if x.strip() and not x.startswith("#")]
        except OSError:
            pass
        return pats

    def _ignored(self, rel: str, name: str, pats) -> bool:
        return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.rstrip("/")) for p in pats)

    def walk(self):
        """(rel, lstat) for every kept file, symlink and empty directory."""
        pats = self._patterns()
        for d, dirs, files in os.walk(self.root):
            reld = os.path.relpath(d, self.root)
            reld = "" if reld == "." else reld
            keep = []
            for x in sorted(dirs):
                rel = os.path.join(reld, x)
                if self._ignored(rel, x, pats):
                    continue
                p = os.path.join(d, x)
                if os.path.islink(p):  # a symlink to a directory: keep the link, do not descend
                    files.append(x)
                    continue
                keep.append(x)
            dirs[:] = keep
            if reld and not keep and not files:
                yield reld, None
            for x in sorted(files):
                rel = os.path.join(reld, x)
                if self._ignored(rel, x, pats):
                    continue
                try:
                    yield rel, os.lstat(os.path.join(d, x))
                except FileNotFoundError:
                    continue

    def stamp(self) -> tuple:
        return tuple((rel, st.st_size, st.st_mtime_ns) if st else (rel,) for rel, st in self.walk())

    def _chunks(self, rel: str, st) -> list[str]:
        key = (st.st_size, st.st_mtime_ns, st.st_ino)
        old = self.cache.get(rel)
        if old and old[0] == key:
            return old[1]
        out = []
        with open(os.path.join(self.root, rel), "rb") as f:
            while True:
                b = f.read(CHUNK)
                if not b and out:
                    break
                out.append(hashlib.sha256(b).hexdigest())
                if len(b) < CHUNK:
                    break
        self.cache[rel] = (key, out)
        return out

    def manifest(self) -> dict:
        files, links, dirs = {}, {}, []
        for rel, st in self.walk():
            if st is None:
                dirs.append(rel)
            elif stat.S_ISLNK(st.st_mode):
                links[rel] = os.readlink(os.path.join(self.root, rel))
            elif stat.S_ISREG(st.st_mode):
                try:
                    h = self._chunks(rel, st)
                except (FileNotFoundError, PermissionError):
                    continue
                files[rel] = {"h": h, "s": st.st_size, "x": bool(st.st_mode & 0o111), "t": st.st_mtime_ns // 1_000_000}
        return {"version": 1, "files": files, "links": links, "dirs": dirs}

    # ------------------------------------------------------------ save

    def snapshot(self, reason: str, force: bool = False) -> str | None:
        """Uploads new chunks and records a snapshot; None when nothing changed."""
        t0 = time.time()
        m = self.manifest()
        if not force and self.last is not None and _same(m, self.last):
            return None
        where = {}  # chunk -> every (file, index) that has it
        for rel, f in m["files"].items():
            for i, h in enumerate(f["h"]):
                where.setdefault(h, []).append((rel, i))
        missing = self.api.missing(sorted(where)) if where else set()

        moved = set()  # files that changed while this ran (a build writing): the next snapshot has them

        def upload(h):
            for rel, i in where[h]:
                try:
                    with open(os.path.join(self.root, rel), "rb") as fh:
                        fh.seek(i * CHUNK)
                        data = fh.read(CHUNK)
                except FileNotFoundError:
                    data = b""
                if hashlib.sha256(data).hexdigest() == h:
                    self.api.put(h, data)
                    return len(data)
            moved.update(rel for rel, _ in where[h])
            return 0

        sent = sum(self.pool.map(upload, sorted(missing)))
        for rel in moved:  # keep what the last snapshot had of it (or leave it out)
            self.cache.pop(rel, None)
            old = (self.last or {}).get("files", {}).get(rel)
            if old:
                m["files"][rel] = old
            else:
                m["files"].pop(rel, None)
        sid = self.api.snapshot(m, reason)
        self.last, self.last_id = m, sid
        self.log(f"snapshot {sid} ({reason}): {len(m['files'])} files, {len(missing)} new chunks, {sent >> 10} KiB, {time.time() - t0:.1f} s")
        return sid

    # ------------------------------------------------------------ restore

    def restore(self, m: dict) -> int:
        """Makes /workspace match manifest `m`; returns the number of files written."""
        os.makedirs(self.root, exist_ok=True)
        want = set(m["files"]) | set(m.get("links", {}))
        # remove what the snapshot does not have
        for rel, st in list(self.walk()):
            if st is not None and rel not in want:
                os.remove(os.path.join(self.root, rel))
        todo = []
        for rel, f in m["files"].items():
            p = os.path.join(self.root, rel)
            try:
                st = os.lstat(p)
                if stat.S_ISREG(st.st_mode) and self._chunks(rel, st) == f["h"]:
                    continue
            except FileNotFoundError:
                pass
            todo.append((rel, f))

        def fetch(item):
            rel, f = item
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tule-tmp"
            with open(tmp, "wb") as out:
                for h in f["h"]:
                    out.write(self.api.get(h))
            os.chmod(tmp, 0o755 if f.get("x") else 0o644)
            os.utime(tmp, ns=(f["t"] * 1_000_000, f["t"] * 1_000_000))
            if os.path.isdir(p) and not os.path.islink(p):
                os.rmdir(p)
            os.replace(tmp, p)
            return rel

        for rel in self.pool.map(fetch, todo):
            st = os.lstat(os.path.join(self.root, rel))
            self.cache[rel] = ((st.st_size, st.st_mtime_ns, st.st_ino), m["files"][rel]["h"])
        for rel, target in m.get("links", {}).items():
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if os.path.lexists(p):
                if os.path.islink(p) and os.readlink(p) == target:
                    continue
                os.remove(p)
            os.symlink(target, p)
        for rel in m.get("dirs", []):
            os.makedirs(os.path.join(self.root, rel), exist_ok=True)
        # directories emptied by the removals above, unless the snapshot has them
        keep = set(m.get("dirs", [])) | {"outputs"}
        pats = self._patterns()
        for d, dirs, files in os.walk(self.root, topdown=False):
            rel = os.path.relpath(d, self.root)
            if rel != "." and rel not in keep and not os.listdir(d) and not self._ignored(rel, os.path.basename(d), pats):
                os.rmdir(d)
        self.last, self.last_id = m, m.get("id")
        return len(todo)


def _same(a: dict, b: dict) -> bool:
    strip = lambda m: ({k: (v["h"], v["x"]) for k, v in m["files"].items()}, m.get("links"), sorted(m.get("dirs", [])))
    return strip(a) == strip(b)
