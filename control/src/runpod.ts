// RunPod, with a user's own API key (stored encrypted by Accounts), used only
// by the control plane: checking the key and balance, the GPU catalog, and
// the GPU backend (create, check and terminate pods). REST v2
// (https://api.runpod.io/v2, openapi.json there); the balance exists only in
// the GraphQL API (deprecated, retiring in 2027), so it is one function here.

const V2 = "https://api.runpod.io/v2";

/** Short machine names (gpu-<name>) for RunPod GPU type ids. */
export const GPUS: Record<string, string> = {
  rtx4090: "NVIDIA GeForce RTX 4090",
  rtx5090: "NVIDIA GeForce RTX 5090",
  rtx3090: "NVIDIA GeForce RTX 3090",
  l4: "NVIDIA L4",
  l40s: "NVIDIA L40S",
  a40: "NVIDIA A40",
  "rtx-a6000": "NVIDIA RTX A6000",
  a100: "NVIDIA A100 80GB PCIe",
  "a100-sxm": "NVIDIA A100-SXM4-80GB",
  h100: "NVIDIA H100 80GB HBM3",
};

type Reply = { status: number; body: Record<string, unknown> };

async function call(key: string, method: string, path: string, body?: unknown): Promise<Reply> {
  for (let attempt = 0; ; attempt++) {
    const r = await fetch(V2 + path, {
      method,
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (r.status === 429 && attempt < 2) {
      await new Promise((res) => setTimeout(res, 1000 * Math.min(10, Number(r.headers.get("Retry-After") ?? 2))));
      continue;
    }
    const text = await r.text();
    let parsed: Record<string, unknown> = {};
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch {
      parsed = { detail: text.slice(0, 200) };
    }
    return { status: r.status, body: parsed };
  }
}

const detail = (r: Reply) => String(r.body.detail ?? r.body.title ?? r.body.error ?? `HTTP ${r.status}`);

/** The account's balance, or why the key does not work. */
export async function runpodBalance(key: string): Promise<{ balance?: number; error?: string }> {
  if (!/^[A-Za-z0-9_-]{20,200}$/.test(key)) return { error: "that does not look like a RunPod API key" };
  try {
    const v2 = await call(key, "GET", "/pods?limit=1");
    if (v2.status === 401) return { error: "the key is invalid or expired" };
    const r = await fetch("https://api.runpod.io/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify({ query: "query { myself { id clientBalance } }" }),
    });
    const j = (await r.json().catch(() => ({}))) as { data?: { myself?: { clientBalance?: number } | null }; errors?: { message: string }[] };
    if (!r.ok || j.errors?.length || !j.data?.myself) return { error: j.errors?.[0]?.message ?? `the key is not accepted (HTTP ${r.status})` };
    return { balance: Number(j.data.myself.clientBalance ?? 0) };
  } catch (e) {
    return { error: `cannot reach RunPod: ${e instanceof Error ? e.message : String(e)}` };
  }
}

export type Pod = { id: string; status: string; cost: number; gpu: string; cloud: string };

/** Creates a pod running `image` with `env`; tries Secure Cloud, then Community Cloud. */
export async function createPod(key: string, opts: { name: string; image: string; gpu: string; env: Record<string, string>; disk?: number }): Promise<Pod | { error: string; hint?: string }> {
  let last: Reply | null = null;
  for (const cloud of ["SECURE", "COMMUNITY"]) {
    const r = await call(key, "POST", "/pods", {
      name: opts.name,
      image: opts.image,
      gpu: { id: opts.gpu, count: 1 },
      cloud,
      disk: opts.disk ?? 40,
      env: opts.env,
    });
    if (r.status === 201 || r.status === 200) {
      const b = r.body as { id: string; status: string; cost?: number };
      return { id: b.id, status: b.status, cost: Number(b.cost ?? 0), gpu: opts.gpu, cloud };
    }
    last = r;
    if (r.status !== 400 && r.status !== 403) break; // only "no capacity here" is worth another cloud
  }
  const r = last!;
  if (r.status === 400) return { error: `no ${opts.gpu} is available on RunPod right now (${detail(r)})`, hint: "try another GPU (gpus_list shows availability) or later" };
  if (r.status === 402) return { error: "your RunPod balance is too low for this GPU (RunPod needs at least an hour of credit)", hint: "add credit at runpod.io" };
  if (r.status === 401) return { error: "RunPod refused the stored key", hint: "replace it in Tule's Settings" };
  if (r.status === 403) return { error: `RunPod refused: ${detail(r)}`, hint: "the key needs read/write access (Settings → RunPod)" };
  return { error: `RunPod could not create the pod: ${detail(r)}` };
}

export async function getPod(key: string, id: string): Promise<Pod | null> {
  const r = await call(key, "GET", `/pods/${encodeURIComponent(id)}`);
  if (r.status === 404) return null;
  if (r.status !== 200) throw new Error(`RunPod: ${detail(r)}`);
  const b = r.body as { id: string; status: string; cost?: number; gpu?: { id?: string }; cloud?: string };
  return { id: b.id, status: b.status, cost: Number(b.cost ?? 0), gpu: b.gpu?.id ?? "", cloud: b.cloud ?? "" };
}

/** Terminates (deletes) a pod: this is what ends its billing. */
export async function terminatePod(key: string, id: string): Promise<void> {
  const r = await call(key, "DELETE", `/pods/${encodeURIComponent(id)}`);
  if (r.status !== 204 && r.status !== 200 && r.status !== 404) throw new Error(`RunPod could not terminate pod ${id}: ${detail(r)}`);
}

/** The GPUs Tule knows, with RunPod's price and availability for one GPU. */
export async function gpuCatalog(key: string): Promise<Record<string, unknown>[]> {
  const r = await call(key, "GET", "/catalog/gpus?include=AVAILABILITY&product=POD&cloud=SECURE&count=1");
  if (r.status !== 200) throw new Error(`RunPod: ${detail(r)}`);
  const gpus = (r.body.gpus ?? []) as { id: string; memory?: number; price?: { secure?: number; community?: number }; availability?: string }[];
  return Object.entries(GPUS).map(([name, id]) => {
    const g = gpus.find((x) => x.id === id);
    return {
      machine: `gpu-${name}`,
      gpu: id,
      vramGb: g?.memory ?? null,
      pricePerHour: g?.price?.secure ?? g?.price?.community ?? null,
      availability: g?.availability ?? "unknown",
    };
  });
}
