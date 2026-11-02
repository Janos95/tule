"""Your own computer as a Tule machine host (DESIGN.md, "Your own computers").

    tule host up --key tulep_…  [--name rechenknecht] [--url https://tule.example]
        check Docker (and the NVIDIA runtime if the computer has a GPU), add
        the computer to your account (with the one-time key from Settings, or
        without one by approving a code in the browser), and install the
        background service (systemd user unit / LaunchAgent)

Usually install.sh runs it: Settings → Your computers → Add a computer gives
`curl -fsSL <site>/install.sh | sh -s -- tulep_…`, which first installs Docker
and this CLI where they are missing.
    tule host status      the pairing, the service and the machines running here
    tule host down        stop and remove the service (--forget: unpair too)
    tule host run         the service itself: one WebSocket to Tule, starting and
                          stopping Tule's machine image for your projects

The service only ever runs Tule's own image (IMAGE below, by architecture;
the CUDA one when the machine wants a GPU), unprivileged, with no CPU or
memory limit and none of this computer's files; the cloud chooses only which
project and its TULE_* environment.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from . import CONFIG_DIR, DEFAULT_URL, CliError, load

HOST_CONFIG = os.path.join(CONFIG_DIR, "host.json")
IMAGE = os.environ.get("TULE_HOST_IMAGE", "ghcr.io/janos95/tule-runtime")
LABEL = "tule.project"
UNIT = "tule-host.service"
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.tule.host.plist")


def log(*a):
    print("tule host:", *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- this computer


_DOCKER: list[str] | None = None
DOCKER_PLACES = [
    "/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
    os.path.expanduser("~/.orbstack/bin/docker"),
    "/Applications/Docker.app/Contents/Resources/bin/docker",
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
]
SOCKETS = [os.path.expanduser("~/.orbstack/run/docker.sock"), os.path.expanduser("~/.docker/run/docker.sock")]


def find_docker() -> list[str]:
    """The docker command to use: on PATH or where OrbStack / Docker Desktop put it, talking to
    a running engine (setting DOCKER_HOST to the Mac apps' sockets, or going through `sg docker`
    right after Linux added you to the docker group)."""
    global _DOCKER
    if _DOCKER:
        return _DOCKER
    exe = shutil.which("docker") or next((p for p in DOCKER_PLACES if os.access(p, os.X_OK)), None)
    if not exe:
        raise CliError("Docker is not installed: install Docker Engine (Linux), or OrbStack / Docker Desktop (macOS); install.sh does it for you")

    def works(cmd: list[str]) -> str:
        r = subprocess.run([*cmd, "info", "--format", "{{.ServerVersion}}"] if cmd[0] != "sg" else
                           ["sg", "docker", "-c", f"{cmd[3]} info --format '{{{{.ServerVersion}}}}'"], capture_output=True, text=True)
        return "" if r.returncode == 0 else (r.stderr or r.stdout).strip()

    err = works([exe])
    if err and not os.environ.get("DOCKER_HOST"):
        for sock in SOCKETS:
            if os.path.exists(sock):
                os.environ["DOCKER_HOST"] = f"unix://{sock}"
                if not (err := works([exe])):
                    break
                del os.environ["DOCKER_HOST"]
    if err and "permission denied" in err.lower() and shutil.which("sg"):
        if not works(["sg", "docker", "-c", exe]):
            _DOCKER = ["sg", "docker", "-c", exe]
            return _DOCKER
    if err:
        raise CliError(f"Docker is installed but not running ({err[:200]}): start Docker Desktop / OrbStack, or `sudo systemctl start docker`")
    _DOCKER = [exe]
    return _DOCKER


def docker(*args: str, check: bool = True, capture: bool = True) -> str:
    cmd = find_docker()
    full = ["sg", "docker", "-c", shlex.join([cmd[3], *args])] if cmd[0] == "sg" else [*cmd, *args]
    r = subprocess.run(full, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise CliError(f"docker {args[0]}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout if capture else ""


def specs() -> dict:
    """What this computer offers: cores, memory, GPUs (only if Docker can use them), architecture."""
    info = json.loads(docker("info", "--format", "{{json .}}"))
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), platform.machine())
    gpus: list[str] = []
    if shutil.which("nvidia-smi"):
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True)
        names = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        if names and "nvidia" in (info.get("Runtimes") or {}):
            gpus = names
        elif names:
            log("this computer has", ", ".join(names), "but Docker cannot use them: install NVIDIA's container toolkit "
                "(nvidia-ctk runtime configure --runtime=docker) to run GPU machines here")
    return {
        "cpus": info.get("NCPU") or os.cpu_count(),
        "memoryGb": round((info.get("MemTotal") or 0) / 2**30),
        "gpus": gpus,
        "arch": arch,
        "os": f"{platform.system()} {platform.release()}",
        "docker": info.get("ServerVersion"),
    }


def read_config() -> dict | None:
    try:
        with open(HOST_CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_config(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = HOST_CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, HOST_CONFIG)


# ---------------------------------------------------------------- pairing


def pair_with_key(url: str, name: str, key: str, token: str | None) -> dict:
    body = {"key": key, "name": name, **({"token": token} if token else {})}
    req = urllib.request.Request(f"{url}/api/hosts/pair", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "tule-host/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            t = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        raise CliError(f"{body.get('error', e)}; {body.get('hint', '')}".rstrip("; ")) from None
    except urllib.error.URLError as e:
        raise CliError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from None
    log(f"{'already' if t.get('already') else 'added'} as {t['name']} in {t['email']}'s computers")
    return {"url": url, "id": t["hostId"], "name": t["name"], "token": t["hostToken"]}


def pair(url: str, name: str, open_browser: bool) -> dict:
    def post(path: str, body: dict) -> dict:
        req = urllib.request.Request(f"{url}{path}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "tule-host/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    try:
        d = post("/api/device/code", {"client": f"tule host on {socket.gethostname()}", "host": name})
    except urllib.error.URLError as e:
        raise CliError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from None
    log(f"to add this computer to your account, open {d['verification_uri_complete']}")
    log(f"and approve the code {d['user_code']}")
    if open_browser:
        webbrowser.open(d["verification_uri_complete"])
    deadline = time.time() + d["expires_in"]
    while time.time() < deadline:
        time.sleep(d.get("interval", 2))
        try:
            t = post("/api/device/token", {"device_code": d["device_code"]})
        except urllib.error.URLError:
            continue
        if t["status"] == "approved" and t.get("hostToken"):
            log(f"added as {t['name']} to {t['email']}'s computers")
            return {"url": url, "id": t["hostId"], "name": t["name"], "token": t["hostToken"]}
        if t["status"] == "expired":
            break
    raise CliError("the code expired; run tule host up again")


# ---------------------------------------------------------------- the service


def install_service():
    exe = shutil.which("tule") or ""
    cmd = [exe, "host", "run"] if exe else [sys.executable, "-m", "tule_cli", "host", "run"]
    find_docker()  # sets DOCKER_HOST for the Mac apps' engines; the service gets it below
    docker_dir = next((os.path.dirname(p) for p in [shutil.which("docker") or "", *DOCKER_PLACES] if p and os.access(p, os.X_OK)), "")
    path = ":".join(dict.fromkeys(filter(None, [docker_dir, *os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin").split(":")])))
    env = {"PATH": path, **{k: v for k, v in os.environ.items() if k in ("TULE_HOST_IMAGE", "TULE_CONFIG_DIR", "DOCKER_HOST", "DOCKER_CONTEXT")}}
    if sys.platform == "darwin":
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        logf = os.path.expanduser("~/Library/Logs/tule-host.log")
        args = "".join(f"<string>{a}</string>" for a in cmd)
        with open(PLIST, "w") as f:
            f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.tule.host</string>
<key>ProgramArguments</key><array>{args}</array>
<key>EnvironmentVariables</key><dict>{"".join(f"<key>{k}</key><string>{v}</string>" for k, v in env.items())}</dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
<key>StandardOutPath</key><string>{logf}</string><key>StandardErrorPath</key><string>{logf}</string>
</dict></plist>
""")
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", PLIST], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", PLIST], check=True)
        log(f"service installed (LaunchAgent, runs while you are logged in); log: {logf}")
        return
    if not shutil.which("systemctl"):
        raise CliError("no systemd here: run `tule host run` yourself (e.g. under your init system or in tmux)")
    d = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, UNIT), "w") as f:
        f.write(f"""[Unit]
Description=Tule machine host (runs Tule machines on this computer)
After=network-online.target docker.service

[Service]
ExecStart={' '.join(cmd)}
{chr(10).join(f"Environment={k}={v}" for k, v in env.items())}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"], capture_output=True, text=True).stdout
    log("service installed (systemd user unit); log: journalctl --user -u tule-host -f")
    if "Linger=yes" not in linger:
        log("to run it from boot without logging in: sudo loginctl enable-linger $USER")


def remove_service():
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", PLIST], capture_output=True)
        if os.path.exists(PLIST):
            os.remove(PLIST)
    elif shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], capture_output=True)
        p = os.path.expanduser(f"~/.config/systemd/user/{UNIT}")
        if os.path.exists(p):
            os.remove(p)


# ---------------------------------------------------------------- machines


def image_for(gpu: bool, arch: str) -> str:
    if gpu:
        return f"{IMAGE}:cuda"
    return f"{IMAGE}:cpu" if arch == "amd64" else f"{IMAGE}:cpu-{arch}"


def container(project: str) -> str:
    if not re.fullmatch(r"p_[a-z0-9]{8,32}", project):
        raise ValueError(f"not a project id: {project!r}")
    return f"tule-{project}"


def running() -> list[str]:
    out = docker("ps", "--filter", f"label={LABEL}", "--format", f'{{{{.Label "{LABEL}"}}}}', check=False)
    return [x for x in out.split() if x]


def start(project: str, env: dict, gpu: bool, arch: str):
    """Starts a project's machine with the newest image (or the one here if the registry is unreachable)."""
    name = container(project)
    image = image_for(gpu, arch)
    env = {k: str(v) for k, v in env.items() if re.fullmatch(r"TULE_[A-Z_]+", k)}  # nothing else gets in
    docker("rm", "-f", name, check=False)
    have = bool(docker("images", "-q", image, check=False).strip())
    if not have:
        log(f"downloading {image} (the first time only)")
    try:
        docker("pull", "-q", image)
    except CliError:
        if not have:
            raise
        log(f"could not update {image}; using the one here")
    args = ["run", "-d", "--name", name, "--label", f"{LABEL}={project}", "--restart", "no"]
    if gpu:
        args += ["--gpus", "all"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    docker(*args, image)
    log(f"started {name} ({image}{', GPUs' if gpu else ''})")


def stop(project: str):
    docker("rm", "-f", container(project), check=False)
    log(f"stopped tule-{project}")


async def serve(cfg: dict):
    import websockets

    url = cfg["url"].replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/hosts/connect"
    sp = specs()
    delay = 1.0
    while True:
        try:
            async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {cfg['token']}"},
                                          ping_interval=20, ping_timeout=20, open_timeout=20) as ws:
                delay = 1.0
                log(f"connected as {cfg['name']} ({sp['cpus']} cores, {sp['memoryGb']} GB{', ' + ', '.join(sp['gpus']) if sp['gpus'] else ''})")
                await ws.send(json.dumps({"t": "hello", "name": cfg["name"], "specs": sp}))

                async def beat():
                    while True:
                        await ws.send(json.dumps({"t": "beat", "running": await asyncio.to_thread(running)}))
                        await asyncio.sleep(30)

                async def run_one(m):
                    try:
                        pulled_first = not docker("images", "-q", image_for(bool(m.get("gpu")), sp["arch"]), check=False).strip()
                        await ws.send(json.dumps({"t": "reply", "rid": m["rid"], "ok": True, "pulling": pulled_first}))
                        await asyncio.to_thread(start, m["project"], m.get("env") or {}, bool(m.get("gpu")) and bool(sp["gpus"]), sp["arch"])
                    except Exception as e:  # report every failure to the project
                        log("could not start", m.get("project"), e)
                        await ws.send(json.dumps({"t": "failed", "project": m.get("project"), "error": str(e)[:300]}))

                tasks = [asyncio.create_task(beat())]
                try:
                    async for raw in ws:
                        m = json.loads(raw)
                        if m.get("t") == "run":
                            tasks.append(asyncio.create_task(run_one(m)))
                        elif m.get("t") == "stop":
                            try:
                                await asyncio.to_thread(stop, m["project"])
                                await ws.send(json.dumps({"t": "reply", "rid": m["rid"], "ok": True}))
                            except Exception as e:
                                await ws.send(json.dumps({"t": "reply", "rid": m["rid"], "error": str(e)[:300]}))
                        elif m.get("t") == "forget":
                            log("this computer was removed from the account; stopping its machines")
                            for p in await asyncio.to_thread(running):
                                await asyncio.to_thread(stop, p)
                            os.remove(HOST_CONFIG)
                            return
                finally:
                    for t in tasks:
                        t.cancel()
        except (OSError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - reconnect on anything
            if getattr(e, "status_code", None) == 401 or "401" in str(e):
                # a clean exit: the service manager restarts only after a crash (Restart=on-failure, SuccessfulExit)
                log("Tule does not know this computer any more; pair it again: tule host up")
                return
            log("connection lost:", repr(e)[:200])
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)


# ---------------------------------------------------------------- commands


def cmd_up(args):
    sp = specs()  # fails early without Docker
    cfg = read_config()
    url = (args.url or (cfg or {}).get("url") or load().get("url") or DEFAULT_URL).rstrip("/")
    if not cfg or cfg.get("url") != url or args.name and args.name != cfg.get("name") or args.key:
        name = args.name or socket.gethostname().split(".")[0]
        same = cfg and cfg.get("url") == url
        cfg = pair_with_key(url, name, args.key, cfg["token"] if same else None) if args.key else pair(url, name, not args.no_browser)
        write_config(cfg)
    install_service()
    gpus = f", GPUs: {', '.join(sp['gpus'])}" if sp["gpus"] else ""
    log(f"{cfg['name']} is ready: {sp['cpus']} cores, {sp['memoryGb']} GB{gpus}. Start a machine here with: tule up -p <project> --on {cfg['name']}")


def cmd_status(args):
    cfg = read_config()
    if not cfg:
        print("not paired: run tule host up")
        return
    print(f"{cfg['name']} at {cfg['url']} (id {cfg['id']})")
    if sys.platform == "darwin":
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.tule.host"], capture_output=True, text=True)
        print("service:", "running" if "state = running" in r.stdout else "not running")
    elif shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True)
        print("service:", r.stdout.strip() or "not installed")
    ps = running()
    print("machines here:", ", ".join(ps) if ps else "none")


def cmd_down(args):
    remove_service()
    for p in running():
        stop(p)
    if args.forget and os.path.exists(HOST_CONFIG):
        os.remove(HOST_CONFIG)
        log("unpaired here; remove it from your account in Settings too")
    log("service removed")


def cmd_run(args):
    cfg = read_config()
    if not cfg:  # removed from the account (or never paired): a clean exit, so the service is not restarted
        log("not paired: run tule host up")
        return
    asyncio.run(serve(cfg))


def add_commands(sub):
    sp = sub.add_parser("host", help="this computer as a Tule machine host: tule host up | status | down")
    hs = sp.add_subparsers(dest="host_cmd", required=True)
    up = hs.add_parser("up", help="pair this computer and install its service")
    up.add_argument("--key", help="the one-time key from Settings → Your computers (else: approve a code in the browser)")
    up.add_argument("--name", help="the computer's name in Tule (default: its hostname)")
    up.add_argument("--url", help=f"the Tule site (default {DEFAULT_URL})")
    up.add_argument("--no-browser", action="store_true")
    up.set_defaults(fn=cmd_up)
    hs.add_parser("status", help="the pairing, the service and the machines here").set_defaults(fn=cmd_status)
    down = hs.add_parser("down", help="stop and remove the service")
    down.add_argument("--forget", action="store_true", help="also unpair this computer here")
    down.set_defaults(fn=cmd_down)
    hs.add_parser("run", help="the service itself (the background service runs this)").set_defaults(fn=cmd_run)
