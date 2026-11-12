"""The `tule` command line (see DESIGN.md, "`tule` CLI").

    tule login [--url https://tule.example]  sign in: approve a code in the browser once
    tule new bracket                         create a project (tule ls lists them, tule delete removes one)
    tule up [-p bracket] [--machine cpu-1]   start the project's machine, wait until it is ready
    tule up --gpu rtx4090                    a GPU machine on your RunPod account (tule gpus lists them)
    tule up --on rechenknecht                the machine on one of your own computers (tule hosts lists them)
    tule host up                             on your own computer: add it to your account (see host.py)
    tule ssh [-p bracket] [cmd ...]          a shell or a command on the machine
    tule ssh --config [-p bracket]           write ~/.ssh/config so plain `ssh tule-bracket` works
    tule proxy tule-bracket                  the SSH ProxyCommand: stdin/stdout over the WebSocket tunnel
    tule snapshot | snapshots | restore <id>   save /workspace now, list snapshots, go back to one
    tule status | down | open | join | keys add <file>

On the machine (and on a laptop, for local files; see machine.py):

    tule screenshot -o /tmp/view.png [--output NAME | FILE] [--view iso] [--labels]
    tule inspect FILE | tule docs [topic]
    tule snapshot                            on the machine: the local tule-agent saves /workspace now

The project comes from -p, else $TULE_PROJECT, else the last one used.
`tule login` stores the control plane's URL and an API key for your account
in ~/.config/tule/config.json ($TULE_URL and $TULE_TOKEN override them).
"""

from __future__ import annotations

import argparse
import json
import os
try:
    import pwd
except ImportError:  # Windows
    pwd = None
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

DEFAULT_URL = "https://tule.janosmeny.com"
CONFIG_DIR = os.path.expanduser(os.environ.get("TULE_CONFIG_DIR", "~/.config/tule"))
CONFIG = os.path.join(CONFIG_DIR, "config.json")
KEY = os.path.join(CONFIG_DIR, "id_ed25519")
KNOWN_HOSTS = os.path.join(CONFIG_DIR, "known_hosts")
HOST_PREFIX = "tule-"


class CliError(Exception):
    pass


# ---------------------------------------------------------------- config


def load() -> dict:
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if os.environ.get("TULE_URL"):
        cfg["url"] = os.environ["TULE_URL"]
    if os.environ.get("TULE_TOKEN"):
        cfg["token"] = os.environ["TULE_TOKEN"]
    return cfg


def store(**kv):
    cfg = {}
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg.update(kv)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


def need(cfg: dict) -> tuple[str, str]:
    if not cfg.get("url") or not cfg.get("token"):
        raise CliError("not logged in: run tule login")
    return cfg["url"].rstrip("/"), cfg["token"]


def _machine_info() -> dict:
    """On a Tule machine, its project and control plane (from the tule-agent's machine file)."""
    try:
        with open(os.environ.get("TULE_MACHINE_FILE", "/etc/tule/machine.json")) as f:
            m = json.load(f)
        return {k: m[k] for k in ("url", "project") if m.get(k)}
    except (OSError, ValueError):
        return {}


def project_of(args, cfg) -> str:
    p = (getattr(args, "project", None) or os.environ.get("TULE_PROJECT") or cfg.get("project")
         or _machine_info().get("project"))
    if not p:
        raise CliError("which project? pass -p <name> (lowercase letters, digits and '-')")
    if p != cfg.get("project"):
        store(project=p)
    return p


# ---------------------------------------------------------------- HTTP


def api(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    """One call to the control plane. ``timeout``: the server waits up to 10 minutes for a machine to snapshot
    or restore, so those calls pass a longer one."""
    url, token = need(load())
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "tule-cli/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)
        except ValueError:
            err = {"error": e.reason}
        msg = err.get("error", str(e))
        if err.get("hint"):
            msg += f" ({err['hint']})"
        raise CliError(f"{method} {path}: {msg}") from None
    except urllib.error.URLError as e:
        raise CliError(f"cannot reach {url}: {e.reason}") from None
    except (TimeoutError, ConnectionError) as e:  # a slow answer is a TimeoutError, not a URLError
        raise CliError(f"{method} {path}: no answer from {url} within {timeout:g} s ({e or type(e).__name__}); it may still finish: tule status") from None


def status(p: str) -> dict:
    return api("GET", f"/api/projects/{p}")


def slot_of(machine: str) -> str:
    """The slot a machine name runs in: cpu-* share the one Tule cloud machine; others are their own."""
    if machine.startswith("cpu-") or machine == "cloud":
        return "cloud"
    return "joined" if machine == "external" else machine


def find(s: dict, slot: str) -> dict:
    """A slot's entry in a status (state "stopped" when it is not listed)."""
    for m in s.get("machines") or []:
        if m["id"] == slot:
            return m
    return {"id": slot, "state": "stopped"}


def cloud_size(s: dict) -> str:
    """The size the Tule cloud machine last ran at (cpu-1 when it never ran)."""
    size = find(s, "cloud").get("size") or ""
    return size if size.startswith("cpu-") else "cpu-1"


def live(s: dict) -> list[dict]:
    return [m for m in s.get("machines") or [] if m["state"] != "stopped"]


def wait_running(p: str, slot: str, timeout: float = 120) -> dict:
    t0 = time.time()
    shown = None
    while True:
        s = status(p)
        m = find(s, slot)
        if m["state"] == "running" and s.get("hostKey"):
            return s
        if m["state"] != shown:
            print(f"tule: {p} on {slot} is {m['state']}…", file=sys.stderr)
            shown = m["state"]
        if m["state"] == "stopped" and m.get("lastError"):
            raise CliError(f"{p}: {slot} did not start: {m['lastError']}")
        if m["state"] == "stopped" and slot == "cloud" and time.time() - t0 > 10:
            raise CliError(f"{p}: {slot} stopped while starting (tule status; check the Worker's logs)")
        if slot != "cloud" and m["state"] == "stopped" and time.time() - t0 > timeout:
            raise CliError(f"{p}: the computer {slot} is not connected (is its tule join agent running?); "
                           "--machine cloud uses a Tule machine instead")
        if time.time() - t0 > timeout:
            raise CliError(f"{p}: {slot} is {m['state']} but its agent did not connect within {int(timeout)} s "
                           "(the container's logs say why: Cloudflare dashboard, or docker logs in local dev)")
        time.sleep(0.5)


def up(p: str, machine: str) -> dict:
    t0 = time.time()
    slot = slot_of(machine)
    s = api("POST", f"/api/projects/{p}/up", {"machine": machine})
    if s.get("note"):
        print(f"tule: {s['note']}", file=sys.stderr)
    if find(s, slot)["state"] != "running" or not s.get("hostKey"):
        # a GPU pod or your own computer may first download the image
        s = wait_running(p, slot, timeout=120 if machine.startswith("cpu-") else 1500)
    cs = find(s, slot).get("coldStartMs")
    extra = f" (cold start {cs / 1000:.1f} s: start to agent connected)" if cs and time.time() - t0 > 1 else ""
    others = [m["id"] for m in live(s) if m["id"] != slot]
    print(f"tule: {p} is running on {slot}{extra}" + (f"; also on {', '.join(others)}" if others else ""), file=sys.stderr)
    return s


# ---------------------------------------------------------------- ssh


def ensure_key() -> str:
    if not os.path.exists(KEY):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "tule", "-f", KEY], check=True)
    return KEY


def ensure_access(p: str, s: dict):
    """Adds the CLI's key to the account (once) and pins the project's host key."""
    pub = open(ensure_key() + ".pub").read().strip()
    cfg = load()
    if cfg.get("keyAdded") != cfg.get("url"):
        api("POST", "/api/keys/ssh", {"key": pub, "name": f"tule CLI ({socket.gethostname()})"})
        store(keyAdded=cfg.get("url"))
    host = HOST_PREFIX + p
    line = f"{host} {s['hostKey'].strip()}\n"
    lines = []
    if os.path.exists(KNOWN_HOSTS):
        lines = [x for x in open(KNOWN_HOSTS) if not x.startswith(host + " ")]
    if line not in lines:
        with open(KNOWN_HOSTS, "w") as f:
            f.writelines(lines + [line])


def ssh_options(p: str) -> list[str]:
    proxy = f"{shlex.quote(sys.executable)} -m tule_cli proxy %h"
    return [
        "-o", f"ProxyCommand={proxy}",
        "-o", f"HostKeyAlias={HOST_PREFIX}{p}",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",  # a machine busy with a build may answer late: 3 minutes before giving up
        "-o", "LogLevel=ERROR",
        "-i", KEY,
    ]


def cmd_ssh(args):
    cfg = load()
    p = project_of(args, cfg)
    if args.config:
        return write_ssh_config(p)
    s = status(p)
    if args.machine:
        slot = slot_of(args.machine)
        st = find(s, slot)["state"]
        if st == "starting" or (st == "stopped" and slot == "joined"):
            s = wait_running(p, slot, timeout=60)  # a joined machine (re)connects by itself
        elif st != "running" or not s.get("hostKey"):
            s = up(p, cloud_size(s) if args.machine == "cloud" else args.machine)
    else:
        if len(live(s)) > 1:
            raise CliError(f"{p} runs on {' and '.join(m['id'] for m in live(s))}: say which with --machine")
        # a joined computer that is (re)connecting is still the project's machine: wait for it, don't start another.
        # A stopped GPU or computer is not: with nothing running, the Tule cloud machine starts at its last size.
        joined = [m for m in s.get("machines") or [] if m.get("kind", m["id"]) == "joined"]
        m = live(s)[0] if live(s) else joined[0] if joined else None
        slot = m["id"] if m else "cloud"
        if m and slot != "cloud" and (m["state"] != "running" or not s.get("hostKey")):
            s = wait_running(p, slot, timeout=60)
        elif not m or m["state"] != "running" or not s.get("hostKey"):
            s = up(p, m["size"] if m else cloud_size(s))
    ensure_access(p, s)
    argv = ["ssh", *ssh_options(p)]
    if args.command and not (sys.stdin.isatty() and sys.stdout.isatty()):  # no terminal: bytes pass unchanged
        argv.append("-T")
    argv += [f"root@{HOST_PREFIX}{p}.{slot}", *args.command]
    if os.name == "nt" or args.command:
        import signal

        signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C goes to ssh, which ends the command
        rc = subprocess.call(argv)
        if rc == 255 and args.command:  # ssh's own failure, which it does not explain at LogLevel=ERROR
            print(f"tule: the connection to {p} dropped (ssh exit 255); what the command started may still run there: "
                  f"a tule build goes on (tule build --check shows it, outputs/.build/build.log its log)", file=sys.stderr)
        sys.exit(rc)
    os.execvp("ssh", argv)


def write_ssh_config(p: str):
    s = status(p)
    if s.get("hostKey"):
        ensure_access(p, s)
    path = os.path.expanduser("~/.ssh/config")
    block = [f"Host {HOST_PREFIX}{p} {HOST_PREFIX}{p}.*", "  User root"]  # .* names one of several machines
    opts = ssh_options(p)
    for k, v in zip(opts[::2], opts[1::2]):
        if k == "-o":
            key, _, val = v.partition("=")
            block.append(f"  {key} {val}")
        else:
            block.append(f"  IdentityFile {v}")
    try:
        text = open(path).read() if os.path.exists(path) else ""
    except OSError as e:
        raise CliError(f"cannot read {path} ({e.strerror}); without it, tule ssh -p {p} -- COMMAND works") from None
    start, end = f"# tule {p} begin\n", f"# tule {p} end\n"
    if start in text:
        a = text.index(start)
        b = text.index(end, a) + len(end)
        text = text[:a] + text[b:]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text.rstrip("\n") + ("\n\n" if text.strip() else "") + start + "\n".join(block) + "\n" + end)
    except OSError as e:
        raise CliError(f"cannot write {path} ({e.strerror}); without it, tule ssh -p {p} -- COMMAND works") from None
    # ssh reads the real home's config, not $HOME's (an agent's sandbox may set HOME to its folder)
    real = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".ssh", "config") if pwd else path
    f = "" if os.path.realpath(path) == os.path.realpath(real) else f"-F {path} "
    print(f"tule: wrote {HOST_PREFIX}{p} to {path}; run: ssh {f}{HOST_PREFIX}{p}, scp {f}FILE {HOST_PREFIX}{p}:/workspace/… "
          f"(start the machine first: tule up -p {p})")


def cmd_proxy(args):
    """stdin/stdout <-> the project's SSH WebSocket."""
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    host = args.host
    p = host[len(HOST_PREFIX):] if host.startswith(HOST_PREFIX) else host
    p, _, slot = p.partition(".")  # tule-<project>.<machine>: one of several machines
    url, token = need(load())
    ws_url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + f"/api/projects/{p}/ssh"
    if slot:
        ws_url += f"?machine={urllib.parse.quote(slot)}"
    try:
        ws = connect(ws_url, additional_headers={"Authorization": f"Bearer {token}"}, max_size=None,
                     open_timeout=30, ping_interval=20, compression=None)
    except Exception as e:  # report every failure on stderr, where ssh shows it
        sys.exit(f"tule proxy: cannot open the tunnel to {p}: {e}")

    def upstream():
        fd = sys.stdin.fileno()
        try:
            while data := os.read(fd, 65536):
                ws.send(data)
        except (OSError, ConnectionClosed):
            pass
        finally:
            ws.close()

    threading.Thread(target=upstream, daemon=True).start()
    out = sys.stdout.fileno()
    try:
        for msg in ws:
            os.write(out, msg if isinstance(msg, bytes) else msg.encode())
    except ConnectionClosed:
        pass
    reason = ws.close_reason
    if ws.close_code and ws.close_code >= 4000 and reason:
        print(f"tule proxy: {reason}", file=sys.stderr)


# ---------------------------------------------------------------- the rest


def cmd_login(args):
    url = (args.url or os.environ.get("TULE_URL") or load().get("url") or DEFAULT_URL).rstrip("/")
    if args.token:  # an API key from Settings (or the dev token)
        store(url=url, token=args.token, keyAdded=None)
        print(f"tule: logged in to {url}")
        return
    if args.code:  # approved in advance: the connector's connect_info hands it to the agent
        req = urllib.request.Request(f"{url}/api/device/token", data=json.dumps({"device_code": args.code}).encode(),
                                     method="POST", headers={"Content-Type": "application/json", "User-Agent": "tule-cli/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                t = json.load(r)
        except urllib.error.URLError as e:
            raise CliError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from None
        if t.get("status") != "approved":
            raise CliError("this sign-in code is used up or expired; ask the connector's connect_info for a new one")
        store(url=url, token=t["apiKey"], keyAdded=None)
        print(f"tule: logged in to {url} as {t['email']}", file=sys.stderr)
        return
    req = urllib.request.Request(f"{url}/api/device/code", data=json.dumps({"client": f"tule CLI on {socket.gethostname()}"}).encode(),
                                 method="POST", headers={"Content-Type": "application/json", "User-Agent": "tule-cli/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except urllib.error.URLError as e:
        raise CliError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from None
    print(f"tule: to sign in, open {d['verification_uri_complete']}\n      and approve the code {d['user_code']}", file=sys.stderr)
    if not args.no_browser:
        webbrowser.open(d["verification_uri_complete"])
    deadline = time.time() + d["expires_in"]
    while time.time() < deadline:
        time.sleep(d.get("interval", 2))
        req = urllib.request.Request(f"{url}/api/device/token", data=json.dumps({"device_code": d["device_code"]}).encode(),
                                     method="POST", headers={"Content-Type": "application/json", "User-Agent": "tule-cli/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                t = json.load(r)
        except urllib.error.URLError:
            continue
        if t["status"] == "approved":
            store(url=url, token=t["apiKey"], keyAdded=None)
            print(f"tule: logged in to {url} as {t['email']}", file=sys.stderr)
            return
        if t["status"] == "expired":
            break
    raise CliError("the sign-in code expired; run tule login again")


def cmd_new(args):
    r = api("POST", "/api/projects", {"name": args.name})
    store(project=args.name)
    print(f"tule: created {args.name}; viewer: {r['viewer']}\n      load it on a machine: tule up -p {args.name}")


def cmd_ls(args):
    for p in api("GET", "/api/projects")["projects"]:
        ms = p.get("machines") or []
        where = ", ".join(f"{m['id']} {m['state']}" + (" (unsaved)" if sum((m.get("unsaved") or {}).values()) else "") for m in ms) or "not loaded"
        print(f"{p['name']:<32} {where:<36} {p['viewer']}")


def cmd_delete(args):
    if args.yes != args.name:
        raise CliError(f"this deletes {args.name}'s /workspace and every snapshot; confirm with --yes {args.name}")
    api("DELETE", f"/api/projects/{args.name}")
    print(f"tule: deleted {args.name}")


def cmd_up(args):
    p = project_of(args, load())
    if sum(map(bool, (args.gpu, args.on, args.machine != "cpu-1"))) > 1:
        raise CliError("choose one of --machine, --gpu and --on")
    s = up(p, f"gpu-{args.gpu}" if args.gpu else args.on or args.machine)
    if s.get("hostKey"):
        ensure_access(p, s)  # plain `ssh tule-<project>` (tule ssh --config) works from here on


def cmd_hosts(args):
    hs = api("GET", "/api/hosts")["hosts"]
    if not hs:
        print("no computers yet: run `tule host up` on one", file=sys.stderr)
    for h in hs:
        sp = h.get("specs") or {}
        what = f"{sp.get('cpus', '?')} cores, {sp.get('memoryGb', '?')} GB" + (f", {', '.join(sp['gpus'])}" if sp.get("gpus") else "")
        run = h.get("running") or []
        print(f"{h['name']:<20} {'online' if h['online'] else 'offline':<8} {what:<48} {len(run)} running")


def cmd_gpus(args):
    for g in api("GET", "/api/gpus")["gpus"]:
        price = f"${g['pricePerHour']:.2f}/h" if g.get("pricePerHour") is not None else "?"
        vram = f"{g['vramGb']} GB" if g.get("vramGb") else ""
        print(f"{g['machine']:<16} {g['gpu']:<28} {vram:>7} {price:>9}  {g['availability']}")


def cmd_down(args):
    p = project_of(args, load())
    api("POST", f"/api/projects/{p}/down", {"machine": args.machine} if args.machine else {}, timeout=150)
    print(f"tule: {p} stopped" + (f" on {args.machine}" if args.machine else ""), file=sys.stderr)


def cmd_forget(args):
    p = project_of(args, load())
    r = api("POST", f"/api/projects/{p}/forget", {"machine": args.machine})
    print(f"tule: {p}: forgot the copy on {r['forgot']} (its unsaved work is gone)", file=sys.stderr)


def _over_ssh(args, cmd: list[str]):
    """Runs a tule command in the machine's /workspace (this is not a project folder, or -p was given)."""
    return cmd_ssh(argparse.Namespace(project=args.project, machine=args.machine, config=False,
                                      command=[f"cd /workspace && {shlex.join(cmd)}"]))


def _maybe_stale(root: str):
    """Agent work whose sources changed, said once after a build or check."""
    from . import build

    stale = [r for r in build.based_on_status(root) if r["changed"]]
    if len(stale) > 3:  # one line for many
        print(f"tule: check {len(stale)} files in work/ again ({', '.join(r['path'] for r in stale)}): "
              f"their sources changed; redo each, or if it still holds: tule based-on <file>", file=sys.stderr)
        return
    for r in stale:
        print(f"tule: check {r['path']} again ({', '.join(r['changed'])} changed): "
              f"redo it, or if it still holds: tule based-on {r['path']}", file=sys.stderr)


def _check(root: str, names: list[str] | None = None):
    """What needs updating: outputs that are out of date or failed (with the reason and how long
    the last run took), outputs whose script is gone, and work in work/ to check again (all of
    it, or just the named outputs). Prints a line when all is current; returns whether it is."""
    from . import build
    from .cache import Cache

    only = set(build.select(build.targets(root), names)) if names else None
    lines = []
    cache = Cache.here()

    def cached(name: str) -> str | None:
        nonlocal cache
        try:
            return build.cached_on(root, name, cache, hashes) if cache else None
        except Exception:  # offline: --check still answers
            cache = None
            return None

    hashes = build.Hashes(root)  # one for the whole check: each file hashed once
    now = build.running(root)
    for r in build.status(root, hashes):
        if only is not None and r["name"] not in only:
            continue
        took = f"last run {r['seconds']:g} s" if r.get("seconds") is not None else ""
        if r["name"] in now:
            lines.append((r["name"], "building", f"started {time.strftime('%H:%M', time.localtime(now[r['name']]))}; "
                          "tule build again waits for it; --stop ends it", took))
        elif r.get("failed"):
            last = (r.get("log") or "").strip().splitlines()[-1:]  # the exception, for a script that raised
            lines.append((r["name"], "failed", last[0] if last and r["failed"].startswith("exit status") else r["failed"], took))
        elif r.get("why"):
            where = cached(r["name"])
            lines.append((r["name"], "out of date", r["why"] + (f" (the cache has a matching build from {where}: tule build fetches it)" if where else ""), took))
    for n in [] if only is not None else build.orphans(root):
        lines.append((n, "no script", f"src/{n}.py is gone: delete outputs/{n} if it is not needed", ""))
    for w in [] if only is not None else build.based_on_status(root, hashes):
        if w["changed"]:
            lines.append((w["path"], "check again", f"{', '.join(w['changed'])} changed", ""))
    if not lines:
        print("everything is up to date")
        return True
    width = max(len(x[0]) for x in lines)
    for name, state, why, took in lines:
        print(f"{name:<{width}}  {state:<11}  {why}" + (f"  ({took})" if took else ""))
    return False


def cmd_build(args):
    """Runs the scripts in src/ whose inputs changed: here when this is a project (a machine's /workspace), else on the machine over SSH."""
    from . import build

    root = None if args.project else build.find_root()
    if root is None and not args.project and build.root_without_src():
        raise CliError("no src/ folder: put build scripts in src/ (src/<name>.py makes outputs/<name>/)")
    if root is None:
        return _over_ssh(args, ["tule", "build", *args.names] + (["--check"] if args.check else []) + (["--force"] if args.force else []) + (["--stop"] if args.stop else [])
                         + (["--dependents"] if args.dependents else []) + [x for n in args.skip for x in ("--skip", n)])
    if args.stop:
        pids = build.stop(root)
        print(f"stopped {len(pids)} build{'s' * (len(pids) != 1)}" if pids else "no build is running", file=sys.stderr)
        return
    if not args.check and not os.environ.get("TULE_BUILD_CHILD"):
        return _build_detached(root, sys.argv[sys.argv.index("build") + 1:] if "build" in sys.argv else [])
    try:
        if args.check:  # exit status 1 when anything needs updating: tule build --check && tule save
            if not _check(root, args.names):
                sys.exit(1)
            return
        done = build.build(root, args.names or None, force=args.force, dependents=args.dependents, skip=args.skip)
    except build.BuildError as e:
        raise CliError(str(e)) from None
    counts = {k: sum(1 for v in done.values() if v == k) for k in ("built", "fetched", "up to date", "skipped", "failed")}
    print("tule: " + ", ".join(f"{n} {k}" for k, n in counts.items() if n), file=sys.stderr)
    _maybe_stale(root)
    if counts["failed"]:
        sys.exit(1)


def _build_detached(root: str, argv: list[str]):
    """tule build in a session of its own, its log followed here: the build goes on when the SSH
    command that started it ends (a tool timeout, a dropped connection); tule build again waits
    for it. Ctrl-C stops it."""
    import signal

    from . import build

    state = os.path.join(root, "outputs", build.STATE)
    os.makedirs(state, exist_ok=True)
    # each build its own log, so one waiting for another shows only its own lines; build.log links to
    # the one holding the lock (build.build)
    path = os.path.join(state, f"build-{os.getpid()}.log")
    with open(path, "wb") as out:
        child = subprocess.Popen([sys.executable, "-m", "tule_cli", "build", *argv], cwd=root, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=out, start_new_session=True,
                                 env={**os.environ, "TULE_BUILD_CHILD": "1", "TULE_BUILD_LOG": path})
    signal.signal(signal.SIGINT, lambda *_: os.killpg(child.pid, signal.SIGTERM))
    for sig in (signal.SIGHUP, signal.SIGPIPE):
        signal.signal(sig, signal.SIG_IGN)
    shown = True
    with open(path, "rb") as log_:
        while True:
            done = child.poll() is not None
            chunk = log_.read()
            if chunk and shown:
                try:
                    sys.stderr.buffer.write(chunk)
                    sys.stderr.flush()
                except (BrokenPipeError, OSError):
                    shown = False  # nobody reads any more: the build goes on by itself
            if done and not chunk:
                break
            if not chunk:
                time.sleep(0.2)
    rc = child.returncode
    sys.exit(128 - rc if rc < 0 else rc)  # stopped by a signal: 128 + its number, as a shell reports it


def cmd_based_on(args):
    """Records what a piece of agent work in work/ was based on; without sources, that it still holds."""
    from . import build

    root = None if args.project else build.find_root()
    if root is None:
        return _over_ssh(args, ["tule", "based-on", args.result, *args.sources])
    try:
        srcs = build.based_on(root, args.result, args.sources or None)
    except build.BuildError as e:
        raise CliError(str(e)) from None
    what = "still holds; recorded" if not args.sources else "recorded"
    print(f"tule: {build._clean(root, args.result)} is based on {', '.join(srcs)} ({what})", file=sys.stderr)


def cmd_save(args):
    """Commit, merge what other machines saved, push to main: on the machine through the local tule-agent, else through the API."""
    cfg = load()
    from . import machine

    if not args.project and not (cfg.get("url") and cfg.get("token")) and machine.on_machine():
        r = machine.local_request({"op": "save", "message": args.message or ""})
    else:
        p = project_of(args, cfg)
        body = {"message": args.message or "", **({"machine": args.machine} if args.machine else {})}
        url, token = need(cfg)
        req = urllib.request.Request(url + f"/api/projects/{p}/save", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "tule-cli/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=900) as res:
                r = json.load(res)
        except urllib.error.HTTPError as e:
            try:
                r = json.load(e)
            except ValueError:
                raise CliError(f"save: {e.reason}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise CliError(f"save: cannot reach {url} ({getattr(e, 'reason', None) or e or type(e).__name__}); it may still finish: tule status") from None
    r = r.get("result", r)
    if r.get("error"):
        files = "".join(f"\n  {f}" for f in r.get("conflicts") or [])
        raise CliError(f"{r['error']}{files}" + (f"\n{r['hint']}" if r.get("hint") else ""))
    print(f"tule: saved {r['saved'][:12]}" + (" (merged the latest save)" if r.get("merged") else "") + ("" if r.get("committed", True) else " (nothing new to commit)"))
    if r.get("warning"):
        print(f"tule: {r['warning']}", file=sys.stderr)
    root = _project_root() if not args.project and machine.on_machine() else None
    if root:
        _say_stale(root)


def _project_root():
    from . import build

    try:
        return build.find_root()
    except Exception:
        return None


def _say_stale(root: str):
    """After a save: outputs saved out of date or failed, which the viewer marks too."""
    from . import build

    try:
        bad = [r["name"] for r in build.status(root) if r.get("failed") or r.get("why")]
    except Exception:
        return
    if bad:
        print(f"tule: saved with {len(bad)} output{'s' * (len(bad) != 1)} out of date or failed "
              f"({', '.join(bad[:5])}{', …' if len(bad) > 5 else ''}): tule build, then tule save", file=sys.stderr)


def cmd_status(args):
    cfg = load()
    from . import build, machine

    root = None if args.project or (cfg.get("url") and cfg.get("token")) else build.find_root()
    if root and machine.on_machine():  # on the machine itself: its outputs, and what is not saved
        _check(root)
        r = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
        n = len([x for x in r.stdout.splitlines() if x.strip()]) if r.returncode == 0 else 0
        print(f"{n} changed file{'s' if n != 1 else ''} not saved (tule save)" if n else "everything is saved")
        return
    p = project_of(args, cfg)
    print(json.dumps(status(p), indent=2))


def cmd_join(args):
    p = project_of(args, load())
    r = api("POST", f"/api/projects/{p}/join")
    print(" ".join(f"{k}={v}" for k, v in r["env"].items()))


def cmd_open(args):
    p = project_of(args, load())
    url = (load().get("url") or _machine_info().get("url") or "").rstrip("/")
    if not url:
        url, _ = need(load())
    link = f"{url}/p/{p}"
    print(link)
    if not args.print:
        webbrowser.open(link)


def cmd_snapshot(args):
    cfg = load()
    if not args.project and not (cfg.get("url") and cfg.get("token")):
        from . import machine

        if machine.on_machine():
            return machine.snapshot_here()
    p = project_of(args, cfg)
    r = api("POST", f"/api/projects/{p}/snapshot", {"machine": args.machine} if args.machine else {}, timeout=650)
    print(f"tule: {p}: snapshot {r['snapshot']}" + ("" if r.get("changed", True) else " (nothing changed since)"))


def cmd_snapshots(args):
    p = project_of(args, load())
    q = f"&machine={urllib.parse.quote(args.machine)}" if args.machine else ""
    r = api("GET", f"/api/projects/{p}/snapshots?limit={args.limit}{q}")
    for s in r["snapshots"]:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["at"] / 1000))
        print(f"{s['id']}  {when}  {s.get('machine') or '':<14} {s['files']:>6} files  {s['bytes'] / 2**20:>9.1f} MiB  {s['reason']}")


def cmd_restore(args):
    p = project_of(args, load())
    r = api("POST", f"/api/projects/{p}/restore", {"id": args.id, **({"machine": args.machine} if args.machine else {})}, timeout=650)
    print(f"tule: {p}: /workspace is now snapshot {r['snapshot']} ({r['files']} files written; the state before is saved too)")


def cmd_keys(args):
    if args.action == "ls":
        for k in api("GET", "/api/keys/ssh")["keys"]:
            print(f"{k['id']}  {k['name']:<32} {k['key'][:40]}…")
        return
    if not args.file:
        raise CliError("tule keys add <file.pub>")
    try:
        key = open(os.path.expanduser(args.file)).read()
    except OSError as e:
        raise CliError(f"{args.file}: {e.strerror}") from None
    r = api("POST", "/api/keys/ssh", {"key": key})
    print(f"tule: SSH key {r['id']} {'added' if r.get('added') else 'was already there'}; it works on every project's machine")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tule", description="Tule: an engineering computer for your AI agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, project=True):
        sp = sub.add_parser(name, help=help)
        if project:
            sp.add_argument("-p", "--project", help="project name (default: $TULE_PROJECT or the last one used)")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("login", cmd_login, "sign in (approve a code in the browser)", project=False)
    sp.add_argument("--url", help=f"the Tule site (default {DEFAULT_URL})")
    sp.add_argument("--token", help="an API key from Settings instead of the browser")
    sp.add_argument("--code", help="a one-time sign-in code from the connector's connect_info (no browser)")
    sp.add_argument("--no-browser", action="store_true", help="only print the link")
    sp = add("new", cmd_new, "create a project", project=False)
    sp.add_argument("name")
    add("ls", cmd_ls, "list your projects", project=False)
    sp = add("delete", cmd_delete, "delete a project (its /workspace and snapshots)", project=False)
    sp.add_argument("name")
    sp.add_argument("--yes", metavar="NAME", help="the name again, to confirm")
    sp = add("up", cmd_up, "load the project on a machine (others keep running); resizes the Tule cloud one")
    sp.add_argument("--machine", default="cpu-1", help="cpu-1 (default), cpu-2, cpu-4, or gpu-<type>")
    sp.add_argument("--gpu", metavar="TYPE", help="a GPU on your RunPod account, e.g. rtx4090 (tule gpus)")
    sp.add_argument("--on", metavar="COMPUTER", help="one of your own computers (tule hosts)")
    add("hosts", cmd_hosts, "your own computers: online, cores, memory, GPUs", project=False)
    add("gpus", cmd_gpus, "GPU machines: price and availability on your RunPod account", project=False)
    sp = add("down", cmd_down, "stop the project's machines (or one: --machine)")
    sp.add_argument("--machine", help="cloud, gpu-<type>, a computer's name, or joined")
    sp = add("forget", cmd_forget, "drop a stopped machine's copy and its unsaved work")
    sp.add_argument("--machine", required=True)
    sp = add("build", cmd_build, "run the scripts in src/ whose inputs changed; each makes outputs/<name>/")
    sp.add_argument("names", nargs="*", help="only these (and what they read); patterns work: 'plate-*'")
    sp.add_argument("--check", action="store_true", help="list what is out of date or failed (and work to check again); run nothing")
    sp.add_argument("--force", action="store_true", help="run them even if nothing changed")
    sp.add_argument("--stop", action="store_true", help="stop the builds running here (and those waiting for them)")
    sp.add_argument("--dependents", action="store_true", help="also rebuild what reads them (the BOM after the part)")
    sp.add_argument("--skip", action="append", default=[], metavar="NAME", help="leave this one as it is (a long study); repeatable, patterns work")
    sp.add_argument("--machine", help="which machine (when several run)")
    sp = add("based-on", cmd_based_on, "record what agent work in work/ was made from, so it is flagged when that changes")
    sp.add_argument("result", help="the work, e.g. work/suppliers/bracket.json")
    sp.add_argument("sources", nargs="*", help="what it was made from, e.g. outputs/bracket (none: it was checked and still holds)")
    sp.add_argument("--machine", help="which machine (when several run)")
    sp = add("save", cmd_save, "commit, merge the latest save, and push it (on the machine: this copy)")
    sp.add_argument("-m", "--message", help="what changed")
    sp.add_argument("--machine", help="which machine's copy (when several run)")
    add("status", cmd_status, "the project's machine, timings and outputs")
    sp = add("ssh", cmd_ssh, "a shell or a command on the machine (starts it if needed)")
    sp.add_argument("--machine", help="which machine (when several run), or the one to start (default cpu-1)")
    sp.add_argument("--config", action="store_true", help="write an ~/.ssh/config entry instead")
    sp.add_argument("command", nargs=argparse.REMAINDER)
    sp = add("proxy", cmd_proxy, "SSH ProxyCommand (used by tule ssh)", project=False)
    sp.add_argument("host")
    sp = add("open", cmd_open, "open the viewer")
    sp.add_argument("--print", action="store_true", help="only print the link")
    add("join", cmd_join, "a join token for a machine you start yourself")
    sp = add("snapshot", cmd_snapshot, "autosave /workspace now (on the machine: through the local tule-agent)")
    sp.add_argument("--machine")
    sp = add("snapshots", cmd_snapshots, "list the autosaves of /workspace, newest first")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--machine", help="only this machine's copy")
    sp = add("restore", cmd_restore, "make a copy of /workspace match an autosave (saves the current state first)")
    sp.add_argument("id")
    sp.add_argument("--machine", help="restore into this machine (default: the one that made the snapshot)")
    sp = add("keys", cmd_keys, "SSH keys: tule keys ls | tule keys add ~/.ssh/id_ed25519.pub", project=False)
    sp.add_argument("action", choices=["add", "ls"])
    sp.add_argument("file", nargs="?")
    from . import machine

    machine.add_commands(add)
    from . import host

    host.add_commands(sub)
    from . import admin

    admin.add_commands(sub)

    args = ap.parse_args(argv)
    if args.cmd in ("inspect", "docs") and hasattr(signal, "SIGPIPE"):  # `tule inspect … | head` ends quietly
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    if args.cmd == "ssh" and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    try:
        args.fn(args)
    except CliError as e:
        sys.exit(f"tule: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
