"""tule build: run the project's scripts whose inputs changed (DESIGN.md, "Planned: project
layout, builds and output types").

Every ``src/<name>.py`` makes ``outputs/<name>/``. A script runs in its own process with an
audit hook (``sys.addaudithook``) that records what it reads under the project (files,
folder listings, the local modules it imports) and refuses writes outside its staging
folder; nothing is declared. The output replaces ``outputs/<name>/`` only when the script
succeeds. The record, ``outputs/.build/<name>.json``, holds a hash of everything it read, so
the next build skips the script while nothing it read has changed, and anyone can tell an
output is stale without running it.

A script that reads another output (``outputs/bracket/…``) gets that one built first: if it
is not up to date yet, the run stops, the other is built, and the script runs again.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

FORMAT = 1
STATE = ".build"  # outputs/.build: records, staging folders, the hash cache
NEEDS = 75  # the exit status of a run that needs another output first


class BuildError(Exception):
    pass


def log(msg: str):
    print(f"tule: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- the project


def find_root(start: str | None = None) -> str | None:
    """The project folder: $TULE_WORKSPACE, else the nearest folder up from here that has src/."""
    env = os.environ.get("TULE_WORKSPACE")
    if env and os.path.isdir(os.path.join(env, "src")):
        return env
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(d, "src")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def targets(root: str) -> list[str]:
    src = os.path.join(root, "src")
    return sorted(f[:-3] for f in os.listdir(src) if f.endswith(".py") and not f.startswith(("_", ".")))


def env_stamp() -> dict:
    try:
        from importlib.metadata import version

        tule = version("tule")
    except Exception:
        tule = "none"
    return {"format": FORMAT, "tule": tule, "python": "%d.%d" % sys.version_info[:2]}


class Hashes:
    """Content hashes, cached by (size, mtime) in outputs/.build/hashes.json."""

    def __init__(self, root: str):
        self.root = root
        self.path = os.path.join(root, "outputs", STATE, "hashes.json")
        try:
            with open(self.path) as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    def of(self, rel: str) -> str:
        """A file's hash; "dir:" plus a hash of the names for a folder; "missing" when absent."""
        p = os.path.join(self.root, rel)
        try:
            st = os.stat(p)
        except OSError:
            return "missing"
        if os.path.isdir(p):
            names = sorted(n for n in os.listdir(p) if not n.startswith(".") and n != "__pycache__")
            return "dir:" + hashlib.sha256("\n".join(names).encode()).hexdigest()[:24]
        key = [st.st_size, st.st_mtime_ns]
        hit = self.cache.get(rel)
        if hit and hit[0] == key:
            return hit[1]
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()[:32]
        self.cache[rel] = [key, digest]
        return digest

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cache, f)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- records


def record_path(root: str, name: str) -> str:
    return os.path.join(root, "outputs", STATE, f"{name}.json")


def read_record(root: str, name: str) -> dict | None:
    try:
        with open(record_path(root, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_record(root: str, name: str, rec: dict):
    p = record_path(root, name)
    with open(p + ".tmp", "w") as f:
        json.dump(rec, f, indent=1)
    os.replace(p + ".tmp", p)


def output_of(rel: str) -> str | None:
    """outputs/<name>/… → name (None for other paths)."""
    parts = rel.split("/")
    if len(parts) >= 2 and parts[0] == "outputs" and not parts[1].startswith("."):
        return parts[1]
    return None


def why_stale(root: str, name: str, hashes: Hashes, env: dict) -> str | None:
    """None when outputs/<name> is up to date, else the reason."""
    rec = read_record(root, name)
    if not rec or "reads" not in rec:
        return "never built"
    if rec.get("env") != env:
        return "the tule library or Python changed"
    if not os.path.isdir(os.path.join(root, "outputs", name)):
        return f"outputs/{name} is gone"
    for rel, h in rec["reads"].items():
        if hashes.of(rel) != h:
            return f"{rel} changed"
    return None


# ---------------------------------------------------------------- building


class Build:
    def __init__(self, root: str, force: bool = False):
        self.root = root
        self.force = force
        self.all = targets(root)
        self.env = env_stamp()
        self.hashes = Hashes(root)
        self.done: dict[str, str] = {}  # name -> "built" | "up to date" | "failed"
        self.stack: list[str] = []

    def deps(self, name: str) -> list[str]:
        """The outputs it read last time (built first, so a changed one is seen)."""
        rec = read_record(self.root, name) or {}
        seen = []
        for rel in rec.get("reads", {}):
            o = output_of(rel)
            if o and o != name and o in self.all and o not in seen:
                seen.append(o)
        return seen

    def ensure(self, name: str):
        if name in self.done:
            return
        if name in self.stack:
            raise BuildError(f"{' → '.join(self.stack + [name])}: these outputs read each other")
        self.stack.append(name)
        try:
            for d in self.deps(name):
                self.ensure(d)
            why = "forced" if self.force else why_stale(self.root, name, self.hashes, self.env)
            if why is None:
                self.done[name] = "up to date"
                return
            asked: set[str] = set()
            while True:
                needs = self.run(name, why)
                if not needs:
                    break
                for n in needs:
                    self.ensure(n)
                bad = [n for n in needs if self.done.get(n) == "failed" or n in asked]
                if bad:
                    self.fail(name, f"needs outputs/{bad[0]}, which " + ("failed" if self.done.get(bad[0]) == "failed" else "did not get built"))
                    break
                asked.update(needs)
                why = f"{', '.join(needs)} built"
        finally:
            self.stack.pop()

    def run(self, name: str, why: str) -> list[str]:
        """Runs src/<name>.py once; returns the outputs it needs first (empty when it finished)."""
        outputs = os.path.join(self.root, "outputs")
        state = os.path.join(outputs, STATE)
        stage = os.path.join(state, f"stage-{name}-{os.getpid()}")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage)
        result = os.path.join(state, f"result-{name}-{os.getpid()}.json")
        pending = [t for t in self.all if self.done.get(t) not in ("built", "up to date")]
        log(f"{name}: building ({why})")
        t0 = time.time()
        env = dict(os.environ, TULE_OUTPUTS=stage, TULE_BUILD_TARGET=name, TULE_BUILD_ROOT=self.root,
                   TULE_BUILD_RESULT=result, TULE_BUILD_PENDING=json.dumps(pending), PYTHONDONTWRITEBYTECODE="1")
        try:
            code = subprocess.run([sys.executable, "-m", "tule_cli.build", "--run", f"src/{name}.py"],
                                  cwd=self.root, env=env).returncode
            try:
                with open(result) as f:
                    res = json.load(f)
            except (OSError, ValueError):
                res = {}
            if res.get("needs"):
                return [n for n in res["needs"] if n in self.all]
            problems = res.get("refused") or []
            made = sorted(os.listdir(stage))
            if code == 0 and not problems:
                if made != [name]:
                    problems = [f"wrote {', '.join('outputs/' + m for m in made)}; src/{name}.py may write only outputs/{name}/"
                                if made else f"wrote nothing: write outputs/{name}/ with tule.outputs "
                                f"(outputs.part(\"{name}\", …), or files into outputs.folder())"]
            if code != 0 or problems:
                for p in problems[:5]:
                    log(f"{name}: {p}")
                self.fail(name, f"exit status {code}" if code else problems[0])
                return []
            self.publish(name, os.path.join(stage, name))
            reads = {rel: self.hashes.of(rel) for rel in sorted(set(res.get("reads", [])))}
            write_record(self.root, name, {"target": name, "script": f"src/{name}.py", "env": self.env,
                                           "reads": reads, "built": int(time.time() * 1000),
                                           "seconds": round(time.time() - t0, 2)})
            self.done[name] = "built"
            log(f"{name}: built in {time.time() - t0:.1f} s")
            return []
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            try:
                os.unlink(result)
            except OSError:
                pass

    def publish(self, name: str, new: str):
        """Swaps the staged folder in (the old one is out of the way for a moment only)."""
        dest = os.path.join(self.root, "outputs", name)
        old = os.path.join(self.root, "outputs", STATE, f"old-{name}-{os.getpid()}")
        if os.path.lexists(dest):
            os.replace(dest, old)
        os.replace(new, dest)
        shutil.rmtree(old, ignore_errors=True)

    def fail(self, name: str, error: str):
        rec = read_record(self.root, name) or {"target": name, "script": f"src/{name}.py"}
        rec["failed"] = {"at": int(time.time() * 1000), "error": error}
        write_record(self.root, name, rec)
        self.done[name] = "failed"
        kept = os.path.isdir(os.path.join(self.root, "outputs", name))
        log(f"{name}: failed ({error}); " + (f"outputs/{name} is unchanged" if kept else "no output"))


def build(root: str, names: list[str] | None = None, force: bool = False) -> dict:
    os.makedirs(os.path.join(root, "outputs", STATE), exist_ok=True)
    lock = open(os.path.join(root, "outputs", STATE, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another tule build is running here; waiting for it")
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        b = Build(root, force)
        if not b.all:
            raise BuildError("nothing to build: src/ has no scripts (src/<name>.py makes outputs/<name>/)")
        for n in names or []:
            if n not in b.all:
                raise BuildError(f"no src/{n}.py; scripts: {', '.join(b.all)}")
        for n in names or b.all:
            b.ensure(n)
        b.hashes.save()
        return b.done
    finally:
        lock.close()


def status(root: str) -> list[dict]:
    """Every script's output: up to date, stale (and why), never built, or failed."""
    h = Hashes(root)
    env = env_stamp()
    out = []
    for n in targets(root):
        rec = read_record(root, n) or {}
        why = why_stale(root, n, h, env)
        row = {"name": n, "state": "up to date" if why is None else "stale", **({"why": why} if why else {})}
        if rec.get("failed") and (rec.get("built") or 0) < rec["failed"]["at"]:
            row["failed"] = rec["failed"]["error"]
        out.append(row)
    return out


# ---------------------------------------------------------------- inside the script's process


def _run_script(script: str):
    """Runs one script with reads recorded and writes outside its staging folder refused."""
    import runpy

    root = os.environ["TULE_BUILD_ROOT"]
    name = os.environ["TULE_BUILD_TARGET"]
    stage = os.environ["TULE_OUTPUTS"]
    pending = set(json.loads(os.environ.get("TULE_BUILD_PENDING", "[]"))) - {name}
    reads: set[str] = set()
    needs: list[str] = []
    refused: list[str] = []
    allowed = (stage + os.sep, os.path.realpath(stage) + os.sep)

    def rel_of(path) -> str | None:
        if isinstance(path, int) or path is None:
            return None
        try:
            p = os.path.abspath(os.fsdecode(path))
        except (TypeError, ValueError):
            return None
        if not (p == root or p.startswith(root + os.sep)):
            return None
        r = os.path.relpath(p, root).replace(os.sep, "/")
        if r.startswith((".git/", "outputs/.build/")) or "/__pycache__" in r or r == "outputs/.build":
            return None
        return r

    def read(path, listing: bool = False):
        r = rel_of(path)
        if r is None:
            return
        o = output_of(r)
        if o == name:
            refused.append(f"reads its own output ({r}); a script makes its output from inputs and code")
            raise BuildError(refused[-1])
        if o in pending:
            needs.append(o)
            raise BuildError(f"needs outputs/{o} first")
        if r == "outputs" and listing:
            first = sorted(pending)
            if first:
                needs.extend(first)
                raise BuildError("lists outputs/: the other outputs are built first")
        reads.add(r)

    def write(path):
        try:
            p = os.path.abspath(os.fsdecode(path))
        except (TypeError, ValueError):
            return
        if p.startswith(allowed) or p == stage:
            return
        if rel_of(path) is not None:
            r = os.path.relpath(p, root)
            refused.append(f"writes {r}: a script writes only its own output (tule.outputs, or files in outputs.folder())")
            raise PermissionError(refused[-1])

    def by_import() -> bool:
        """Whether the import system is asking (it lists folders on sys.path; not a dependency)."""
        f = sys._getframe(2)
        for _ in range(30):
            if f is None:
                return False
            if str(f.f_globals.get("__name__", "")).startswith("importlib"):
                return True
            f = f.f_back
        return False

    def hook(event, args):
        if event == "open":
            path, mode, flags = args
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                write(path)
            elif isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                write(path)
            else:
                read(path)
        elif event == "tule.read":  # files read by native code (tule.io.import_step)
            read(args[0])
        elif event in ("os.listdir", "os.scandir"):
            if not by_import():
                read(args[0] if args and args[0] is not None else ".", listing=True)
        elif event == "glob.glob":
            read(os.path.dirname(os.fsdecode(args[0])) or ".", listing=True)
        elif event in ("os.rename", "os.replace"):
            write(args[1])
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "shutil.rmtree", "shutil.move", "shutil.copyfile"):
            write(args[1] if event in ("shutil.move", "shutil.copyfile") else args[0])

    def report(code: int):
        with open(os.environ["TULE_BUILD_RESULT"], "w") as f:
            json.dump({"reads": sorted(reads), "needs": needs, "refused": refused}, f)
        sys.stdout.flush()
        os._exit(NEEDS if needs else code)

    reads.add(script)
    sys.path[:0] = [root, os.path.join(root, os.path.dirname(script))]
    sys.argv = [script]
    sys.addaudithook(hook)
    code = 0
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        if not needs:
            import traceback

            traceback.print_exc()
        code = 1
    report(code)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--run":
    _run_script(sys.argv[2])
