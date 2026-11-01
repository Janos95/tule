"""The `tule` command line (see DESIGN.md, "`tule` CLI").

    tule login [--url https://tule.example]  sign in: approve a code in the browser once
    tule new bracket                         create a project (tule ls lists them, tule delete removes one)
    tule up [-p bracket] [--machine cpu-1]   start the project's machine, wait until it is ready
    tule up --gpu rtx4090                    a GPU machine on your RunPod account (tule gpus lists them)
    tule ssh [-p bracket] [cmd ...]          a shell or a command on the machine
    tule ssh --config [-p bracket]           write ~/.ssh/config so plain `ssh tule-bracket` works
    tule proxy tule-bracket                  the SSH ProxyCommand: stdin/stdout over the WebSocket tunnel
    tule snapshot | snapshots | restore <id>   save /workspace now, list snapshots, go back to one
    tule status | down | open | join | keys add <file>

On the machine (and on a laptop, for local files; see machine.py):

    tule screenshot -o view.png [--output NAME | FILE] [--view iso] [--labels]
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
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

DEFAULT_URL = "https://tule.genau-mcp-relay.workers.dev"
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


def project_of(args, cfg) -> str:
    p = getattr(args, "project", None) or os.environ.get("TULE_PROJECT") or cfg.get("project")
    if not p:
        raise CliError("which project? pass -p <name> (lowercase letters, digits and '-')")
    if p != cfg.get("project"):
        store(project=p)
    return p


# ---------------------------------------------------------------- HTTP


def api(method: str, path: str, body: dict | None = None) -> dict:
    url, token = need(load())
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "tule-cli/0.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
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


def status(p: str) -> dict:
    return api("GET", f"/api/projects/{p}")


def wait_running(p: str, timeout: float = 120, quiet: bool = False) -> dict:
    t0 = time.time()
    shown = None
    while True:
        s = status(p)
        if s["state"] == "running" and s.get("hostKey"):
            return s
        if s["state"] != shown and not quiet:
            print(f"tule: {p} is {s['state']}…", file=sys.stderr)
            shown = s["state"]
        if s["state"] == "stopped" and time.time() - t0 > 10:
            raise CliError(f"{p}: the machine stopped while starting (tule status; check the Worker's logs)")
        if time.time() - t0 > timeout:
            raise CliError(f"{p}: the machine is {s['state']} but its agent did not connect within {int(timeout)} s "
                           "(the container's logs say why: Cloudflare dashboard, or docker logs in local dev)")
        time.sleep(0.5)


def up(p: str, machine: str, quiet: bool = False) -> dict:
    t0 = time.time()
    s = api("POST", f"/api/projects/{p}/up", {"machine": machine})
    if s.get("note") and not quiet:
        print(f"tule: {s['note']}", file=sys.stderr)
    if s["state"] != "running" or not s.get("hostKey"):
        s = wait_running(p, timeout=1500 if machine.startswith("gpu-") else 120, quiet=quiet)
    if not quiet:
        cs = s.get("coldStartMs")
        extra = f" (cold start {cs / 1000:.1f} s: start to agent connected)" if cs and time.time() - t0 > 1 else ""
        print(f"tule: {p} is running on {s['machine']}{extra}", file=sys.stderr)
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
        api("POST", "/api/keys/ssh", {"key": pub, "name": f"tule CLI ({os.uname().nodename})"})
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
        "-o", "LogLevel=ERROR",
        "-i", KEY,
    ]


def cmd_ssh(args):
    cfg = load()
    p = project_of(args, cfg)
    if args.config:
        return write_ssh_config(p)
    s = status(p)
    if s["state"] != "running" or not s.get("hostKey"):
        s = up(p, args.machine)
    ensure_access(p, s)
    argv = ["ssh", *ssh_options(p)]
    if args.command and not sys.stdin.isatty():
        argv.append("-T")
    argv += [f"root@{HOST_PREFIX}{p}", *args.command]
    if os.name == "nt":
        sys.exit(subprocess.call(argv))
    os.execvp("ssh", argv)


def write_ssh_config(p: str):
    s = status(p)
    if s.get("hostKey"):
        ensure_access(p, s)
    path = os.path.expanduser("~/.ssh/config")
    block = [f"Host {HOST_PREFIX}{p}", "  User root"]
    opts = ssh_options(p)
    for k, v in zip(opts[::2], opts[1::2]):
        if k == "-o":
            key, _, val = v.partition("=")
            block.append(f"  {key} {val}")
        else:
            block.append(f"  IdentityFile {v}")
    text = open(path).read() if os.path.exists(path) else ""
    start, end = f"# tule {p} begin\n", f"# tule {p} end\n"
    if start in text:
        a = text.index(start)
        b = text.index(end, a) + len(end)
        text = text[:a] + text[b:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text.rstrip("\n") + ("\n\n" if text.strip() else "") + start + "\n".join(block) + "\n" + end)
    print(f"tule: wrote {HOST_PREFIX}{p} to {path}; run: ssh {HOST_PREFIX}{p} (start the machine first: tule up -p {p})")


def cmd_proxy(args):
    """stdin/stdout <-> the project's SSH WebSocket."""
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    host = args.host
    p = host[len(HOST_PREFIX):] if host.startswith(HOST_PREFIX) else host
    url, token = need(load())
    ws_url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + f"/api/projects/{p}/ssh"
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
    req = urllib.request.Request(f"{url}/api/device/code", data=json.dumps({"client": f"tule CLI on {os.uname().nodename}"}).encode(),
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
    print(f"tule: created {args.name}; viewer: {r['viewer']}\n      start its machine: tule up -p {args.name}")


def cmd_ls(args):
    for p in api("GET", "/api/projects")["projects"]:
        state = p["state"] if p["state"] == "stopped" else f"{p['machine']} {p['state']}"
        print(f"{p['name']:<32} {state:<20} {p['viewer']}")


def cmd_delete(args):
    if args.yes != args.name:
        raise CliError(f"this deletes {args.name}'s /workspace and every snapshot; confirm with --yes {args.name}")
    api("DELETE", f"/api/projects/{args.name}")
    print(f"tule: deleted {args.name}")


def cmd_up(args):
    p = project_of(args, load())
    up(p, f"gpu-{args.gpu}" if args.gpu else args.machine)


def cmd_gpus(args):
    for g in api("GET", "/api/gpus")["gpus"]:
        price = f"${g['pricePerHour']:.2f}/h" if g.get("pricePerHour") is not None else "?"
        vram = f"{g['vramGb']} GB" if g.get("vramGb") else ""
        print(f"{g['machine']:<16} {g['gpu']:<28} {vram:>7} {price:>9}  {g['availability']}")


def cmd_down(args):
    p = project_of(args, load())
    api("POST", f"/api/projects/{p}/down")
    print(f"tule: {p} stopped", file=sys.stderr)


def cmd_status(args):
    p = project_of(args, load())
    print(json.dumps(status(p), indent=2))


def cmd_join(args):
    p = project_of(args, load())
    r = api("POST", f"/api/projects/{p}/join")
    print(" ".join(f"{k}={v}" for k, v in r["env"].items()))


def cmd_open(args):
    p = project_of(args, load())
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
    r = api("POST", f"/api/projects/{p}/snapshot")
    print(f"tule: {p}: snapshot {r['snapshot']}" + ("" if r.get("changed", True) else " (nothing changed since)"))


def cmd_snapshots(args):
    p = project_of(args, load())
    r = api("GET", f"/api/projects/{p}/snapshots?limit={args.limit}")
    for s in r["snapshots"]:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["at"] / 1000))
        print(f"{s['id']}  {when}  {s['files']:>6} files  {s['bytes'] / 2**20:>9.1f} MiB  {s['reason']}")


def cmd_restore(args):
    p = project_of(args, load())
    r = api("POST", f"/api/projects/{p}/restore", {"id": args.id})
    print(f"tule: {p}: /workspace is now snapshot {r['snapshot']} ({r['files']} files written; the state before is saved too)")


def cmd_keys(args):
    if args.action == "ls":
        for k in api("GET", "/api/keys/ssh")["keys"]:
            print(f"{k['id']}  {k['name']:<32} {k['key'][:40]}…")
        return
    if not args.file:
        raise CliError("tule keys add <file.pub>")
    key = open(os.path.expanduser(args.file)).read()
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
    sp.add_argument("--no-browser", action="store_true", help="only print the link")
    sp = add("new", cmd_new, "create a project", project=False)
    sp.add_argument("name")
    add("ls", cmd_ls, "list your projects", project=False)
    sp = add("delete", cmd_delete, "delete a project (its /workspace and snapshots)", project=False)
    sp.add_argument("name")
    sp.add_argument("--yes", metavar="NAME", help="the name again, to confirm")
    sp = add("up", cmd_up, "start (or resize) the project's machine")
    sp.add_argument("--machine", default="cpu-1", help="cpu-1 (default), cpu-2, cpu-4, or gpu-<type>")
    sp.add_argument("--gpu", metavar="TYPE", help="a GPU on your RunPod account, e.g. rtx4090 (tule gpus)")
    add("gpus", cmd_gpus, "GPU machines: price and availability on your RunPod account", project=False)
    add("down", cmd_down, "stop the project's machine")
    add("status", cmd_status, "the project's machine, timings and outputs")
    sp = add("ssh", cmd_ssh, "a shell or a command on the machine (starts it if needed)")
    sp.add_argument("--machine", default="cpu-1")
    sp.add_argument("--config", action="store_true", help="write an ~/.ssh/config entry instead")
    sp.add_argument("command", nargs=argparse.REMAINDER)
    sp = add("proxy", cmd_proxy, "SSH ProxyCommand (used by tule ssh)", project=False)
    sp.add_argument("host")
    sp = add("open", cmd_open, "open the viewer")
    sp.add_argument("--print", action="store_true", help="only print the link")
    add("join", cmd_join, "a join token for a machine you start yourself")
    add("snapshot", cmd_snapshot, "save /workspace now (on the machine: through the local tule-agent)")
    sp = add("snapshots", cmd_snapshots, "list the snapshots of /workspace, newest first")
    sp.add_argument("--limit", type=int, default=20)
    sp = add("restore", cmd_restore, "make /workspace match a snapshot (saves the current state first)")
    sp.add_argument("id")
    sp = add("keys", cmd_keys, "SSH keys: tule keys ls | tule keys add ~/.ssh/id_ed25519.pub", project=False)
    sp.add_argument("action", choices=["add", "ls"])
    sp.add_argument("file", nargs="?")
    from . import machine

    machine.add_commands(add)

    args = ap.parse_args(argv)
    if args.cmd == "ssh" and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    try:
        args.fn(args)
    except CliError as e:
        sys.exit(f"tule: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
