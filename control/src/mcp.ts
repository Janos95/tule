// The hosted MCP server at /mcp (Streamable HTTP, JSON responses, stateless),
// for the control plane only: projects, machines, snapshots, keys, usage, and
// guide(topic) with the agent instructions (skills/*.md). The work itself
// happens over SSH with the tule CLI, never through these tools.
//
// Auth: OAuth through this site (the client signs the user in with Google
// and asks for consent) or a Tule API key as a Bearer token; index.ts puts the
// user in ctx.props.userId. JSON-RPC handling follows genau's relay.
import type { User } from "./accounts";
import { accounts } from "./auth";
import * as ops from "./ops";
import { gpuCatalog } from "./runpod";
import { SKILLS } from "./skills.gen";

const MODERN = "2026-07-28";
const LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];
const META_VERSION = "io.modelcontextprotocol/protocolVersion";
const SERVER_INFO = { name: "tule", title: "Tule", version: "0.1.0" };

export const INSTRUCTIONS = `Tule runs the user's engineering projects on machines: Linux with OpenCascade, the \`tule\` Python modeling API (immutable shapes, tags that survive operations) and topology optimization. These tools manage the control plane only: projects, machines (cpu-1 = 1 vCPU/6 GiB, cpu-2, cpu-4; GPUs on the user's RunPod account; the user's own computers), saves, autosaves of /workspace, SSH keys, API keys and usage. A project can be loaded on several machines at once, each with its own copy of /workspace. The work happens on a machine over SSH with the tule CLI (connect_info says how): src/<name>.py makes /workspace/outputs/<name>/ and \`tule build\` runs the scripts whose inputs changed (nothing runs by itself). The user watches outputs live in the viewer (STEP/STL in 3D, tables, Vega-Lite charts, Markdown); check them with \`tule screenshot\`. Each copy is autosaved continuously; \`tule save\` (or the save tool) commits it to the project's git history and merges what other machines saved. Nothing outside /workspace persists. Read guide("index") first, then the topic you need.`;

type Tool = { name: string; description: string; inputSchema: Record<string, unknown>; annotations?: Record<string, unknown> };
const str = (description: string) => ({ type: "string", description });
const project = { project: str("project name, e.g. camera-bracket") };
const obj = (properties: Record<string, unknown>, required: string[] = []) => ({ type: "object", properties, required, additionalProperties: false });
const RO = { readOnlyHint: true };

export const TOOLS: Tool[] = [
  { name: "guide", description: "Tule's instructions for agents by topic. Start with 'index'.", inputSchema: obj({ topic: str(`one of: ${Object.keys(SKILLS).join(", ") || "index"}`) }), annotations: RO },
  { name: "projects_list", description: "The user's projects with their machine state and viewer links.", inputSchema: obj({}), annotations: RO },
  { name: "projects_create", description: "Create a project (a new /workspace from the template: AGENTS.md, tule.toml, src/part.py, git).", inputSchema: obj({ name: str("lowercase letters, digits and '-', e.g. camera-bracket") }, ["name"]) },
  { name: "projects_rename", description: "Rename a project.", inputSchema: obj({ ...project, to: str("the new name") }, ["project", "to"]) },
  { name: "projects_delete", description: "Delete a project: stops its machines and deletes every copy of /workspace, its saves and autosaves. Cannot be undone; confirm with the user first.", inputSchema: obj({ ...project, confirm: str("the project name again, to confirm") }, ["project", "confirm"]), annotations: { destructiveHint: true } },
  { name: "machine_start", description: "Load the project on a machine and wait until its /workspace is ready (a machine it ran on before gets its own copy back; a new one starts from the latest save). Other machines keep running. cpu-1 (default, 1 vCPU / 6 GiB), cpu-2 (2 / 8), cpu-4 (4 / 12), or a GPU on the user's RunPod account: gpu-rtx4090, gpu-l40s, gpu-a100, gpu-h100, ... (gpus_list has prices; GPU time costs the user money, so ask first), or one of the user's own computers by name (computers_list). The project has one Tule cloud machine: another cpu size resizes it.", inputSchema: obj({ ...project, machine: str("cpu-1, cpu-2, cpu-4, gpu-<type>, or a computer's name") }, ["project"]) },
  { name: "computers_list", description: "The user's own computers plugged into Tule (tule host up): online state, cores, memory, GPUs. A machine can run on one by passing its name to machine_start.", inputSchema: obj({}), annotations: RO },
  { name: "gpus_list", description: "GPU machines on the user's RunPod account: price per hour and current availability.", inputSchema: obj({}), annotations: RO },
  { name: "machine_stop", description: "Autosave and stop the project's machines, or one of them. Unsaved work stays in that machine's copy.", inputSchema: obj({ ...project, machine: str("one machine's id (default: all)") }, ["project"]) },
  { name: "machine_status", description: "The project's machines: state, size, unsaved work, timings, SSH sessions; and the latest save.", inputSchema: obj(project, ["project"]), annotations: RO },
  { name: "machine_forget", description: "Drop a stopped machine's copy of /workspace with its unsaved work. Confirm with the user first.", inputSchema: obj({ ...project, machine: str("the machine's id from machine_status") }, ["project", "machine"]), annotations: { destructiveHint: true } },
  { name: "save", description: "Commit a machine's /workspace, merge what other machines saved, and push it as the project's latest save. On a merge conflict nothing is saved: the answer lists the files; resolve them on that machine and save again.", inputSchema: obj({ ...project, message: str("what changed"), machine: str("which machine (an id from machine_status: cloud, gpu-<type>, a computer's name, joined); needed only when several run") }, ["project"]) },
  { name: "connect_info", description: "How to work on the project's machine: one command that installs the tule CLI and signs it in (no user step), then SSH in; the viewer link.", inputSchema: obj(project, ["project"]), annotations: RO },
  { name: "join_token", description: "A join token for a machine started elsewhere (e.g. a pod made with the user's own RunPod connector): run the Tule image with these environment variables and it joins the project.", inputSchema: obj(project, ["project"]) },
  { name: "outputs_list", description: "What the viewer shows: outputs/, work/ and inputs/ folders with their files and viewer links; an output that is not up to date has build: stale (why), building, or failed (error, log).", inputSchema: obj(project, ["project"]), annotations: RO },
  { name: "snapshots_list", description: "Autosaves of /workspace (every machine's copy, or one), newest first.", inputSchema: obj({ ...project, limit: { type: "number" }, machine: str("only this machine's copy") }, ["project"]), annotations: RO },
  { name: "snapshot_now", description: "Autosave a machine's /workspace now (it must be running).", inputSchema: obj({ ...project, machine: str("which machine (an id from machine_status: cloud, gpu-<type>, a computer's name, joined); needed only when several run") }, ["project"]) },
  { name: "snapshot_restore", description: "Make a machine's /workspace match an autosave (the current state is autosaved first, so this can be undone).", inputSchema: obj({ ...project, id: str("snapshot id from snapshots_list"), machine: str("restore into this machine (default: the one that made it)") }, ["project", "id"]) },
  { name: "ssh_keys_list", description: "The user's SSH public keys (they work on every project's machine).", inputSchema: obj({}), annotations: RO },
  { name: "ssh_keys_add", description: "Add an SSH public key (ssh-ed25519 ...).", inputSchema: obj({ key: str("the public key line"), name: str("a label") }, ["key"]) },
  { name: "ssh_keys_remove", description: "Remove an SSH key by id.", inputSchema: obj({ id: str("key id from ssh_keys_list") }, ["id"]) },
  { name: "api_keys_list", description: "The user's Tule API keys (names and prefixes only).", inputSchema: obj({}), annotations: RO },
  { name: "api_keys_create", description: "Create a Tule API key (shown once), e.g. for CI. The CLI gets its own with `tule login`.", inputSchema: obj({ name: str("what it is for") }, ["name"]) },
  { name: "api_keys_revoke", description: "Revoke an API key by id.", inputSchema: obj({ id: str("key id from api_keys_list") }, ["id"]) },
  { name: "usage", description: "This month's machine hours (CPU quota) and GPU hours/cost on RunPod.", inputSchema: obj({}), annotations: RO },
];
const NAMES = new Set(TOOLS.map((t) => t.name));

async function call(env: Env, user: User, name: string, a: Record<string, unknown>, origin: string): Promise<ops.Result | string> {
  const acc = accounts(env);
  switch (name) {
    case "guide": {
      const topic = String(a.topic ?? "index").replace(/\.md$/, "");
      return SKILLS[topic] ?? `No topic ${JSON.stringify(topic)}. Topics: ${Object.keys(SKILLS).join(", ")}`;
    }
    case "projects_list": return ops.listProjects(env, user, origin);
    case "projects_create": return ops.createProject(env, user, a.name, origin);
    case "projects_rename": return ops.renameProject(env, user, a.project, a.to);
    case "projects_delete":
      if (a.confirm !== a.project) return { error: "confirm must repeat the project name", hint: "ask the user first: this deletes /workspace and all snapshots" };
      return ops.deleteProject(env, user, a.project);
    case "machine_start": {
      const r = await ops.up(env, user, a.project, a.machine, origin);
      if (r.error) return r;
      // wait (bounded) until the machine is ready, so the agent can SSH in next
      const m = String(a.machine || "cpu-1");
      const slot = m.startsWith("cpu-") ? "cloud" : m;
      const ready = (s: ops.Result) => (s.machines as ops.Result[] | undefined)?.find((x) => x.id === slot);
      const p = await ops.resolve(env, user, a.project); // once: the loop asks the project itself
      if (!ops.isRef(p)) return p;
      const status = async (): Promise<ops.Result> => ({ ...(await ops.projectStub(env, p.id).status()), viewer: ops.viewerUrl(origin, p.name) });
      for (let i = 0; i < (m.startsWith("cpu-") ? 100 : 150); i++) {
        const s = await status();
        const x = ready(s);
        if (s.error || x?.state === "running" || (x?.state === "stopped" && x.lastError)) return s;
        await new Promise((res) => setTimeout(res, 600));
      }
      return { ...(await status()), note: "still starting; check machine_status again shortly" };
    }
    case "gpus_list": {
      const key = await acc.runpodKey(user.id);
      if (!key) return { error: "RunPod is not connected", hint: "the user pastes a RunPod API key in Tule's Settings" };
      return { gpus: await gpuCatalog(key) };
    }
    case "computers_list": return { computers: await ops.listHosts(env, user) };
    case "machine_stop": return ops.down(env, user, a.project, a.machine);
    case "machine_forget": return ops.forget(env, user, a.project, a.machine);
    case "save": return ops.command(env, user, a.project, { t: "save", message: String(a.message ?? "") }, a.machine);
    case "machine_status": return ops.status(env, user, a.project, origin);
    case "connect_info": {
      const p = await ops.resolve(env, user, a.project);
      return ops.isRef(p) ? ops.connectInfo(env, user, origin, p.name) : p;
    }
    case "join_token": return ops.join(env, user, a.project, origin);
    case "outputs_list": return ops.outputs(env, user, a.project, origin);
    case "snapshots_list": return ops.snapshots(env, user, a.project, Number(a.limit ?? 20), a.machine);
    case "snapshot_now": return ops.command(env, user, a.project, { t: "snapshot", reason: "agent" }, a.machine);
    case "snapshot_restore": return ops.command(env, user, a.project, { t: "restore", id: String(a.id ?? "") }, a.machine);
    case "ssh_keys_list": return { keys: (await acc.sshKeys(user.id)).map((k) => ({ id: k.id, name: k.name, key: k.key.slice(0, 40) + "…" })) };
    case "ssh_keys_add": return ops.addSshKey(env, user, a.key, a.name);
    case "ssh_keys_remove": return ops.removeSshKey(env, user, a.id);
    case "api_keys_list": return { keys: await acc.apiKeys(user.id) };
    case "api_keys_create": {
      const k = await acc.createApiKey(user.id, String(a.name ?? "API key"));
      return { ...k, note: "Shown once. Give it to the user; don't store it in the project." };
    }
    case "api_keys_revoke": return (await acc.revokeApiKey(user.id, String(a.id ?? ""))) ? { revoked: a.id } : { error: `no API key ${JSON.stringify(a.id)}` };
    case "usage": return ops.usage(env, user);
  }
  return { error: `unknown tool ${name}` };
}

type Id = string | number | null;
type Rpc = { jsonrpc?: string; id?: Id; method?: unknown; params?: Record<string, unknown> };
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization, MCP-Protocol-Version, Mcp-Session-Id",
  "Access-Control-Expose-Headers": "Mcp-Session-Id",
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...CORS } });
const rpcError = (id: Id, code: number, message: string, status = 200) => json({ jsonrpc: "2.0", id, error: { code, message } }, status);

export async function mcp(req: Request, env: Env, userId: string): Promise<Response> {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (req.method !== "POST") return new Response("Tule's MCP server (Streamable HTTP): POST JSON-RPC here.\n", { status: 405, headers: { Allow: "POST, OPTIONS", ...CORS } });
  let msg: Rpc;
  try {
    msg = await req.json();
  } catch {
    return rpcError(null, -32700, "Parse error", 400);
  }
  if (!msg || typeof msg !== "object" || Array.isArray(msg)) return rpcError(null, -32600, "Expected one JSON-RPC message", 400);
  if (typeof msg.method !== "string" || msg.id === undefined) return new Response(null, { status: 202, headers: CORS });
  const id = msg.id;
  const params = (msg.params && typeof msg.params === "object" ? msg.params : {}) as Record<string, unknown>;
  const meta = (params._meta ?? {}) as Record<string, unknown>;
  const modern = typeof meta[META_VERSION] === "string";
  const ok = (result: Record<string, unknown>) => json({ jsonrpc: "2.0", id, result: { resultType: "complete", ...result } });
  // the pages stop showing "Connect your agent": on the first call any client makes (older
  // clients start with initialize, newer ones with server/discover; tools/list either way)
  if (["initialize", "server/discover", "tools/list"].includes(msg.method)) await accounts(env).agentConnected(userId);
  switch (msg.method) {
    case "initialize": {
      const asked = params.protocolVersion;
      const version = typeof asked === "string" && LEGACY.includes(asked) ? asked : LEGACY[0];
      return ok({ protocolVersion: version, capabilities: { tools: { listChanged: false } }, serverInfo: SERVER_INFO, instructions: INSTRUCTIONS });
    }
    case "server/discover":
      return ok({ supportedVersions: [MODERN], capabilities: { tools: {} }, instructions: INSTRUCTIONS, ttlMs: 3_600_000, _meta: { "io.modelcontextprotocol/serverInfo": SERVER_INFO } });
    case "ping":
      return ok({});
    case "tools/list":
      return ok({ tools: TOOLS, ttlMs: 3_600_000 });
    case "tools/call": {
      const name = params.name;
      const args = (params.arguments ?? {}) as Record<string, unknown>;
      if (typeof name !== "string" || !NAMES.has(name)) return rpcError(id, -32602, `Unknown tool: ${String(name)}`);
      const user = await accounts(env).user(userId);
      if (!user) return ok({ content: [{ type: "text", text: "This connection's account no longer exists; reconnect the Tule connector." }], isError: true });
      const origin = env.PUBLIC_URL;
      const t0 = Date.now();
      let out: ops.Result | string;
      try {
        out = await call(env, user, name, args, origin);
      } catch (e) {
        out = { error: `Tule failed: ${e instanceof Error ? e.message : String(e)}` };
      }
      const isError = typeof out !== "string" && !!out.error;
      console.log(JSON.stringify({ event: "tools/call", tool: name, ms: Date.now() - t0, isError }));
      return ok({ content: [{ type: "text", text: typeof out === "string" ? out : JSON.stringify(out, null, 2) }], ...(isError ? { isError: true } : {}) });
    }
    default:
      return rpcError(id, -32601, `Method not found: ${msg.method}`, modern ? 404 : 200);
  }
}
