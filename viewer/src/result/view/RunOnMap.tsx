// A run's result on a full-screen map of its own: the Leaflet map (its base layers with the run's
// orthophoto once the run's data is there, zoom, scale) and the workspace over it. D's result and a
// run's map.html. Styles in workspace.css (the class names are D's: d-*).

import L from "leaflet";
import { useEffect, useRef, useState } from "preact/hooks";
import { baseLayers } from "../../layers";
import type { Ortho, RunData } from "../../types";
import { ResultWorkspace, type ResultWorkspaceProps } from "./ResultWorkspace";

export type RunOnMapProps = Omit<ResultWorkspaceProps, "map"> & {
  /** The run's orthophoto for this page (its cache folder is relative to the run's map.html; default as it is). */
  ortho?(data: RunData): Ortho | null;
};

export function RunOnMap({ortho, onData, ...props}: RunOnMapProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<L.Map | null>(null);
  useEffect(() => {
    const leaflet = L.map(ref.current!, {zoomControl: false, center: [50, 19.75], zoom: 5});
    L.control.zoom({position: "topright"}).addTo(leaflet);
    (window as any).FINDALINE_MAP = leaflet;  // for tests and the browser console
    setMap(leaflet);
    const observer = new ResizeObserver(() => leaflet.invalidateSize());
    observer.observe(ref.current!);
    return () => { observer.disconnect(); leaflet.remove(); };
  }, []);
  const loaded = (data: RunData) => {
    if (!map) return;
    map.setMaxZoom(data.max_zoom);
    const layers = baseLayers(ortho ? ortho(data) : data.ortho, data.max_zoom);
    Object.values(layers)[0].addTo(map);
    L.control.layers(layers, undefined, {position: "topright"}).addTo(map);
    L.control.scale({position: "bottomleft"}).addTo(map);
    onData?.(data);
  };

  return <div class={`d-result ${props.wide ? "d-wide" : "d-phone"}`}>
    <div class="d-map" ref={ref} />
    {map && <div class="d-layer"><ResultWorkspace map={map} onData={loaded} {...props} /></div>}
  </div>;
}
