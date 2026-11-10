"""tule-agent: the process every Tule machine runs (see DESIGN.md, "Pluggable backends").

It dials a WebSocket out to the project's Durable Object with the join token
and carries everything over it:

- SSH: the Durable Object opens a stream per `tule proxy` client; the agent
  pipes it to the local sshd (127.0.0.1:22).
- Outputs: it watches /workspace/outputs, work/ and inputs/ and sends an index when they change;
  the viewer asks for meshes by file and content hash, which the agent
  tessellates (cached by hash).
- Config: SSH authorized keys and the project's SSH host key come from the
  Durable Object, so the host key survives machine restarts.

Environment: TULE_URL (the control plane, https://...), TULE_PROJECT,
TULE_JOIN_TOKEN, optionally TULE_WORKSPACE (default /workspace).

Wire protocol on the one WebSocket (text frames are JSON, binary frames are
[kind u8][id u32 big-endian][payload]):

  agent -> DO   {"t": "hello", "version": 1, "started": ms}
                {"t": "hostkey", "private": "...", "public": "..."}
                {"t": "outputs", "outputs": [...]}
                {"t": "ssh-close", "sid": n}
                {"t": "mesh-error", "rid": n, "error": "..."}
                {"t": "beat"} every 30 s
                {"t": "ready", "snapshot": id, "files": n, "restoreMs": ms, "from": "copy"|"save"|"template"}
                {"t": "unsaved", "files": n, "commits": n}  work not in the latest save (after autosaves)
                {"t": "reply", "rid": n, ...}  the answer to stop / snapshot / restore / save
                {"t": "res", "rid": n, ...}  the answer to a viewer's request
                {"t": "broadcast", ...}  an event for every viewer
                binary kind 1: SSH bytes for stream id; kind 2: a mesh for request id
  DO -> agent   {"t": "config", "keys": [...], "hostKey": {...} | null}
                {"t": "ssh-open", "sid": n} / {"t": "ssh-close", "sid": n}
                {"t": "mesh", "rid": n, "path": "outputs/bracket/bracket.step", "hash": "..."}
                {"t": "stop" | "snapshot", "rid": n}  save now (stop: the machine is going away)
                {"t": "restore", "rid": n, "id": "..."}  make /workspace match a snapshot
                {"t": "save", "rid": n, "message": "..."}  commit, merge main, push (saves.py)
                {"t": "req", "rid": n, "op": "...", "args": {...}}  a viewer's request (REQUESTS)
                binary kind 1: SSH bytes for stream id
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time

import websockets

import saves
from annotations import REQUESTS as ANNOTATION_REQUESTS
from files import REQUESTS as FILE_REQUESTS, resolve
from workspace import Api, Workspace

VERSION = 1
SSH, MESH, MESH_PART = 1, 2, 3  # MESH_PART: a big binary reply's chunk, before its last one (MESH)
CHUNK = 8 << 20  # one WebSocket message through the control plane stays well under its 32 MiB
WORKSPACE = os.environ.get("TULE_WORKSPACE", "/workspace")
OUTPUTS = os.path.join(WORKSPACE, "outputs")
ETC = os.environ.get("TULE_ETC", "/etc/tule")
TEMPLATE = os.environ.get("TULE_TEMPLATE", "/opt/tule/template")
BOOT_LOG = os.environ.get("TULE_BOOT_LOG", "/var/log/tule-boot.log")
SSHD_PORT = int(os.environ.get("TULE_SSHD_PORT", "22"))
GEOMETRY = (".step", ".stp", ".stl")
STARTED = int(time.time() * 1000)


def log(*a):
    print("tule-agent:", *a, file=sys.stderr, flush=True)


def frame(kind: int, ident: int, payload: bytes) -> bytes:
    return struct.pack(">BI", kind, ident) + payload


# ---------------------------------------------------------------- outputs


class Outputs:
    """The index of the project's outputs/, work/ and inputs/ (every file, by folder),
    whether each output is up to date (tule build's records) and whether agent work in
    work/ needs checking again (tule based-on); and tessellations cached by content hash."""

    ROOTS = ("outputs", "work", "inputs")
    WATCHED = ("src", "lib")  # only for freshness: an edited script makes its output stale

    def __init__(self, root: str):
        self.root = root  # /workspace
        self.stamp = None
        self.hashes: dict[str, tuple[tuple, str]] = {}  # path -> ((size, mtime), sha256)
        self.meshes: dict[str, bytes] = {}
        self.build_hashes = None  # tule_cli.build.Hashes, kept so files are hashed once

    def _walk(self, top: str, out: list, keep=lambda rel: False):
        """(path, size, mtime) of the regular files under top, without hidden ones unless keep(rel)."""
        for d, dirs, files in os.walk(os.path.join(self.root, top)):
            rd = os.path.relpath(d, self.root)
            inside = any(x.startswith(".") and x != "." for x in rd.split(os.sep))  # in a hidden folder: only what keep names
            dirs[:] = sorted(x for x in dirs if (not inside and not x.startswith(".") and x != "__pycache__") or keep(os.path.join(rd, x)))
            for f in sorted(files):
                rel = os.path.join(rd, f)
                if ((inside or f.startswith(".")) and not keep(rel)) or ".tmp" in f:
                    continue
                try:
                    st = os.stat(os.path.join(self.root, rel))
                except OSError:  # gone, a symlink loop, unreadable
                    continue
                if stat.S_ISREG(st.st_mode):  # not a FIFO or a link to /dev/zero: hashing would never end
                    out.append((rel, st.st_size, st.st_mtime_ns))

    def _stamp(self):
        out: list = []
        build = os.path.join("outputs", ".build")
        running = self._running()

        def keep(rel: str) -> bool:
            """In outputs/.build: the records (freshness) and what a running tule build is making
            (a sequence grows there until the script ends); not the hash cache, lock or results."""
            if rel == build:
                return True
            if not rel.startswith(build + os.sep):
                return False
            sub = rel[len(build) + 1:]
            if sub.startswith("stage-"):
                return sub.split(os.sep, 1)[0] in running
            return os.sep not in sub and sub.endswith(".json") and not sub.startswith(("hashes", "result-"))

        self._walk("outputs", out, keep)
        self._walk("work", out, lambda rel: rel == os.path.join("work", ".based-on.json"))
        self._walk("inputs", out)
        for top in self.WATCHED:
            self._walk(top, out)
        return tuple(out)

    def changed(self) -> bool:
        s = self._stamp()
        if s == self.stamp:
            return False
        self.stamp = s
        return True

    def _hash(self, rel: str, size: int, mtime: int) -> str:
        old = self.hashes.get(rel)
        if old and old[0] == (size, mtime):
            return old[1]
        h = hashlib.sha256()
        with open(os.path.join(self.root, rel), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()[:32]
        self.hashes[rel] = ((size, mtime), digest)
        return digest

    def index(self) -> list[dict]:
        """One entry per folder (or loose file) in outputs/, work/ and inputs/:
        {name, root, files: [{path, file, hash, size, basedOn?}], meta, error?, building?, build?}.
        path is relative to /workspace; file to the entry's folder."""
        groups: dict[tuple, dict] = {}
        staged: dict[str, dict] = {}
        running = self._running()
        src = os.path.join(self.root, "src")
        try:  # folders in src/ are groups: src/checks/fit.py makes outputs/checks/fit/
            sections = {f for f in os.listdir(src) if not f.startswith((".", "_")) and os.path.isdir(os.path.join(src, f))}
        except OSError:
            sections = set()
        stamp = self.stamp or ()  # read once: changed() replaces it from another thread
        for rel, size, mtime in stamp:
            root, _, rest = rel.partition(os.sep)
            if root not in self.ROOTS:
                continue
            top, _, rest = rest.partition(os.sep)
            into, key = groups, root
            if root == "outputs" and top == ".build":  # .build/stage-<name>-<pid>/<name>/<file>
                stage, _, rest = rest.partition(os.sep)
                top, _, rest = rest.partition(os.sep)
                if stage not in running or not rest:
                    continue
                into = staged
            elif top.startswith("."):
                continue
            if root == "outputs" and top in sections and rest:
                sub, _, rest = rest.partition(os.sep)
                if not rest or sub.startswith("."):
                    continue
                top = f"{top}/{sub}"
            name = top if rest else os.path.splitext(top)[0]
            g = into.setdefault((key, name), {"name": name, "root": root, "files": [], "meta": None})
            if root == "outputs" and rest == "output.json":
                try:
                    with open(os.path.join(self.root, rel)) as f:
                        g["meta"] = json.load(f)
                    if size > BIG_META and isinstance(g["meta"], dict) and "frames" in g["meta"]:
                        # a long motion study: the index carries the rest, the viewer reads the frames
                        g["meta"].pop("frames")
                        g["meta"]["framesFile"] = {"path": rel.replace(os.sep, "/"), "hash": self._hash(rel, size, mtime), "size": size}
                except (OSError, ValueError) as e:
                    g["error"] = f"output.json: {e}"
                continue
            try:
                digest = self._hash(rel, size, mtime)
            except OSError:
                continue
            g["files"].append({"path": rel.replace(os.sep, "/"), "file": (rest or top).replace(os.sep, "/"), "hash": digest, "size": size})
            g["changed"] = max(g.get("changed", 0), mtime)  # the viewer opens the newest output
        for (_, name), g in staged.items():  # shown instead of the last built one once it has something to show
            if any(f["file"].lower().endswith(GEOMETRY) for f in g["files"]):
                groups[("outputs", name)] = {**g, "building": True}
        self.hashes = {r: self.hashes[r] for r, _, _ in stamp if r in self.hashes}  # of files there are now
        self._freshness(groups, set(running.values()))
        return [g for g in groups.values() if g["files"] or g.get("error") or g.get("build")]

    def _running(self) -> dict[str, str]:
        """tule build's staging folders whose build is still running: {"stage-<name>-<pid>": name}
        (one left by a killed build would otherwise show "building" for ever)."""
        out = {}
        try:
            stages = [n for n in os.listdir(os.path.join(self.root, "outputs", ".build")) if n.startswith("stage-")]
        except OSError:
            return out
        for n in stages:
            name, _, pid = n[6:].rpartition("-")
            try:
                os.kill(int(pid), 0)
            except PermissionError:  # alive, another user's
                pass
            except (ValueError, OSError):
                continue
            out[n] = name.replace("~", "/")  # tule build's key for checks/fit
        return out

    def _freshness(self, groups: dict, running: set):
        """Adds tule build's view of each output (build: {state, why?, error?, log?}) and marks
        agent work in work/ whose sources changed (file basedOn: {sources, changed})."""
        try:
            from tule_cli import build
        except ImportError:  # an image without the CLI: no freshness
            return
        if not os.path.isdir(os.path.join(self.root, "src")):
            return
        try:
            if self.build_hashes is None:
                self.build_hashes = build.Hashes(self.root)
            rows = build.status(self.root, self.build_hashes)
            notes = build.based_on_status(self.root, self.build_hashes)
        except Exception as e:
            log("freshness:", repr(e))
            return
        for r in rows:
            g = groups.get(("outputs", r["name"]))
            state = "building" if r["name"] in running else "failed" if r.get("failed") else "stale" if r.get("why") else "ok"
            if g is None:
                if state == "ok":
                    continue
                g = groups[("outputs", r["name"])] = {"name": r["name"], "root": "outputs", "files": [], "meta": None}
            g["build"] = {"state": state, "script": f"src/{r['name']}.py",
                          **{k: r[k] for k in ("why", "log") if r.get(k)}, **({"error": r["failed"]} if r.get("failed") else {})}
        for n in notes:
            top = n["path"].split("/")[1:2]  # work/<top>…, as index() groups it
            g = top and (groups.get(("work", top[0])) or groups.get(("work", os.path.splitext(top[0])[0])))
            if not g:
                continue
            for f in g["files"]:
                if f["path"] == n["path"] or f["path"].startswith(n["path"] + "/"):
                    f["basedOn"] = {"sources": n["sources"], "changed": n["changed"]}

    def mesh(self, rel: str, digest: str) -> bytes:
        if not rel.startswith(tuple(r + "/" for r in self.ROOTS)):
            rel = "outputs/" + rel  # a page from before paths were relative to /workspace
        path = resolve(self.root, rel)  # as files.read: no "..", no symlinks out of /workspace
        return self.cached(digest, lambda: mesh_apart(path))

    def cached(self, key: str, make) -> bytes:
        if key in self.meshes:
            return self.meshes[key]
        data = make()
        self.meshes[key] = data
        while len(self.meshes) > 1 and sum(map(len, self.meshes.values())) > MESH_CACHE:
            self.meshes.pop(next(iter(self.meshes)), None)
        return data


def expendable(pid: int | str = "self", score: int = 800):
    """When memory runs out, the kernel kills this process before the agent (which would take the
    machine down with it): build scripts and mesh workers, not the machine."""
    try:
        with open(f"/proc/{pid}/oom_score_adj", "w") as f:
            f.write(str(score))
    except OSError:
        pass


def oom_kills() -> int:
    """How many processes the kernel killed for memory in this machine (its cgroup)."""
    try:
        for line in open("/sys/fs/cgroup/memory.events"):
            if line.startswith("oom_kill "):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def memory_limit() -> str:
    try:
        v = open("/sys/fs/cgroup/memory.max").read().strip()
        return "" if v == "max" else f"{int(v) / 2**30:.0f} GiB"
    except (OSError, ValueError):
        return ""


_pool = None
_pool_lock = threading.Lock()
MESH_CACHE = 256 << 20  # bytes of meshes kept for the viewer
MESH_SECONDS = 900
BIG_META = 256 << 10  # an output.json larger than this keeps its frames out of the index


def mesh_apart(path: str) -> bytes:
    """encode_mesh in worker processes: a file that crashes OpenCascade (or never finishes)
    fails that one request instead of taking the machine's agent down with it. A crash is
    tried once more: the pool is shared (another file's crash fails this one too), and a file
    read while it was still being written can crash the reader."""
    try:
        return _mesh_apart(path)
    except _Crashed:
        time.sleep(1)
        try:
            return _mesh_apart(path)
        except _Crashed:
            raise RuntimeError(f"reading {os.path.basename(path)} crashed OpenCascade twice (the machine is fine); "
                               "check it with tule inspect") from None


class _Crashed(Exception):
    pass


def _mesh_apart(path: str) -> bytes:
    global _pool
    import concurrent.futures as cf
    import multiprocessing as mp
    from concurrent.futures.process import BrokenProcessPool

    with _pool_lock:
        if _pool is None:
            _pool = cf.ProcessPoolExecutor(max_workers=2, mp_context=mp.get_context("spawn"), initializer=expendable, initargs=("self", 1000))
        pool = _pool
    name = os.path.basename(path)
    kills = oom_kills()
    try:
        return pool.submit(encode_mesh, path).result(timeout=MESH_SECONDS)
    except (BrokenProcessPool, cf.TimeoutError) as e:
        with _pool_lock:
            if _pool is pool:
                _pool = None
        for p in list(getattr(pool, "_processes", {}).values()):  # a stuck one too
            p.kill()
        pool.shutdown(wait=False, cancel_futures=True)
        if isinstance(e, cf.TimeoutError):
            raise RuntimeError(f"{name} took over {MESH_SECONDS // 60} minutes to show; make it lighter (fewer faces, or STL)") from None
        if oom_kills() > kills:
            mem = memory_limit()
            raise RuntimeError(f"showing {name} ran out of memory{f' ({mem})' if mem else ''}; the machine is fine. Make it lighter "
                               "(simplified purchased parts, fewer faces, or STL for display), or use a bigger machine") from None
        raise _Crashed() from None


def encode_mesh(path: str) -> bytes:
    """A file's parts as one binary message: [u32 header length][JSON header][arrays].

    Header: {"parts": [{"name", "color", "tags": {tag: [face ids]},
    "positions": [offset, length], "normals", "indices", "faceIds", "edges"}]}
    where offsets are bytes into the array section; positions/normals are
    float32 xyz, indices/faceIds uint32, edges float32 line segments (pairs of
    points) with "edgeIds" uint32 per segment.
    """
    import numpy as np

    arrays: list[bytes] = []
    offset = 0

    def put(a) -> list[int]:
        nonlocal offset
        b = np.ascontiguousarray(a).tobytes()
        arrays.append(b)
        start = offset
        offset += len(b)
        return [start, len(b)]

    parts = []
    if path.lower().endswith(".stl"):
        tri = read_stl(path)
        n = len(tri)
        e1, e2 = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
        nor = np.cross(e1, e2)
        nor /= np.maximum(np.linalg.norm(nor, axis=1, keepdims=True), 1e-20)
        parts.append({
            "name": os.path.splitext(os.path.basename(path))[0],
            "color": None,
            "tags": {},
            "positions": put(tri.reshape(-1, 3).astype(np.float32)),
            "normals": put(np.repeat(nor, 3, axis=0).astype(np.float32)),
            "indices": put(np.arange(3 * n, dtype=np.uint32)),
            "faceIds": put(np.zeros(n, dtype=np.uint32)),
        })
    else:
        from tule import Assembly, import_step, tessellate

        item = import_step(path)
        shapes = item.parts() if isinstance(item, Assembly) else [(item.name or "", item)]
        for name, shp in shapes:
            m = tessellate(shp)
            segs, ids = [], []
            for eid, poly in m.edges:
                if len(poly) < 2:
                    continue
                segs.append(np.stack([poly[:-1], poly[1:]], axis=1).reshape(-1, 3))
                ids.append(np.full(len(poly) - 1, eid, dtype=np.uint32))
            tags = {t: shp.faces[t].ids for t in shp.tags if _faces_ok(shp, t)}
            parts.append({
                "name": name or shp.name or os.path.splitext(os.path.basename(path))[0],
                "color": shp.color,
                "tags": tags,
                "positions": put(m.positions),
                "normals": put(m.normals),
                "indices": put(m.indices.reshape(-1)),
                "faceIds": put(m.face_ids),
                "edges": put(np.concatenate(segs).astype(np.float32) if segs else np.zeros((0, 3), np.float32)),
                "edgeIds": put(np.concatenate(ids) if ids else np.zeros(0, np.uint32)),
            })
    header = json.dumps({"parts": parts}).encode()
    header += b" " * (-len(header) % 4)
    return struct.pack("<I", len(header)) + header + b"".join(arrays)


def _faces_ok(shp, tag) -> bool:
    try:
        return bool(shp.faces[tag].items)
    except Exception:
        return False


def read_stl(path: str):
    """(n, 3, 3) float32 triangles from a binary or ASCII STL."""
    import numpy as np

    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * n == len(data):
            rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]), count=n, offset=84)
            return rec["v"].astype(np.float32)
    pts = [list(map(float, line.split()[1:4])) for line in data.decode(errors="replace").splitlines() if line.strip().startswith("vertex")]
    return np.asarray(pts, dtype=np.float32).reshape(-1, 3, 3)


# ---------------------------------------------------------------- sshd


class Sshd:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.key = os.path.join(ETC, "ssh_host_ed25519_key")
        self.auth = os.path.join(ETC, "authorized_keys")

    def configure(self, keys: list[str], host_key: dict | None) -> dict | None:
        """Writes the keys; returns a new host key to store if the project had none."""
        os.makedirs(ETC, exist_ok=True)
        _write(self.auth, "".join(k.strip() + "\n" for k in keys), 0o600)
        new = None
        if host_key:
            _write(self.key, host_key["private"], 0o600)
            _write(self.key + ".pub", host_key["public"], 0o644)
        elif not os.path.exists(self.key):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "tule", "-f", self.key], check=True)
            new = {"private": open(self.key).read(), "public": open(self.key + ".pub").read()}
        self.start()
        return new

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        try:
            os.makedirs("/run/sshd", exist_ok=True)  # privilege separation (as root)
        except OSError:
            pass
        cmd = [sshd, "-D", "-e", "-p", str(SSHD_PORT), "-o", "ListenAddress=127.0.0.1", "-h", self.key,
               "-o", f"AuthorizedKeysFile={self.auth}", "-o", "PasswordAuthentication=no",
               "-o", "KbdInteractiveAuthentication=no", "-o", "PermitRootLogin=prohibit-password",
               "-o", "StrictModes=no", "-o", "PidFile=none", "-o", "SetEnv=TULE_WORKSPACE=" + WORKSPACE]
        self.proc = subprocess.Popen(cmd)
        expendable(self.proc.pid, 500)  # and every session it starts (tule build, the agent's scripts)
        log("sshd started", self.proc.pid)


class Stream:
    """One SSH session: bytes from the client are buffered until sshd accepts the connection."""

    def __init__(self):
        self.writer: asyncio.StreamWriter | None = None
        self.early: list[bytes] = []

    def attach(self, writer: asyncio.StreamWriter):
        self.writer = writer
        for b in self.early:
            writer.write(b)
        self.early.clear()

    def write(self, data: bytes):
        if self.writer is None:
            self.early.append(data)
        else:
            self.writer.write(data)

    def close(self):
        if self.writer is not None:
            self.writer.close()


def _write(path: str, text: str, mode: int):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---------------------------------------------------------------- the connection


class Agent:
    def __init__(self, url: str, project: str, token: str):
        base = url.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        self.url = f"{base}/api/projects/{project}/agent"
        self.token = token
        self.sshd = Sshd()
        self.outputs = Outputs(WORKSPACE)
        self.streams: dict[int, Stream] = {}
        self.ws = None
        self.workspace = Workspace(WORKSPACE, Api(url, project, token), log=lambda *a: log(*a))
        self.git = (url, project, token)  # for saves.setup
        self.reported: dict | None = None  # the last unsaved counts sent
        self.lock = asyncio.Lock()  # one snapshot or restore at a time
        self.booted: dict | None = None  # the ready message, once /workspace is restored

    async def run(self):
        delay = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                    max_size=64 * 2**20, ping_interval=20, ping_timeout=20, open_timeout=20,
                ) as ws:
                    self.ws = ws
                    delay = 1.0
                    log("connected", self.url)
                    await ws.send(json.dumps({"t": "hello", "version": VERSION, "started": STARTED}))
                    tasks = [asyncio.create_task(self.watch()), asyncio.create_task(self.beat())]
                    try:
                        async for msg in ws:
                            await self.handle(msg)
                    finally:
                        for t in tasks:
                            t.cancel()
            except Exception as e:  # also a boot that failed (storage, git): reconnect and try again, not exit
                log("connection lost:", repr(e))
            self.ws = None
            for w in self.streams.values():
                w.close()
            self.streams.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)

    async def send(self, m):
        if self.ws is not None:
            await self.ws.send(m if isinstance(m, bytes) else json.dumps(m))

    async def send_bytes(self, rid: int, data: bytes):
        """A binary reply in chunks of CHUNK (the last one MESH), so no message is too big to relay."""
        view = memoryview(data)
        for at in range(0, max(len(data) - CHUNK, 0), CHUNK):
            await self.send(frame(MESH_PART, rid, bytes(view[at:at + CHUNK])))
        last = (max(len(data) - 1, 0) // CHUNK) * CHUNK
        await self.send(frame(MESH, rid, bytes(view[last:])))

    async def handle(self, msg):
        if isinstance(msg, bytes):
            kind, ident = struct.unpack_from(">BI", msg)
            if kind == SSH and ident in self.streams:
                self.streams[ident].write(msg[5:])
            return
        m = json.loads(msg)
        t = m.get("t")
        if t == "config":
            if self.booted is None:
                async with self.lock:
                    self.booted = await asyncio.to_thread(self.boot)
                asyncio.create_task(self.sync())
            new = await asyncio.to_thread(self.sshd.configure, m.get("keys", []), m.get("hostKey"))
            if new:
                await self.send({"t": "hostkey", **new})
            await self.send(self.booted)
            self.reported = None
            await self.report()
        elif t in ("stop", "snapshot", "restore", "save"):
            asyncio.create_task(self.command(m))
        elif t == "req":
            asyncio.create_task(self.request(m))
        elif t == "ssh-open":
            self.streams[m["sid"]] = Stream()
            asyncio.create_task(self.ssh(m["sid"]))
        elif t == "ssh-close":
            w = self.streams.pop(m["sid"], None)
            if w:
                w.close()
        elif t == "mesh":
            asyncio.create_task(self.mesh(m["rid"], m["path"], m.get("hash", "")))

    async def ssh(self, sid: int):
        if self.sshd.proc and self.sshd.proc.poll() is not None:  # killed (out of memory): start it again
            await asyncio.to_thread(self.sshd.start)
            await asyncio.sleep(0.3)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", SSHD_PORT)
        except OSError as e:
            log("ssh connect failed:", e)
            self.streams.pop(sid, None)
            await self.send({"t": "ssh-close", "sid": sid})
            return
        stream = self.streams.get(sid)
        if stream is None:  # closed while connecting
            writer.close()
            return
        stream.attach(writer)
        try:
            while data := await reader.read(65536):
                await self.send(frame(SSH, sid, data))
        except (OSError, websockets.WebSocketException):
            pass
        finally:
            if self.streams.pop(sid, None) is not None:
                writer.close()
                await self.send({"t": "ssh-close", "sid": sid})

    async def mesh(self, rid: int, path: str, digest: str):
        try:
            data = await asyncio.to_thread(self.outputs.mesh, path, digest)
            await self.send_bytes(rid, data)
        except Exception as e:  # report every failure to the viewer
            await self.send({"t": "mesh-error", "rid": rid, "error": f"{type(e).__name__}: {e}"})

    def boot(self) -> dict:
        """This machine's working copy: its own last autosave (it is coming back), else
        the latest save (git main), else a new project from the template (saved as main)."""
        t0 = time.time()
        git_ok = saves.available()
        if git_ok:
            try:
                saves.setup(WORKSPACE, *self.git)
            except Exception as e:  # git is optional: autosave still works
                log("git setup failed:", repr(e))
                git_ok = False
        m = self.workspace.api.manifest("latest")
        sid, n, source = None, 0, "copy"
        if m is not None:
            sid = m.get("id")
            n = self.workspace.restore(m)
        elif git_ok and saves.has_main(WORKSPACE):
            source = "save"
            saves.start_from_main(WORKSPACE)
            sid = self.workspace.snapshot("started from the latest save", force=True)
        else:
            source = "template"
            new_project(WORKSPACE)
            if git_ok:
                saves.configure(WORKSPACE)
                if not saves.push_new(WORKSPACE):
                    log("could not save the new project to its remote; it stays unsaved")
            sid = self.workspace.snapshot("new project", force=True)
        if git_ok:
            saves.configure(WORKSPACE)
        os.makedirs(OUTPUTS, exist_ok=True)
        ms = int((time.time() - t0) * 1000)
        log(f"workspace ready ({source}): snapshot {sid}, {n} files written, {ms} ms")
        install_packages(WORKSPACE)
        return {"t": "ready", "snapshot": sid, "files": n, "restoreMs": ms, "from": source}

    async def report(self):
        """Sends the unsaved counts when they changed."""
        try:
            u = await asyncio.to_thread(saves.unsaved, WORKSPACE)
        except Exception as e:
            log("unsaved check failed:", repr(e))
            return
        if u != self.reported:
            self.reported = u
            await self.send({"t": "unsaved", **u})

    async def sync(self):
        """Saves /workspace a few seconds after it stops changing, and at least every minute while it changes."""
        stamp = await asyncio.to_thread(self.workspace.stamp)
        changed_at, saved_at, dirty = 0.0, time.time(), False
        while True:
            await asyncio.sleep(2)
            s = await asyncio.to_thread(self.workspace.stamp)
            now = time.time()
            if s != stamp:
                stamp, changed_at, dirty = s, now, True
            if dirty and (now - changed_at >= 3 or now - saved_at >= 60):
                try:
                    async with self.lock:
                        await asyncio.to_thread(self.workspace.snapshot, "change")
                    dirty, saved_at = False, now
                    await self.report()
                except Exception as e:  # keep the changes dirty; the next round retries
                    log("snapshot failed:", repr(e))

    async def command(self, m: dict):
        t, rid = m["t"], m.get("rid")
        if self.booted is None:
            await self.send({"t": "reply", "rid": rid, "error": "the workspace is not restored yet"})
            return
        try:
            async with self.lock:
                if t == "restore":
                    await asyncio.to_thread(self.workspace.snapshot, "before restore")
                    manifest = await asyncio.to_thread(self.workspace.api.manifest, m["id"])
                    if manifest is None:
                        raise RuntimeError(f"no snapshot {m['id']}")
                    n = await asyncio.to_thread(self.workspace.restore, manifest)
                    reply = {"snapshot": m["id"], "files": n}
                elif t == "save":
                    reply = await asyncio.to_thread(saves.save, WORKSPACE, m.get("message") or "")
                    if not reply.get("error"):
                        await asyncio.to_thread(self.workspace.snapshot, "saved")
                else:
                    sid = await asyncio.to_thread(self.workspace.snapshot, t if t == "stop" else m.get("reason", "manual"))
                    reply = {"snapshot": sid or self.workspace.last_id, "changed": sid is not None}
            await self.send({"t": "reply", "rid": rid, **reply})
            await self.report()
        except Exception as e:
            await self.send({"t": "reply", "rid": rid, "error": f"{type(e).__name__}: {e}"})

    async def final(self):
        """SIGTERM: save, then exit (never before /workspace was restored: that would save an empty project)."""
        if self.booted is None:
            os._exit(0)
        try:
            async with self.lock:
                await asyncio.wait_for(asyncio.to_thread(self.workspace.snapshot, "shutdown"), 600)
        except Exception as e:
            log("final snapshot failed:", repr(e))
        os._exit(0)

    async def request(self, m: dict):
        """A viewer's request ({t: "req", rid, op, args}): REQUESTS[op](self, args) runs in a
        thread and returns a dict (sent as {t: "res", rid, ...}) or bytes (binary kind 2)."""
        rid, op = m.get("rid"), m.get("op")
        fn = REQUESTS.get(op)
        try:
            if fn is None:
                raise ValueError(f"unknown request {op!r}; known: {', '.join(sorted(REQUESTS))}")
            out = await asyncio.to_thread(fn, self, m.get("args") or {})
            if isinstance(out, (bytes, bytearray)):
                await self.send_bytes(rid, bytes(out))
            else:
                event = (out or {}).pop("broadcast", None)  # a handler's event for every viewer
                await self.send({"t": "res", "rid": rid, **(out or {})})
                if event:
                    await self.send({"t": "broadcast", **event})
        except Exception as e:  # every failure goes back to the page
            await self.send({"t": "res", "rid": rid, "error": f"{type(e).__name__}: {e}"})

    async def beat(self):
        """Keeps the Durable Object awake (protocol pings do not wake it), so the platform does not stop the machine."""
        while True:
            await asyncio.sleep(30)
            await self.send({"t": "beat"})

    async def watch(self):
        # not before /workspace is restored: an empty index would empty every viewer's list
        while self.booted is None:
            await asyncio.sleep(0.2)
        os.makedirs(OUTPUTS, exist_ok=True)
        fresh = True  # a new connection: send the whole index (changes while offline never reached it)
        while True:
            try:
                if await asyncio.to_thread(self.outputs.changed) or fresh:
                    fresh = False
                    index = await asyncio.to_thread(self.outputs.index)
                    await self.send({"t": "outputs", "outputs": index})
            except Exception as e:  # a bad file must not end the index for good; the next change retries
                log("outputs index:", repr(e))
            await asyncio.sleep(0.5)


def _ping(agent: "Agent", args: dict) -> dict:
    return {"pong": True, "workspace": WORKSPACE}


# viewer requests by op (see Agent.request): a handler returns a dict or bytes;
# a dict's "broadcast" entry is an event for every viewer. files.py: read
# (the viewer's Download and file views); annotations.py: annotate, annotations.
REQUESTS = {"ping": _ping, **FILE_REQUESTS, **ANNOTATION_REQUESTS}


def new_project(root: str):
    """A new project: the template (AGENTS.md, CLAUDE.md, tule.toml, src/part.py) and a git repository."""
    os.makedirs(root, exist_ok=True)
    if os.path.isdir(TEMPLATE):
        shutil.copytree(TEMPLATE, root, dirs_exist_ok=True)
    if shutil.which("git") and not os.path.exists(os.path.join(root, ".git")):
        env = {**os.environ, "GIT_AUTHOR_NAME": "Tule", "GIT_AUTHOR_EMAIL": "tule@localhost",
               "GIT_COMMITTER_NAME": "Tule", "GIT_COMMITTER_EMAIL": "tule@localhost"}
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "New Tule project"]):
            subprocess.run(cmd, cwd=root, env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install_packages(root: str):
    """apt and pip packages from tule.toml ([packages] apt = [...], pip = [...]), in the
    background (logged to BOOT_LOG)."""
    try:
        import tomllib

        with open(os.path.join(root, "tule.toml"), "rb") as f:
            want = tomllib.load(f).get("packages", {})
        apt = [str(p) for p in want.get("apt", [])]
        pip = [str(p) for p in want.get("pip", [])]
    except (OSError, ValueError, ImportError, AttributeError, TypeError):
        return
    steps = []
    if apt and shutil.which("apt-get"):
        steps.append("apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "
                     + " ".join(shlex.quote(p) for p in apt))
    if pip:
        steps.append(f"{shlex.quote(sys.executable)} -m pip install --quiet " + " ".join(shlex.quote(p) for p in pip))
    if not steps:
        return
    try:
        out = open(BOOT_LOG, "a")
    except OSError:
        out = subprocess.DEVNULL
    subprocess.Popen(["sh", "-c", "; ".join(steps)], stdout=out, stderr=out)
    log("installing from tule.toml:", " ".join(apt + pip))


def main():
    url, project, token = (os.environ.get(k) for k in ("TULE_URL", "TULE_PROJECT", "TULE_JOIN_TOKEN"))
    if not (url and project and token):
        sys.exit("tule-agent: set TULE_URL, TULE_PROJECT and TULE_JOIN_TOKEN")

    async def run():
        agent = Agent(url, project, token)
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(agent.final()))
        import local  # the control socket for commands on the machine, e.g. `tule snapshot` (runtime/local.py)
        agent.local = asyncio.create_task(local.serve(agent))
        await agent.run()

    asyncio.run(run())


if __name__ == "__main__":
    main()
