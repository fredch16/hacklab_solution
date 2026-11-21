// Build laptop assets. The legacy phone-hosted UI is an explicit compatibility build.
import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { join } from "node:path";

const dist = "dist";
rmSync(dist, { recursive: true, force: true });
execFileSync("npx", ["tsc"], { stdio: "inherit" });
cpSync("vendor/qrcode.js", join(dist, "qrcode.js"));
cpSync("index.html", join(dist, "index.html"));
cpSync("app.css", join(dist, "app.css"));
if (process.argv.includes("--phone")) {
  const out = new URL("../mobile/assets/dashboard/", import.meta.url).pathname;
  mkdirSync(out, { recursive: true });
  for (const f of readdirSync(out)) if (/\.(js|html|css)$/.test(f)) rmSync(join(out, f));
  for (const f of readdirSync(dist)) cpSync(join(dist, f), join(out, f));
}
