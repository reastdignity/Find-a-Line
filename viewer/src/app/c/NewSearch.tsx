// C's focused new search (c2): the form (SearchPanel, variant "c") in a column, the map on the right
// and the live estimate with "Start search" in a bar at the bottom. A new search starts with the
// "Typical highline" preset; "Edit & run again" (#new&edit=<job>) starts with the job's request. On
// phones the map is on top, the form scrolls under it and the bar stays at the bottom.

import { useEffect, useRef, useState } from "preact/hooks";
import { Button } from "../../ui/components";
import { EstimateBar, SearchPanel, useSearch, type UsageState } from "../../search";
import { api, type Schema, type SearchRequest } from "../api";
import { PlaceMap } from "../PlaceMap";

/** The form and the bar, once the map, the schema and the request to edit are there. */
function Form({place, schema, initial, usage, wide, onStarted}:
              {place: PlaceMap; schema: Schema; initial: SearchRequest | null; usage: UsageState; wide: boolean;
               onStarted(id: string): void}) {
  const search = useSearch({place, schema, onStarted, initial, preset: initial ? undefined : "typical"});
  (window as any).FINDALINE_SEARCH = search;  // for tests and the browser console
  return <>
    <aside class="c-form" aria-label="New search">
      <SearchPanel place={place} schema={schema} onStarted={onStarted} search={search} variant="c" estimate={false}
                   usage={usage.usage} phone={!wide} />
    </aside>
    <footer class="c-bar"><EstimateBar search={search} usage={usage.usage} layout="bar" /></footer>
  </>;
}

export function NewSearch({edit, schema, usage, wide, onStarted}:
                          {edit: string | null; schema: Schema | null; usage: UsageState; wide: boolean;
                           onStarted(id: string): void}) {
  const mapRef = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<PlaceMap | null>(null);
  const [initial, setInitial] = useState<SearchRequest | null | undefined>(edit ? undefined : null);
  const [error, setError] = useState("");

  useEffect(() => {
    const placeMap = new PlaceMap(mapRef.current!, {controls: "bottomright"});
    (window as any).FINDALINE_PLACE = placeMap;  // for tests and the browser console
    setPlace(placeMap);
    const observer = new ResizeObserver(() => placeMap.map.invalidateSize());
    observer.observe(mapRef.current!);
    return () => { observer.disconnect(); placeMap.map.remove(); };
  }, []);
  useEffect(() => {
    if (edit) api.job(edit).then((job) => setInitial(job.request), (e) => setError(e.message));
  }, [edit]);

  return <div class="c-newsearch">
    <div class="c-subtop">
      <Button kind="ghost" icon="back" href="#home">Home</Button>
      <h1 class="c-subtitle">{edit ? "Edit & run again" : "New search"}</h1>
    </div>
    <div class="c-newmap" ref={mapRef} />
    {error && <p class="c-error c-form">{error}</p>}
    {!error && place && schema && initial !== undefined &&
      <Form key={edit || "new"} place={place} schema={schema} initial={initial} usage={usage} wide={wide}
            onStarted={onStarted} />}
  </div>;
}
