"""tule-agent: the process every Tule machine runs (see DESIGN.md, "Pluggable backends").

It dials a WebSocket out to the project's Durable Object with the join token
and carries everything over it:

- SSH: the Durable Object opens a stream per `tule proxy` client; the agent
  pipes it to the local sshd (127.0.0.1:22).
- Outputs: it watches /workspace/outputs and sends an index when it changes;
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
                binary kind 1: SSH bytes for stream id; kind 2: a mesh for request id
  DO -> agent   {"t": "config", "keys": [...], "hostKey": {...} | null}
                {"t": "ssh-open", "sid": n} / {"t": "ssh-close", "sid": n}
                {"t": "mesh", "rid": n, "path": "bracket/bracket.step", "hash": "..."}
                binary kind 1: SSH bytes for stream id
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time

import websockets

VERSION = 1
SSH, MESH = 1, 2
WORKSPACE = os.environ.get("TULE_WORKSPACE", "/workspace")
OUTPUTS = os.path.join(WORKSPACE, "outputs")
ETC = os.environ.get("TULE_ETC", "/etc/tule")
SSHD_PORT = int(os.environ.get("TULE_SSHD_PORT", "22"))
GEOMETRY = (".step", ".stp", ".stl")
STARTED = int(time.time() * 1000)


def log(*a):
    print("tule-agent:", *a, file=sys.stderr, flush=True)


def frame(kind: int, ident: int, payload: bytes) -> bytes:
    return struct.pack(">BI", kind, ident) + payload


# ---------------------------------------------------------------- outputs


class Outputs:
    """The index of /workspace/outputs and tessellations cached by content hash."""

    def __init__(self, root: str):
        self.root = root
        self.stamp = None
        self.hashes: dict[str, tuple[tuple, str]] = {}  # path -> ((size, mtime), sha256)
        self.meshes: dict[str, bytes] = {}

    def _stamp(self):
        out = []
        for d, dirs, files in os.walk(self.root):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for f in sorted(files):
                if f.startswith(".") or ".tmp" in f:
                    continue
                p = os.path.join(d, f)
                try:
                    st = os.stat(p)
                except FileNotFoundError:
                    continue
                out.append((os.path.relpath(p, self.root), st.st_size, st.st_mtime_ns))
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
        """One entry per output folder (or loose file directly in outputs/)."""
        groups: dict[str, dict] = {}
        for rel, size, mtime in self.stamp or ():
            top, _, rest = rel.partition(os.sep)
            name = top if rest else os.path.splitext(top)[0]
            g = groups.setdefault(name, {"name": name, "files": [], "meta": None})
            if rest == "output.json":
                try:
                    with open(os.path.join(self.root, rel)) as f:
                        g["meta"] = json.load(f)
                except (OSError, ValueError) as e:
                    g["error"] = f"output.json: {e}"
                continue
            if not rel.lower().endswith(GEOMETRY):
                continue
            try:
                digest = self._hash(rel, size, mtime)
            except FileNotFoundError:
                continue
            g["files"].append({"path": rel.replace(os.sep, "/"), "file": rest or top, "hash": digest, "size": size})
        return [g for g in groups.values() if g["files"] or g.get("error")]

    def mesh(self, rel: str, digest: str) -> bytes:
        if digest in self.meshes:
            return self.meshes[digest]
        path = os.path.normpath(os.path.join(self.root, rel))
        if not path.startswith(self.root + os.sep):
            raise ValueError(f"{rel}: outside outputs/")
        data = encode_mesh(path)
        if len(self.meshes) > 64:
            self.meshes.pop(next(iter(self.meshes)))
        self.meshes[digest] = data
        return data


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
        self.outputs = Outputs(OUTPUTS)
        self.streams: dict[int, Stream] = {}
        self.ws = None

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
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
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

    async def handle(self, msg):
        if isinstance(msg, bytes):
            kind, ident = struct.unpack_from(">BI", msg)
            if kind == SSH and ident in self.streams:
                self.streams[ident].write(msg[5:])
            return
        m = json.loads(msg)
        t = m.get("t")
        if t == "config":
            new = await asyncio.to_thread(self.sshd.configure, m.get("keys", []), m.get("hostKey"))
            if new:
                await self.send({"t": "hostkey", **new})
            self.outputs.stamp = None  # resend the index
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
            await self.send(frame(MESH, rid, data))
        except Exception as e:  # report every failure to the viewer
            await self.send({"t": "mesh-error", "rid": rid, "error": f"{type(e).__name__}: {e}"})

    async def beat(self):
        """Keeps the Durable Object awake (protocol pings do not wake it), so the platform does not stop the machine."""
        while True:
            await asyncio.sleep(30)
            await self.send({"t": "beat"})

    async def watch(self):
        os.makedirs(OUTPUTS, exist_ok=True)
        while True:
            if await asyncio.to_thread(self.outputs.changed):
                index = await asyncio.to_thread(self.outputs.index)
                await self.send({"t": "outputs", "outputs": index})
            await asyncio.sleep(0.5)


def main():
    url, project, token = (os.environ.get(k) for k in ("TULE_URL", "TULE_PROJECT", "TULE_JOIN_TOKEN"))
    if not (url and project and token):
        sys.exit("tule-agent: set TULE_URL, TULE_PROJECT and TULE_JOIN_TOKEN")
    os.makedirs(OUTPUTS, exist_ok=True)
    asyncio.run(Agent(url, project, token).run())


if __name__ == "__main__":
    main()
