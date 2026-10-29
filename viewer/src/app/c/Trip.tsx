// C's favourite lines as a trip list (the old app's Favourites.tsx data): every favourite of the runs
// the user may open, sorted by any value, each opens its line in the result page, removed with its star;
// exported as GeoJSON or CSV (favourites.ts). Loaded again whenever it is shown.

import { useEffect, useMemo, useState } from "preact/hooks";
import { Button, IconButton } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { exportFavourites, favouriteLine, type Favourite } from "../../favourites";
import { formatValue, sortKey, sortRows, SORT_KEYS, type Row } from "../../list";
import { api } from "../api";
import { runHash } from "./route";

const ADDED = "added";  // the server's order: marked last first
type FavouriteRow = Row & {fav: Favourite};

export function Trip({full = false, limit}: {full?: boolean; limit?: number}) {
  const [items, setItems] = useState<Favourite[] | null>(null);
  const [error, setError] = useState("");
  const [sort, setSort] = useState({key: ADDED, desc: true});
  const [all, setAll] = useState(false);
  useEffect(() => { api.favourites().then((r) => setItems(r.favourites), (e) => setError(e.message)); }, []);
  const rows: FavouriteRow[] = useMemo(() => (items || []).map((fav) => ({line: favouriteLine(fav), n: 1, fav})), [items]);

  if (error) return <p class="c-error">{error}</p>;
  if (!items) return <p class="u-small">Loading…</p>;
  if (!items.length) return <p class="u-small c-trip-empty">No favourite lines yet: mark them with{" "}
    <Icon name="star" /> in a result.</p>;
  const key = sort.key === ADDED ? null : sortKey(sort.key);
  const sorted = key ? sortRows(rows, key, sort.desc) : sort.desc ? rows : rows.slice().reverse();
  const shown = limit && !all ? sorted.slice(0, limit) : sorted;
  const remove = (r: FavouriteRow) => api.unfavourite(r.fav.run_id, r.fav.line)
    .then(() => setItems((now) => (now || []).filter((f) => f !== r.fav)), (e) => setError(e.message));
  const exported = () => sorted.map((r) => ({line: r.line, runId: r.fav.run_id, runName: r.fav.run_name}));
  const extra = key && !["len", "clr"].includes(key.key) ? key : full ? sortKey("hmax") : null;

  return <div class="c-trip">
    <div class="c-trip-tools">
      <label class="c-trip-sort"><span class="u-small">Sort</span>
        <select class="u-input" value={sort.key} onChange={(e) => {
          const value = e.currentTarget.value;
          setSort({key: value, desc: value === ADDED || sortKey(value).desc});
        }}>
          <option value={ADDED}>marked, last first</option>
          {SORT_KEYS.filter((k) => k.key !== "n").map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
        </select></label>
      <IconButton icon="down" label={sort.desc ? "Largest first" : "Smallest first"} plain
                  class={sort.desc ? "" : "c-up"} onClick={() => setSort({...sort, desc: !sort.desc})} />
    </div>
    <ul class="c-trip-list">
      {shown.map((r) => <li class="c-fav" key={r.fav.run_id + "|" + r.fav.line}>
        <button type="button" class="c-fav-star" aria-label="Remove from favourites" title="Remove from favourites"
                onClick={() => remove(r)}><Icon name="star" fav /></button>
        <a class="c-fav-open" href={runHash(r.fav.run_id, r.fav.line)}>
          <span class="u-grow">
            <b>{formatValue(sortKey("len"), r)} m</b> · {formatValue(sortKey("clr"), r)} m clear
            {extra && <span class="u-small"> · {extra.head} {formatValue(extra, r)}{extra.unit}</span>}
            {r.line.over && <span class="c-fav-over"> · over trees</span>}
            <span class="u-small c-fav-run">{r.fav.run_name} · {r.fav.run_created.slice(0, 10)}</span>
          </span>
          <Icon name="next" />
        </a>
      </li>)}
    </ul>
    {limit && sorted.length > limit && <button type="button" class="c-link" onClick={() => setAll(!all)}>
      {all ? "Show fewer" : `Show all ${sorted.length}`}</button>}
    <div class="u-h4">Export for the trip</div>
    <div class="u-row">
      <Button icon="download" onClick={() => exportFavourites(exported(), "geojson")}>GeoJSON</Button>
      <Button icon="download" onClick={() => exportFavourites(exported(), "csv")}>CSV</Button>
    </div>
  </div>;
}
