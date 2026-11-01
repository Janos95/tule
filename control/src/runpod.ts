// RunPod, with a user's own API key (stored encrypted by Accounts). Used only
// by the control plane: checking the key and balance, and (milestone 7) the
// GPU backend that creates, checks and stops pods.

/** The account's balance, or why the key does not work. */
export async function runpodBalance(key: string): Promise<{ balance?: number; error?: string }> {
  if (!/^[A-Za-z0-9_-]{20,200}$/.test(key)) return { error: "that does not look like a RunPod API key" };
  try {
    const r = await fetch("https://api.runpod.io/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify({ query: "query { myself { id clientBalance } }" }),
    });
    const j = (await r.json().catch(() => ({}))) as { data?: { myself?: { clientBalance?: number } }; errors?: { message: string }[] };
    if (!r.ok || j.errors?.length || !j.data?.myself) return { error: j.errors?.[0]?.message ?? `HTTP ${r.status}` };
    return { balance: Number(j.data.myself.clientBalance ?? 0) };
  } catch (e) {
    return { error: `cannot reach RunPod: ${e instanceof Error ? e.message : String(e)}` };
  }
}
