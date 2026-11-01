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

export const INSTRUCTIONS = `Tule gives the user's engineering projects a machine each: Linux with OpenCascade, the \`tule\` Python modeling API (immutable shapes, tags that survive operations), DFM checks and topology optimization. These tools manage the control plane only: projects, machines (cpu-1 = 1 vCPU/6 GiB, cpu-2, cpu-4; GPUs on the user's RunPod account), snapshots of /workspace, SSH keys, API keys and usage. The work happens on the machine over SSH with the tule CLI (connect_info says how): write Python in /workspace, write results to /workspace/outputs/<name>/ (STEP/STL; the user watches them live in the viewer), check them with \`tule screenshot\`. /workspace is saved continuously; nothing else on the machine persists. Read guide("index") first, then the topic you need.`;

type Tool = { name: string; description: string; inputSchema: Record<string, unknown>; annotations?: Record<string, unknown> };
const str = (description: string) => ({ type: "string", description });
const project = { project: str("project name, e.g. camera-bracket") };
const obj = (properties: Record<string, unknown>, required: string[] = []) => ({ type: "object", properties, required, additionalProperties: false });
const RO = { readOnlyHint: true };

export const TOOLS: Tool[] = [
  { name: "guide", description: "Tule's instructions for agents by topic. Start with 'index'.", inputSchema: obj({ topic: str(`one of: ${Object.keys(SKILLS).join(", ") || "index"}`) }), annotations: RO },
  { name: "projects_list", description: "The user's projects with their machine state and viewer links.", inputSchema: obj({}), annotations: RO },
  { name: "projects_create", description: "Create a project (a new /workspace from the template: AGENTS.md, tule.toml, main.py, git).", inputSchema: obj({ name: str("lowercase letters, digits and '-', e.g. camera-bracket") }, ["name"]) },
  { name: "projects_rename", description: "Rename a project.", inputSchema: obj({ ...project, to: str("the new name") }, ["project", "to"]) },
  { name: "projects_delete", description: "Delete a project: stops its machine and deletes /workspace and every snapshot. Cannot be undone; confirm with the user first.", inputSchema: obj({ ...project, confirm: str("the project name again, to confirm") }, ["project", "confirm"]), annotations: { destructiveHint: true } },
  { name: "machine_start", description: "Start (or resize) the project's machine and wait until /workspace is restored. cpu-1 (default, 1 vCPU / 6 GiB), cpu-2 (2 / 8), cpu-4 (4 / 12), or a GPU on the user's RunPod account: gpu-rtx4090, gpu-l40s, gpu-a100, gpu-h100, ... (gpus_list has prices; GPU time costs the user money, so ask first). Resizing moves /workspace to the new machine.", inputSchema: obj({ ...project, machine: str("cpu-1, cpu-2, cpu-4 or gpu-<type>") }, ["project"]) },
  { name: "gpus_list", description: "GPU machines on the user's RunPod account: price per hour and current availability.", inputSchema: obj({}), annotations: RO },
  { name: "machine_stop", description: "Save /workspace and stop the project's machine.", inputSchema: obj(project, ["project"]) },
  { name: "machine_status", description: "The project's machine: state, size, timings, SSH sessions, latest snapshot.", inputSchema: obj(project, ["project"]), annotations: RO },
  { name: "connect_info", description: "How to work on the project's machine: install the tule CLI, sign it in, SSH in; the viewer link.", inputSchema: obj(project, ["project"]), annotations: RO },
  { name: "join_token", description: "A join token for a machine started elsewhere (e.g. a pod made with the user's own RunPod connector): run the Tule image with these environment variables and it joins the project.", inputSchema: obj(project, ["project"]) },
  { name: "outputs_list", description: "The project's outputs (folders in /workspace/outputs) with viewer links.", inputSchema: obj(project, ["project"]), annotations: RO },
  { name: "snapshots_list", description: "Snapshots of /workspace, newest first.", inputSchema: obj({ ...project, limit: { type: "number" } }, ["project"]), annotations: RO },
  { name: "snapshot_now", description: "Save /workspace now (the machine must be running).", inputSchema: obj(project, ["project"]) },
  { name: "snapshot_restore", description: "Make /workspace match a snapshot (the current state is saved first, so this can be undone).", inputSchema: obj({ ...project, id: str("snapshot id from snapshots_list") }, ["project", "id"]) },
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
      for (let i = 0; i < (String(a.machine ?? "").startsWith("gpu-") ? 150 : 100); i++) {
        const s = await ops.status(env, user, a.project, origin);
        if (s.state === "running" || s.error) return s;
        await new Promise((res) => setTimeout(res, 600));
      }
      return { ...(await ops.status(env, user, a.project, origin)), note: "still starting; check machine_status again shortly" };
    }
    case "gpus_list": {
      const key = await acc.runpodKey(user.id);
      if (!key) return { error: "RunPod is not connected", hint: "the user pastes a RunPod API key in Tule's Settings" };
      return { gpus: await gpuCatalog(key) };
    }
    case "machine_stop": return ops.down(env, user, a.project);
    case "machine_status": return ops.status(env, user, a.project, origin);
    case "connect_info": {
      const p = await ops.resolve(env, user, a.project);
      return ops.isRef(p) ? ops.connectInfo(origin, p.name) : p;
    }
    case "join_token": return ops.join(env, user, a.project, origin);
    case "outputs_list": return ops.outputs(env, user, a.project, origin);
    case "snapshots_list": return ops.snapshots(env, user, a.project, Number(a.limit ?? 20));
    case "snapshot_now": return ops.command(env, user, a.project, { t: "snapshot", reason: "agent" });
    case "snapshot_restore": return ops.command(env, user, a.project, { t: "restore", id: String(a.id ?? "") });
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
      const origin = env.PUBLIC_URL || new URL(req.url).origin;
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
