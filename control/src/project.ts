// One Durable Object per project. It owns the project's machine (a Cloudflare
// Container started through ctx.container, or a machine started elsewhere
// with a join token) and routes every stream to it over the one WebSocket the
// machine's tule-agent dials in with (runtime/tule_agent.py has the protocol).
//
// Sockets (hibernation API, tagged): "agent" (the machine), "ssh" + "sid:<n>"
// (one per `tule proxy` client), "view" (viewer pages). Text frames are JSON;
// binary frames between the project and the agent are [kind u8][id u32][payload]
// with kind 1 = SSH bytes of stream id, 2 = a mesh for request id. A viewer
// gets meshes as [rid u32][payload] with its own request id.
//
// Stored: the project's state (machine, timings, join token hash), its SSH
// keys and host key (so the host key survives restarts), the last outputs
// index (so the viewer lists outputs while the machine is down) and the index
// of workspace snapshots ("snap:<id>": when, why, how many files and bytes;
// the manifests and chunks are in R2, see index.ts).
//
// Viewer requests to the machine are relayed generically, so new ones need no
// change here: the page sends {t: "req", rid, op, args}, the agent gets
// {t: "req", rid, op, args} (with the project's own rid) and answers
// {t: "res", rid, ...} (or binary kind 2 for bytes), which the page gets back
// with its rid. {t: "broadcast", ...} from the agent goes to every page as
// {t: "event", ...}. ("mesh" requests predate this and work the same way.)
//
// Commands for the agent (stop, snapshot, restore) carry a request id; the
// agent answers {t: "reply", rid, ...}. A machine is "running" once its agent
// has restored /workspace and sent {t: "ready"}.
import { DurableObject } from "cloudflare:workers";

/** Machine names (DESIGN.md) to Cloudflare instance types. */
const MACHINES: Record<string, "standard-2" | "standard-3" | "standard-4"> = {
  "cpu-1": "standard-2", // 1 vCPU, 6 GiB
  "cpu-2": "standard-3", // 2 vCPU, 8 GiB
  "cpu-4": "standard-4", // 4 vCPU, 12 GiB
};
const IDLE_MS = 30 * 60_000; // no SSH session and no activity for this long: stop the machine
const CHECK_MS = 5 * 60_000;
const SSH = 1;
const MESH = 2;

type State = {
  name?: string;
  machine?: string; // "cpu-1", or "external" for a machine started with a join token
  joinHash?: string;
  upAt?: number; // start() called
  helloAt?: number; // the agent connected after the last start
  agentStarted?: number; // the agent process started (its clock)
  readyAt?: number; // the agent restored /workspace
  restoreMs?: number;
  lastActive?: number;
  stoppedAt?: number;
};
type HostKey = { private: string; public: string };
type Snapshot = { id: string; at: number; reason: string; files: number; bytes: number };

/** A snapshot's manifest in R2. */
export const snapshotKey = (p: string, id: string) => `projects/${p}/snapshots/${id}.json`;
type Result = Record<string, unknown>;

const b64url = (b: Uint8Array) => btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function sha256(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

function frame(kind: number, id: number, payload: Uint8Array): Uint8Array {
  const out = new Uint8Array(5 + payload.byteLength);
  out[0] = kind;
  new DataView(out.buffer).setUint32(1, id);
  out.set(payload, 5);
  return out;
}

export class Project extends DurableObject<Env> {
  state: State = {};
  keys: string[] = [];
  outputs: unknown[] = [];
  pending = new Map<number, { ws: WebSocket; rid: number }>(); // mesh requests in flight
  asks = new Map<number, (reply: Result) => void>(); // commands to the agent awaiting a reply
  lastSaved = 0;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.state = (await ctx.storage.get<State>("state")) ?? {};
      this.keys = (await ctx.storage.get<string[]>("keys")) ?? [];
      this.outputs = (await ctx.storage.get<unknown[]>("outputs")) ?? [];
      // the timeout resets when the object restarts (a deploy)
      if (ctx.container?.running) await ctx.container.setInactivityTimeout(IDLE_MS + 2 * CHECK_MS);
    });
  }

  save() {
    return this.ctx.storage.put("state", this.state);
  }

  agent(): WebSocket | undefined {
    return this.ctx.getWebSockets("agent").find((w) => w.readyState === WebSocket.OPEN);
  }

  ssh(sid: number): WebSocket | undefined {
    return this.ctx.getWebSockets(`sid:${sid}`)[0];
  }

  sessions(): number {
    return this.ctx.getWebSockets("ssh").length;
  }

  touch() {
    const now = Date.now();
    this.state.lastActive = now;
    if (now - this.lastSaved > 60_000) {
      this.lastSaved = now;
      void this.save();
    }
  }

  // ------------------------------------------------------------ RPC (from the Worker)

  async status(name: string): Promise<Result> {
    this.state.name ??= name;
    const c = this.ctx.container;
    const agent = !!this.agent();
    const running = !!c?.running;
    const s = this.state;
    const ready = agent && !!s.readyAt && !!s.helloAt && s.readyAt >= s.helloAt;
    const hostKey = await this.ctx.storage.get<HostKey>("hostKey");
    return {
      project: name,
      machine: running || agent ? (s.machine ?? null) : null,
      state: ready ? "running" : agent || running ? "starting" : "stopped",
      sessions: this.sessions(),
      // the cold start: start() to the agent's hello, and the agent process's own start
      startedAt: s.upAt ?? null,
      coldStartMs: s.helloAt && s.upAt && s.helloAt >= s.upAt ? s.helloAt - s.upAt : null,
      agentStartMs: s.agentStarted && s.upAt && s.agentStarted >= s.upAt ? s.agentStarted - s.upAt : null,
      readyMs: s.readyAt && s.upAt && s.readyAt >= s.upAt ? s.readyAt - s.upAt : null,
      restoreMs: s.restoreMs ?? null,
      snapshot: (await this.latestSnapshot()) ?? null,
      hostKey: hostKey?.public ?? null,
      keys: this.keys.length,
      outputs: this.outputs.length,
      containers: !!c,
    };
  }

  async up(name: string, machine: string, origin: string): Promise<Result> {
    this.state.name = name;
    const instance = MACHINES[machine];
    if (!instance)
      return { error: `unknown machine ${JSON.stringify(machine)}`, hint: `use one of ${Object.keys(MACHINES).join(", ")}` };
    const c = this.ctx.container;
    if (!c)
      return {
        error: "this deployment has no container runtime",
        hint: "run wrangler dev with containers enabled, or start a machine yourself with a join token (POST .../join)",
      };
    if (c.running) {
      if (this.state.machine === machine) return this.status(name);
      // resize: the old machine saves /workspace, the new one restores it
      await this.stopMachine("resize");
    }
    const token = b64url(crypto.getRandomValues(new Uint8Array(32)));
    this.state = { ...this.state, machine, joinHash: await sha256(token), upAt: Date.now(), helloAt: undefined, agentStarted: undefined, readyAt: undefined, restoreMs: undefined, lastActive: Date.now() };
    await this.save();
    c.start({
      image: c.images.cpu,
      instance,
      enableInternet: true, // the agent dials out; pip and git need the internet too
      env: { TULE_URL: origin, TULE_PROJECT: name, TULE_JOIN_TOKEN: token },
    });
    await c.setInactivityTimeout(IDLE_MS + 2 * CHECK_MS);
    await this.ctx.storage.setAlarm(Date.now() + CHECK_MS);
    return this.status(name);
  }

  async down(name: string): Promise<Result> {
    await this.stopMachine("down");
    return this.status(name);
  }

  /** A join token for a machine started elsewhere (a RunPod pod, a local test). */
  async join(name: string): Promise<Result> {
    this.state.name = name;
    const token = b64url(crypto.getRandomValues(new Uint8Array(32)));
    this.state = { ...this.state, machine: this.ctx.container?.running ? this.state.machine : "external", joinHash: await sha256(token), upAt: Date.now(), helloAt: undefined, agentStarted: undefined, readyAt: undefined };
    await this.save();
    return { project: name, joinToken: token, env: { TULE_PROJECT: name, TULE_JOIN_TOKEN: token } };
  }

  async addKey(name: string, key: string): Promise<Result> {
    this.state.name = name;
    const k = key.trim().split(/\s+/).slice(0, 3).join(" ");
    if (!/^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+/.test(k))
      return { error: "not an SSH public key", hint: "pass the contents of ~/.ssh/id_ed25519.pub (ssh-keygen -t ed25519 makes one)" };
    if (!this.keys.includes(k)) {
      this.keys.push(k);
      await this.ctx.storage.put("keys", this.keys);
      await this.sendConfig();
    }
    return { project: name, keys: this.keys.length };
  }

  /** Whether `token` is the current machine's join token. */
  async checkJoin(token: string): Promise<boolean> {
    return !!this.state.joinHash && (await sha256(token)) === this.state.joinHash;
  }

  // ------------------------------------------------------------ snapshots

  async addSnapshot(name: string, reason: string, manifest: Record<string, unknown>): Promise<Result> {
    const id = `${Date.now().toString(36).padStart(9, "0")}-${b64url(crypto.getRandomValues(new Uint8Array(3))).toLowerCase().replace(/[^0-9a-z]/g, "0")}`;
    const files = (manifest.files ?? {}) as Record<string, { s?: number }>;
    const rec: Snapshot = {
      id,
      at: Date.now(),
      reason: reason.slice(0, 80),
      files: Object.keys(files).length,
      bytes: Object.values(files).reduce((n, f) => n + (Number(f.s) || 0), 0),
    };
    await this.env.WORKSPACES.put(snapshotKey(name, id), JSON.stringify({ ...manifest, id }), {
      httpMetadata: { contentType: "application/json" },
    });
    await this.ctx.storage.put({ [`snap:${id}`]: rec, latestSnapshot: id });
    this.touch();
    return { id };
  }

  async snapshots(limit = 50): Promise<Result> {
    const m = await this.ctx.storage.list<Snapshot>({ prefix: "snap:", reverse: true, limit: Math.min(Math.max(limit, 1), 500) });
    return { snapshots: [...m.values()] };
  }

  async latestSnapshot(): Promise<string | undefined> {
    return this.ctx.storage.get<string>("latestSnapshot");
  }

  /** Sends a command to the agent and waits for its reply. */
  ask(msg: Result, timeoutMs: number): Promise<Result> {
    const agent = this.agent();
    if (!agent) return Promise.resolve({ error: "the project's machine is not running", hint: "start it with tule up" });
    const rid = crypto.getRandomValues(new Uint32Array(1))[0];
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.asks.delete(rid);
        resolve({ error: `the machine did not answer ${String(msg.t)} within ${timeoutMs / 1000} s` });
      }, timeoutMs);
      this.asks.set(rid, (r) => {
        clearTimeout(timer);
        resolve(r);
      });
      agent.send(JSON.stringify({ ...msg, rid }));
    });
  }

  async command(name: string, msg: Result): Promise<Result> {
    this.state.name ??= name;
    const r = await this.ask(msg, 600_000);
    this.touch();
    return { project: name, ...r };
  }

  async stopMachine(why: string) {
    const c = this.ctx.container;
    if (this.agent() && this.state.readyAt) {
      // the final snapshot: what the machine saved is what the next one restores
      const r = await this.ask({ t: "stop", reason: why }, 120_000);
      if (r.error) console.log(JSON.stringify({ event: "final-snapshot", project: this.state.name, error: r.error }));
    }
    for (const ws of this.ctx.getWebSockets("agent")) ws.close(4001, `machine stopped (${why})`);
    for (const ws of this.ctx.getWebSockets("ssh")) ws.close(4001, `machine stopped (${why})`);
    if (c?.running) {
      try {
        await c.destroy(why);
      } catch (e) {
        console.log(JSON.stringify({ event: "destroy", error: String(e) }));
      }
    }
    this.state = { ...this.state, joinHash: undefined, stoppedAt: Date.now() };
    await this.save();
    this.broadcast();
  }

  async alarm() {
    const c = this.ctx.container;
    if (!c?.running && !this.agent()) return;
    const idle = Date.now() - (this.state.lastActive ?? 0);
    if (this.sessions() === 0 && idle > IDLE_MS) {
      console.log(JSON.stringify({ event: "idle-stop", project: this.state.name, idleMin: Math.round(idle / 60_000) }));
      await this.stopMachine("idle");
      return;
    }
    await this.ctx.storage.setAlarm(Date.now() + CHECK_MS);
  }

  // ------------------------------------------------------------ sockets

  async fetch(req: Request): Promise<Response> {
    const what = new URL(req.url).pathname.split("/").filter(Boolean).pop();
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    if (what === "agent") {
      const h = req.headers.get("Authorization") ?? "";
      const token = h.startsWith("Bearer ") ? h.slice(7) : "";
      if (!token || !this.state.joinHash || (await sha256(token)) !== this.state.joinHash)
        return new Response("invalid join token\n", { status: 401 });
      for (const old of this.ctx.getWebSockets("agent")) old.close(4000, "replaced by a new connection");
      for (const s of this.ctx.getWebSockets("ssh")) s.close(4002, "the machine reconnected");
      this.ctx.acceptWebSocket(server, ["agent"]);
    } else if (what === "ssh") {
      const agent = this.agent();
      if (!agent) {
        const st = await this.status(this.state.name ?? "");
        return new Response(`the project's machine is ${st.state}; start it with tule up\n`, { status: 503 });
      }
      const sid = crypto.getRandomValues(new Uint32Array(1))[0];
      this.ctx.acceptWebSocket(server, ["ssh", `sid:${sid}`]);
      server.serializeAttachment({ sid });
      agent.send(JSON.stringify({ t: "ssh-open", sid }));
      this.touch();
    } else if (what === "view") {
      this.ctx.acceptWebSocket(server, ["view"]);
      server.send(JSON.stringify({ t: "outputs", outputs: this.outputs, status: await this.status(this.state.name ?? "") }));
    } else {
      return new Response("not found\n", { status: 404 });
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, msg: string | ArrayBuffer) {
    const tags = this.ctx.getTags(ws);
    if (tags.includes("agent")) return this.fromAgent(ws, msg);
    if (tags.includes("ssh")) {
      const agent = this.agent();
      const { sid } = ws.deserializeAttachment() as { sid: number };
      if (!agent) return ws.close(4003, "the machine is gone");
      const data = typeof msg === "string" ? new TextEncoder().encode(msg) : new Uint8Array(msg);
      agent.send(frame(SSH, sid, data));
      this.touch();
      return;
    }
    if (tags.includes("view") && typeof msg === "string") {
      let m: { t?: string; rid?: number; path?: string; hash?: string; op?: unknown; args?: unknown };
      try {
        m = JSON.parse(msg);
      } catch {
        return;
      }
      if (typeof m.rid !== "number") return;
      const agent = this.agent();
      if (m.t === "req") {
        const r = m as { rid: number; op?: unknown; args?: unknown };
        if (!agent) return ws.send(JSON.stringify({ t: "res", rid: r.rid, error: "the project's machine is not running (tule up)" }));
        const grid = crypto.getRandomValues(new Uint32Array(1))[0];
        this.pending.set(grid, { ws, rid: r.rid });
        agent.send(JSON.stringify({ t: "req", rid: grid, op: r.op, args: r.args ?? {} }));
        this.touch();
        return;
      }
      if (m.t !== "mesh" || typeof m.path !== "string") return;
      if (!agent)
        return ws.send(JSON.stringify({ t: "mesh-error", rid: m.rid, error: "the project's machine is not running (tule up)" }));
      const grid = crypto.getRandomValues(new Uint32Array(1))[0];
      this.pending.set(grid, { ws, rid: m.rid });
      agent.send(JSON.stringify({ t: "mesh", rid: grid, path: m.path, hash: m.hash ?? "" }));
    }
  }

  async fromAgent(ws: WebSocket, msg: string | ArrayBuffer) {
    if (typeof msg !== "string") {
      const b = new Uint8Array(msg);
      if (b.byteLength < 5) return;
      const kind = b[0];
      const id = new DataView(msg).getUint32(1);
      if (kind === SSH) {
        const s = this.ssh(id);
        if (s) s.send(b.subarray(5));
        else ws.send(JSON.stringify({ t: "ssh-close", sid: id }));
        this.touch();
      } else if (kind === MESH) {
        const p = this.pending.get(id);
        this.pending.delete(id);
        if (p && p.ws.readyState === WebSocket.OPEN) {
          const out = new Uint8Array(b.byteLength - 1);
          new DataView(out.buffer).setUint32(0, p.rid);
          out.set(b.subarray(5), 4);
          p.ws.send(out);
        }
      }
      return;
    }
    let m: Record<string, unknown>;
    try {
      m = JSON.parse(msg);
    } catch {
      return;
    }
    switch (m.t) {
      case "hello": {
        this.state.helloAt = Date.now();
        this.state.agentStarted = typeof m.started === "number" ? m.started : undefined;
        this.state.lastActive = Date.now();
        await this.save();
        await this.sendConfig();
        console.log(JSON.stringify({ event: "agent-hello", project: this.state.name, coldStartMs: this.state.upAt ? this.state.helloAt - this.state.upAt : null }));
        this.broadcast();
        break;
      }
      case "hostkey":
        if (typeof m.private === "string" && typeof m.public === "string" && !(await this.ctx.storage.get("hostKey")))
          await this.ctx.storage.put("hostKey", { private: m.private, public: m.public });
        break;
      case "outputs":
        this.outputs = Array.isArray(m.outputs) ? m.outputs : [];
        try {
          await this.ctx.storage.put("outputs", this.outputs);
        } catch {
          // an index too large for one stored value stays in memory
        }
        this.broadcast();
        break;
      case "ssh-close": {
        const s = this.ssh(Number(m.sid));
        if (s) s.close(1000, "session ended");
        break;
      }
      case "res": {
        const p = this.pending.get(Number(m.rid));
        this.pending.delete(Number(m.rid));
        if (p && p.ws.readyState === WebSocket.OPEN) p.ws.send(JSON.stringify({ ...m, rid: p.rid }));
        break;
      }
      case "broadcast": {
        const { t: _t, ...rest } = m;
        const msg = JSON.stringify({ ...rest, t: "event" });
        for (const v of this.ctx.getWebSockets("view")) {
          try {
            v.send(msg);
          } catch {
            // closing
          }
        }
        break;
      }
      case "mesh-error": {
        const p = this.pending.get(Number(m.rid));
        this.pending.delete(Number(m.rid));
        if (p) p.ws.send(JSON.stringify({ t: "mesh-error", rid: p.rid, error: m.error }));
        break;
      }
      case "ready":
        this.state.readyAt = Date.now();
        this.state.restoreMs = typeof m.restoreMs === "number" ? m.restoreMs : undefined;
        await this.save();
        this.broadcast();
        break;
      case "reply": {
        const done = this.asks.get(Number(m.rid));
        this.asks.delete(Number(m.rid));
        const { t: _t, rid: _rid, ...rest } = m;
        done?.(rest);
        break;
      }
      case "beat":
        break;
    }
  }

  async sendConfig() {
    const agent = this.agent();
    if (!agent) return;
    const hostKey = (await this.ctx.storage.get<HostKey>("hostKey")) ?? null;
    agent.send(JSON.stringify({ t: "config", keys: this.keys, hostKey }));
  }

  broadcast() {
    void this.status(this.state.name ?? "").then((status) => {
      const msg = JSON.stringify({ t: "outputs", outputs: this.outputs, status });
      for (const v of this.ctx.getWebSockets("view")) {
        try {
          v.send(msg);
        } catch {
          // closing
        }
      }
    });
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string) {
    const tags = this.ctx.getTags(ws);
    try {
      ws.close(code === 1005 || code === 1006 ? 1000 : code, reason);
    } catch {
      // already closed
    }
    if (tags.includes("ssh")) {
      const { sid } = ws.deserializeAttachment() as { sid: number };
      this.agent()?.send(JSON.stringify({ t: "ssh-close", sid }));
    } else if (tags.includes("agent") && !this.agent()) {
      for (const s of this.ctx.getWebSockets("ssh")) s.close(4003, "the machine disconnected");
      this.broadcast();
    }
  }

  async webSocketError(ws: WebSocket) {
    await this.webSocketClose(ws, 1011, "error");
  }
}
