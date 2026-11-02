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
import shutil
import signal
import subprocess
import sys
import tempfile
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
IMAGE = os.environ.get("TULE_QA_IMAGE", "tule-runtime:dev")
TURN_MINUTES = 25
CACHE = QA / ".cache"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- the control plane


def start_control(night: Path) -> subprocess.Popen:
    """A wrangler dev of control/ on PORT with the night's own state; machines reach it from Docker."""
    subprocess.run(["node", "web/build.mjs", "--dev"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    out = open(night / "control.log", "w")
    p = subprocess.Popen(
        ["npx", "wrangler", "dev", "--port", str(PORT), "--ip", "127.0.0.1", "--inspector-port", str(PORT + 1000),
         "--enable-containers=false", "--persist-to", str(night / "state"), "--local-upstream", f"127.0.0.1:{PORT}",
         "--var", f"MACHINE_URL:{MACHINE_URL}", "--var", f"PUBLIC_URL:{URL}"],
        cwd=ROOT / "control", stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(120):
        try:
            urllib.request.urlopen(URL + "/__dev/version", timeout=2)
            return p
        except Exception:
            time.sleep(1)
    raise SystemExit(f"the control plane did not start; see {night / 'control.log'}")


class Guest:
    """A fresh guest account, as a first visit makes one: a session cookie and the API."""

    def __init__(self):
        self.jar = CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        # each guest from its own "network", so the per-network guest limit does not stop the night
        self.ip = f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
        self.page("/")
        if not any(c.name == "tule_session" for c in self.jar):
            raise RuntimeError("no guest session (guest limit?)")

    def page(self, path: str) -> str:
        req = urllib.request.Request(URL + path, headers={"CF-Connecting-IP": self.ip})
        return self.http.open(req, timeout=30).read().decode()

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(URL + path, method=method, data=json.dumps(body or {}).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "Origin": URL, "CF-Connecting-IP": self.ip})
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


def start_machine(g: Guest, project: str, name: str):
    env = g.api("POST", f"/api/projects/{project}/join", {})["env"]
    sh("docker", "rm", "-f", name, check=False)
    sh("docker", "run", "-d", "--name", name, "--add-host", "host.docker.internal:host-gateway",
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
                       capture_output=True, text=True, timeout=timeout)
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
    opts = ["--json", "--skip-git-repo-check", "--ignore-user-config", "--disable", "apps",
            "-c", 'sandbox_mode="workspace-write"', "-c", "sandbox_workspace_write.network_access=true",
            "-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(str(CACHE))}]",
            "-c", f'mcp_servers.tule.url="{URL}/mcp"', "-c", 'mcp_servers.tule.bearer_token_env_var="TULE_QA_KEY"',
            "-c", 'mcp_servers.tule.default_tools_approval_mode="approve"',  # a user would click Allow
            "-o", str(out.with_suffix(".last.txt"))]
    cmd = ["codex", "exec", "resume", *opts, session, msg] if session else ["codex", "exec", *opts, msg]
    run_logged(cmd, home, env, out)
    for line in out.read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "thread.started":
            session = ev.get("thread_id") or session
    last = out.with_suffix(".last.txt")
    return (last.read_text().strip() if last.exists() else ""), session


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
    r = subprocess.run(["node", str(QA / "shot.mjs"), f"{URL}/__dev/as?key={key}&next={next_}", str(path)],
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


def main():
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
