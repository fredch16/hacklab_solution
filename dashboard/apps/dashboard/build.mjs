// Build laptop assets. The legacy phone-hosted UI is an explicit compatibility build.
import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const dist = "dist";
rmSync(dist, { recursive: true, force: true });
execFileSync("npx", ["tsc"], { stdio: "inherit" });
cpSync("vendor/qrcode.js", join(dist, "qrcode.js"));
cpSync("index.html", join(dist, "index.html"));
cpSync("app.css", join(dist, "app.css"));
for (const f of ["peter_icon.png", "favicon.png"]) cpSync(f, join(dist, f));   // the Peter app icon
// 3-D twin (offline). The add-ons import "three" by name; the dashboard's CSP forbids an inline import map,
// so they get the bundled file's path instead.
cpSync("vendor/three.module.min.js", join(dist, "three.module.min.js"));
for (const f of ["STLLoader.js", "OrbitControls.js"])
  writeFileSync(join(dist, f), readFileSync(join("vendor", f), "utf8").replace(/from\s+['"]three['"]/g, "from './three.module.min.js'"));
if (process.argv.includes("--phone")) {
  const out = new URL("../mobile/assets/dashboard/", import.meta.url).pathname;
  mkdirSync(out, { recursive: true });
  for (const f of readdirSync(out)) if (/\.(js|html|css)$/.test(f)) rmSync(join(out, f));
  for (const f of readdirSync(dist)) cpSync(join(dist, f), join(out, f));
}
