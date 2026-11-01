"""The `tule` command line (see DESIGN.md, "`tule` CLI").

    tule login --url https://tule.<account>.workers.dev --token <dev token>
    tule up [-p bracket] [--machine cpu-1]   start the project's machine, wait until it is ready
    tule ssh [-p bracket] [cmd ...]          a shell or a command on the machine
    tule ssh --config [-p bracket]           write ~/.ssh/config so plain `ssh tule-bracket` works
    tule proxy tule-bracket                  the SSH ProxyCommand: stdin/stdout over the WebSocket tunnel
    tule status | down | open | join | keys add <file>

The project comes from -p, else $TULE_PROJECT, else the last one used.
Until accounts exist (milestone 4) `tule login` stores the control plane's
URL and its dev token in ~/.config/tule/config.json.
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
        raise CliError("not logged in: run tule login --url <control plane URL> --token <dev token>")
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
    if s["state"] != "running" or not s.get("hostKey"):
        s = wait_running(p, quiet=quiet)
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
    """Adds the CLI's key to the project and pins the project's host key."""
    pub = open(ensure_key() + ".pub").read().strip()
    cfg = load()
    added = set(cfg.get("keysAdded", []))
    if p not in added:
        api("POST", f"/api/projects/{p}/keys", {"key": pub})
        store(keysAdded=sorted(added | {p}))
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
    url = args.url or load().get("url")
    token = args.token or os.environ.get("TULE_TOKEN")
    if not url or not token:
        raise CliError("pass --url and --token (device-code sign-in comes with accounts)")
    store(url=url.rstrip("/"), token=token)
    print(f"tule: logged in to {url}")


def cmd_up(args):
    p = project_of(args, load())
    up(p, args.machine)


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
    url, _ = need(load())
    print(f"TULE_URL={url} TULE_PROJECT={p} TULE_JOIN_TOKEN={r['joinToken']}")


def cmd_open(args):
    p = project_of(args, load())
    url, token = need(load())
    link = f"{url}/p/{p}#t={token}"
    print(link)
    if not args.print:
        webbrowser.open(link)


def cmd_keys(args):
    p = project_of(args, load())
    key = open(os.path.expanduser(args.file)).read()
    r = api("POST", f"/api/projects/{p}/keys", {"key": key})
    print(f"tule: {p} has {r['keys']} SSH key(s)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tule", description="Tule: an engineering computer for your AI agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help, project=True):
        sp = sub.add_parser(name, help=help)
        if project:
            sp.add_argument("-p", "--project", help="project name (default: $TULE_PROJECT or the last one used)")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("login", cmd_login, "store the control plane URL and token", project=False)
    sp.add_argument("--url")
    sp.add_argument("--token")
    sp = add("up", cmd_up, "start (or resize) the project's machine")
    sp.add_argument("--machine", default="cpu-1", help="cpu-1 (default), cpu-2, cpu-4")
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
    sp = add("keys", cmd_keys, "add an SSH public key: tule keys add ~/.ssh/id_ed25519.pub")
    sp.add_argument("action", choices=["add"])
    sp.add_argument("file")

    args = ap.parse_args(argv)
    if args.cmd == "ssh" and args.command[:1] == ["--"]:
        args.command = args.command[1:]
    try:
        args.fn(args)
    except CliError as e:
        sys.exit(f"tule: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
