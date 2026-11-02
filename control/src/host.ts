// One Durable Object per computer a user plugged in (DESIGN.md, "Your own
// computers"). The computer's `tule host run` service holds one WebSocket here
// (tag "host"); through it the object asks the computer to start or stop a
// project's container and learns its specs and which containers run.
//
// Messages (JSON both ways):
//   host -> here   {t: "hello", name, specs: {cpus, memoryGb, gpus: [...], arch, os, docker}}
//                  {t: "reply", rid, ...}            the answer to run / stop
//                  {t: "failed", project, error}     a container that could not start (an image pull, say)
//                  {t: "beat", running: [project ids]}
//   here -> host   {t: "run", rid, project, env, gpu}   start Tule's machine image for a project
//                  {t: "stop", rid, project}            stop and remove it
//                  {t: "forget"}                        the computer was removed from the account
//
// The host decides the image itself (Tule's runtime image for its architecture,
// CUDA when it has GPUs and the machine wants one); the cloud only says which
// project and which environment (TULE_* variables), so it cannot make a
// computer run anything else.
import { DurableObject } from "cloudflare:workers";
import type { Accounts } from "./accounts";
import type { Project } from "./project";
import { stub } from "./rpc";

type Result = Record<string, unknown>;
type Info = { id?: string; name?: string; owner?: string; specs?: Record<string, unknown>; seenAt?: number; running?: string[] };

export class Host extends DurableObject<Env> {
  info: Info = {};
  asks = new Map<number, (r: Result) => void>();

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.info = (await ctx.storage.get<Info>("info")) ?? {};
    });
  }

  socket(): WebSocket | undefined {
    return this.ctx.getWebSockets("host").find((w) => w.readyState === WebSocket.OPEN);
  }

  /** The computer connects (the Worker checked its token and passes who it is). */
  async fetch(req: Request): Promise<Response> {
    const id = req.headers.get("X-Tule-Host-Id") ?? "";
    const owner = req.headers.get("X-Tule-Host-Owner") ?? "";
    const name = req.headers.get("X-Tule-Host-Name") ?? "";
    for (const old of this.ctx.getWebSockets("host")) old.close(4000, "replaced by a new connection");
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server, ["host"]);
    this.info = { ...this.info, id, owner, name, seenAt: Date.now() };
    await this.ctx.storage.put("info", this.info);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(_ws: WebSocket, msg: string | ArrayBuffer) {
    if (typeof msg !== "string") return;
    let m: Result;
    try {
      m = JSON.parse(msg);
    } catch {
      return;
    }
    if (m.t === "hello" && m.specs && typeof m.specs === "object") {
      this.info = { ...this.info, specs: m.specs as Record<string, unknown>, seenAt: Date.now() };
      await this.ctx.storage.put("info", this.info);
      if (this.info.id) await stub<Accounts>(this.env.ACCOUNTS, "global").setHostSpecs(this.info.id, this.info.specs!);
    } else if (m.t === "reply") {
      const done = this.asks.get(Number(m.rid));
      this.asks.delete(Number(m.rid));
      const { t: _t, rid: _r, ...rest } = m;
      done?.(rest);
    } else if (m.t === "failed" && typeof m.project === "string") {
      await stub<Project>(this.env.PROJECT, m.project).hostFailed(String(m.error ?? "the container did not start"), this.info.id);
    } else if (m.t === "beat") {
      this.info = { ...this.info, seenAt: Date.now(), running: Array.isArray(m.running) ? (m.running as string[]) : [] };
      await this.ctx.storage.put("info", this.info);
    }
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string) {
    try {
      ws.close(code === 1005 || code === 1006 ? 1000 : code, reason);
    } catch {
      // closed
    }
  }

  ask(msg: Result, timeoutMs = 20_000): Promise<Result> {
    const ws = this.socket();
    if (!ws) return Promise.resolve({ error: `${this.info.name ?? "the computer"} is offline`, hint: "start it, or check its service: tule host status" });
    const rid = crypto.getRandomValues(new Uint32Array(1))[0];
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.asks.delete(rid);
        resolve({ error: `${this.info.name ?? "the computer"} did not answer` });
      }, timeoutMs);
      this.asks.set(rid, (r) => {
        clearTimeout(timer);
        resolve(r);
      });
      ws.send(JSON.stringify({ ...msg, rid }));
    });
  }

  // ------------------------------------------------------------ RPC

  status(): Result {
    return { name: this.info.name ?? null, online: !!this.socket(), specs: this.info.specs ?? null, seenAt: this.info.seenAt ?? null, running: this.info.running ?? [] };
  }

  /** Start Tule's machine image for a project; the host answers once it has started pulling or running. */
  run(project: string, env: Record<string, string>, gpu: boolean): Promise<Result> {
    return this.ask({ t: "run", project, env, gpu });
  }

  stop(project: string): Promise<Result> {
    return this.ask({ t: "stop", project }, 60_000);
  }

  /** Removed from the account: the computer's service learns it and stops reconnecting. */
  async forget() {
    for (const ws of this.ctx.getWebSockets("host")) {
      try {
        ws.send(JSON.stringify({ t: "forget" }));
        ws.close(4004, "removed from the account");
      } catch {
        // gone
      }
    }
    await this.ctx.storage.deleteAll();
    this.info = {};
  }
}
