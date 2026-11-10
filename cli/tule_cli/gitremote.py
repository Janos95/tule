"""Git with a Tule project as the remote: `git push`/`git fetch` to `tule::<site>/api/projects/<id>/git`.

Two small programs, both installed with the CLI:

- git-remote-tule: a git remote helper (gitremote-helpers(7)). The remote keeps
  the refs and, per push, a git bundle of the commits it added
  (control/src/index.ts, "git and Git LFS"). A fetch applies the bundles whose
  commits it lacks, in push order; a push sends one bundle of what the remote
  lacks. Big files belong in Git LFS: a push carrying a plain blob over
  MAX_BLOB is refused with the command that fixes it.
- git-credential-tule: a git credential helper, so git-lfs signs in to the
  project's LFS server with the same credentials.

Credentials: on a Tule machine /etc/tule/machine.json (the tule-agent writes the
machine's join token there); elsewhere the CLI's API key (tule login).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

MACHINE = os.environ.get("TULE_MACHINE_FILE", "/etc/tule/machine.json")
MAX_BLOB = int(os.environ.get("TULE_MAX_BLOB", str(5 << 20)))
ZERO = ""


def token() -> str:
    try:
        with open(MACHINE) as f:
            t = json.load(f).get("token")
            if t:
                return t
    except (OSError, ValueError):
        pass
    from . import load

    t = load().get("token")
    if not t:
        die("not signed in: run `tule login` (on a Tule machine, the tule-agent provides credentials)")
    return t


def die(msg: str):
    print(f"tule: {msg}", file=sys.stderr)
    sys.exit(1)


def git(*args: str, input: bytes | None = None, check: bool = True) -> str:
    r = subprocess.run(["git", *args], input=input, capture_output=True)
    if check and r.returncode:
        die(f"git {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout.decode(errors="replace").strip()


def has(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode == 0


class Remote:
    def __init__(self, url: str, name: str = "tule"):
        self.url = url.rstrip("/")
        self.name = name  # the remote's name in the repository (git passes the URL when there is none)
        self.auth = {"Authorization": f"Bearer {token()}", "User-Agent": "tule-git"}  # Cloudflare refuses Python-urllib (error 1010)

    def call(self, method: str, path: str, data: bytes | None = None, raw: bool = False):
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=dict(self.auth))
        if data is not None:
            req.add_header("Content-Type", "application/x-git-bundle")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                j = json.loads(body)
            except ValueError:
                die(f"{self.url}{path}: HTTP {e.code} {body[:200].decode(errors='replace')}")
            if e.code == 409:
                return j
            die(f"{j.get('error', e.reason)}" + (f" ({j['hint']})" if j.get("hint") else ""))
        except urllib.error.URLError as e:
            die(f"cannot reach {self.url}: {e.reason}")
        return body if raw else json.loads(body)

    def refs(self) -> dict:
        return self.call("GET", "/refs")


# ---------------------------------------------------------------- the helper protocol


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        die("git-remote-tule is run by git: git remote add tule tule::https://<site>/api/projects/<id>/git")
    remote = Remote(argv[1], argv[0] if "://" not in argv[0] else "tule")
    state: dict = {}
    out = sys.stdout
    lines = iter(sys.stdin.readline, "")
    for line in lines:
        cmd = line.strip()
        if cmd == "capabilities":
            out.write("fetch\npush\n\n")
        elif cmd in ("list", "list for-push"):
            state = remote.refs()
            refs = state["refs"]
            for ref, sha in sorted(refs.items()):
                out.write(f"{sha} {ref}\n")
            if "refs/heads/main" in refs:
                out.write("@refs/heads/main HEAD\n")
            out.write("\n")
        elif cmd.startswith("fetch "):
            batch = [cmd]
            for more in lines:
                if not more.strip():
                    break
                batch.append(more.strip())
            fetch(remote, state, [b.split()[1] for b in batch])
            out.write("\n")
        elif cmd.startswith("push "):
            batch = [cmd]
            for more in lines:
                if not more.strip():
                    break
                batch.append(more.strip())
            if not state:
                state = remote.refs()
            for b in batch:
                out.write(push(remote, state, b.split(" ", 1)[1]) + "\n")
            out.write("\n")
        elif cmd == "":
            break
        else:
            die(f"unsupported command {cmd!r}")
        out.flush()


def fetch(remote: Remote, state: dict, wanted: list[str]):
    """Applies, in push order, every bundle whose tip this repository lacks."""
    if all(has(w) for w in wanted):
        return
    for b in state.get("bundles") or remote.refs()["bundles"]:
        if has(b["tip"]):
            continue
        data = remote.call("GET", f"/bundles/{b['id']}", raw=True)
        with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as f:
            f.write(data)
        try:
            git("bundle", "unbundle", f.name)
        finally:
            os.unlink(f.name)
    missing = [w for w in wanted if not has(w)]
    if missing:
        die(f"the remote's bundles do not contain {', '.join(m[:12] for m in missing)}")


def push(remote: Remote, state: dict, spec: str) -> str:
    force = spec.startswith("+")
    src, _, dst = spec.lstrip("+").partition(":")
    refs = state["refs"]
    old = refs.get(dst, ZERO)
    if not src:  # git push tule :branch deletes it
        r = remote.call("POST", f"/push?ref={urllib.parse.quote(dst)}&old={old}&new=", data=b"")
        if r.get("error"):
            return f"error {dst} {r['error']}"
        refs.pop(dst, None)
        return f"ok {dst}"
    new = git("rev-parse", f"{src}^{{commit}}")
    if new == old:
        return f"ok {dst}"
    if old and not force:
        if not has(old):
            return f"error {dst} fetch first"
        if subprocess.run(["git", "merge-base", "--is-ancestor", old, new], capture_output=True).returncode:
            return f"error {dst} non-fast-forward"
    # what the remote has already: every remote tip present here
    exclude = [f"^{s}" for s in set(refs.values()) if has(s)]
    big = large_blobs(new, exclude)
    if big:
        files = ", ".join(f"{p} ({n >> 20} MB)" for p, n in big[:5])
        # rewrite only the commits the remote lacks
        only = f"--include-ref={src if src.startswith('refs/') else 'HEAD'}" + (f" --exclude-ref=refs/remotes/{remote.name}/{dst.removeprefix('refs/heads/')}" if old else "")
        return f"error {dst} large files belong in Git LFS: {files}. Run: git lfs migrate import --include=\"{big[0][0]}\" {only}, then push again"
    count = int(git("rev-list", "--count", new, *exclude) or 0)
    data = b""
    if count:
        tmp = "refs/tule-push/tip"
        git("update-ref", tmp, new)
        with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as f:
            path = f.name
        try:
            git("bundle", "create", "-q", path, tmp, *exclude)
            with open(path, "rb") as f:
                data = f.read()
        finally:
            os.unlink(path)
            git("update-ref", "-d", tmp, check=False)
    r = remote.call("POST", f"/push?ref={urllib.parse.quote(dst)}&old={old}&new={new}", data=data)
    if r.get("error"):
        return f"error {dst} {r['error']}"
    refs[dst] = new
    return f"ok {dst}"


def large_blobs(new: str, exclude: list[str]) -> list[tuple[str, int]]:
    """Plain (non-LFS) blobs over MAX_BLOB among the commits being pushed, with a path each."""
    objs = git("rev-list", "--objects", new, *exclude)
    if not objs:
        return []
    paths = {}
    for line in objs.splitlines():
        sha, _, path = line.partition(" ")
        paths[sha] = path
    check = git("cat-file", "--batch-check=%(objecttype) %(objectname) %(objectsize)", input="\n".join(paths).encode())
    big = []
    for line in check.splitlines():
        kind, sha, size = line.split()
        if kind == "blob" and int(size) > MAX_BLOB:
            big.append((paths.get(sha) or sha, int(size)))
    return sorted(big, key=lambda x: -x[1])


# ---------------------------------------------------------------- credentials for git-lfs


def credential(argv=None):
    """git-credential-tule get: the same token, as Basic auth (user "tule"), for Tule's own host only:
    the helper is installed for every URL, and any other server that asks (GitHub, on a private or
    mistyped repository) must not be sent the machine's token."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != "get":
        return  # store / erase: nothing to keep
    asked = {}
    for line in sys.stdin:  # the request ends with a blank line (git may keep the pipe open)
        if not line.strip():
            break
        k, _, v = line.strip().partition("=")
        asked[k] = v
    if asked.get("host", "").lower() != _tule_host():
        return
    sys.stdout.write(f"username=tule\npassword={token()}\n")


def _tule_host() -> str:
    """host[:port] of the Tule server this machine (or this CLI) belongs to; "" when unknown."""
    import urllib.parse

    url = ""
    try:
        with open(MACHINE) as f:
            url = json.load(f).get("url") or ""
    except (OSError, ValueError):
        pass
    if not url:
        from . import load

        url = load().get("url") or ""
    return urllib.parse.urlsplit(url).netloc.lower()


if __name__ == "__main__":
    main()
