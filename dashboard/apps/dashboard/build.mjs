// tsc -> dist/, then copy flat files into the Flutter asset dir (not recursive there).
import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, readdirSync, rmSync } from "node:fs";
const out = new URL("../mobile/assets/dashboard/", import.meta.url).pathname;
rmSync("dist", { recursive: true, force: true });
execFileSync("npx", ["tsc"], { stdio: "inherit" });
mkdirSync(out, { recursive: true });
for (const f of readdirSync(out)) if (/\.(js|html|css)$/.test(f)) rmSync(out + f);
for (const f of readdirSync("dist")) if (f.endsWith(".js")) cpSync("dist/" + f, out + f);
cpSync("vendor/qrcode.js", out + "qrcode.js");
cpSync("index.html", out + "index.html");
cpSync("app.css", out + "app.css");
console.log("copied to", out);
