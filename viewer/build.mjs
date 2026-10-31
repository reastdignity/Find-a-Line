// Builds dist/viewer.js and .css (the map of every run, map.html) and dist/app.js and .css
// (the web app of `findaline.py web`) and dist/index.css (runs/index.html); dist is committed:
// users need no Node.
// The logo's files go to dist/ as they are: logo.svg for web.py's server-rendered pages (the app bundles
// the same file as text), favicon.svg for every page of the app.
// `node build.mjs --out <dir>` builds elsewhere (tests compare it with dist/).
import { copyFileSync, mkdirSync } from "node:fs";
import * as esbuild from "esbuild";

const at = process.argv.indexOf("--out");
const outdir = at > 0 ? process.argv[at + 1] : "dist";

await esbuild.build({
  entryPoints: { viewer: "src/map/main.tsx", app: "src/app/app.tsx", index: "src/index.css" },
  bundle: true,
  minify: true,
  legalComments: "none",
  format: "iife",
  target: "es2020",
  outdir,
  loader: { ".png": "dataurl", ".svg": "text" },
  logLevel: "warning",
});
mkdirSync(outdir, { recursive: true });
for (const name of ["logo.svg", "favicon.svg"]) copyFileSync(`src/ui/logo/${name}`, `${outdir}/${name}`);
