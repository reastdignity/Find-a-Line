// The results of prototype B (B3, B5) on the app's one map: the shared ResultWorkspace on PlaceMap's
// Leaflet map, its pill "‹ run name" going back to where the user came from, with a switcher of runs,
// the route's hash (#run=…&line=…) for the selected line, and the run's orthophoto among the base layers.

import type { ComponentChildren } from "preact";
import { useEffect } from "preact/hooks";
import type { RunListed } from "../api";
import type { PlaceMap } from "../PlaceMap";
import { orthoForApp } from "../../result/load";
import { ResultWorkspace } from "../../result/view/ResultWorkspace";
import { routeHash } from "./route";

export interface ResultsProps {
  place: PlaceMap;
  runId: string;
  /** The line of the route (#…&line=), opened in its group; `nav` changes with every navigation. */
  line: string | null;
  nav: number;
  wide: boolean;
  runs: RunListed[] | null;
  onBack(): void;
  onRun(runId: string): void;
  /** The phone's menu button (the top bar is the results' own). */
  menu?: ComponentChildren;
}

const hashOf = (run: string, line: string | null) => routeHash({section: "run", run, line});

export function ResultsB({place, onBack, ...props}: ResultsProps) {
  useEffect(() => () => place.setRunOrtho(null), [place]);  // the run's orthophoto leaves with the result
  return <ResultWorkspace map={place.map} back={{label: "Back", onClick: onBack}} hashOf={hashOf}
                          onData={(d) => place.setRunOrtho(orthoForApp(d.ortho, d.run.id), d.max_zoom)} {...props} />;
}
