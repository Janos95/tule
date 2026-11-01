// web/dist: index.html and favicon.svg, everything else in assets-<content
// hash>/ so a new deploy never pairs a new page with an old app.js.
import { build } from "esbuild";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(web, "dist");
const tmp = path.join(dist, ".next");
// empty dist but keep the folder, so a running `wrangler dev` sees the new files
await fs.mkdir(dist, { recursive: true });
for (const f of await fs.readdir(dist)) await fs.rm(path.join(dist, f), { recursive: true, force: true });
await build({
  absWorkingDir: web,
  entryPoints: { app: "src/main.js" },
  outdir: tmp,
  bundle: true,
  format: "esm",
  minify: true,
  sourcemap: true,
  target: "es2022",
  logLevel: "warning",
});
await fs.copyFile(path.join(web, "src/style.css"), path.join(tmp, "style.css"));
const h = crypto.createHash("sha256");
for (const f of ["app.js", "style.css"]) h.update(await fs.readFile(path.join(tmp, f)));
const v = `assets-${h.digest("hex").slice(0, 10)}`;
await fs.rename(tmp, path.join(dist, v));
const html = (await fs.readFile(path.join(web, "src/index.html"), "utf8"))
  .replace('href="style.css"', `href="/${v}/style.css"`)
  .replace('src="app.js"', `src="/${v}/app.js"`);
await fs.writeFile(path.join(dist, "index.html"), html);
await fs.copyFile(path.join(web, "src/favicon.svg"), path.join(dist, "favicon.svg"));
console.log(`web/dist/${v}`);
