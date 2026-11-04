// web/dist: index.html and favicon.svg, everything else in assets-<content
// hash>/ so a new deploy never pairs a new page with an old app.js.
//
//   node build.mjs           the deployable build
//   node build.mjs --dev     fixed names (assets-dev/), not minified, plus a
//                            version file the dev page polls to reload itself
//   node build.mjs --watch   --dev, rebuilt on every change in src/
import { context } from "esbuild";
import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import { watch as watchDir } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const web = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(web, "dist");
const watching = process.argv.includes("--watch");
const dev = watching || process.argv.includes("--dev");
const out = path.join(dist, dev ? "assets-dev" : ".next");
const OCCT = `occt-${JSON.parse(await fs.readFile(path.join(web, "node_modules", "occt-import-js", "package.json"), "utf8")).version}`;

// the dev page reloads when /__dev/version changes (control/src/dev.ts)
const RELOAD = `<script>(()=>{let v;setInterval(async()=>{try{const r=await fetch("/__dev/version",{cache:"no-store"});const t=await r.text();if(v&&t!==v)location.reload();v=t}catch{}},700)})()</script>`;

async function finish() {
  // the palette's variables first, then the viewer's rules that use them
  const css = await Promise.all(["palette.css", "style.css"].map((f) => fs.readFile(path.join(web, "src", f), "utf8")));
  await fs.writeFile(path.join(out, "style.css"), css.join("\n"));
  // a view-only link meshes models in the visitor's browser: a worker and OpenCascade as WebAssembly (loaded only there)
  const worker = await fs.readFile(path.join(web, "src", "stepmesh.js"), "utf8");
  await fs.writeFile(path.join(out, "stepmesh.js"), worker.replaceAll("__OCCT__", `/${OCCT}/`));
  const occt = path.join(dist, OCCT);
  await fs.mkdir(occt, { recursive: true });
  for (const f of ["occt-import-js.js", "occt-import-js.wasm", "license.occt.txt", "license.occt-import-js.txt"])
    await fs.copyFile(path.join(web, "node_modules", "occt-import-js", "dist", f), path.join(occt, f));
  let v = "assets-dev";
  if (!dev) {
    const h = crypto.createHash("sha256");
    for (const f of ["app.js", "style.css"]) h.update(await fs.readFile(path.join(out, f)));
    v = `assets-${h.digest("hex").slice(0, 10)}`;
    await fs.rename(out, path.join(dist, v));
  }
  const bust = dev ? `?v=${Date.now()}` : ""; // dev file names never change: make the browser fetch them again
  let html = (await fs.readFile(path.join(web, "src/index.html"), "utf8"))
    .replace('href="style.css"', `href="/${v}/style.css${bust}"`)
    .replace('src="app.js"', `src="/${v}/app.js${bust}"`);
  if (dev) html = html.replace("</body>", `${RELOAD}</body>`);
  await fs.writeFile(path.join(dist, "index.html"), html);
  for (const f of ["favicon.svg", "logo-light.svg"]) await fs.copyFile(path.join(web, "src", f), path.join(dist, f));
  if (dev) await fs.writeFile(path.join(dist, "assets-dev", "version.txt"), String(Date.now()));
  await installer();
  console.log(`web/dist/${v}`);
}

// dist/install: the tule CLI as a wheel, and install.sh and cli.sh naming it (served as /install.sh and /cli.sh, see
// control/src/index.ts). Built once per run; the CLI's sources are outside web/src.
let wheel;
async function installer() {
  const dir = path.join(dist, "install");
  await fs.mkdir(dir, { recursive: true });
  if (!wheel) {
    const tmp = await fs.mkdtemp(path.join(os.tmpdir(), "tule-wheel-"));
    // from a copy with the guides inside, so tule docs works on the agent's own computer too
    const src = path.join(tmp, "cli");
    await fs.cp(path.join(web, "../cli"), src, { recursive: true, filter: (f) => !/(__pycache__|\.egg-info|[/\\]build$)/.test(f) });
    await fs.cp(path.join(web, "../skills"), path.join(src, "tule_cli", "skills"), { recursive: true });
    execFileSync("uv", ["build", "--wheel", "--quiet", "--out-dir", tmp, src], { stdio: "inherit" });
    wheel = path.join(tmp, (await fs.readdir(tmp)).find((f) => f.endsWith(".whl")));
  }
  // under its content's hash: the version stays 0.1.0, and uv reuses what it fetched from a URL before
  const hash = crypto.createHash("sha256").update(await fs.readFile(wheel)).digest("hex").slice(0, 12);
  const served = `${hash}/${path.basename(wheel)}`;
  await fs.mkdir(path.join(dir, hash), { recursive: true });
  await fs.copyFile(wheel, path.join(dir, served));
  for (const name of ["install.sh", "cli.sh"]) {
    const sh = await fs.readFile(path.join(web, "src", name), "utf8");
    await fs.writeFile(path.join(dir, name), sh.replaceAll("__WHEEL__", served));
  }
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
  splitting: true, // charts and documents load their libraries only when shown
  chunkNames: "chunk-[hash]",
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
