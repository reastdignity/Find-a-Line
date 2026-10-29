// The favourite lines of prototype B: the user's favourites of every run they may open (/api/favourites,
// listed again each time the section opens, so lines marked in a result are there without a reload),
// sorted by any of their values, drawn on the map, exported as GeoJSON or CSV; a click opens the line
// in its run (#run=…&line=…).

import { useEffect, useMemo, useState } from "preact/hooks";
import { exportFavourites, favouriteLine, type Favourite } from "../../favourites";
import { formatValue, sortKey, sortRows, SORT_KEYS, type Row } from "../../list";
import { Button, IconButton } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { api } from "../api";
import type { PlaceMap } from "../PlaceMap";

const ADDED = "added";  // the server's order: marked last first
type FavouriteRow = Row & {fav: Favourite};

export function FavouritesB({place, onOpen}: {place: PlaceMap; onOpen(runId: string, line: string): void}) {
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
  if (error) return <p class="b-card-error">{error}</p>;
  if (!items) return <p class="u-small">Loading…</p>;
  if (!items.length) {
    return <p class="u-small b-empty">No favourite lines yet: mark them with <Icon name="star" /> in a result.</p>;
  }
  const key = sort.key === ADDED ? null : sortKey(sort.key);
  const sorted = key ? sortRows(rows, key, sort.desc) : sort.desc ? rows : rows.slice().reverse();
  const columns = ["len", "clr", "hmax"].map(sortKey);
  if (key && !columns.includes(key)) columns.push(key);
  const remove = (r: FavouriteRow) => api.unfavourite(r.fav.run_id, r.fav.line)
    .then(() => setItems(items.filter((f) => f !== r.fav)), (e) => setError(e.message));
  const exported = () => sorted.map((r) => ({line: r.line, runId: r.fav.run_id, runName: r.fav.run_name}));
  return <div class="b-favs">
    <div class="b-favs-tools">
      <label class="r-sortby"><span class="u-small">Sort by</span>
        <select class="r-sort b-favs-sort" value={sort.key} onChange={(e) => {
          const value = e.currentTarget.value;
          setSort({key: value, desc: value === ADDED || sortKey(value).desc});
        }}>
          <option value={ADDED}>marked, last first</option>
          {SORT_KEYS.filter((k) => k.key !== "n").map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
        </select></label>
      <button type="button" class="r-dir" aria-label={sort.desc ? "Largest first" : "Smallest first"}
              onClick={() => setSort({...sort, desc: !sort.desc})}>{sort.desc ? "↓" : "↑"}</button>
      <span class="u-grow" />
      <Button icon="download" onClick={() => exportFavourites(exported(), "geojson")}>GeoJSON</Button>
      <Button icon="download" onClick={() => exportFavourites(exported(), "csv")}>CSV</Button>
    </div>
    <ul class="b-fav-list">
      {sorted.map((r) => <li key={r.fav.run_id + "|" + r.fav.line} class="b-fav">
        <button type="button" class="b-fav-open" onClick={() => onOpen(r.fav.run_id, r.fav.line)}>
          <span class="b-fav-nums">{columns.map((c) => <span key={c.key}><b>{formatValue(c, r)}</b>{c.unit} {c.head}</span>)}</span>
          <span class="u-small">{r.fav.run_name} · {r.fav.run_created.slice(0, 10)}
            {r.line.over && " · over trees or buildings"}</span>
        </button>
        <IconButton icon="star" fav label="Remove from favourites" plain onClick={() => remove(r)} />
      </li>)}
    </ul>
  </div>;
}
