// npm run deploy: the Worker from this checkout, with the machine image CI built.
//
// CI (.github/workflows/runtime.yml) builds runtime/Dockerfile on every push to main that changes what goes
// into the image, tests it, and pushes it to Cloudflare's registry as tule-project-cpu:sha-<commit>. This
// finds the image for the newest such commit, pins its digest in a copy of wrangler.jsonc and deploys with
// that, so nothing is built or uploaded from this computer.
//
//   node deploy.mjs                 the image of the newest image commit on origin/main
//   node deploy.mjs --local-image   build and push runtime/Dockerfile from here instead (wrangler's own way)
import { execFileSync, spawnSync } from "node:child_process";
import { readFileSync, rmSync, writeFileSync } from "node:fs";

const ACCOUNT = "d4ea6567fb9a32c0979ace82a862f4fe";
const REPO = "tule-project-cpu";
// what goes into the image: the same list as runtime.yml's paths, so the commit found here is one CI built
const IMAGE_PATHS = ["runtime", "python", "cli", "skills", "templates", "examples", "tools/build_agents_md.py", ".dockerignore", ".github/workflows/runtime.yml"];

const git = (...a) => execFileSync("git", ["-C", "..", ...a], { encoding: "utf8" }).trim();
const fail = (msg) => {
  console.error(`deploy: ${msg}`);
  process.exit(1);
};
const run = (args) => {
  const r = spawnSync("npx", ["wrangler", ...args], { stdio: "inherit" });
  if (r.status) process.exit(r.status ?? 1);
};

if (process.argv.includes("--local-image")) run(["deploy"]);
else {
  git("fetch", "-q", "origin", "main");
  const commit = git("log", "-1", "--format=%H", "HEAD", "--", ...IMAGE_PATHS);
  const short = commit.slice(0, 7);
  const subject = git("log", "-1", "--format=%s", commit);
  if (spawnSync("git", ["-C", "..", "merge-base", "--is-ancestor", commit, "origin/main"]).status)
    fail(`${short} (${subject}) changes the machine image but is not on origin/main: push it, and CI builds its image`);
  const dirty = git("status", "--porcelain", "--", ...IMAGE_PATHS);
  if (dirty) console.warn(`deploy: uncommitted changes under ${IMAGE_PATHS.slice(0, 3).join(", ")}… are not in the image:\n${dirty}`);

  const digest = await imageDigest(`sha-${short}`);
  if (!digest) fail(`the machine image for ${short} (${subject}) is not in Cloudflare's registry yet. CI builds it after the tests: ${ciStatus(commit)}`);
  console.log(`deploy: machine image of ${short} (${subject}), ${digest.slice(0, 19)}…`);

  // a copy of wrangler.jsonc that names the image instead of its Dockerfile (same folder: its paths stay right)
  const config = readFileSync("wrangler.jsonc", "utf8");
  const built = '"images": { "cpu": { "dockerfile": "../runtime/Dockerfile", "build_context": ".." } }';
  if (!config.includes(built)) fail(`wrangler.jsonc no longer has ${built}: update deploy.mjs`);
  writeFileSync(".wrangler-deploy.jsonc", config.replace(built, `"images": { "cpu": { "image": "registry.cloudflare.com/${ACCOUNT}/${REPO}@${digest}" } }`));
  try {
    run(["deploy", "--config", ".wrangler-deploy.jsonc"]);
  } finally {
    rmSync(".wrangler-deploy.jsonc", { force: true });
  }
}

/** The digest of REPO:tag in Cloudflare's registry, or null when it is not there. */
async function imageDigest(tag) {
  const out = execFileSync("npx", ["wrangler", "containers", "registries", "credentials", "registry.cloudflare.com", "--pull", "--expiration-minutes", "5", "--json"], { encoding: "utf8" });
  const c = JSON.parse(out.slice(out.indexOf("{")));
  const r = await fetch(`https://registry.cloudflare.com/v2/${ACCOUNT}/${REPO}/manifests/${tag}`, {
    method: "HEAD",
    headers: {
      Authorization: `Basic ${Buffer.from(`${c.username}:${c.password}`).toString("base64")}`,
      Accept: "application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json",
    },
  });
  if (r.status === 404) return null;
  if (!r.ok) fail(`Cloudflare's registry answered ${r.status} for ${REPO}:${tag}`);
  return r.headers.get("docker-content-digest");
}

/** Where CI's run for the commit stands, if the gh CLI can tell. */
function ciStatus(commit) {
  const r = spawnSync("gh", ["run", "list", "--workflow", "runtime.yml", "--commit", commit, "--json", "status,conclusion,url", "--limit", "1"], { encoding: "utf8" });
  const [run] = r.status ? [] : JSON.parse(r.stdout || "[]");
  if (!run) return "no runtime workflow run found for it (gh run list --workflow runtime.yml)";
  return run.status === "completed" ? `its run ended: ${run.conclusion} (${run.url})` : `its run is ${run.status} (${run.url}; gh run watch)`;
}
