// One Durable Object per project. A project can be loaded on several machines
// at once ("slots"), each with its own working copy of /workspace:
//
//   cloud            Tule's container (ctx.container, one per project), cpu-1 .. cpu-4
//   gpu-<type>       a pod on the owner's RunPod account
//   <computer name>  one of the owner's computers (its `tule host` service)
//   joined           a machine started elsewhere with a join token
//
// Each machine's tule-agent dials /api/projects/<id>/agent with its slot's
// join token; this object routes every stream over that one WebSocket
// (runtime/tule_agent.py has the protocol). Each working copy autosaves to its
// own snapshots (crash safety, never merged); saving is git: the project's
// remote (gitRefs/gitPush, bundles in R2) and Git LFS (index.ts). A machine
// that starts fresh begins from the latest save (main); a slot that comes
// back resumes its own last snapshot. A stopped slot with unsaved work stays
// listed until it is loaded again or forgotten (DESIGN.md, "Planned: several
// machines per project").
//
// Sockets (hibernation API, tagged): "agent" + "m:<slot>" (a machine), "ssh" +
// "sid:<n>" + "sm:<slot>" (one per `tule proxy` client), "view" (viewer pages;
// each picks a slot to look at). Text frames are JSON; binary frames between
// the project and an agent are [kind u8][id u32][payload] with kind 1 = SSH
// bytes of stream id, 2 = a mesh for request id. A viewer gets meshes as
// [rid u32][payload] with its own request id.
//
// Identity: the project id ("p_..." names this object), its name and owner
// (a user of the Accounts object), set by init() when the project is made.
// SSH keys are the owner's (Accounts), sent to machines on connect and when
// they change.
//
// Viewer requests are relayed generically: the page sends {t: "req", rid, op,
// args}, the agent of the slot it looks at gets {t: "req", rid, op, args}
// (with the project's own rid) and answers {t: "res", rid, ...} (or binary
// kind 2 for bytes). {t: "broadcast", ...} from an agent goes to the pages
// looking at that slot as {t: "event", ...}. Commands for an agent (stop,
// snapshot, restore, save) carry a request id; the agent answers
// {t: "reply", rid, ...}. A machine is "running" once its agent has restored
// /workspace and sent {t: "ready"}.
import { DurableObject } from "cloudflare:workers";
import type { Accounts } from "./accounts";
import { stub } from "./rpc";
import type { Host } from "./host";
import { createPod, getPod, GPUS, terminatePod } from "./runpod";

/** Cloud machine sizes (DESIGN.md) to Cloudflare instance types. */
const MACHINES: Record<string, "standard-2" | "standard-3" | "standard-4"> = {
  "cpu-1": "standard-2", // 1 vCPU, 6 GiB
  "cpu-2": "standard-3", // 2 vCPU, 8 GiB
  "cpu-4": "standard-4", // 4 vCPU, 12 GiB
};
const IDLE_MS = 30 * 60_000; // no SSH session and no activity for this long: stop the machine
const GUEST_IDLE_MS = 10 * 60_000; // the same for guests
const CHECK_MS = 5 * 60_000;
const SSH = 1;
const MESH = 2;
const MESH_PART = 3; // a big binary reply's chunk before its last one (MESH); to the viewer with the rid's top bit set

type Kind = "cloud" | "gpu" | "computer" | "joined";
/** One slot: a machine the project is (or was) loaded on, and its working copy. */
type Machine = {
  id: string; // the slot: "cloud", "gpu-rtx4090", a computer's name, "joined"
  kind: Kind;
  size: string; // cpu-1 .. cpu-4, gpu-<type>, the computer's name, "joined"
  on?: boolean; // started and not stopped
  pod?: { id: string; cost: number; gpu: string; cloud: string };
  host?: { id: string; name: string };
  hostError?: string; // why the last start on a computer failed
  guest?: boolean; // the owner was a guest when it started (tighter idle stop and minutes)
  joinHash?: string;
  upAt?: number; // started
  helloAt?: number; // the agent connected after the last start
  lostAt?: number; // the agent's connection closed and it has not come back
  agentStarted?: number; // the agent process started (its clock)
  readyAt?: number; // the agent restored /workspace
  restoreMs?: number;
  lastActive?: number;
  billedUntil?: number; // machine time is added to the owner's usage up to here
  stoppedAt?: number;
  snapshot?: string; // this copy's newest autosave
  unsaved?: { files: number; commits: number; at: number }; // work not in the latest save
};
type State = {
  id?: string; // p_...
  name?: string;
  owner?: string; // user id
  machines?: Record<string, Machine>;
  // before several machines (migrated in the constructor)
  machine?: string;
  pod?: Machine["pod"];
  host?: Machine["host"];
  joinHash?: string;
  upAt?: number;
};
type HostKey = { private: string; public: string };
type Snapshot = { id: string; at: number; reason: string; files: number; bytes: number; machine?: string };

/** A snapshot's manifest in R2. */
export const snapshotKey = (p: string, id: string) => `projects/${p}/snapshots/${id}.json`;
/** A pushed git bundle, and a Git LFS object, in R2. */
export const gitKey = (p: string, id: string) => `projects/${p}/git/${id}.bundle`;
export const lfsKey = (p: string, oid: string) => `projects/${p}/lfs/${oid}`;
type Result = Record<string, unknown>;

/** r[k] for an own key only: names come from requests, and "__proto__" or "constructor" are no machine. */
const own = <T>(r: Record<string, T>, k: string): T | undefined => (Object.hasOwn(r, k) ? r[k] : undefined);

/** The slot a machine name runs in. */
export function slotOf(machine: string): { id: string; kind: Kind } {
  if (own(MACHINES, machine)) return { id: "cloud", kind: "cloud" };
  if (machine.startsWith("gpu-")) return { id: machine, kind: "gpu" };
  if (machine === "joined" || machine === "external") return { id: "joined", kind: "joined" };
  return { id: machine, kind: "computer" };
}

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

const fresh = { helloAt: undefined, agentStarted: undefined, readyAt: undefined, restoreMs: undefined, lostAt: undefined };

/** Whether a machine reported work that is not in the latest save. */
function dirty(u: unknown) {
  const x = u as { files: number; commits: number } | null;
  return !!x && x.files + x.commits > 0;
}

export class Project extends DurableObject<Env> {
  state: State = {};
  outputs = new Map<string, unknown[]>(); // per slot
  pending = new Map<number, { ws: WebSocket; rid: number }>(); // viewer requests in flight (also in storage as rq:<id>: hibernation forgets this map)
  asks = new Map<number, (reply: Result) => void>(); // commands to agents awaiting a reply
  lastSaved = 0;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.state = (await ctx.storage.get<State>("state")) ?? {};
      this.migrate();
      for (const [k, v] of await ctx.storage.list<unknown[]>({ prefix: "outputs:" })) this.outputs.set(k.slice(8), v);
      // the timeout resets when the object restarts (a deploy)
      if (ctx.container?.running) await ctx.container.setInactivityTimeout(IDLE_MS + 2 * CHECK_MS);
    });
  }

  /** A project from before several machines: its one machine becomes a slot. */
  migrate() {
    const s = this.state;
    if (s.machines || !s.id) return;
    s.machines = {};
    if (s.machine && (s.joinHash || s.pod || s.host || this.ctx.container?.running)) {
      const slot = s.host ? { id: s.host.name, kind: "computer" as Kind } : slotOf(s.machine);
      s.machines[slot.id] = { id: slot.id, kind: slot.kind, size: s.machine, on: true, pod: s.pod, host: s.host, joinHash: s.joinHash, upAt: s.upAt, lastActive: Date.now() };
    }
    for (const k of ["machine", "pod", "host", "joinHash", "upAt"] as const) delete s[k];
  }

  get machines(): Record<string, Machine> {
    return (this.state.machines ??= {});
  }

  save() {
    return this.ctx.storage.put("state", this.state);
  }

  agent(slot: string): WebSocket | undefined {
    return this.ctx.getWebSockets(`m:${slot}`).find((w) => w.readyState === WebSocket.OPEN);
  }

  /** Slots whose agent is connected, the most recently ready first. */
  connected(): Machine[] {
    return Object.values(this.machines)
      .filter((m) => this.agent(m.id))
      .sort((a, b) => (b.readyAt ?? 0) - (a.readyAt ?? 0));
  }

  ssh(sid: number): WebSocket | undefined {
    return this.ctx.getWebSockets(`sid:${sid}`)[0];
  }

  sessions(slot?: string): number {
    return this.ctx.getWebSockets(slot ? `sm:${slot}` : "ssh").length;
  }

  touch(slot?: string) {
    const now = Date.now();
    for (const m of slot ? [this.machines[slot]].filter(Boolean) : Object.values(this.machines).filter((x) => x.on)) m.lastActive = now;
    if (now - this.lastSaved > 60_000) {
      this.lastSaved = now;
      void this.save();
    }
  }

  /** The slot a request means: the one named, else the only connected one (else an error that lists them). */
  pick(slot?: string): { slot: string } | { error: string; hint?: string } {
    if (slot) {
      const s = own(this.machines, slot) ? slot : own(this.machines, slotOf(slot).id) ? slotOf(slot).id : "";
      if (!s) return { error: `the project is not loaded on ${slot}`, hint: `loaded on: ${this.onNames() || "nothing"}` };
      return { slot: s };
    }
    const on = this.connected();
    if (on.length === 1) return { slot: on[0].id };
    if (!on.length) return { error: "the project's machine is not running", hint: "start one: tule up, or machine_start" };
    return { error: `the project runs on ${on.map((m) => m.id).join(" and ")}`, hint: "say which machine (--machine, or the machine argument)" };
  }

  onNames(): string {
    return Object.values(this.machines)
      .filter((m) => m.on)
      .map((m) => m.id)
      .join(", ");
  }

  // ------------------------------------------------------------ RPC (from the Worker)

  accounts() {
    return stub<Accounts>(this.env.ACCOUNTS, "global");
  }

  /** Called once when the project is created. */
  async init(id: string, name: string, owner: string) {
    this.state = { ...this.state, id, name, owner, machines: {} };
    await this.save();
  }

  /** A guest signed in: the project now belongs to their account (Accounts.mergeGuest). */
  async adopt(owner: string, name: string) {
    this.state = { ...this.state, owner, name };
    for (const m of Object.values(this.machines)) m.guest = false;
    await this.save();
    await this.sendConfig(); // the account's SSH keys
    this.broadcast();
  }

  async rename(name: string) {
    this.state.name = name;
    await this.save();
    this.broadcast();
  }

  /** Stops every machine and forgets everything: storage here and the project's files in R2. */
  async destroyProject(): Promise<Result> {
    for (const m of Object.values(this.machines)) if (m.on || m.pod) await this.stopMachine(m.id, "delete");
    // a pod RunPod would not terminate just now: keep the project (and the pod's id) and let the caller try again,
    // or the pod would bill on with nothing left that knows of it
    const left = Object.values(this.machines).filter((m) => m.pod);
    if (left.length && this.state.owner && (await this.accounts().runpodKey(this.state.owner)))
      throw new Error(`could not terminate the RunPod pod of ${left.map((m) => m.id).join(", ")} yet; try again in a minute`);
    const id = this.state.id;
    if (id) {
      let cursor: string | undefined;
      do {
        const l = await this.env.WORKSPACES.list({ prefix: `projects/${id}/`, cursor, limit: 1000 });
        if (l.objects.length) await this.env.WORKSPACES.delete(l.objects.map((o) => o.key));
        cursor = l.truncated ? l.cursor : undefined;
      } while (cursor);
    }
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.deleteAll();
    this.state = {};
    this.outputs.clear();
    return { deleted: true };
  }

  /** The outputs of a slot's copy (default: the one a viewer would show). */
  outputsIndex(slot?: string): unknown[] {
    return this.outputs.get(slot ?? this.viewSlot() ?? "") ?? [];
  }

  /** Adds a machine's time since its last bill to the owner's usage (computers and joined machines are free). */
  async bill(m: Machine) {
    const owner = this.state.owner;
    if (!owner || !m.upAt) return;
    const from = Math.max(m.upAt, m.billedUntil ?? 0);
    const now = Date.now();
    if (now <= from) return;
    const secs = (now - from) / 1000;
    if (m.kind === "cloud") await this.accounts().addUsage(owner, "cpu", secs, 0);
    if (m.kind === "gpu") await this.accounts().addUsage(owner, "gpu", secs, ((m.pod?.cost ?? 0) * secs) / 3600);
    m.billedUntil = now;
    await this.save();
  }

  machineState(m: Machine): "running" | "starting" | "stopped" {
    const agent = !!this.agent(m.id);
    if (!agent && this.lost(m)) return "stopped";
    const ready = agent && !!m.readyAt && !!m.helloAt && m.readyAt >= m.helloAt;
    const up = m.kind === "cloud" ? !!this.ctx.container?.running : !!m.on;
    return ready ? "running" : agent || (m.on && up) ? "starting" : "stopped";
  }

  /** A machine (not Tule cloud, whose container we see) whose agent went away over 90 s ago:
   * the computer, pod or container stopped or crashed, e.g. out of memory. */
  lost(m: Machine): string | null {
    if (m.kind === "cloud" || !m.on || !m.lostAt || Date.now() - m.lostAt < 90_000 || this.agent(m.id)) return null;
    return `lost contact at ${new Date(m.lostAt).toISOString().slice(11, 16)} UTC: the machine stopped or crashed (out of memory?); start it again (${m.kind === "joined" ? "where it ran" : `tule up --machine ${m.id}`})`;
  }

  machineStatus(m: Machine): Result {
    // null: not known (an agent from before saves); else changed files and commits not in the latest save
    const unsaved = m.unsaved ? { files: m.unsaved.files, commits: m.unsaved.commits } : null;
    return {
      id: m.id,
      kind: m.kind,
      size: m.size,
      state: this.machineState(m),
      ...(m.pod ? { gpu: m.pod.gpu, pricePerHour: m.pod.cost, cloud: m.pod.cloud } : {}),
      ...(m.host ? { computer: m.host.name } : {}),
      ...(m.hostError || this.lost(m) ? { lastError: m.hostError ?? this.lost(m) } : {}),
      sessions: this.sessions(m.id),
      unsaved,
      snapshot: m.snapshot ?? null,
      startedAt: m.upAt ?? null,
      stoppedAt: m.on ? null : (m.stoppedAt ?? null),
      // the cold start: start to the agent's hello, and the agent process's own start
      coldStartMs: m.helloAt && m.upAt && m.helloAt >= m.upAt ? m.helloAt - m.upAt : null,
      agentStartMs: m.agentStarted && m.upAt && m.agentStarted >= m.upAt ? m.agentStarted - m.upAt : null,
      readyMs: m.readyAt && m.upAt && m.readyAt >= m.upAt ? m.readyAt - m.upAt : null,
      restoreMs: m.restoreMs ?? null,
    };
  }

  async status(): Promise<Result> {
    const ms = Object.values(this.machines)
      .map((m) => this.machineStatus(m))
      // a stopped slot is listed only while it holds unsaved work (or failed to start)
      .filter((m) => m.state !== "stopped" || dirty(m.unsaved) || m.lastError)
      .sort((a, b) => Number(a.state === "stopped") - Number(b.state === "stopped") || Number(b.startedAt ?? 0) - Number(a.startedAt ?? 0));
    const states = ms.map((m) => m.state);
    const hostKey = await this.ctx.storage.get<HostKey>("hostKey");
    const refs = (await this.ctx.storage.get<Record<string, string>>("git:refs")) ?? {};
    const live = ms.filter((m) => m.state !== "stopped");
    return {
      project: this.state.name ?? null,
      state: states.includes("running") ? "running" : states.includes("starting") ? "starting" : "stopped",
      machines: ms,
      // the one machine, for clients from before several machines
      machine: live.length === 1 ? live[0].size : live.length ? live.map((m) => m.size).join(", ") : null,
      saved: refs["refs/heads/main"] ?? null,
      sessions: this.sessions(),
      snapshot: (await this.ctx.storage.get<string>("latestSnapshot")) ?? null,
      hostKey: hostKey?.public ?? null,
      outputs: this.outputsIndex().length,
      containers: !!this.ctx.container,
    };
  }

  /** Starts the project on a machine (or resizes the cloud one); other machines keep running. */
  async up(machine: string, origin: string): Promise<Result> {
    const owner = this.state.owner ? await this.accounts().user(this.state.owner) : null;
    const joined = this.machines[machine === "external" ? "joined" : machine];
    if (joined?.kind === "joined")
      return {
        error: `${joined.id} is a machine that joined this project itself (tule join): Tule cannot start it`,
        hint: `start it again where it ran (its container or computer), or use a Tule machine: --machine cpu-1`,
      };
    if (owner?.guest && machine !== "cpu-1") return { error: `guests can use cpu-1 machines only`, hint: "sign in for bigger machines and GPUs" };
    if (machine.startsWith("gpu-")) return this.upGpu(machine, origin);
    if (!own(MACHINES, machine) && this.state.owner) {
      const h = await this.accounts().host(this.state.owner, machine);
      if (h) return this.upHost(h, origin);
    }
    const instance = own(MACHINES, machine);
    if (!instance)
      return { error: `unknown machine ${JSON.stringify(machine)}`, hint: `use one of ${Object.keys(MACHINES).join(", ")}, gpu-<type> (${Object.keys(GPUS).map((g) => `gpu-${g}`).join(", ")}), or one of your computers (tule hosts)` };
    const c = this.ctx.container;
    if (!c) {
      // no cloud machines here: a machine that is on already serves the project
      const live = Object.values(this.machines).find((m) => m.on);
      if (live) return { ...(await this.status()), note: `the project is running on ${live.id}` };
      return {
        error: "this deployment has no cloud machines",
        hint: "start a machine with the project's join token (tule join), or use one of your computers (tule hosts)",
      };
    }
    const cur = this.machines.cloud;
    if (c.running && cur?.on) {
      if (cur.size === machine) return this.status();
      // resize: the old machine saves its copy, the new one resumes it
      await this.stopMachine("cloud", "resize");
    }
    if (this.state.owner) {
      const ok = await this.accounts().machineStart(this.state.id ?? "", this.state.owner);
      if (ok.error) return ok;
      const quota = Number(this.env.CPU_HOURS_MONTH || 100);
      const u = await this.accounts().usage(this.state.owner);
      if (u.cpuHours >= quota)
        return { error: `this month's CPU machine time is used up (${u.cpuHours} of ${quota} h)`, hint: "it renews on the 1st; GPU machines on your RunPod account are not limited by it" };
    }
    const token = await this.slot("cloud", "cloud", machine, { guest: !!owner?.guest });
    c.start({
      image: c.images.cpu,
      instance,
      enableInternet: true, // the agent dials out; pip and git need the internet too
      env: this.machineEnv(origin, token),
    });
    await c.setInactivityTimeout(IDLE_MS + 2 * CHECK_MS);
    await this.wake((owner?.guest ? 60_000 : CHECK_MS));
    return this.status();
  }

  /** Marks a slot started with a new join token; returns the token. */
  async slot(id: string, kind: Kind, size: string, extra: Partial<Machine> = {}): Promise<string> {
    const token = b64url(crypto.getRandomValues(new Uint8Array(32)));
    const now = Date.now();
    const prev = this.machines[id];
    this.machines[id] = { ...prev, id, kind, size, ...fresh, ...extra, on: true, joinHash: await sha256(token), upAt: now, billedUntil: now, lastActive: now, stoppedAt: undefined, hostError: undefined };
    await this.save();
    this.broadcast();
    return token;
  }

  machineEnv(origin: string, token: string): Record<string, string> {
    return { TULE_URL: origin, TULE_PROJECT: this.state.id ?? "", TULE_NAME: this.state.name ?? "", TULE_JOIN_TOKEN: token };
  }

  /** A machine on one of the owner's computers: its `tule host` service runs Tule's image, which dials in like any machine. */
  async upHost(h: { id: string; name: string }, origin: string): Promise<Result> {
    const cur = this.machines[h.name];
    if (cur?.on && cur.host?.id === h.id && (this.agent(h.name) || (cur.upAt ?? 0) > Date.now() - 20 * 60_000)) return this.status();
    const host = stub<Host>(this.env.HOST, h.id);
    const info = await host.status();
    if (!info.online) return { error: `${h.name} is offline`, hint: "start the computer, or check its service there: tule host status" };
    const gpus = ((info.specs as { gpus?: unknown[] } | null)?.gpus ?? []).length > 0;
    const token = await this.slot(h.name, "computer", h.name, { host: h, pod: undefined, guest: false });
    const r = await host.run(this.state.id ?? "", this.machineEnv(origin, token), gpus);
    if (r.error) {
      Object.assign(this.machines[h.name], { on: false, joinHash: undefined, hostError: String(r.error) });
      await this.save();
      return r;
    }
    await this.wake(60_000);
    return { ...(await this.status()), note: r.pulling ? `${h.name} is downloading Tule's image; the first start there takes a few minutes` : undefined };
  }

  /** The computer could not start the container (the Host object relays it). */
  async hostFailed(error: string, hostId?: string) {
    const m = Object.values(this.machines).find((x) => x.kind === "computer" && x.on && (!hostId || x.host?.id === hostId));
    if (!m) return;
    console.log(JSON.stringify({ event: "host-failed", project: this.state.name, host: m.host?.name, error }));
    Object.assign(m, { on: false, joinHash: undefined, hostError: error, stoppedAt: Date.now() });
    await this.save();
    this.broadcast();
  }

  /** A GPU machine: a pod on the owner's RunPod account running the CUDA image, which dials in like any machine. */
  async upGpu(machine: string, origin: string): Promise<Result> {
    const gpu = own(GPUS, machine.slice(4));
    if (!gpu) return { error: `unknown GPU machine ${JSON.stringify(machine)}`, hint: `use one of ${Object.keys(GPUS).map((g) => `gpu-${g}`).join(", ")}` };
    if (!this.state.owner) return { error: "this project has no owner" };
    const key = await this.accounts().runpodKey(this.state.owner);
    if (!key) return { error: "RunPod is not connected", hint: "paste a RunPod API key in Tule's Settings (GPU machines run on your RunPod account)" };
    const cur = this.machines[machine];
    // running, or started in the last 20 minutes (the image is still coming): not a second pod
    if (cur?.on && cur.pod && (this.agent(machine) || (cur.upAt ?? 0) > Date.now() - 20 * 60_000)) return this.status();
    if (cur?.pod) {
      await this.stopMachine(machine, "restart");
      if (this.machines[machine]?.pod) return { error: "the last pod of this machine could not be terminated yet", hint: "try again in a minute (its id is kept, so it is not lost)" };
    }
    const token = b64url(crypto.getRandomValues(new Uint8Array(32)));
    const pod = await createPod(key, {
      name: `tule-${this.state.name ?? "project"}-${b64url(crypto.getRandomValues(new Uint8Array(3))).toLowerCase()}`,
      image: this.env.GPU_IMAGE || "ghcr.io/janos95/tule-runtime:cuda",
      gpu,
      env: this.machineEnv(origin, token),
    });
    if ("error" in pod) return pod;
    const now = Date.now();
    this.machines[machine] = {
      ...cur, id: machine, kind: "gpu", size: machine, ...fresh, on: true, pod: { id: pod.id, cost: pod.cost, gpu, cloud: pod.cloud },
      joinHash: await sha256(token), upAt: now, billedUntil: now, lastActive: now, stoppedAt: undefined,
    };
    await this.save();
    await this.wake(60_000);
    console.log(JSON.stringify({ event: "pod-created", project: this.state.name, gpu, cloud: pod.cloud, cost: pod.cost }));
    return { ...(await this.status()), note: "RunPod is pulling the image; the first start on a host can take several minutes" };
  }

  /** Stops one machine (`machine` names it or its slot), or every machine. */
  async down(machine?: string): Promise<Result> {
    if (machine) {
      const p = this.pick(machine);
      if ("error" in p) return p;
      await this.stopMachine(p.slot, "down");
    } else for (const m of Object.values(this.machines)) if (m.on) await this.stopMachine(m.id, "down");
    return this.status();
  }

  /** Forgets a stopped slot's working copy and its unsaved work. */
  async forget(machine: string): Promise<Result> {
    const p = this.pick(machine);
    if ("error" in p) return p;
    const m = this.machines[p.slot];
    if (m.on) return { error: `${m.id} is running`, hint: "stop it first (machine_stop), or save its work" };
    delete this.machines[p.slot];
    this.outputs.delete(p.slot);
    await this.ctx.storage.delete(`outputs:${p.slot}`);
    await this.save();
    this.broadcast();
    return { forgot: p.slot };
  }

  /** A join token for a machine started elsewhere (a pod from the user's own RunPod connector, a local test). */
  async join(): Promise<Result> {
    const token = await this.slot("joined", "joined", "joined");
    return { project: this.state.name, machine: "joined", joinToken: token };
  }

  /** The slot whose join token this is, or null. */
  async checkJoin(token: string): Promise<string | null> {
    if (!token) return null;
    const h = await sha256(token);
    return Object.values(this.machines).find((m) => m.joinHash === h)?.id ?? null;
  }

  // ------------------------------------------------------------ git (saves)
  //
  // The project's git remote (cli/tule_cli/gitremote.py speaks to it): refs here,
  // each push a git bundle in R2 (gitKey) holding the commits the push added.
  // A fetch applies the bundles it lacks in push order.

  async gitRefs(): Promise<{ refs: Record<string, string>; bundles: { id: string; tip: string }[] }> {
    const refs = (await this.ctx.storage.get<Record<string, string>>("git:refs")) ?? {};
    const b = await this.ctx.storage.list<{ id: string; tip: string }>({ prefix: "gitb:" });
    return { refs, bundles: [...b.values()].map((x) => ({ id: x.id, tip: x.tip })) };
  }

  /** Moves `ref` from `old` to `next` ("" = absent) if it is still at `old`; `bundle` holds the new commits. */
  async gitPush(ref: string, old: string, next: string, bundle?: { id: string; tip: string; size: number }): Promise<Result> {
    const refs = (await this.ctx.storage.get<Record<string, string>>("git:refs")) ?? {};
    if ((refs[ref] ?? "") !== old)
      return { error: `${ref} moved on the remote (it is at ${refs[ref]?.slice(0, 12) ?? "nothing"} now)`, hint: "fetch, merge, and push again" };
    if (next) refs[ref] = next;
    else delete refs[ref];
    const put: Record<string, unknown> = { "git:refs": refs };
    if (bundle) {
      const seq = ((await this.ctx.storage.get<number>("git:seq")) ?? 0) + 1;
      put["git:seq"] = seq;
      put[`gitb:${String(seq).padStart(9, "0")}`] = { ...bundle, ref, at: Date.now() };
    }
    await this.ctx.storage.put(put);
    if (ref === "refs/heads/main") this.broadcast();
    return { ref, from: old || null, to: next || null };
  }

  // ------------------------------------------------------------ snapshots (autosave, per working copy)

  async addSnapshot(slot: string, reason: string, manifest: Record<string, unknown>): Promise<Result> {
    const name = this.state.id ?? "";
    const id = `${Date.now().toString(36).padStart(9, "0")}-${b64url(crypto.getRandomValues(new Uint8Array(3))).toLowerCase().replace(/[^0-9a-z]/g, "0")}`;
    const files = (manifest.files ?? {}) as Record<string, { s?: number }>;
    const rec: Snapshot = {
      id,
      at: Date.now(),
      reason: reason.slice(0, 80),
      files: Object.keys(files).length,
      bytes: Object.values(files).reduce((n, f) => n + (Number(f.s) || 0), 0),
      machine: slot,
    };
    await this.env.WORKSPACES.put(snapshotKey(name, id), JSON.stringify({ ...manifest, id }), {
      httpMetadata: { contentType: "application/json" },
    });
    await this.ctx.storage.put({ [`snap:${id}`]: rec, latestSnapshot: id });
    const m = this.machines[slot];
    if (m) m.snapshot = id;
    this.touch(slot);
    await this.save();
    return { id };
  }

  async snapshots(limit = 50, machine?: string): Promise<Result> {
    const m = await this.ctx.storage.list<Snapshot>({ prefix: "snap:", reverse: true, limit: machine ? 2000 : Math.min(Math.max(limit, 1), 500) });
    const all = [...m.values()];
    const slot = machine ? slotOf(machine).id : undefined;
    return { snapshots: (slot ? all.filter((s) => (s.machine ?? slot) === machine || s.machine === slot) : all).slice(0, Math.min(Math.max(limit, 1), 500)) };
  }

  /**
   * What a machine in `slot` restores on boot: its own newest snapshot (it is
   * coming back). A fresh slot starts from the latest save (git, the agent
   * fetches it), or, for a project that was never saved in git (from before
   * saves), from the project's newest snapshot.
   */
  async bootSnapshot(slot: string): Promise<string | undefined> {
    const own = this.machines[slot]?.snapshot;
    if (own) return own;
    const refs = (await this.ctx.storage.get<Record<string, string>>("git:refs")) ?? {};
    if (refs["refs/heads/main"]) return undefined;
    return this.ctx.storage.get<string>("latestSnapshot");
  }

  /** For a view-only link: the copy being viewed (or `machine`), autosaved now if it runs, and its outputs list.
      Opening the link later uses only this snapshot, not a machine. */
  async freezeShare(machine?: string): Promise<{ snapshot: string; at: number; outputs: unknown[] } | { error: string; hint?: string }> {
    const slot = machine && own(this.machines, machine) ? machine : this.viewSlot();
    if (!slot) return { error: "nothing to share yet", hint: "load the project on a machine (tule up) and build its outputs" };
    let now: number | undefined; // the machine just saved: what it has now (an unchanged save keeps its old id)
    let saved: string | undefined;
    if (this.agent(slot) && this.machines[slot]?.readyAt) {
      const r = await this.ask(slot, { t: "snapshot", reason: "shared" }, 300_000);
      if (r.error) return { error: `could not autosave ${slot} for the link: ${r.error}`, hint: "try again in a moment" };
      now = Date.now();
      if (typeof r.snapshot === "string") saved = r.snapshot;
    }
    const sid = saved ?? this.machines[slot]?.snapshot;
    if (!sid) return { error: `${slot} has no autosave yet`, hint: "start it once (tule up) so it autosaves" };
    const rec = await this.ctx.storage.get<Snapshot>(`snap:${sid}`);
    return { snapshot: sid, at: now ?? rec?.at ?? Date.now(), outputs: this.outputs.get(slot) ?? [] };
  }

  /** For an admin copy: the newest autosave (or `machine`'s), without asking any machine. */
  async adminSnapshot(machine?: string): Promise<{ id: string; at: number; machine?: string } | { error: string; hint?: string }> {
    const id = machine ? this.machines[machine]?.snapshot : await this.ctx.storage.get<string>("latestSnapshot");
    if (!id) return machine ? { error: `no machine ${machine} with an autosave`, hint: `machines: ${Object.keys(this.machines).join(", ") || "none"}` } : { error: "this project has no autosave yet" };
    const rec = await this.ctx.storage.get<Snapshot>(`snap:${id}`);
    return { id, at: rec?.at ?? 0, machine: rec?.machine };
  }

  /** An admin copy's snapshot, once its files are all here: a machine started on the copy restores it. */
  async importSnapshot(id: string, rec: { at: number; files: number; bytes: number; reason: string }): Promise<void> {
    await this.ctx.storage.put({ [`snap:${id}`]: { id, ...rec }, latestSnapshot: id });
  }

  async latestSnapshot(): Promise<string | undefined> {
    return this.ctx.storage.get<string>("latestSnapshot");
  }

  /** Sends a command to a slot's agent and waits for its reply. */
  ask(slot: string, msg: Result, timeoutMs: number): Promise<Result> {
    const agent = this.agent(slot);
    if (!agent) return Promise.resolve({ error: `the machine ${slot} is not running`, hint: "start it with tule up, or machine_start" });
    const rid = crypto.getRandomValues(new Uint32Array(1))[0];
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.asks.delete(rid);
        resolve({ error: `the machine ${slot} did not answer ${String(msg.t)} within ${timeoutMs / 1000} s` });
      }, timeoutMs);
      this.asks.set(rid, (r) => {
        clearTimeout(timer);
        resolve(r);
      });
      agent.send(JSON.stringify({ ...msg, rid }));
    });
  }

  /** A command for one machine (snapshot, restore, save); `machine` may be omitted when only one runs. */
  async command(msg: Result, machine?: string): Promise<Result> {
    let target = machine;
    if (!target && msg.t === "restore" && typeof msg.id === "string") target = (await this.ctx.storage.get<Snapshot>(`snap:${msg.id}`))?.machine;
    const p = this.pick(target);
    if ("error" in p) return { project: this.state.name, ...p };
    const r = await this.ask(p.slot, msg, 600_000);
    this.touch(p.slot);
    return { project: this.state.name, machine: p.slot, ...r };
  }

  async stopMachine(slot: string, why: string) {
    const m = this.machines[slot];
    if (!m) return;
    if (this.agent(slot) && m.readyAt) {
      // the final snapshot: what the machine saved is what this slot resumes
      const r = await this.ask(slot, { t: "stop", reason: why }, 120_000);
      if (r.error) console.log(JSON.stringify({ event: "final-snapshot", project: this.state.name, machine: slot, error: r.error }));
    }
    for (const ws of this.ctx.getWebSockets(`m:${slot}`)) ws.close(4001, `machine stopped (${why})`);
    for (const ws of this.ctx.getWebSockets(`sm:${slot}`)) ws.close(4001, `machine stopped (${why})`);
    const c = this.ctx.container;
    if (m.kind === "cloud" && c?.running) {
      try {
        await c.destroy(why);
      } catch (e) {
        console.log(JSON.stringify({ event: "destroy", error: String(e) }));
      }
    }
    await this.bill(m);
    if (m.kind === "cloud" && this.state.id) await this.accounts().machineStopped(this.state.id);
    if (m.host) {
      const r = await stub<Host>(this.env.HOST, m.host.id).stop(this.state.id ?? "");
      if (r.error) console.log(JSON.stringify({ event: "host-stop", project: this.state.name, host: m.host.name, error: r.error }));
    }
    if (m.pod && this.state.owner) {
      const key = await this.accounts().runpodKey(this.state.owner);
      try {
        if (key) {
          await terminatePod(key, m.pod.id);
          m.pod = undefined;
        } else {
          // RunPod was disconnected: the pod cannot be ended from here. Its id stays, so it is ended once a key is back.
          console.log(JSON.stringify({ event: "terminate", project: this.state.name, pod: m.pod.id, error: "no RunPod key" }));
        }
      } catch (e) {
        // keep the pod id: the alarm retries, so a pod never keeps billing unnoticed
        console.log(JSON.stringify({ event: "terminate", project: this.state.name, error: String(e) }));
        await this.wake(60_000);
      }
    }
    Object.assign(m, { on: false, joinHash: undefined, stoppedAt: Date.now() });
    await this.save();
    this.broadcast();
  }

  /** How long a started machine has been out of contact: since it was started if its agent never said hello,
   * else since the connection closed. 0 while connected. A machine that ran for hours and reconnects after a
   * deploy is not "never connected". */
  away(m: Machine): number {
    if (this.agent(m.id)) return 0;
    if (!m.helloAt) return Date.now() - (m.upAt ?? 0);
    m.lostAt ??= Date.now(); // the close was not seen (this object restarted): the clock starts now
    return Date.now() - m.lostAt;
  }

  /** Ends every GPU machine (RunPod is being disconnected): how many pods could not be terminated. */
  async stopGpus(): Promise<number> {
    for (const m of Object.values(this.machines)) if (m.pod) await this.stopMachine(m.id, "RunPod disconnected");
    return Object.values(this.machines).filter((m) => m.pod).length;
  }

  /** The alarm runs within ms (or sooner, if one is already due sooner): a later request never postpones an earlier check. */
  async wake(ms: number) {
    const at = Date.now() + Math.max(ms, 0);
    const cur = await this.ctx.storage.getAlarm();
    if (cur === null || cur > at) await this.ctx.storage.setAlarm(at);
  }

  async alarm() {
    let next = 0;
    const later = (ms: number) => (next = next ? Math.min(next, Date.now() + ms) : Date.now() + ms);
    for (const m of Object.values(this.machines)) {
      if (m.pod && this.state.owner) {
        // a pod that failed, or one left over from a failed terminate
        const key = await this.accounts().runpodKey(this.state.owner);
        const p = key ? await getPod(key, m.pod.id).catch(() => undefined) : undefined; // no key: unknown, not gone
        const dead = p === null || (p && ["EXITED", "ERROR", "TERMINATED"].includes(p.status));
        const stuck = this.away(m) > 20 * 60_000;
        if (dead || stuck || !m.on) {
          console.log(JSON.stringify({ event: "pod-cleanup", project: this.state.name, machine: m.id, status: p?.status ?? (key ? "gone" : "no key"), stuck }));
          await this.stopMachine(m.id, dead ? "the pod stopped" : m.helloAt ? "the pod lost contact" : "the pod never connected");
          if (m.pod && key) later(60_000); // not terminated: again in a minute
          continue;
        }
      }
      if (!m.on) continue;
      if (m.kind === "computer" && this.away(m) > 20 * 60_000) {
        await this.hostFailed("the machine did not connect within 20 minutes", m.host?.id);
        continue;
      }
      if (m.kind === "cloud" && !this.ctx.container?.running && !this.agent(m.id)) {
        Object.assign(m, { on: false, joinHash: undefined, stoppedAt: Date.now() });
        await this.bill(m);
        if (this.state.id) await this.accounts().machineStopped(this.state.id); // or a guest's slot stays taken
        this.broadcast();
        continue;
      }
      await this.bill(m);
      if (m.guest && this.state.owner && (await this.accounts().guestTimeLeft(this.state.owner)) <= 0) {
        console.log(JSON.stringify({ event: "guest-minutes", project: this.state.name }));
        await this.stopMachine(m.id, "today's guest machine time is used up");
        continue;
      }
      const idle = Date.now() - (m.lastActive ?? 0);
      if (this.sessions(m.id) === 0 && idle > (m.guest ? GUEST_IDLE_MS : IDLE_MS)) {
        console.log(JSON.stringify({ event: "idle-stop", project: this.state.name, machine: m.id, idleMin: Math.round(idle / 60_000) }));
        await this.stopMachine(m.id, "idle");
        continue;
      }
      later(m.pod || m.guest || (m.kind === "computer" && !this.agent(m.id)) ? 60_000 : CHECK_MS);
    }
    await this.save();
    if (next) await this.wake(next - Date.now());
  }

  // ------------------------------------------------------------ sockets

  /** A viewer request passed on to a machine: its id there, remembered in memory and in storage. */
  relay(ws: WebSocket, rid: number, kind: "req" | "mesh", slot: string): number {
    const grid = crypto.getRandomValues(new Uint32Array(1))[0];
    this.pending.set(grid, { ws, rid });
    const v = (ws.deserializeAttachment() as { v?: string } | null)?.v;
    if (v) void this.ctx.storage.put(`rq:${grid}`, { v, rid, kind, slot, at: Date.now() });
    return grid;
  }

  /** Who asked request `grid` (it is answered: forgotten, unless `more` of the answer is to come). */
  async answer(grid: number, more = false): Promise<{ ws: WebSocket; rid: number } | undefined> {
    const p = this.pending.get(grid);
    if (!more) this.pending.delete(grid);
    const key = `rq:${grid}`;
    const saved = p ? undefined : await this.ctx.storage.get<{ v: string; rid: number }>(key);
    if (!more) void this.ctx.storage.delete(key);
    if (p) return p;
    const ws = saved && this.ctx.getWebSockets(`v:${saved.v}`)[0];
    return ws ? { ws, rid: saved!.rid } : undefined;
  }

  /** The machine's connection went: what it was asked will not be answered, so the viewers hear it now. */
  async dropRelays(slot: string) {
    const all = await this.ctx.storage.list<{ v: string; rid: number; kind: string; slot: string; at: number }>({ prefix: "rq:" });
    const gone: string[] = [];
    for (const [key, r] of all) {
      const old = Date.now() - r.at > 3600_000;
      if (r.slot !== slot && !old) continue;
      gone.push(key);
      this.pending.delete(Number(key.slice(3)));
      if (old) continue;
      const error = "the machine reconnected while answering; try again";
      for (const ws of this.ctx.getWebSockets(`v:${r.v}`)) {
        try {
          ws.send(JSON.stringify(r.kind === "mesh" ? { t: "mesh-error", rid: r.rid, error } : { t: "res", rid: r.rid, error }));
        } catch {
          // closing
        }
      }
    }
    if (gone.length) await this.ctx.storage.delete(gone);
  }

  /** The slot a viewer looks at: its pick, else the most recently ready connected one, else the newest copy with outputs. */
  viewSlot(ws?: WebSocket): string | undefined {
    const picked = (ws?.deserializeAttachment() as { m?: string } | null)?.m;
    if (picked && own(this.machines, picked)) return picked;
    const live = this.connected()[0];
    if (live) return live.id;
    return Object.values(this.machines)
      .filter((m) => this.outputs.has(m.id))
      .sort((a, b) => (b.stoppedAt ?? 0) - (a.stoppedAt ?? 0))[0]?.id;
  }

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const what = url.pathname.split("/").filter(Boolean).pop();
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    if (what === "agent") {
      const h = req.headers.get("Authorization") ?? "";
      const slot = await this.checkJoin(h.startsWith("Bearer ") ? h.slice(7) : "");
      if (!slot) return new Response("invalid join token\n", { status: 401 });
      for (const old of this.ctx.getWebSockets(`m:${slot}`)) old.close(4000, "replaced by a new connection");
      await this.dropRelays(slot);
      for (const s of this.ctx.getWebSockets(`sm:${slot}`)) s.close(4002, "the machine reconnected");
      this.ctx.acceptWebSocket(server, ["agent", `m:${slot}`]);
      server.serializeAttachment({ m: slot });
    } else if (what === "ssh") {
      const p = this.pick(url.searchParams.get("machine") || undefined);
      if ("error" in p) return new Response(`${p.error}${p.hint ? ` (${p.hint})` : ""}\n`, { status: 503 });
      const agent = this.agent(p.slot);
      if (!agent) return new Response(`the machine ${p.slot} is ${this.machineState(this.machines[p.slot])}; start it with tule up\n`, { status: 503 });
      const sid = crypto.getRandomValues(new Uint32Array(1))[0];
      this.ctx.acceptWebSocket(server, ["ssh", `sid:${sid}`, `sm:${p.slot}`]);
      server.serializeAttachment({ sid, m: p.slot });
      agent.send(JSON.stringify({ t: "ssh-open", sid }));
      this.touch(p.slot);
    } else if (what === "view") {
      const v = crypto.randomUUID();
      this.ctx.acceptWebSocket(server, ["view", `v:${v}`]);
      const m = url.searchParams.get("machine");
      server.serializeAttachment({ m: m && own(this.machines, m) ? m : undefined, v });
      await this.sendView(server, await this.status());
    } else {
      return new Response("not found\n", { status: 404 });
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  async sendView(ws: WebSocket, status: Result) {
    const slot = this.viewSlot(ws);
    try {
      ws.send(JSON.stringify({ t: "outputs", outputs: this.outputs.get(slot ?? "") ?? [], status: { ...status, viewing: slot ?? null } }));
    } catch {
      // closing
    }
  }

  async webSocketMessage(ws: WebSocket, msg: string | ArrayBuffer) {
    const tags = this.ctx.getTags(ws);
    if (tags.includes("agent")) return this.fromAgent(ws, msg);
    if (tags.includes("ssh")) {
      const { sid, m } = ws.deserializeAttachment() as { sid: number; m: string };
      const agent = this.agent(m);
      if (!agent) return ws.close(4003, "the machine is gone");
      const data = typeof msg === "string" ? new TextEncoder().encode(msg) : new Uint8Array(msg);
      agent.send(frame(SSH, sid, data));
      this.touch(m);
      return;
    }
    if (tags.includes("view") && typeof msg === "string") {
      let m: { t?: string; rid?: number; path?: string; hash?: string; op?: unknown; args?: unknown; machine?: unknown };
      try {
        m = JSON.parse(msg);
      } catch {
        return;
      }
      if (m.t === "pick") {
        const slot = typeof m.machine === "string" && own(this.machines, m.machine) ? m.machine : undefined;
        ws.serializeAttachment({ ...(ws.deserializeAttachment() as object), m: slot });
        await this.sendView(ws, await this.status());
        return;
      }
      if (typeof m.rid !== "number") return;
      const slot = this.viewSlot(ws);
      const agent = slot ? this.agent(slot) : undefined;
      const m0 = slot && own(this.machines, slot) ? this.machines[slot] : undefined;
      const away = m0?.on ? "the machine is reconnecting; try again in a moment" : "the machine is stopped: start it (tule up)";
      if (m.t === "req") {
        const r = m as { rid: number; op?: unknown; args?: unknown };
        if (!agent) return ws.send(JSON.stringify({ t: "res", rid: r.rid, error: away }));
        const grid = this.relay(ws, r.rid, "req", slot!);
        agent.send(JSON.stringify({ t: "req", rid: grid, op: r.op, args: r.args ?? {} }));
        this.touch(slot);
        return;
      }
      if (m.t !== "mesh" || typeof m.path !== "string") return;
      if (!agent) return ws.send(JSON.stringify({ t: "mesh-error", rid: m.rid, error: away }));
      const grid = this.relay(ws, m.rid, "mesh", slot!);
      agent.send(JSON.stringify({ t: "mesh", rid: grid, path: m.path, hash: m.hash ?? "" }));
    }
  }

  async fromAgent(ws: WebSocket, msg: string | ArrayBuffer) {
    const slot = (ws.deserializeAttachment() as { m: string }).m;
    const me = this.machines[slot];
    if (typeof msg !== "string") {
      const b = new Uint8Array(msg);
      if (b.byteLength < 5) return;
      const kind = b[0];
      const id = new DataView(msg).getUint32(1);
      if (kind === SSH) {
        const s = this.ssh(id);
        if (s) s.send(b.subarray(5));
        else ws.send(JSON.stringify({ t: "ssh-close", sid: id }));
        this.touch(slot);
      } else if (kind === MESH || kind === MESH_PART) {
        const p = kind === MESH ? await this.answer(id) : await this.answer(id, true);
        if (p && p.ws.readyState === WebSocket.OPEN) {
          const out = new Uint8Array(b.byteLength - 1);
          new DataView(out.buffer).setUint32(0, kind === MESH ? p.rid : (p.rid | 0x80000000) >>> 0);
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
        if (!me) return;
        me.helloAt = Date.now();
        me.lostAt = undefined;
        me.agentStarted = typeof m.started === "number" ? m.started : undefined;
        me.lastActive = Date.now();
        await this.save();
        await this.sendConfig(slot);
        await this.wake(CHECK_MS); // a joined machine was not started here: its checks start with its hello
        console.log(JSON.stringify({ event: "agent-hello", project: this.state.name, machine: slot, coldStartMs: me.upAt ? me.helloAt - me.upAt : null }));
        this.broadcast();
        break;
      }
      case "hostkey":
        if (typeof m.private === "string" && typeof m.public === "string" && !(await this.ctx.storage.get("hostKey")))
          await this.ctx.storage.put("hostKey", { private: m.private, public: m.public });
        break;
      case "outputs": {
        const list = Array.isArray(m.outputs) ? m.outputs : [];
        this.outputs.set(slot, list);
        try {
          await this.ctx.storage.put(`outputs:${slot}`, list);
        } catch {
          // an index too large for one stored value stays in memory (the agent sends it again
          // on every connection); an older stored one must not come back after a restart
          await this.ctx.storage.delete(`outputs:${slot}`).catch(() => {});
        }
        this.broadcast();
        break;
      }
      case "unsaved":
        if (!me) return;
        me.unsaved = { files: Number(m.files) || 0, commits: Number(m.commits) || 0, at: Date.now() };
        await this.save();
        this.broadcast();
        break;
      case "ssh-close": {
        const s = this.ssh(Number(m.sid));
        if (s) s.close(1000, "session ended");
        break;
      }
      case "res": {
        const p = await this.answer(Number(m.rid));
        if (p && p.ws.readyState === WebSocket.OPEN) p.ws.send(JSON.stringify({ ...m, rid: p.rid }));
        break;
      }
      case "broadcast": {
        const { t: _t, ...rest } = m;
        const out = JSON.stringify({ ...rest, t: "event" });
        for (const v of this.ctx.getWebSockets("view")) {
          if (this.viewSlot(v) !== slot) continue;
          try {
            v.send(out);
          } catch {
            // closing
          }
        }
        break;
      }
      case "mesh-error": {
        const p = await this.answer(Number(m.rid));
        if (p && p.ws.readyState === WebSocket.OPEN) p.ws.send(JSON.stringify({ t: "mesh-error", rid: p.rid, error: m.error }));
        break;
      }
      case "ready":
        if (!me) return;
        me.readyAt = Date.now();
        me.restoreMs = typeof m.restoreMs === "number" ? m.restoreMs : undefined;
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

  /** The owner's SSH keys and the host key, to the machines (on connect and when keys change). */
  async sendConfig(slot?: string) {
    const agents = slot ? [this.agent(slot)].filter((a): a is WebSocket => !!a) : this.ctx.getWebSockets("agent");
    if (!agents.length) return;
    const hostKey = (await this.ctx.storage.get<HostKey>("hostKey")) ?? null;
    const keys = this.state.owner ? (await this.accounts().sshKeys(this.state.owner)).map((k) => k.key) : [];
    const msg = JSON.stringify({ t: "config", keys, hostKey });
    for (const a of agents) {
      try {
        a.send(msg);
      } catch {
        // closing
      }
    }
  }

  broadcast() {
    void this.status().then((status) => {
      for (const v of this.ctx.getWebSockets("view")) void this.sendView(v, status);
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
      const { sid, m } = ws.deserializeAttachment() as { sid: number; m: string };
      this.agent(m)?.send(JSON.stringify({ t: "ssh-close", sid }));
    } else if (tags.includes("agent")) {
      const slot = (ws.deserializeAttachment() as { m: string }).m;
      if (!this.agent(slot)) {
        const m = this.machines[slot];
        if (m?.on) {
          m.lostAt = Date.now();
          await this.save();
        }
        for (const s of this.ctx.getWebSockets(`sm:${slot}`)) s.close(4003, "the machine disconnected");
        await this.dropRelays(slot);
        this.broadcast();
      }
    }
  }

  async webSocketError(ws: WebSocket) {
    await this.webSocketClose(ws, 1011, "error");
  }
}
