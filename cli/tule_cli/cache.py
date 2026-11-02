"""The project's build cache: outputs built on one machine, for the others.

After `tule build` makes an output it uploads it: the build record (what the script read,
with hashes) and the output's files as chunks (sha256 of up to 16 MiB each, stored once per
project, like snapshots). Before running a script, `tule build` looks for an upload whose
record matches the files here and downloads it instead. So a study built on a GPU machine
(or one of your computers) is there on the others without running it again.

Only on a Tule machine: it signs in with the machine's join token from /etc/tule/machine.json.

    /api/projects/<p>/cache            GET {entries} / POST {name, key, record, files}
    /api/projects/<p>/blobs/missing    POST {"hashes": [...]} -> {"missing": [...]}
    /api/projects/<p>/blobs/<sha256>   PUT the bytes / GET them
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MACHINE = os.environ.get("TULE_MACHINE_FILE", "/etc/tule/machine.json")
CHUNK = 16 << 20


class Cache:
    def __init__(self, url: str, project: str, token: str):
        self.base = f"{url.rstrip('/')}/api/projects/{project}"
        self.token = token
        self._entries: dict[str, list[dict]] | None = None

    @classmethod
    def here(cls) -> Cache | None:
        """The cache of this machine's project; None off a Tule machine."""
        try:
            with open(MACHINE) as f:
                m = json.load(f)
            return cls(m["url"], m["project"], m["token"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def call(self, method: str, path: str, data: bytes | None = None, ctype="application/json", timeout=120):
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": ctype, "User-Agent": "tule-build",
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code < 500 or attempt == 2:
                    try:
                        why = json.loads(e.read()).get("error")
                    except ValueError:
                        why = None
                    raise RuntimeError(f"{method} {path.split('?')[0]}: {why or e.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == 2:
                    raise RuntimeError(f"{method} {path.split('?')[0]}: {e}") from None
            time.sleep(0.5 * 2**attempt)

    def entries(self, name: str) -> list[dict]:
        """Uploads of outputs/<name>, newest first (all of them are fetched once per build)."""
        if self._entries is None:
            r = self.call("GET", "/cache")
            self._entries = {}
            for e in json.loads(r)["entries"] if r else []:
                self._entries.setdefault(e.get("name"), []).append(e)
        return self._entries.get(name, [])

    # ------------------------------------------------------------ upload

    def upload(self, root: str, name: str, record: dict):
        top = os.path.join(root, "outputs", name)
        files: dict[str, dict] = {}
        where: dict[str, tuple[str, int]] = {}  # chunk -> (path, offset), to read it again for the upload
        for d, dirs, names in os.walk(top):
            dirs.sort()
            for n in sorted(names):
                p = os.path.join(d, n)
                st = os.lstat(p)
                if not stat.S_ISREG(st.st_mode):
                    continue
                chunks = []
                with open(p, "rb") as f:
                    for off in range(0, max(st.st_size, 1), CHUNK):
                        h = hashlib.sha256(f.read(CHUNK)).hexdigest()
                        chunks.append(h)
                        where.setdefault(h, (p, off))
                files[os.path.relpath(p, top).replace(os.sep, "/")] = {
                    "chunks": chunks, "size": st.st_size, **({"x": True} if st.st_mode & 0o111 else {})}
        hashes = list(where)
        missing: set[str] = set()
        for i in range(0, len(hashes), 1000):
            r = self.call("POST", "/blobs/missing", json.dumps({"hashes": hashes[i:i + 1000]}).encode())
            missing |= set(json.loads(r)["missing"])

        def put(h: str):
            p, off = where[h]
            with open(p, "rb") as f:
                f.seek(off)
                self.call("PUT", f"/blobs/{h}", f.read(CHUNK), "application/octet-stream", timeout=300)

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(put, sorted(missing)))
        key = hashlib.sha256(json.dumps({"env": record.get("env"), "reads": record["reads"]}, sort_keys=True).encode()).hexdigest()
        self.call("POST", "/cache", json.dumps({"name": name, "key": key, "record": record, "files": files}).encode())

    # ------------------------------------------------------------ download

    def download(self, entry: dict, dest: str):
        """Writes the entry's files into dest (a new folder)."""
        os.makedirs(dest)
        files = entry.get("files") or {}
        for rel in files:
            parts = rel.split("/")
            if not rel or rel.startswith("/") or any(x in ("", ".", "..") for x in parts):
                raise RuntimeError(f"a cached file has a bad path: {rel!r}")

        def get(h: str) -> bytes:
            data = self.call("GET", f"/blobs/{h}", timeout=300)
            if data is None or hashlib.sha256(data).hexdigest() != h:
                raise RuntimeError(f"chunk {h[:12]} is missing or corrupt")
            return data

        with ThreadPoolExecutor(8) as pool:
            for rel, f in files.items():
                p = os.path.join(dest, *rel.split("/"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as out:
                    for data in pool.map(get, f["chunks"] if f.get("size") else []):
                        out.write(data)
                if f.get("x"):
                    os.chmod(p, 0o755)
