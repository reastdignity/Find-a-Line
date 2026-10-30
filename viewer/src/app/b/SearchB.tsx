// The search of prototype B (B1, B4): the search panel (variant "b") floating on the map, a sheet on
// phones. New searches start with the "Typical highline" preset (the user's decision); an edited one
// keeps its values. After Start the place stays on the map and the panel follows the search (its steps
// and bar) instead of the form, so it cannot be started twice by accident; "New search" clears the place,
// "Back to the form" keeps it (to change and start again). A search the list has not shown after a few
// seconds is asked for again, then said to be missing rather than "Queued…" for ever.

import { api, type Schema, type SearchRequest } from "../api";
import type { PlaceMap } from "../PlaceMap";
import type { JobsState } from "../useJobs";
import { Button, Sheet } from "../../ui/components";
import { SearchPanel } from "../../search/SearchPanel";
import { useSearch, type UsageState } from "../../search/useSearch";
import type { RunListed } from "../api";
import { JobCard } from "./Searches";
import { useEffect, useState } from "preact/hooks";

const MISSING_MS = 5000;  // a started search not in the list yet: asked for again, then said to be missing

export interface SearchBProps {
  place: PlaceMap;
  schema: Schema;
  /** A search to edit and run again (null: a new one). */
  initial: SearchRequest | null;
  jobs: JobsState;
  usage: UsageState;
  runs: RunListed[] | null;
  wide: boolean;
  onShow(runId: string): void;
  onEdit(jobId: string): void;
  /** The job the panel follows after Start ("" again for a new search). */
  onFollow?(jobId: string): void;
}

export function SearchB({place, schema, initial, jobs, usage, runs, wide, onShow, onEdit, onFollow}: SearchBProps) {
  const [started, setStarted] = useState("");
  useEffect(() => onFollow?.(started), [started]);
  const search = useSearch({place, schema, initial, preset: "typical", onStarted: (id) => {
    setStarted(id);
    jobs.refresh();
    usage.refresh();
  }});
  (window as any).FINDALINE_SEARCH = search;  // for tests and the browser console
  const job = started ? jobs.jobs.find((j) => j.id === started) : undefined;
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    setMissing(false);
    if (!started || job) return;
    const ask = setTimeout(() => jobs.refresh(), MISSING_MS);
    const give = setTimeout(() => setMissing(true), 2 * MISSING_MS);
    return () => { clearTimeout(ask); clearTimeout(give); };
  }, [started, !!job]);
  const again = () => { search.clear(); setStarted(""); };
  const run = job?.run_id ? (runs || []).find((r) => r.id === job.run_id) : null;

  const following = started && <div class="b-following" data-started={started}>
    <h2 class="u-h">Your search</h2>
    {job ? <JobCard job={job} run={run} onShow={onShow} onEdit={onEdit}
                    onCancel={(id) => api.cancel(id).catch(() => {})} />
      : missing ? <p class="u-small">This search is not in the list of searches yet: look in My searches, or
          reload the page.</p>
      : <p class="u-small">Queued…</p>}
    <p class="u-small">The place stays on the map. You can follow this search in My searches too.</p>
    <Button kind="primary" large full icon="plus" class="b-new" onClick={again}>New search</Button>
    <Button kind="ghost" full icon="edit" class="b-back-form" onClick={() => setStarted("")}>Back to the form</Button>
  </div>;
  return <Sheet label={started ? "Your search" : "New search"} desktop="float" initial="half" peek={150} top={64}>
    {following || <SearchPanel place={place} schema={schema} onStarted={() => {}} search={search} variant="b"
                               usage={usage.usage} phone={!wide} />}
  </Sheet>;
}
