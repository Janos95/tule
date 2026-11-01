// web/dist: index.html and favicon.svg, everything else in assets-<content
// hash>/ so a new deploy never pairs a new page with an old app.js.
//
//   node build.mjs           the deployable build
//   node build.mjs --dev     fixed names (assets-dev/), not minified, plus a
//                            version file the dev page polls to reload itself
//   node build.mjs --watch   --dev, rebuilt on every change in src/
import { context } from "esbuild";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import { watch as watchDir } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(web, "dist");
const watching = process.argv.includes("--watch");
const dev = watching || process.argv.includes("--dev");
const out = path.join(dist, dev ? "assets-dev" : ".next");

// the dev page reloads when /__dev/version changes (control/src/dev.ts)
const RELOAD = `<script>(()=>{let v;setInterval(async()=>{try{const r=await fetch("/__dev/version",{cache:"no-store"});const t=await r.text();if(v&&t!==v)location.reload();v=t}catch{}},700)})()</script>`;

async function finish() {
  await fs.copyFile(path.join(web, "src/style.css"), path.join(out, "style.css"));
  let v = "assets-dev";
  if (!dev) {
    const h = crypto.createHash("sha256");
    for (const f of ["app.js", "style.css"]) h.update(await fs.readFile(path.join(out, f)));
    v = `assets-${h.digest("hex").slice(0, 10)}`;
    await fs.rename(out, path.join(dist, v));
  }
  let html = (await fs.readFile(path.join(web, "src/index.html"), "utf8"))
    .replace('href="style.css"', `href="/${v}/style.css"`)
    .replace('src="app.js"', `src="/${v}/app.js"`);
  if (dev) html = html.replace("</body>", `${RELOAD}</body>`);
  await fs.writeFile(path.join(dist, "index.html"), html);
  for (const f of ["favicon.svg", "logo-light.svg"]) await fs.copyFile(path.join(web, "src", f), path.join(dist, f));
  if (dev) await fs.writeFile(path.join(dist, "assets-dev", "version.txt"), String(Date.now()));
  console.log(`web/dist/${v}`);
}

// empty dist but keep the folder, so a running `wrangler dev` sees the new files
await fs.mkdir(dist, { recursive: true });
for (const f of await fs.readdir(dist)) await fs.rm(path.join(dist, f), { recursive: true, force: true });
const ctx = await context({
  absWorkingDir: web,
  entryPoints: { app: "src/main.js" },
  outdir: out,
  bundle: true,
  format: "esm",
  minify: !dev,
  sourcemap: true,
  target: "es2022",
  logLevel: "warning",
});
await ctx.rebuild();
await finish();
if (watching) {
  let timer;
  watchDir(path.join(web, "src"), { recursive: true }, () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        await ctx.rebuild();
        await finish();
      } catch (e) {
        console.error(e.message);
      }
    }, 80);
  });
  console.log("watching web/src");
} else {
  await ctx.dispose();
}
