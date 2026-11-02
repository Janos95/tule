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

On a Tule machine every output built is also uploaded to the project's build cache
(cache.py), and an output that is out of date here is downloaded from it instead of run when
some machine built it from the same files: so outputs travel between machines (a study run on
a GPU is on the others too) while outputs/ stays out of git.

Agent work is never re-run: its result lives in ``work/`` and ``tule based-on`` records
what it was based on (``work/.based-on.json``: each source's fingerprint). When a source
changes, the result is reported as possibly out of date until someone redoes it or confirms
it still holds (``tule based-on <result>`` again records the sources as they are now).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections import deque

FORMAT = 1
STATE = ".build"  # outputs/.build: records, staging folders, the hash cache
NEEDS = 75  # the exit status of a run that needs another output first
BASED_ON = "work/.based-on.json"
LOG_LINES = 40  # of a failed script's output, kept in its record for the viewer


class BuildError(Exception):
    pass


def log(msg: str):
    print(f"tule: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- the project


def find_root(start: str | None = None) -> str | None:
    """The project folder: $TULE_WORKSPACE (a machine's /workspace), else the nearest folder
    up from here with tule.toml and src/ (any repo has a src/, so that alone is not enough)."""
    env = os.environ.get("TULE_WORKSPACE")
    if env and os.path.isdir(os.path.join(env, "src")):
        return os.path.abspath(env)
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(d, "tule.toml")) and os.path.isdir(os.path.join(d, "src")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def _scripts(d: str) -> list[str]:
    return [f[:-3] for f in os.listdir(d) if f.endswith(".py") and not f.startswith(("_", ".")) and os.path.isfile(os.path.join(d, f))]


def groups(root: str) -> list[str]:
    """Folders in src/: src/checks/fit.py makes outputs/checks/fit/, shown in a group "checks"."""
    src = os.path.join(root, "src")
    try:
        return sorted(f for f in os.listdir(src) if not f.startswith(("_", ".")) and os.path.isdir(os.path.join(src, f)))
    except OSError:
        return []


def targets(root: str) -> list[str]:
    """Script names: "bracket" for src/bracket.py, "checks/fit" for src/checks/fit.py."""
    src = os.path.join(root, "src")
    return sorted(_scripts(src) + [f"{g}/{n}" for g in groups(root) for n in _scripts(os.path.join(src, g))])


def key(name: str) -> str:
    """A name as one file name (records, staging folders): checks/fit → checks~fit."""
    return name.replace("/", "~")


def orphans(root: str) -> list[str]:
    """Outputs whose script is gone (deleted or renamed): left for the agent to remove."""
    out = os.path.join(root, "outputs")
    have = set(targets(root))
    gs = set(groups(root))
    names = []
    for n in os.listdir(out) if os.path.isdir(out) else []:
        if n.startswith(".") or not os.path.isdir(os.path.join(out, n)):
            continue
        if n in gs:
            names += [f"{n}/{m}" for m in os.listdir(os.path.join(out, n))
                      if not m.startswith(".") and os.path.isdir(os.path.join(out, n, m))]
        else:
            names.append(n)
    return sorted(n for n in names if n not in have)


def memory() -> str:
    """ " (6 GiB)": this machine's memory limit, or ""."""
    try:
        v = open("/sys/fs/cgroup/memory.max").read().strip()
        return "" if v == "max" else f" ({int(v) / 2**30:.0f} GiB)"
    except (OSError, ValueError):
        return ""


_LIB = None


def env_stamp() -> dict:
    """What a build ran with: a fix in the tule library rebuilds what it made (its files' hash, not
    its version number, which a new machine image need not change)."""
    global _LIB
    if _LIB is None:
        try:
            import importlib.util

            d = os.path.dirname(importlib.util.find_spec("tule").origin)
            h = hashlib.sha256()
            for f in sorted(x for x in os.listdir(d) if x.endswith(".py")):
                h.update(f.encode() + b"\0" + open(os.path.join(d, f), "rb").read())
            _LIB = h.hexdigest()[:16]
        except Exception:
            _LIB = "none"
    return {"format": FORMAT, "tule": _LIB, "python": "%d.%d" % sys.version_info[:2]}


def stamp(path: str) -> list | None:
    """A file's (size, mtime), to tell it changed while a script ran; None for a folder or nothing."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return None if os.path.isdir(path) else [st.st_size, st.st_mtime_ns]


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
        """A file's hash; "dir:" plus a hash of the names for a folder; "missing" when absent
        (or not a file: a FIFO or a device would never end)."""
        p = os.path.join(self.root, rel)
        try:
            st = os.stat(p)
        except OSError:
            return "missing"
        if stat.S_ISDIR(st.st_mode):
            names = sorted(n for n in os.listdir(p) if not n.startswith(".") and n != "__pycache__")
            return "dir:" + hashlib.sha256("\n".join(names).encode()).hexdigest()[:24]
        if not stat.S_ISREG(st.st_mode):
            return "missing"
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
    return os.path.join(root, "outputs", STATE, f"{key(name)}.json")


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


def output_of(rel: str, root: str) -> str | None:
    """outputs/<name>/… → name, outputs/<group>/<name>/… → group/name (None for other paths)."""
    parts = rel.split("/")
    if len(parts) >= 2 and parts[0] == "outputs" and not parts[1].startswith("."):
        if len(parts) >= 3 and os.path.isdir(os.path.join(root, "src", parts[1])):
            return f"{parts[1]}/{parts[2]}"
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
    def __init__(self, root: str, cache=None):
        self.root = root
        self.cache = cache  # cache.Cache on a Tule machine
        self.force: set[str] = set()  # run even if up to date
        self.skip: set[str] = set()  # left as they are
        self.all = targets(root)
        self.env = env_stamp()
        self.hashes = Hashes(root)
        self.done: dict[str, str] = {}  # name -> "built" | "fetched" | "up to date" | "failed" | "skipped"
        self.stack: list[str] = []  # being checked or run
        self.running: list[str] = []  # being run (and building what they need first)

    def deps(self, name: str) -> list[str]:
        """The outputs it read last time (built first, so a changed one is seen)."""
        rec = read_record(self.root, name) or {}
        seen = []
        for rel in rec.get("reads", {}):
            o = output_of(rel, self.root)
            if o and o != name and o in self.all and o not in seen:
                seen.append(o)
        return seen

    def readers(self, names: list[str]) -> list[str]:
        """The outputs that read these, and what reads those, from the last records."""
        out: list[str] = []
        todo = list(names)
        while todo:
            n = todo.pop()
            for t in self.all:
                if t not in out and t not in names and n in self.deps(t):
                    out.append(t)
                    todo.append(t)
        return out

    def ensure(self, name: str):
        if name in self.done:
            return
        if name in self.skip:  # its last output stays, stale or not
            self.done[name] = "skipped"
            return
        self.stack.append(name)
        try:
            for d in self.deps(name):  # what it read last time (it may read other things now)
                if d not in self.stack:
                    self.ensure(d)
            if name in self.done:  # built meanwhile: something checked above needed it
                return
            why = "forced" if name in self.force else why_stale(self.root, name, self.hashes, self.env)
            if why is None:
                self.done[name] = "up to date"
                rec = read_record(self.root, name)
                if rec and rec.pop("failed", None):  # back to what last built: the failure is moot
                    write_record(self.root, name, rec)
                return
            if self.cache and name not in self.force and self.fetch(name, why):
                return
            self.running.append(name)
            try:
                after = []  # the outputs it stopped for: run it again once they are built
                while needs := self.run(name, why, after):
                    after = needs
                    for n in needs:
                        if n in self.running:
                            loop = " → ".join(self.running[self.running.index(n):] + [n])
                            return self.fail(name, f"outputs/{n} and outputs/{name} read each other ({loop})")
                        self.ensure(n)
                    failed = [n for n in needs if self.done[n] == "failed"]
                    if failed:
                        return self.fail(name, f"needs outputs/{failed[0]}, which failed")
            finally:
                self.running.pop()
        finally:
            self.stack.pop()

    def run(self, name: str, why: str, after: list[str] = ()) -> list[str]:
        """Runs src/<name>.py once; returns the outputs it needs first (empty when it finished)."""
        outputs = os.path.join(self.root, "outputs")
        state = os.path.join(outputs, STATE)
        stage = os.path.join(state, f"stage-{key(name)}-{os.getpid()}")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage)
        result = os.path.join(state, f"result-{key(name)}-{os.getpid()}.json")
        pending = [t for t in self.all if self.done.get(t) not in ("built", "fetched", "up to date", "skipped")]
        log(f"{name}: building again, now that {', '.join(f'outputs/{n}' for n in after)} is built" if after else f"{name}: building ({why})")
        t0 = time.time()
        env = dict(os.environ, TULE_OUTPUTS=stage, TULE_BUILD_TARGET=name, TULE_BUILD_ROOT=self.root,
                   TULE_BUILD_RESULT=result, TULE_BUILD_PENDING=json.dumps(pending), PYTHONDONTWRITEBYTECODE="1")
        tail: deque[str] = deque(maxlen=LOG_LINES)
        try:
            # stderr goes to the terminal as it comes and its end is kept for the record
            p = subprocess.Popen([sys.executable, "-m", "tule_cli.build", "--run", f"src/{name}.py"],
                                 cwd=self.root, env=env, stderr=subprocess.PIPE, text=True, errors="replace")
            for line in p.stderr:
                sys.stderr.write(line)
                tail.append(line.rstrip("\n"))
            code = p.wait()
            try:
                with open(result) as f:
                    res = json.load(f)
            except (OSError, ValueError):
                res = {}
            if res.get("needs"):
                return res["needs"]
            problems = res.get("refused") or []
            made = sorted(os.listdir(stage))
            if "/" in name and made == [name.split("/")[0]]:  # a group's folder: what is in it
                made = [f"{made[0]}/{m}" for m in sorted(os.listdir(os.path.join(stage, made[0])))]
            if code == 0 and not problems:
                if made != [name]:
                    problems = [f"wrote {', '.join('outputs/' + m for m in made)}; src/{name}.py may write only outputs/{name}/"
                                if made else f"wrote nothing: write outputs/{name}/ with tule.outputs "
                                f"(outputs.part(\"{name}\", …), or files into outputs.folder())"]
            if code != 0 or problems:
                for p in problems[:5]:
                    log(f"{name}: {p}")
                why = {15: " (SIGTERM: stopped)",
                       9: f" (SIGKILL: out of memory{memory()}? lighter purchased parts or fewer faces, or a bigger machine: tule up --machine cpu-4)"}.get(-code, "")
                error = f"killed by signal {-code}{why}" if code < 0 else f"exit status {code}" if code else problems[0]
                self.fail(name, error, "\n".join(tail))
                return []
            # a file changed while the script ran is recorded as changed: the output may have the old one
            moved = {rel for rel, st in res.get("reads", {}).items() if stamp(os.path.join(self.root, rel)) != st}
            self.publish(name, os.path.join(stage, name))
            reads = {rel: "changed while it ran" if rel in moved else self.hashes.of(rel) for rel in sorted(res.get("reads", {}))}
            rec = {"target": name, "script": f"src/{name}.py", "env": self.env, "reads": reads,
                   "built": int(time.time() * 1000), "seconds": round(time.time() - t0, 2)}
            write_record(self.root, name, rec)
            self.done[name] = "built"
            log(f"{name}: built in {time.time() - t0:.1f} s")
            heavy = models_mb(os.path.join(self.root, "outputs", name))
            if heavy > HEAVY_MB:
                log(f"{name}: {heavy:.0f} MB of models: the viewer may take minutes to show it, or fail; "
                    "use simpler stand-ins for big purchased parts (their envelope and mounting features)")
            if self.cache and not moved:
                try:
                    self.cache.upload(self.root, name, rec)
                except Exception as e:  # the output is here either way
                    log(f"build cache: {e}; not uploading this build's outputs")
                    self.cache = None
            return []
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            try:
                os.unlink(result)
            except OSError:
                pass

    def fetch(self, name: str, why: str) -> bool:
        """Downloads outputs/<name> from the build cache when some machine built it from the
        files here (what it read is built here first); False when none did."""
        try:
            entries = self.cache.entries(name)
        except Exception as e:
            log(f"build cache: {e}")
            self.cache = None
            return False
        for e in entries:
            rec = e.get("record") or {}
            reads = rec.get("reads")
            if rec.get("env") != self.env or not isinstance(reads, dict):
                continue
            if any(self.hashes.of(rel) != h for rel, h in reads.items() if not output_of(rel, self.root)):
                continue  # a script, module or input differs: no need to look at outputs
            for rel in reads:
                o = output_of(rel, self.root)
                if o and o != name and o in self.all and o not in self.stack:
                    self.ensure(o)
            if any(self.hashes.of(rel) != h for rel, h in reads.items()):
                continue
            stage = os.path.join(self.root, "outputs", STATE, f"stage-{key(name)}-{os.getpid()}")
            shutil.rmtree(stage, ignore_errors=True)
            t0 = time.time()
            try:
                self.cache.download(e, os.path.join(stage, name))
                self.publish(name, os.path.join(stage, name))
            except Exception as err:
                log(f"{name}: could not download it from the build cache ({err})")
                return False
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            write_record(self.root, name, {**rec, "target": name, "script": f"src/{name}.py",
                                           "fetched": {"from": e.get("machine"), "at": int(time.time() * 1000)}})
            self.done[name] = "fetched"
            took = f"; its run took {rec['seconds']:g} s" if rec.get("seconds") is not None else ""
            log(f"{name}: downloaded in {time.time() - t0:.1f} s, as built on {machine_label(e.get('machine'))} ({why}{took})")
            return True
        return False

    def publish(self, name: str, new: str):
        """Swaps the staged folder in (the old one is out of the way for a moment only)."""
        dest = os.path.join(self.root, "outputs", name)
        old = os.path.join(self.root, "outputs", STATE, f"old-{key(name)}-{os.getpid()}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)  # a group's folder
        if os.path.lexists(dest):
            os.replace(dest, old)
        os.replace(new, dest)
        shutil.rmtree(old, ignore_errors=True)

    def fail(self, name: str, error: str, output: str = ""):
        rec = read_record(self.root, name) or {"target": name, "script": f"src/{name}.py"}
        rec["failed"] = {"at": int(time.time() * 1000), "error": error, **({"log": output} if output else {})}
        write_record(self.root, name, rec)
        self.done[name] = "failed"
        kept = os.path.isdir(os.path.join(self.root, "outputs", name))
        log(f"{name}: failed ({error}); " + (f"outputs/{name} is unchanged" if kept else "no output"))


def select(all_names: list[str], patterns: list[str]) -> list[str]:
    """Script names matching names or patterns (plate-*, src/plate.py); an error for one that matches nothing."""
    import fnmatch

    out: list[str] = []
    for p in patterns:
        p = p.rstrip("/").removeprefix("./").removeprefix("src/").removeprefix("outputs/").removesuffix(".py")
        hit = [n for n in all_names if fnmatch.fnmatchcase(n, p)]
        if not hit:
            raise BuildError(f"no src/{p}.py; scripts: {', '.join(all_names)}")
        out += [n for n in hit if n not in out]
    return out


def build(root: str, names: list[str] | None = None, force: bool = False,
          dependents: bool = False, skip: list[str] | None = None) -> dict:
    """Builds the named outputs (patterns allowed; all when none) and what they read; with
    dependents, also what reads them; never the ones in skip. force runs the named ones (not
    what they read) even if they are up to date."""
    state = os.path.join(root, "outputs", STATE)
    os.makedirs(state, exist_ok=True)
    lock = open(os.path.join(state, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"another tule build is running here (its log: outputs/{STATE}/build.log); waiting for it")
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        for n in os.listdir(state):  # left by a build that was killed (the viewer would show it building)
            if n.startswith(("stage-", "old-")):
                shutil.rmtree(os.path.join(state, n), ignore_errors=True)
            elif n.startswith("result-"):
                os.unlink(os.path.join(state, n))
        from .cache import Cache

        b = Build(root, Cache.here())
        if not b.all:
            raise BuildError("nothing to build: src/ has no scripts (src/<name>.py makes outputs/<name>/)")
        b.skip = set(select(b.all, skip or []))
        wanted = select(b.all, names) if names else list(b.all)
        if dependents:
            wanted += b.readers(wanted)
        if force:
            b.force = set(wanted)
        for n in wanted:
            b.ensure(n)
        b.hashes.save()
        return b.done
    finally:
        lock.close()


def status(root: str, hashes: Hashes | None = None) -> list[dict]:
    """Every script's output: up to date, stale (and why: something it read changed, or an
    output it reads is itself out of date), never built, or failed (with the end of its log)."""
    h = hashes or Hashes(root)
    env = env_stamp()
    names = targets(root)
    recs = {n: read_record(root, n) or {} for n in names}
    why = {n: why_stale(root, n, h, env) for n in names}

    def upstream(n: str, seen: tuple = ()) -> str | None:
        if why[n] is not None:
            return why[n]
        for rel in recs[n].get("reads", {}):
            o = output_of(rel, root)
            if o in why and o != n and o not in seen and upstream(o, seen + (n,)) is not None:
                return f"outputs/{o} is out of date"
        return None

    out = []
    for n in names:
        w = upstream(n)
        row = {"name": n, "state": "up to date" if w is None else "stale", **({"why": w} if w else {})}
        rec = recs[n]
        if w and rec.get("failed"):  # (a build that succeeds or finds it up to date drops it)
            row["failed"] = rec["failed"]["error"]
            if rec["failed"].get("log"):
                row["log"] = rec["failed"]["log"]
        if rec.get("built"):
            row["built"] = rec["built"]
        if rec.get("seconds") is not None:
            row["seconds"] = rec["seconds"]
        out.append(row)
    return out


def machine_label(slot: str | None) -> str:
    """A machine slot as people say it: "cloud", "joined", "gpu-rtx4090", a computer's name."""
    return {"cloud": "Tule cloud", "joined": "a joined computer", None: "a machine", "": "a machine"}.get(slot, slot)


def cached_on(root: str, name: str, cache, hashes: Hashes | None = None) -> str | None:
    """The machine whose upload of outputs/<name> matches the files here, if any (for --check).
    An output it read may be missing here too, as long as the cache has it with the same bytes."""
    h = hashes or Hashes(root)
    env = env_stamp()

    def match(n: str, seen: tuple) -> dict | None:
        for e in cache.entries(n):
            rec = e.get("record") or {}
            reads = rec.get("reads")
            if rec.get("env") != env or not isinstance(reads, dict):
                continue
            if all(h.of(rel) == x or same_upstream(rel, x, seen + (n,)) for rel, x in reads.items()):
                return e
        return None

    def same_upstream(rel: str, x: str, seen: tuple) -> bool:
        o = output_of(rel, root)
        if not o or o in seen:
            return False
        up = match(o, seen)
        f = (up or {}).get("files", {}).get(rel[len(f"outputs/{o}/"):])
        # a file under 16 MiB is one chunk, whose sha256 starts like the build's hash
        return bool(f) and len(f.get("chunks", [])) == 1 and f["chunks"][0][:32] == x

    e = match(name, ())
    return machine_label(e.get("machine")) if e else None


# ---------------------------------------------------------------- what agent work was based on


def fingerprint(root: str, rel: str, hashes: Hashes) -> str:
    """A file's hash, or for a folder a hash over its files' names and hashes."""
    p = os.path.join(root, rel)
    if os.path.isfile(p):
        return hashes.of(rel)
    if not os.path.isdir(p):
        return "missing"
    lines = []
    for d, dirs, files in os.walk(p):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "__pycache__")
        for f in sorted(files):
            if not f.startswith("."):
                r = os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                lines.append(f"{r[len(rel) + 1:]}:{hashes.of(r)}")
    return "tree:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()[:32]


def _inside(root: str, rel) -> bool:
    """Whether rel is a path in the project (symlinks resolved), not the project itself."""
    if not isinstance(rel, str):
        return False
    real = os.path.realpath(root)
    return os.path.realpath(os.path.join(root, rel)).startswith(real + os.sep)


def _clean(root: str, rel: str) -> str:
    """A path the user typed (absolute, ./x, x/) as a path relative to the project."""
    root = os.path.abspath(root)
    r = os.path.relpath(os.path.abspath(os.path.join(root, rel)), root).replace(os.sep, "/")
    if r != "." and not _inside(root, r):
        raise BuildError(f"{rel}: not in the project ({root})")
    return r


HEAVY_MB = 60  # models the viewer meshes at once (a sequence shows one frame: not counted)


def models_mb(folder: str) -> float:
    try:
        with open(os.path.join(folder, "output.json")) as f:
            if json.load(f).get("kind") == "sequence":
                return 0
    except (OSError, ValueError, AttributeError):
        pass
    total = 0
    for d, _, files in os.walk(folder):
        total += sum(os.path.getsize(os.path.join(d, f)) for f in files if f.lower().endswith((".step", ".stp", ".stl")))
    return total / 2**20


def read_based_on(root: str) -> dict[str, dict[str, str]]:
    """work/.based-on.json, without entries outside the project (it is in git: anyone can edit it)."""
    try:
        with open(os.path.join(root, BASED_ON)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {r: {x: fp for x, fp in srcs.items() if _inside(root, x) and isinstance(fp, str)}
            for r, srcs in data.items() if r.startswith("work/") and _inside(root, r) and isinstance(srcs, dict)}


def based_on(root: str, result: str, sources: list[str] | None = None, hashes: Hashes | None = None) -> dict[str, str]:
    """Records that work/<result> was made from `sources` as they are now (no sources: the
    ones recorded before, i.e. it was checked and still holds). Returns {source: fingerprint}."""
    h = hashes or Hashes(root)
    result = _clean(root, result)
    if not result.startswith("work/") or result == BASED_ON:
        raise BuildError(f"{result}: agent work is kept in work/ (e.g. work/suppliers/bracket.json)")
    if not os.path.exists(os.path.join(root, result)):
        raise BuildError(f"no {result}; write the result first, then record what it is based on")
    notes = read_based_on(root)
    if sources:
        srcs = [_clean(root, x) for x in sources]
    else:
        srcs = list(notes.get(result, {}))
        if not srcs:
            raise BuildError(f"{result} has no sources recorded yet: tule based-on {result} outputs/<name> …")
    for x in srcs:
        if x in (".", result, BASED_ON) or result.startswith(x + "/") or x.startswith("outputs/.build"):
            raise BuildError(f"{x}: not something work can be based on")
        if not os.path.exists(os.path.join(root, x)):
            raise BuildError(f"no {x}" + ("" if sources else f"; name what it is based on now: tule based-on {result} <sources>"))
    notes[result] = {x: fingerprint(root, x, h) for x in srcs}
    notes = {r: v for r, v in notes.items() if os.path.exists(os.path.join(root, r))}  # results deleted since
    os.makedirs(os.path.join(root, "work"), exist_ok=True)
    tmp = os.path.join(root, BASED_ON + ".tmp")
    with open(tmp, "w") as f:
        json.dump(notes, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, os.path.join(root, BASED_ON))
    return notes[result]


def based_on_status(root: str, hashes: Hashes | None = None) -> list[dict]:
    """Each recorded result: its sources and which of them changed since."""
    h = hashes or Hashes(root)
    out = []
    for result, srcs in sorted(read_based_on(root).items()):
        if not os.path.exists(os.path.join(root, result)):
            continue
        changed = [x for x, fp in srcs.items() if fingerprint(root, x, h) != fp]
        out.append({"path": result, "sources": sorted(srcs), "changed": changed})
    return out


# ---------------------------------------------------------------- inside the script's process


def _run_script(script: str):
    """Runs one script with reads recorded and writes outside its staging folder refused."""
    import runpy

    root = os.environ["TULE_BUILD_ROOT"]
    name = os.environ["TULE_BUILD_TARGET"]
    stage = os.environ["TULE_OUTPUTS"]
    pending = set(json.loads(os.environ.get("TULE_BUILD_PENDING", "[]"))) - {name}
    reads: dict[str, list | None] = {}  # path -> its stamp when first read
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
        o = output_of(r, root)
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
        if r not in reads:
            reads[r] = stamp(os.path.join(root, r))

    def write(path):
        if path is None:
            return
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

    def at(path, dir_fd):
        """A path given relative to an open folder (dir_fd: shutil.rmtree deletes so) made absolute."""
        if dir_fd is None or not isinstance(path, (str, bytes)) or os.path.isabs(path):
            return path
        try:
            return os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), os.fsdecode(path))
        except OSError:
            return None  # not knowable here (no /proc): not checked

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
            write(at(args[1], args[3]))
        elif event in ("os.remove", "os.rmdir"):
            write(at(args[0], args[1]))
        elif event == "os.mkdir":
            write(at(args[0], args[2]))
        elif event in ("shutil.rmtree", "shutil.move", "shutil.copyfile"):
            write(args[1] if event in ("shutil.move", "shutil.copyfile") else args[0])

    def report(code: int):
        with open(os.environ["TULE_BUILD_RESULT"], "w") as f:
            json.dump({"reads": reads, "needs": needs, "refused": refused}, f)
        sys.stdout.flush()
        os._exit(NEEDS if needs else code)

    reads[script] = stamp(os.path.join(root, script))
    sys.path[:0] = [root, os.path.join(root, os.path.dirname(script))]
    sys.argv = [script]
    sys.addaudithook(hook)
    code = 0
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException as e:
        if not needs:
            import traceback

            # from the script's first frame on: tule build's own frames (runpy) say nothing
            tb = e.__traceback__
            while tb and (tb.tb_frame.f_code.co_filename == __file__ or tb.tb_frame.f_code.co_filename.startswith("<frozen")):
                tb = tb.tb_next
            traceback.print_exception(type(e), e, tb or e.__traceback__)
        code = 1
    report(code)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--run":
    _run_script(sys.argv[2])
