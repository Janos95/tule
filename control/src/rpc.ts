// Durable Object stubs typed by the class's own method signatures. (The
// generated RPC types turn results like Record<string, unknown> into never,
// though they serialize fine.)
export type Rpc<T> = {
  [K in Exclude<keyof T, "fetch">]: T[K] extends (...a: infer A) => infer R ? (...a: A) => Promise<Awaited<R>> : never;
} & { fetch(req: Request): Promise<Response> };

export function stub<T>(ns: unknown, name: string): Rpc<T> {
  return (ns as { getByName(n: string): unknown }).getByName(name) as Rpc<T>;
}
