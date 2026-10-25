// Builds dist/viewer.js and .css (the result viewer of every run) and dist/app.js and .css
// (the web app of `findaline.py web`); dist is committed: users need no Node.
// `node build.mjs --out <dir>` builds elsewhere (tests compare it with dist/).
import * as esbuild from "esbuild";

const at = process.argv.indexOf("--out");
const outdir = at > 0 ? process.argv[at + 1] : "dist";

await esbuild.build({
  entryPoints: { viewer: "src/main.tsx", app: "src/app/app.tsx" },
  bundle: true,
  minify: true,
  legalComments: "none",
  format: "iife",
  target: "es2020",
  outdir,
  loader: { ".png": "dataurl" },
  logLevel: "warning",
});
