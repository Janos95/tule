"""Saving: /workspace is a git repository whose remote ("tule") is the project's
own, hosted by Tule (cli/tule_cli/gitremote.py speaks to it; big files go
through Git LFS). Autosave (workspace.py's snapshots) keeps each machine's
working copy safe; a save is a commit pushed to main, merged with what other
machines saved (DESIGN.md, "Planned: several machines per project").

    setup(root, url, project, token)  credentials for git (/etc/tule/machine.json) and the repo's remote
    has_main(root)                    whether the project was ever saved
    start_from_main(root)             a fresh working copy from the latest save
    unsaved(root)                     {"files": n, "commits": n}: work not in the latest save
    save(root, message)               commit everything, merge the latest save of this branch, push
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

ETC = os.environ.get("TULE_ETC", "/etc/tule")
REMOTE = "tule"


def git(root: str, *args: str, check: bool = False, timeout: int = 600) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r


def available() -> bool:
    return bool(shutil.which("git") and shutil.which("git-remote-tule"))


def setup(root: str, url: str, project: str, token: str):
    """The machine's credentials for git-remote-tule and git-lfs, and the repo's remote (every boot: the token changes)."""
    os.makedirs(ETC, exist_ok=True)
    path = os.path.join(ETC, "machine.json")
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"url": url, "project": project, "token": token}, f)
    os.replace(tmp, path)
    configure(root, url, project)


def configure(root: str, url: str | None = None, project: str | None = None):
    """Points the repository's "tule" remote and Git LFS at the project (when /workspace is a repository)."""
    if not os.path.isdir(os.path.join(root, ".git")):
        return
    if url is None:
        try:
            with open(os.path.join(ETC, "machine.json")) as f:
                cfg = json.load(f)
            url, project = cfg["url"], cfg["project"]
        except (OSError, ValueError, KeyError):
            return
    base = f"{url.rstrip('/')}/api/projects/{project}"
    if git(root, "remote", "get-url", REMOTE).returncode:
        git(root, "remote", "add", REMOTE, f"tule::{base}/git")
    else:
        git(root, "remote", "set-url", REMOTE, f"tule::{base}/git")
    git(root, "config", "lfs.url", f"{base}/lfs")


def has_main(root: str) -> bool:
    r = git(root if os.path.isdir(root) else "/", "ls-remote", f"tule::{_base()}/git", "refs/heads/main", timeout=120)
    return r.returncode == 0 and bool(r.stdout.strip())


def _base() -> str:
    with open(os.path.join(ETC, "machine.json")) as f:
        cfg = json.load(f)
    return f"{cfg['url'].rstrip('/')}/api/projects/{cfg['project']}"


def start_from_main(root: str):
    """/workspace becomes a checkout of main (its LFS files included)."""
    os.makedirs(root, exist_ok=True)
    if not os.path.isdir(os.path.join(root, ".git")):
        git(root, "init", "-q", "-b", "main", check=True)
    configure(root)
    git(root, "fetch", "-q", REMOTE, check=True)
    git(root, "checkout", "-q", "-f", "-B", "main", f"{REMOTE}/main", check=True)
    git(root, "branch", "-q", "--set-upstream-to", f"{REMOTE}/main", "main")
    if shutil.which("git-lfs"):
        git(root, "lfs", "pull", REMOTE)


def push_new(root: str) -> bool:
    """The first save of a new project (its template)."""
    r = git(root, "push", "-q", "-u", REMOTE, "main")
    return r.returncode == 0


def _has_remote(root: str, branch: str = "main") -> bool:
    return git(root, "rev-parse", "-q", "--verify", f"refs/remotes/{REMOTE}/{branch}").returncode == 0


def branch(root: str) -> str:
    """The branch checked out (a save goes to the same branch on the remote); main when detached."""
    b = git(root, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    return b or "main"


def unsaved(root: str) -> dict:
    """Changed files (git status) and commits that are not in the latest save this copy knows of."""
    if not os.path.isdir(os.path.join(root, ".git")):
        return {"files": 0, "commits": 0}
    st = git(root, "status", "--porcelain", timeout=60)
    files = len([x for x in st.stdout.splitlines() if x.strip()]) if st.returncode == 0 else 0
    b = branch(root)
    base = b if _has_remote(root, b) else "main"
    rng = [f"{REMOTE}/{base}..HEAD"] if _has_remote(root, base) else ["HEAD"]
    c = git(root, "rev-list", "--count", *rng, timeout=60)
    commits = int(c.stdout.strip() or 0) if c.returncode == 0 else 0
    return {"files": files, "commits": commits}


def save(root: str, message: str = "", who: str = "") -> dict:
    """Commits everything, merges what other machines saved, and pushes (main, or the branch checked out).

    On a merge conflict nothing is pushed: the answer lists the files (they have
    <<<<<<< markers); resolve them, commit, and save again.
    """
    if not os.path.isdir(os.path.join(root, ".git")):
        raise RuntimeError("/workspace is not a git repository")
    configure(root)
    # a merge from an earlier save: go on once no file has conflict markers left
    unmerged = git(root, "diff", "--name-only", "--diff-filter=U").stdout.split()
    if any(_markers(root, f) for f in unmerged):
        return _conflict(root)
    _track_large(root)
    git(root, "add", "-A", check=True)
    committed = False
    if git(root, "diff", "--cached", "--quiet").returncode:
        git(root, "commit", "-q", "-m", message.strip() or f"Save from {who or 'a Tule machine'}", check=True)
        committed = True
    merged = False
    b = branch(root)
    for attempt in range(3):
        f = git(root, "fetch", "-q", REMOTE)
        if f.returncode and attempt == 0 and "not signed in" in f.stderr:
            raise RuntimeError(f.stderr.strip())
        if _has_remote(root, b) and git(root, "merge-base", "--is-ancestor", f"{REMOTE}/{b}", "HEAD").returncode:
            m = git(root, "merge", "--no-edit", "-q", f"{REMOTE}/{b}")
            if m.returncode:
                return _conflict(root)
            merged = True
        p = git(root, "push", "-q", REMOTE, f"HEAD:refs/heads/{b}")
        if p.returncode == 0:
            if git(root, "symbolic-ref", "-q", "HEAD").returncode == 0:
                git(root, "branch", "-q", "--set-upstream-to", f"{REMOTE}/{b}")
            head = git(root, "rev-parse", "HEAD").stdout.strip()
            return {"saved": head, "branch": b, "committed": committed, "merged": merged}
        if "non-fast-forward" not in p.stderr and "moved on the remote" not in p.stderr and "fetch first" not in p.stderr:
            raise RuntimeError((p.stderr or p.stdout).strip())
    raise RuntimeError("main kept moving while saving; try again")


LARGE = int(os.environ.get("TULE_MAX_BLOB", str(5 << 20)))  # the remote refuses plain blobs over this


def _track_large(root: str):
    """New or changed files over LARGE go to Git LFS (.gitattributes), as the remote wants them."""
    out = git(root, "ls-files", "-z", "--others", "--modified", "--exclude-standard").stdout
    for path in sorted(set(filter(None, out.split("\0")))):
        try:
            if os.path.getsize(os.path.join(root, path)) <= LARGE:
                continue
        except OSError:
            continue
        if git(root, "check-attr", "filter", "--", path).stdout.strip().endswith(": lfs"):
            continue
        git(root, "lfs", "track", "--filename", "--", path)


def _markers(root: str, path: str) -> bool:
    try:
        with open(os.path.join(root, path), "rb") as f:
            return b"<<<<<<< " in f.read()
    except OSError:
        return False


def _conflict(root: str) -> dict:
    files = git(root, "diff", "--name-only", "--diff-filter=U").stdout.split()
    return {
        "error": "merge conflict with the latest save",
        "conflicts": files,
        "hint": "resolve the <<<<<<< sections in these files in /workspace (a binary file: git checkout --ours or --theirs <file>), then save again; git merge --abort drops the merge",
    }
