// Small helpers shared by the Worker and the Durable Objects.

/** The SHA-256 of a string (as UTF-8) or of bytes, as lowercase hex. */
export async function sha256hex(data: string | ArrayBuffer): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", typeof data === "string" ? new TextEncoder().encode(data) : data);
  return [...new Uint8Array(d)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

/** The token from "Authorization: Bearer <token>", or "". */
export function bearer(req: Request): string {
  const h = req.headers.get("Authorization") ?? "";
  return h.startsWith("Bearer ") ? h.slice(7).trim() : "";
}
