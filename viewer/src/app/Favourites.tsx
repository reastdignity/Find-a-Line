// The tab "Favourites": the user's favourite lines of every run they may open, sorted by any of
// their values, shown on the app's map; each opens in its run's map (map.html#line=...).

import { useEffect, useMemo, useState } from "preact/hooks";
import { exportFavourites, favouriteLine, type Favourite } from "../favourites";
import { formatValue, sortKey, sortRows, SORT_KEYS, type Row } from "../list";
import { api } from "./api";
import type { PlaceMap } from "./PlaceMap";

const ADDED = "added";  // the server's order: marked last first
type FavouriteRow = Row & {fav: Favourite};

export function Favourites({place}: {place: PlaceMap}) {
  const [items, setItems] = useState<Favourite[] | null>(null);
  const [error, setError] = useState("");
  const [sort, setSort] = useState({key: ADDED, desc: true});
  useEffect(() => { api.favourites().then((r) => setItems(r.favourites), (e) => setError(e.message)); }, []);
  const rows: FavouriteRow[] = useMemo(() => (items || []).map((fav) => ({line: favouriteLine(fav), n: 1, fav})),
                                       [items]);
  useEffect(() => {
    place.showFavourites(rows.map((r) => ({line: r.line, label: r.fav.run_name})));
    return () => place.showFavourites(null);
  }, [rows]);
  if (error) return <p class="fa-error">{error}</p>;
  if (!items) return <p class="fa-hint">Loading…</p>;
  if (!items.length) return <p class="fa-hint">No favourite lines yet: mark them with ☆ in a run's map (in its
    popups or its list of lines).</p>;
  const key = sort.key === ADDED ? null : sortKey(sort.key);
  const sorted = key ? sortRows(rows, key, sort.desc) : sort.desc ? rows : rows.slice().reverse();
  const columns = ["len", "clr", "hmax"].map(sortKey);
  if (key && !columns.includes(key)) columns.push(key);
  const remove = (r: FavouriteRow) => api.unfavourite(r.fav.run_id, r.fav.line)
    .then(() => setItems(items.filter((f) => f !== r.fav)), (e) => setError(e.message));
  const exported = () => sorted.map((r) => ({line: r.line, runId: r.fav.run_id, runName: r.fav.run_name}));
  return <div class="fa-favourites">
    <div class="fa-favourites-tools">
      Sort by <select class="fa-favourites-sort" value={sort.key}
                      onChange={(e) => {
                        const value = e.currentTarget.value;
                        setSort({key: value, desc: value === ADDED || sortKey(value).desc});
                      }}>
        <option value={ADDED}>marked, last first</option>
        {SORT_KEYS.filter((k) => k.key !== "n").map((k) => <option value={k.key}>{k.label}</option>)}
      </select>{" "}
      <button class="fa-small" title="reverse the order" onClick={() => setSort({...sort, desc: !sort.desc})}>
        {sort.desc ? "↓" : "↑"}</button>
      <span class="fa-hint"> · export {items.length}: </span>
      <button class="fa-small" onClick={() => exportFavourites(exported(), "geojson")}>GeoJSON</button>{" "}
      <button class="fa-small" onClick={() => exportFavourites(exported(), "csv")}>CSV</button>
    </div>
    {sorted.map((r) => <div class="fa-run fa-favourite" key={r.fav.run_id + "|" + r.fav.line}
                            onClick={() => place.zoomToLine(r.line)}>
      <div>{columns.map((c, i) => <span>{i > 0 && " · "}{c.head} <b>{formatValue(c, r)}</b>{c.unit}</span>)}
        {r.line.over && <span class="fa-badge" title="passes too close to trees or buildings">over trees</span>}</div>
      <div class="fa-hint">{r.fav.run_name} · {r.fav.run_created.slice(0, 10)}</div>
      <a href={`/runs/${encodeURIComponent(r.fav.run_id)}/map.html#line=${encodeURIComponent(r.fav.line)}`}
         target="_blank" onClick={(e) => e.stopPropagation()}>open on map</a>{" "}
      <button class="fa-small" title="remove from favourites"
              onClick={(e) => { e.stopPropagation(); remove(r); }}>★ remove</button>
    </div>)}
  </div>;
}
