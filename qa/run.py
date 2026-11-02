#!/usr/bin/env python3
"""Tule's agent QA: real coding agents use Tule the way a user would, all night.

For each scenario (scenarios.toml) and agent (codex, claude):
  1. a fresh guest account on a local control plane (port 8788, its own state), a project,
     and a machine for it (a tule-runtime:dev container joined to the project), set up;
  2. the agent gets the user's opening message with Tule's connector (an API key of the
     guest), and a simulated user (Claude, prompts/user.md) answers it turn by turn, looking
     at the agent's replies and a screenshot of the viewer, until the goal is met or it gives up;
  3. everything is kept in the run's folder: transcripts, screenshots, the project afterwards,
     the control plane's log; a reviewer (Claude, prompts/review.md) lists findings.
At the end, report.md merges the findings of the night (prompts/report.md).

    python3 qa/run.py                       # every scenario with both agents, once
    python3 qa/run.py --hours 8 --repeat 5  # all night: rounds until the time is up
    python3 qa/run.py --scenarios thicker --agents codex --turns 2

Runs go to qa/runs/<time>/. The agents run on this computer, each in its own folder with its
own tule CLI and sign-in; Codex in its workspace-write sandbox, Claude in its Bash sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.parse
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

QA = Path(__file__).resolve().parent
ROOT = QA.parent
PORT = int(os.environ.get("TULE_QA_PORT", "8788"))
URL = f"http://127.0.0.1:{PORT}"
MACHINE_URL = f"http://host.docker.internal:{PORT}"
# Machines on another computer (start with TULE_QA_DOCKER=ssh://rechenknecht): its Docker runs them, and
# `run.py tunnel rechenknecht` carries their connection to this control plane: there, docker0's PORT (what
# host.docker.internal is) leads to its 127.0.0.1:REMOTE_PORT, the ssh -R end. So they use MACHINE_URL
# as local ones do, the address the control plane gives out (git LFS uploads go there).
REMOTE_PORT = PORT + 10000
IMAGE = os.environ.get("TULE_QA_IMAGE", "tule-runtime:dev")
TURN_MINUTES = 40
# the Codex CLI of the ChatGPT app is newer than Homebrew's and knows the newest models
APP_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
CODEX = os.environ.get("TULE_QA_CODEX") or (APP_CODEX if os.path.exists(APP_CODEX) else "codex")
CACHE = QA / ".cache"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- the control plane


def freeze(night: Path) -> Path:
    """A copy of the control plane and viewer for the night: wrangler dev reloads on every edit
    of its sources, which would drop every run's SSH sessions while Tule is being fixed."""
    subprocess.run(["node", "web/build.mjs", "--dev"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    app = night / "app"
    shutil.rmtree(app, ignore_errors=True)
    shutil.copytree(ROOT / "control", app / "control", ignore=shutil.ignore_patterns("node_modules", ".wrangler"))
    (app / "control" / "node_modules").symlink_to(ROOT / "control" / "node_modules")
    for d in ("web/dist", "web/src", "skills", "templates"):
        shutil.copytree(ROOT / d, app / d)
    (app / "runtime").mkdir()
    shutil.copy(ROOT / "runtime" / "Dockerfile", app / "runtime")
    return app


def start_control(night: Path) -> subprocess.Popen:
    """A wrangler dev of a frozen copy of control/ on PORT with the night's own state; machines
    reach it from Docker. `run.py serve --restart` takes up Tule's changes (it drops live sessions)."""
    app = freeze(night)
    out = open(night / "control.log", "w")
    p = subprocess.Popen(
        ["npx", "wrangler", "dev", "--port", str(PORT), "--ip", "127.0.0.1", "--inspector-port", str(PORT + 1000),
         "--enable-containers=false", "--persist-to", str(night / "state"), "--local-upstream", f"127.0.0.1:{PORT}",
         "--var", f"MACHINE_URL:{MACHINE_URL}", "--var", f"PUBLIC_URL:{URL}"],
        cwd=app / "control", stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(120):
        try:
            urllib.request.urlopen(URL + "/__dev/version", timeout=2)
            return p
        except Exception:
            time.sleep(1)
    raise SystemExit(f"the control plane did not start; see {night / 'control.log'}")


class Guest:
    """A fresh guest account, as a first visit makes one: a session cookie and the API."""

    def __init__(self, ip: str | None = None, session: str | None = None):
        self.jar = CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        # each guest from its own "network", so the per-network guest limit does not stop the night
        self.ip = ip or f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
        self.session = session
        if not session:
            self.page("/")
            self.session = next((c.value for c in self.jar if c.name == "tule_session"), None)
            if not self.session:
                raise RuntimeError("no guest session (guest limit?)")

    def cookie(self) -> dict:
        return {"Cookie": f"tule_session={self.session}"} if self.session else {}

    def page(self, path: str) -> str:
        req = urllib.request.Request(URL + path, headers={"CF-Connecting-IP": self.ip, **self.cookie()})
        return self.http.open(req, timeout=30).read().decode()

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(URL + path, method=method, data=json.dumps(body or {}).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Origin": URL, "CF-Connecting-IP": self.ip, **self.cookie()})
        try:
            r = self.http.open(req, timeout=120).read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:300]!r}") from None
        return json.loads(r) if r.strip() else {}


# ---------------------------------------------------------------- the machine


def sh(*cmd, timeout=600, check=True, **kw) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(map(str, cmd))[:200]}: {r.stderr.strip()[-500:] or r.stdout.strip()[-500:]}")
    return r.stdout


def cores(name: str) -> str:
    """Four cores of the other computer for this machine (so nproc says 4, and pools are sized to it)."""
    n = int(sh("docker", "info", "--format", "{{.NCPU}}").strip() or 4)
    k = int.from_bytes(name.encode(), "little") % max(1, n // 4)
    return f"{4 * k}-{4 * k + 3}"


def start_machine(g: Guest, project: str, name: str):
    env = g.api("POST", f"/api/projects/{project}/join", {})["env"]
    sh("docker", "rm", "-f", name, check=False)
    # each its own share, as a cloud machine has (cpu-4: 4 vCPUs, 12 GiB), not the whole Docker VM
    sh("docker", "run", "-d", "--name", name, "--add-host", "host.docker.internal:host-gateway", "--memory", "12g",
       *(["--cpuset-cpus", cores(name)] if os.environ.get("DOCKER_HOST") else ["--cpus", "4"]),
       *[x for k, v in env.items() for x in ("-e", f"{k}={v}")], IMAGE)
    for _ in range(180):
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        if "workspace ready" in logs.stdout + logs.stderr:
            s = g.api("GET", f"/api/projects/{project}")
            if any(m["state"] == "running" for m in s.get("machines", [])):
                return
        time.sleep(1)
    raise RuntimeError(f"machine {name} did not come up: {sh('docker', 'logs', '--tail', '20', name, check=False)}")


def on_machine(name: str, script: str, timeout=600) -> str:
    r = subprocess.run(["docker", "exec", name, "bash", "-lc", f"cd /workspace && {script}"],
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.stdout + (f"\n[stderr]\n{r.stderr}" if r.stderr.strip() else "") + (f"\n[exit {r.returncode}]" if r.returncode else "")


def set_up(name: str, sc: dict, run: Path):
    out = []
    for dest, src in sc.get("files", {}).items():
        on_machine(name, f"mkdir -p {os.path.dirname(dest) or '.'}")
        sh("docker", "cp", str(ROOT / src), f"{name}:/workspace/{dest}")
    for cmd in sc.get("setup", []):
        out.append(f"$ {cmd}\n{on_machine(name, cmd)}")
    (run / "setup.log").write_text("\n".join(out))


MACHINE_DUMP = r"""
echo '## git log'; git log --stat --format='--- %h %s' -n 20 2>&1
echo; echo '## not saved'; git status --short 2>&1
echo; echo '## files (size path)'; find . -path ./.git -prune -o -path ./outputs/.build -prune -o -type f -printf '%s %p\n' | sort -k2
echo; echo '## tule build --check'; tule build --check 2>&1
for f in $(find src lib work inputs tule.toml NOTES.md -type f -size -40k 2>/dev/null | grep -v __pycache__ | sort); do
  case "$f" in *.png|*.jpg|*.step|*.stl|*.pdf) continue;; esac
  echo; echo "## $f"; head -c 20000 "$f"
done
"""


# ---------------------------------------------------------------- the agents


def agent_env(home: Path, key: str) -> dict:
    """The agent's own tule CLI (the connector's install command puts it here) and sign-in."""
    e = dict(os.environ)
    e.update(TULE_QA_KEY=key, TULE_CONFIG_DIR=str(home / ".tule"), UV_TOOL_DIR=str(home / ".uv-tools"),
             UV_TOOL_BIN_DIR=str(home / ".bin"), UV_CACHE_DIR=str(CACHE / "uv"),
             UV_PYTHON_INSTALL_DIR=str(CACHE / "uv-python"), PATH=f"{home / '.bin'}:{e['PATH']}")
    for k in ("TULE_WORKSPACE", "TULE_PROJECT", "TULE_MACHINE_FILE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        e.pop(k, None)
    return e


def codex_turn(home: Path, env: dict, msg: str, session: str | None, out: Path) -> tuple[str, str | None]:
    opts = ["--json", "--skip-git-repo-check", "--ignore-user-config", "--disable", "apps", "-c", 'web_search="live"',
            *(["-m", env["TULE_QA_MODEL"]] if env.get("TULE_QA_MODEL") else []),
            "-c", 'sandbox_mode="workspace-write"', "-c", "sandbox_workspace_write.network_access=true",
            "-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(str(CACHE))}]",
            "-c", f'mcp_servers.tule.url="{URL}/mcp"', "-c", 'mcp_servers.tule.bearer_token_env_var="TULE_QA_KEY"',
            "-c", 'mcp_servers.tule.default_tools_approval_mode="approve"',  # a user would click Allow
            "-o", str(out.with_suffix(".last.txt"))]
    cmd = [CODEX, "exec", "resume", *opts, session, msg] if session else [CODEX, "exec", *opts, msg]
    run_logged(cmd, home, env, out)
    reply = ""
    for line in out.read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "thread.started":
            session = ev.get("thread_id") or session
        if ev.get("type") == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
            reply = ev["item"].get("text", "")
    last = out.with_suffix(".last.txt")
    return (last.read_text().strip() if last.exists() and last.read_text().strip() else reply.strip()), session


CLAUDE_SANDBOX = {"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                              "network": {"allowLocalBinding": True, "allowedDomains": ["127.0.0.1", "localhost"]}}}


def claude_turn(home: Path, env: dict, msg: str, session: str | None, out: Path) -> tuple[str, str | None]:
    mcp = home / ".mcp-qa.json"
    mcp.write_text(json.dumps({"mcpServers": {"tule": {"type": "http", "url": f"{URL}/mcp",
                                                       "headers": {"Authorization": f"Bearer {env['TULE_QA_KEY']}"}}}}))
    cmd = ["claude", "-p", msg, "--output-format", "stream-json", "--verbose", "--mcp-config", str(mcp),
           "--strict-mcp-config", "--setting-sources", "project", "--settings", json.dumps(CLAUDE_SANDBOX),
           "--permission-mode", "dontAsk", "--allowedTools", "Bash", "Read", "Write", "Edit", "Glob", "Grep", "mcp__tule",
           "--add-dir", str(CACHE)]
    if session:
        cmd += ["--resume", session]
    run_logged(cmd, home, env, out)
    reply = ""
    for line in out.read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        session = ev.get("session_id") or session
        if ev.get("type") == "result":
            reply = ev.get("result") or reply
    return reply.strip(), session


AGENTS = {"codex": codex_turn, "claude": claude_turn}


def run_logged(cmd: list[str], cwd: Path, env: dict, out: Path):
    with open(out, "w") as f:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            p.wait(timeout=TURN_MINUTES * 60)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)
            f.write(json.dumps({"type": "qa.timeout", "minutes": TURN_MINUTES}) + "\n")


# ---------------------------------------------------------------- reading transcripts


def clip(s, n=4000) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n // 2] + f"\n… ({len(s) - n} characters left out) …\n" + s[-n // 2:]


def readable(agent: str, raw: Path) -> str:
    """turn-N.md: what the agent did, in order."""
    out = []
    for line in raw.read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            out.append(clip(line, 500))
            continue
        if agent == "codex" and ev.get("type") == "item.completed":
            it = ev.get("item", {})
            t = it.get("type")
            if t == "agent_message":
                out.append(f"**agent:** {it.get('text', '')}")
            elif t == "command_execution":
                out.append(f"```\n$ {it.get('command')}\n{clip(it.get('aggregated_output', ''))}\n[exit {it.get('exit_code')}]\n```")
            elif t == "mcp_tool_call":
                out.append(f"**tule.{it.get('tool')}**({clip(it.get('arguments'), 800)}) → {clip(it.get('result') or it.get('error'), 3000)}")
            elif t == "reasoning":
                out.append(f"_thinking: {clip(it.get('text', ''), 800)}_")
            else:
                out.append(f"[{t}] {clip(it, 1500)}")
        elif agent == "codex" and ev.get("type") in ("error", "turn.failed", "qa.timeout"):
            out.append(f"**{ev['type']}:** {clip(ev, 1500)}")
        elif agent == "claude" and ev.get("type") in ("assistant", "user"):
            for c in ev.get("message", {}).get("content", []) if isinstance(ev.get("message", {}).get("content"), list) else []:
                if c.get("type") == "text":
                    out.append(f"**agent:** {c['text']}")
                elif c.get("type") == "tool_use":
                    out.append(f"**{c.get('name')}**({clip(c.get('input'), 1500)})")
                elif c.get("type") == "tool_result":
                    out.append(f"→ {'ERROR ' if c.get('is_error') else ''}{clip(c.get('content'))}")
        elif ev.get("type") in ("result", "qa.timeout"):
            out.append(f"[{ev['type']}] {clip({k: v for k, v in ev.items() if k in ('subtype', 'is_error', 'num_turns', 'duration_ms', 'total_cost_usd', 'minutes')}, 500)}")
    return "\n\n".join(out)


# ---------------------------------------------------------------- the simulated user and the reviewer


HELPER = "claude"  # plays the user, reviews, reports: claude when its CLI is signed in, else codex


def helper_works(name: str) -> bool:
    try:
        if name == "claude":
            r = subprocess.run(["claude", "-p", "Answer with the word ok.", "--output-format", "json"], capture_output=True, text=True, timeout=120,
                               env={k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")})
            return not json.loads(r.stdout).get("is_error")
        r = subprocess.run(["codex", "exec", "--skip-git-repo-check", "--ignore-user-config", "--disable", "apps", "Answer with the word ok."],
                           capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except Exception:
        return False


def ask(prompt: str, cwd: Path, images=(), write=False, timeout=900, add_dirs=()) -> str:
    """One answer from the helper model, which may read files (and with write, write in cwd)."""
    if HELPER == "codex":
        last = Path(tempfile.mkstemp(suffix=".txt")[1])
        cmd = ["codex", "exec", "--skip-git-repo-check", "--ignore-user-config", "--disable", "apps",
               "-c", f'sandbox_mode="{"workspace-write" if write else "read-only"}"', "-o", str(last)]
        for i in images:
            cmd += ["-i", str(i)]
        subprocess.run([*cmd, "--", prompt], cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        out = last.read_text() if last.exists() else ""
        last.unlink(missing_ok=True)
        return out
    tools = ["Read", "Glob", "Grep"] + (["Write"] if write else [])
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--strict-mcp-config", "--setting-sources", "project",
           "--permission-mode", "dontAsk", "--allowedTools", *tools]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout).get("result", "")
    except ValueError:
        return r.stdout + r.stderr


def json_in(text: str, kind="{"):
    """The first JSON object (or array) in a reply."""
    close = "}" if kind == "{" else "]"
    i, j = text.find(kind), text.rfind(close)
    try:
        return json.loads(text[i:j + 1]) if i >= 0 and j > i else None
    except ValueError:
        return None


def simulated_user(sc: dict, run: Path, conversation: str, shot: str) -> dict:
    prompt = (QA / "prompts" / "user.md").read_text() + (
        f"\n\nPersona: {sc['persona']}\nGoal: {sc['goal']}\n\nThe conversation so far:\n\n{conversation}\n\n"
        f"The viewer right now: {shot} (read it; it is also attached when your tools can attach it).")
    return json_in(ask(prompt, run, images=(shot,))) or {"say": "", "done": True, "satisfied": False, "notes": "the simulated user gave no answer"}


def screenshot(path: Path, key: str, next_: str):
    """The page as the guest sees it, once settled (shot.mjs; /__dev/as signs the browser in with
    the key, dev only). Whether it settled and its console errors go next to it (.json)."""
    r = subprocess.run(["node", str(QA / "shot.mjs"), f"{URL}/__dev/as?key={key}&next={urllib.parse.quote(next_, safe='/')}", str(path)],
                       capture_output=True, text=True, timeout=120)
    path.with_suffix(".json").write_text(r.stdout.strip() or json.dumps({"error": r.stderr.strip()[-1000:]}))


# ---------------------------------------------------------------- one run


def one_run(night: Path, sc: dict, agent: str, n: int, turns: int) -> dict:
    run = night / f"{sc['name']}-{agent}-{n}"
    (run / "shots").mkdir(parents=True)
    home = run / "home"
    home.mkdir()
    info = {"scenario": sc, "agent": agent, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "turns": [], "error": None}
    machine = f"tule-qa-{sc['name']}-{agent}-{n}"
    log_from = (night / "control.log").stat().st_size
    t0 = time.time()
    try:
        g = Guest()
        project = f"{sc['name']}"[:40]
        g.api("POST", "/api/projects", {"name": project})
        start_machine(g, project, machine)
        set_up(machine, sc, run)
        key = g.api("POST", "/api/keys/api", {"name": f"qa {agent}"})["key"]
        env = agent_env(home, key)
        # users say which project they mean (the agent may know nothing else about it)
        msg = sc["opening"] if sc.get("context") is False else f'I\'m working on my Tule project "{project}". {sc["opening"]}'
        session, conversation = None, ""
        for i in range(1, turns + 1):
            log(f"  turn {i}: {msg[:80]!r}")
            t = time.time()
            reply, session = AGENTS[agent](home, env, msg, session, run / f"turn-{i}.jsonl")
            (run / f"turn-{i}.md").write_text(readable(agent, run / f"turn-{i}.jsonl"))
            conversation += f"**User:** {msg}\n\n**Agent:** {reply or '(no reply)'}\n\n"
            (run / "conversation.md").write_text(conversation)
            screenshot(run / "shots" / f"viewer-{i}.png", key, f"/p/{project}")
            screenshot(run / "shots" / f"projects-{i}.png", key, "/")
            banner = "Connect your agent" in g.page("/")
            u = simulated_user(sc, run, conversation, str(run / "shots" / f"viewer-{i}.png"))
            info["turns"].append({"n": i, "minutes": round((time.time() - t) / 60, 1), "user": u, "banner": banner})
            if u.get("done") or not u.get("say"):
                break
            msg = u["say"]
        (run / "machine.md").write_text(on_machine(machine, MACHINE_DUMP))
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        log(f"  error: {info['error']}")
    finally:
        info["minutes"] = round((time.time() - t0) / 60, 1)
        info["goal_met"] = bool(info["turns"] and info["turns"][-1]["user"].get("satisfied"))
        with open(night / "control.log", "rb") as f:
            f.seek(log_from)
            (run / "control.log").write_bytes(f.read())
        local = [f"{p.stat().st_size} {p.relative_to(home)}" for p in sorted(home.rglob("*"))
                 if p.is_file() and not any(x in p.parts for x in (".uv-tools", ".bin", ".tule"))]
        (run / "local-files.md").write_text("Files the agent left in its folder on its own computer:\n\n" + "\n".join(local or ["(none)"]))
        (run / "run.json").write_text(json.dumps(info, indent=1))
        subprocess.run(["docker", "rm", "-f", machine], capture_output=True)
    if info["turns"]:
        review = ask((QA / "prompts" / "review.md").read_text().replace("{ROOT}", str(ROOT)), run, add_dirs=(ROOT / "skills", ROOT))
        findings = json_in(review, "[")
        (run / "findings.json").write_text(json.dumps(findings if findings is not None else [{"title": "reviewer gave no JSON", "severity": "low", "owner": "agent", "evidence": review[:2000], "fix": ""}], indent=1))
    return info


def report(night: Path):
    log("writing the report")
    ask((QA / "prompts" / "report.md").read_text(), night, write=True, timeout=1800)
    if not (night / "report.md").exists():  # the plain version
        rows = [json.loads(p.read_text()) for p in sorted(night.glob("*/run.json"))]
        lines = ["# QA report (plain: the report writer failed)", "", "| run | minutes | turns | goal met | error |", "|---|---|---|---|---|"]
        for p, r in zip(sorted(night.glob("*/run.json")), rows):
            lines.append(f"| {p.parent.name} | {r['minutes']} | {len(r['turns'])} | {r['goal_met']} | {r['error'] or ''} |")
        (night / "report.md").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- turn by turn (someone plays the user)
#
#   run.py serve [--no-image]                    build the image, start the control plane; prints the night
#   run.py start <night> <name> [--model M] [--scenario S | --file DEST=SRC ... --setup CMD ...]
#   run.py say <run> "message"                   send the user's message; returns at once
#   run.py wait <run> [--max 540]                the reply and what the agent did, once the turn is over
#   run.py shell <run> "cmd"                     a command on the run's machine, in /workspace
#   run.py shot <run> [/path]                    a screenshot of a page as the user sees it
#   run.py finish <run>                          the project afterwards, the logs; removes the machine
#   run.py stop <night>                          stops the control plane


def state(run: Path) -> dict:
    st = json.loads((run / "state.json").read_text())
    if st.get("docker"):  # its machine runs on another computer: every docker command goes there
        os.environ["DOCKER_HOST"] = st["docker"]
    return st


def save_state(run: Path, st: dict):
    (run / "state.json").write_text(json.dumps(st, indent=1))


def cmd_serve(a):
    night = (Path(a.night) if a.night else QA / "runs" / time.strftime("%Y-%m-%d-%H%M")).resolve()
    night.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    if not (QA / "node_modules" / "playwright-core").exists():
        sh("npm", "install", "--silent", cwd=QA)
    if not a.no_image:
        log(f"building {IMAGE}")
        sh("docker", "build", "-q", "-f", "runtime/Dockerfile", "-t", IMAGE, ".", cwd=ROOT, timeout=1800)
    if a.restart:
        cmd_stop(argparse.Namespace(night=str(night)))
        for _ in range(30):
            try:
                urllib.request.urlopen(URL + "/__dev/version", timeout=1)
                time.sleep(1)
            except Exception:
                break
    try:
        urllib.request.urlopen(URL + "/__dev/version", timeout=2)
        log(f"a control plane is already on {URL}; using it (--restart to take up changes)")
    except Exception:
        p = start_control(night)
        (night / "control.pid").write_text(str(p.pid))
    print(night)


def cmd_stop(a):
    pid = Path(a.night, "control.pid")
    if pid.exists():
        try:
            os.killpg(int(pid.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass
        pid.unlink()


def cmd_start(a):
    night = Path(a.night).resolve()
    run = night / a.name
    (run / "shots").mkdir(parents=True)
    (run / "home").mkdir()
    sc = {"name": a.name, "files": {}, "setup": []}
    if a.scenario:
        sc = next(x for x in tomllib.loads((QA / "scenarios.toml").read_text())["scenario"] if x["name"] == a.scenario)
    for f in a.file:
        dest, src = f.split("=", 1)
        sc.setdefault("files", {})[dest] = src
    sc["setup"] = [*sc.get("setup", []), *a.setup]
    project = re.sub(r"[^a-z0-9-]+", "-", a.name.lower()).strip("-")[:40]
    machine = f"tule-qa-{night.name}-{project}"[:60]
    log_from = (night / "control.log").stat().st_size if (night / "control.log").exists() else 0
    if os.environ.get("TULE_QA_DOCKER"):
        os.environ["DOCKER_HOST"] = os.environ["TULE_QA_DOCKER"]
    g = Guest()
    g.api("POST", "/api/projects", {"name": project})
    start_machine(g, project, machine)
    set_up(machine, sc, run)
    key = g.api("POST", "/api/keys/api", {"name": "qa codex"})["key"]
    save_state(run, {"project": project, "machine": machine, "key": key, "ip": g.ip, "session": g.session,
                     "model": a.model, "codex_session": None, "turn": 0, "busy": False, "log_from": log_from,
                     "started": time.time(), "night": str(night), "docker": os.environ.get("TULE_QA_DOCKER")})
    print(f"run {run}\nproject {project} on machine {machine} (model {a.model or 'default'})\nviewer {URL}/p/{project}")
    setup_log = (run / "setup.log").read_text()
    if setup_log.strip():
        print("setup:\n" + clip(setup_log, 3000))


def cmd_say(a):
    run = Path(a.run).resolve()
    st = state(run)
    if st["busy"]:
        raise SystemExit("the agent is still on the last turn; `wait` first")
    st["turn"] += 1
    st["busy"] = True
    msg = a.message if st["turn"] > 1 else f'I\'m working on my Tule project "{st["project"]}". {a.message}'
    (run / f"turn-{st['turn']}.msg").write_text(msg)
    save_state(run, st)
    subprocess.Popen([sys.executable, __file__, "_turn", str(run)], stdin=subprocess.DEVNULL,
                     stdout=open(run / f"turn-{st['turn']}.worker.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    print(f"turn {st['turn']} sent")


def summarize(raw: Path) -> dict:
    """What the agent did in a turn, counted (codex)."""
    cmds, failed, tools = 0, [], []
    for line in raw.read_text().splitlines() if raw.exists() else []:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        it = ev.get("item", {})
        if ev.get("type") != "item.completed":
            continue
        if it.get("type") == "command_execution":
            cmds += 1
            if it.get("exit_code") not in (0, None):
                failed.append(f"{clip(it.get('command', ''), 160)} → exit {it.get('exit_code')}: {clip(it.get('aggregated_output', '').strip()[-300:], 300)}")
        elif it.get("type") == "mcp_tool_call":
            res = it.get("result") or {}
            err = bool(it.get("error")) or (isinstance(res, dict) and res.get("is_error"))
            tools.append(it.get("tool", "?") + (" (error)" if err else ""))
        elif it.get("type") == "web_search":
            tools.append("web_search")
    return {"commands": cmds, "failed_commands": failed, "tools": tools}


def cmd_turn(a):
    run = Path(a.run).resolve()
    st = state(run)
    n = st["turn"]
    msg = (run / f"turn-{n}.msg").read_text()
    env = agent_env(run / "home", st["key"])
    if st.get("model"):
        env["TULE_QA_MODEL"] = st["model"]
    t = time.time()
    raw = run / f"turn-{n}.jsonl"
    try:
        reply, session = codex_turn(run / "home", env, msg, st.get("codex_session"), raw)
        (run / f"turn-{n}.md").write_text(readable("codex", raw))
        with open(run / "conversation.md", "a") as f:
            f.write(f"**User:** {msg}\n\n**Agent:** {reply or '(no reply)'}\n\n")
        screenshot(run / "shots" / f"viewer-{n}.png", st["key"], f"/p/{st['project']}")
        screenshot(run / "shots" / f"projects-{n}.png", st["key"], "/")
        g = Guest(st["ip"], st["session"])
        banner = "Connect your agent" in g.page("/")
        text = raw.read_text()
        timed_out = '"qa.timeout"' in text
        # the model's own quota, not Tule: the run is blocked until then
        quota = re.search(r"hit your usage limit[^\"]*?try again at ([^\".]+)", text)
        summary = {"turn": n, "minutes": round((time.time() - t) / 60, 1), "reply": reply, "banner": banner,
                   "timed_out": timed_out, **({"blocked": f"usage limit until {quota.group(1).strip()}"} if quota else {}),
                   **summarize(raw)}
    except Exception as e:
        session = st.get("codex_session")
        summary = {"turn": n, "error": f"{type(e).__name__}: {e}"}
    (run / f"turn-{n}.summary.json").write_text(json.dumps(summary, indent=1))
    st = state(run)
    st.update(busy=False, codex_session=session)
    save_state(run, st)


def cmd_quota(a):
    """How much of the ChatGPT account's Codex quota is used: the newest Codex session's last reading."""
    files = sorted(Path.home().glob(".codex/sessions/*/*/*/*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[:50]:
        found = re.findall(r'"(primary|secondary)":(\{[^}]*\})', f.read_text(errors="replace"))
        found = [(k, json.loads(v)) for k, v in found if "used_percent" in v]
        if found:
            for k, v in dict(found).items():
                print(f"{k}: {v['used_percent']:g}% used of the {v['window_minutes'] // 60} h window, "
                      f"resets {time.strftime('%a %d %b %H:%M', time.localtime(v['resets_at']))}")
            return
    print("no reading yet: no Codex session has reported its limits")


def cmd_wait(a):
    run = Path(a.run).resolve()
    end = time.time() + a.max
    while state(run)["busy"] and time.time() < end:
        time.sleep(5)
    st = state(run)
    if st["busy"]:
        n = st["turn"]
        raw, msg = run / f"turn-{n}.jsonl", run / f"turn-{n}.msg"  # .msg is written when the turn starts
        s = summarize(raw)
        print(f"still working on turn {n} ({round((time.time() - (msg.stat().st_mtime if msg.exists() else time.time())) / 60)} min so far, "
              f"{s['commands']} commands, tools: {', '.join(s['tools']) or 'none'}); wait again")
        return
    n = st["turn"]
    s = json.loads((run / f"turn-{n}.summary.json").read_text())
    if s.get("error"):
        print(f"turn {n} failed in the harness: {s['error']}")
        return
    shots = []
    for k in ("viewer", "projects"):
        png = run / "shots" / f"{k}-{n}.png"
        meta = json.loads(png.with_suffix(".json").read_text()) if png.with_suffix(".json").exists() else {}
        shots.append(f"{png}  (settled: {meta.get('settled')}, console errors: {len(meta.get('errors', []))}{' ' + clip(meta['errors'], 400) if meta.get('errors') else ''}{' ' + meta['error'][:300] if meta.get('error') else ''})")
    if s.get("blocked"):
        print(f"BLOCKED: Codex hit its {s['blocked']}. Not a Tule finding: stop the run here (finish), "
              "write findings for the turns before, and mark the outcome \"blocked by model quota\".\n")
    print(f"# turn {n}: {s['minutes']} min{', TIMED OUT' if s['timed_out'] else ''}\n\n## agent's reply\n{s['reply'] or '(no reply)'}\n\n"
          f"## what it did\n{s['commands']} shell commands, {len(s['failed_commands'])} failed\n"
          f"Tule tools: {', '.join(s['tools']) or 'none'}\n"
          + "".join(f"- failed: {x}\n" for x in s["failed_commands"][:12])
          + f"\nfull log: {run / f'turn-{n}.md'}\n\n## screenshots (Read them)\n" + "\n".join(shots)
          + f"\n\n\"Connect your agent\" banner still shown: {s['banner']}")


def cmd_shell(a):
    print(on_machine(state(Path(a.run))["machine"], a.command, timeout=a.timeout))


def cmd_shot(a):
    run = Path(a.run).resolve()
    st = state(run)
    path = a.path or f"/p/{st['project']}"
    out = run / "shots" / f"extra-{int(time.time())}.png"
    screenshot(out, st["key"], path)
    print(out, out.with_suffix(".json").read_text())


def cmd_finish(a):
    run = Path(a.run).resolve()
    st = state(run)
    (run / "machine.md").write_text(on_machine(st["machine"], MACHINE_DUMP))
    home = run / "home"
    local = [f"{p.stat().st_size} {p.relative_to(home)}" for p in sorted(home.rglob("*"))
             if p.is_file() and not any(x in p.parts for x in (".uv-tools", ".bin", ".tule"))]
    (run / "local-files.md").write_text("Files the agent left in its folder on its own computer:\n\n" + "\n".join(local or ["(none)"]))
    log_file = Path(st["night"]) / "control.log"
    if log_file.exists():
        with open(log_file, "rb") as f:
            f.seek(st["log_from"])
            (run / "control.log").write_bytes(f.read())
    subprocess.run(["docker", "rm", "-f", st["machine"]], capture_output=True)
    st["finished"] = time.time()
    save_state(run, st)
    print(f"kept: {run / 'machine.md'}, {run / 'local-files.md'}, {run / 'control.log'}; machine removed")
    print(clip((run / "machine.md").read_text(), 6000))


FORWARD = """
import asyncio
async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    finally:
        w.close()
async def client(r, w):
    try:
        r2, w2 = await asyncio.open_connection("127.0.0.1", UP)
    except OSError:
        return w.close()
    await asyncio.gather(pipe(r, w2), pipe(r2, w), return_exceptions=True)
async def main():
    ss = [await asyncio.start_server(client, "172.17.0.1", p) for p in (PORT, UP)]  # UP too: machines from before
    print("forwarding", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in ss))
asyncio.run(main())
"""


def cmd_tunnel(a):
    """Runs until stopped: the control plane at <host>'s 127.0.0.1:REMOTE_PORT (ssh -R), and from there on
    docker0, where that computer's machines reach it."""
    code = FORWARD.replace("UP", str(REMOTE_PORT)).replace("PORT", str(PORT))
    while True:
        subprocess.run(["ssh", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes",
                        "-R", f"127.0.0.1:{REMOTE_PORT}:127.0.0.1:{PORT}", a.host, "python3", "-u", "-c", shlex.quote(code)])
        print("tunnel dropped; again in 5 s", flush=True)
        time.sleep(5)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("serve", "stop", "start", "say", "_turn", "wait", "quota", "shell", "shot", "finish", "tunnel"):
        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd")
        p = sub.add_parser("serve"); p.add_argument("--night"); p.add_argument("--no-image", action="store_true"); p.add_argument("--restart", action="store_true")
        p = sub.add_parser("stop"); p.add_argument("night")
        p = sub.add_parser("start"); p.add_argument("night"); p.add_argument("name"); p.add_argument("--model")
        p.add_argument("--scenario"); p.add_argument("--file", action="append", default=[]); p.add_argument("--setup", action="append", default=[])
        p = sub.add_parser("say"); p.add_argument("run"); p.add_argument("message")
        p = sub.add_parser("_turn"); p.add_argument("run")
        p = sub.add_parser("wait"); p.add_argument("run"); p.add_argument("--max", type=int, default=540)
        sub.add_parser("quota")
        p = sub.add_parser("shell"); p.add_argument("run"); p.add_argument("command"); p.add_argument("--timeout", type=int, default=600)
        p = sub.add_parser("shot"); p.add_argument("run"); p.add_argument("path", nargs="?")
        p = sub.add_parser("finish"); p.add_argument("run")
        p = sub.add_parser("tunnel"); p.add_argument("host")
        a = ap.parse_args()
        {"serve": cmd_serve, "stop": cmd_stop, "start": cmd_start, "say": cmd_say, "_turn": cmd_turn, "wait": cmd_wait, "quota": cmd_quota,
         "shell": cmd_shell, "shot": cmd_shot, "finish": cmd_finish, "tunnel": cmd_tunnel}[a.cmd](a)
        return
    auto()


def auto():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenarios", default="", help="comma-separated names (default: all)")
    ap.add_argument("--agents", default="codex,claude")
    ap.add_argument("--turns", type=int, default=4, help="at most this many user messages per run")
    ap.add_argument("--repeat", type=int, default=1, help="rounds over all scenarios and agents")
    ap.add_argument("--hours", type=float, default=0, help="stop starting runs after this long (0: no limit)")
    ap.add_argument("--helper", choices=["claude", "codex"], help="who plays the user and reviews (default: claude if signed in, else codex)")
    ap.add_argument("--no-image", action="store_true", help=f"use {IMAGE} as it is instead of rebuilding it")
    a = ap.parse_args()
    scenarios = tomllib.loads((QA / "scenarios.toml").read_text())["scenario"]
    if a.scenarios:
        want = a.scenarios.split(",")
        scenarios = [s for s in scenarios if s["name"] in want]
    agents = [x for x in a.agents.split(",") if x]
    for x in agents:
        if x not in AGENTS or not shutil.which(x):
            raise SystemExit(f"unknown or missing agent {x!r} (have: {', '.join(AGENTS)})")
    global HELPER
    HELPER = next((h for h in ([a.helper] if a.helper else ["claude", "codex"]) if helper_works(h)), None)
    if not HELPER:
        raise SystemExit("neither `claude -p` nor `codex exec` works here (signed in?)")
    log(f"the simulated user and the reviewer: {HELPER}")
    if "claude" in agents and HELPER != "claude":
        log("claude is not signed in (claude -p fails): leaving it out; run `claude` and /login to add it")
        agents = [x for x in agents if x != "claude"]
    night = QA / "runs" / time.strftime("%Y-%m-%d-%H%M")
    night.mkdir(parents=True)
    CACHE.mkdir(exist_ok=True)
    if not (QA / "node_modules" / "playwright-core").exists():
        sh("npm", "install", "--silent", cwd=QA)
    if not a.no_image:
        log(f"building {IMAGE}")
        sh("docker", "build", "-q", "-f", "runtime/Dockerfile", "-t", IMAGE, ".", cwd=ROOT, timeout=1800)
    log(f"control plane on {URL}")
    control = start_control(night)
    deadline = time.time() + a.hours * 3600 if a.hours else float("inf")
    try:
        for n in range(1, a.repeat + 1):
            for sc in scenarios:
                for agent in agents:
                    if time.time() > deadline:
                        raise TimeoutError
                    log(f"{sc['name']} with {agent} (round {n})")
                    r = one_run(night, sc, agent, n, a.turns)
                    log(f"  {r['minutes']} min, goal met: {r['goal_met']}" + (f", error: {r['error']}" if r["error"] else ""))
    except (TimeoutError, KeyboardInterrupt):
        log("stopping")
    finally:
        os.killpg(control.pid, signal.SIGTERM)
        report(night)
        log(f"report: {night / 'report.md'}")


if __name__ == "__main__":
    main()
