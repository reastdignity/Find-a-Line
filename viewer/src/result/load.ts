// A run's data for the app: its data.js (window.FINDALINE_RUN) loaded as a script, which the CSP
// (script-src 'self') allows and web.RunFiles serves only to who may open the run. The lines of
// other runs still come from their lines.js (others.loadLines).

import type { RunData, RunInfo } from "../types";

const loaded = new Map<string, Promise<RunData>>();

/** The data of run `id` (`/runs/<id>/data.js`), loaded once; rejects when it cannot be loaded
 * (a failed load is tried again on the next call). */
export function loadRun(id: string, base = "/runs/"): Promise<RunData> {
  const known = loaded.get(id);
  if (known) return known;
  const promise = new Promise<RunData>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = base + encodeURIComponent(id) + "/data.js";
    // A script's load event comes right after it ran, before any other script: FINDALINE_RUN is its own
    script.onload = () => {
      const data = window.FINDALINE_RUN;
      delete window.FINDALINE_RUN;
      script.remove();
      if (data) resolve(data); else reject(new Error("no run data in " + script.src));
    };
    script.onerror = () => {
      script.remove();
      reject(new Error("could not load the run " + id));
    };
    document.head.appendChild(script);
  });
  loaded.set(id, promise);
  promise.catch(() => { if (loaded.get(id) === promise) loaded.delete(id); });
  return promise;
}

/** Forget a loaded run (e.g. after it was deleted). */
export function forgetRun(id: string) {
  loaded.delete(id);
}

let runList: Promise<RunInfo[]> | null = null;

/** The runs the user may open as the maps list them (`/runs/runs.js`: window.FINDALINE_RUNS, with their
 * boxes for "other runs nearby"), loaded once; empty when it cannot be loaded. Its FINDALINE_APP (the
 * public site's flags) is set as for a map. */
export function loadRunList(src = "/runs/runs.js"): Promise<RunInfo[]> {
  if (runList) return runList;
  runList = new Promise<RunInfo[]>((resolve) => {
    const script = document.createElement("script");
    script.src = src;
    script.onload = () => { script.remove(); resolve(window.FINDALINE_RUNS || []); };
    script.onerror = () => { script.remove(); runList = null; resolve([]); };
    document.head.appendChild(script);
  });
  return runList;
}
