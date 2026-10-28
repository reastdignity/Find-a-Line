// The result of a run without its UI: lines, filters, groups, the list's rows and order, the chosen
// line of each group, favourites, the selection, other runs shown with it, links to lines and
// profiles. The same logic as start() in main.tsx (filters first, then groupLines, then the group
// size; the same rows in the same order), for the app's shells, which draw it with ResultMap and
// their own panels. Plain TypeScript (no DOM but fetch), so tests run it under Node.

import { dh, drop, lowerAnchor, NO_FILTERS, passes, prominence, range as valueRange, saveFilters, savedFilters,
         wallAngle, type Filters } from "../filters";
import { groupLines, type Group } from "../grouping";
import { loadFavourites, saveFavourite } from "../favourites";
import { lineCoords, lineKey, runLineKey } from "../keys";
import { saveList, savedList, sortKey, sortRows, type ListState, type Row } from "../list";
import { colorOf, inLocalPlane } from "../others";
import type { FilterRanges, LinesState } from "../panels";
import { toLine, type Line, type Profile, type RunData, type RunInfo, type RunLines } from "../types";

export type { Filters, Group, LinesState, ListState, FilterRanges };
export { NO_FILTERS };

/** A row of the list: a group (one line without grouping) and the line shown for it. */
export type Spot = Row & {group: Group<Line>};

/** A message for the user above the results (a link to a line, an error), with an optional action. */
export interface Note {
  text: string;
  action?: string;
  onAction?(): void;
}

/** Another run shown with this one: its lines (in its colour) and area. */
export interface ShownRun {
  info: RunInfo;
  color: string;
  crs: string;
  area: RunLines["area"];
  lines: Line[];
}

/** What the model asks the server; replaced in tests. */
export interface ResultApi {
  loadFavourites(runId: string): Promise<string[]>;
  saveFavourite(runId: string, line: Line, on: boolean): Promise<void>;
  /** The terrain profile of `line` ("lat1,lon1,lat2,lon2") of a run, computed by the server. */
  profile(runId: string, line: string): Promise<Profile>;
}

/** web.py /api/profile, with the header it wants (as fetchProfile in main.tsx). */
export async function fetchProfile(runId: string, line: string): Promise<Profile> {
  const response = await fetch("/api/profile?run=" + encodeURIComponent(runId) + "&line=" + line,
                               {headers: {"X-Findaline-Profile": "1"}});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : response.statusText || "error");
  return body as Profile;
}

export const SERVER_API: ResultApi = {loadFavourites, saveFavourite: (runId, l, on) => saveFavourite(runId, l, on),
                                      profile: fetchProfile};

export interface ResultOptions {
  /** Load and mark the user's favourites (default as main.tsx: FINDALINE_APP.favourites, else from a server). */
  favourites?: boolean;
  /** May the server compute profiles and measure (default: FINDALINE_APP.profiles, else from a server). */
  profiles?: boolean;
  api?: ResultApi;
}

const fromServer = () => typeof location !== "undefined" && location.protocol.startsWith("http");
const appFlags = () => typeof window !== "undefined" ? window.FINDALINE_APP : undefined;
const finite = (v: number) => isFinite(v) ? v : null;

// Copied from main.tsx (start: the `ranges` prop of LinesPanel, and below the line of its measuring):
// remove the copies there when map.html uses this module.
function lineRanges(all: Line[], highAbove: number, largestGroup: number, favourites: boolean): FilterRanges {
  return {len: valueRange(all, (l) => l.len), clr: valueRange(all, (l) => l.clr),
          clrN: valueRange(all, (l) => l.clrN), dh: valueRange(all, dh),
          elev: valueRange(all, lowerAnchor), hmax: valueRange(all, (l) => l.hmax),
          hmean: valueRange(all, (l) => l.hmean), highPct: valueRange(all, (l) => l.highPct),
          highAbove, angle: valueRange(all, wallAngle),
          prom: valueRange(all, (l) => finite(prominence(l, Math.max))),
          drop: valueRange(all, (l) => finite(drop(l))),
          group: largestGroup > 1 ? {min: 1, max: largestGroup} : null,
          water: all.some((l) => l.water > 0), filled: all.some((l) => l.filled > 0), favourites};
}

/** A line measured between two points from its profile (as main.tsx's measuring). */
export function measuredLine(a: [number, number], b: [number, number], p: Profile): Line {
  const n = p.d.length - 1;
  if (p.z[0] === null || p.z[n] === null) throw new Error("no terrain data at a point");
  return {lat1: a[0], lon1: a[1], lat2: b[0], lon2: b[1], x1: 0, y1: 0, x2: 0, y2: 0,
          len: p.d[n], clr: p.min[1], clrN: p.min_nmpt ? p.min_nmpt[1] : null,
          e1: p.z[0]!, e2: p.z[n]!, over: false, water: p.water_m, filled: p.filled_m,
          hmax: p.max_height ? p.max_height[1] : NaN, hmean: NaN, highPct: NaN,
          angle1: NaN, angle2: NaN, prom1: null, prom2: null, drop1: null, drop2: null, prof: p};
}

export class ResultModel {
  readonly data: RunData;
  readonly runId: string;
  /** This run's lines. */
  readonly lines: Line[];
  state: LinesState;
  filters: Filters;
  list: ListState;
  /** The groups shown (filters, grouping, group size), clear ones first, best first: the map's order. */
  groups: Group<Line>[] = [];
  /** The groups as rows in the map's order (clear first); `rows` is them sorted as the list says. */
  baseRows: Spot[] = [];
  rows: Spot[] = [];
  shown = {spots: 0, lines: 0};
  largestGroup = 1;
  ranges!: FilterRanges;
  /** The other runs shown, by id. */
  readonly others = new Map<string, ShownRun>();
  /** The user's favourites are known (the server keeps them for this page). */
  favouritesOn = false;
  note: Note | null = null;
  /** The selected group and the line shown for it; null: none. */
  selected: {group: Group<Line>; line: Line} | null = null;

  private readonly favourites = new Set<string>();  // keyOf
  private readonly chosen = new Set<string>();      // keyOf: the line shown of a group, chosen by the user
  private readonly computed = new Map<string, Profile>();  // by lineCoords (this run's lines only)
  private readonly listeners = new Set<() => void>();
  private readonly api: ResultApi;
  private readonly wantFavourites: boolean;
  readonly canCompute: boolean;
  private selectedKey: string | null = null;

  constructor(data: RunData, options: ResultOptions = {}) {
    this.data = data;
    this.runId = data.run.id;
    this.lines = data.lines.map((row) => toLine(row));
    this.api = options.api || SERVER_API;
    const app = appFlags();
    this.wantFavourites = options.favourites ?? (app ? app.favourites === true : fromServer());
    this.canCompute = options.profiles ?? (app ? app.profiles : fromServer());
    this.state = {group: true, distance: data.distance, by: data.by, showClean: true, showOver: true};
    this.filters = savedFilters();
    this.list = savedList();
    this.compute();
    this.loadRunFavourites(this.runId);
  }

  // --- notifications ---

  /** Call `listener` after every change; returns the function that stops it. */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  private notify() {
    this.listeners.forEach((f) => f());
  }

  // --- lines and keys ---

  /** This run's lines and those of the other runs shown. */
  get all(): Line[] {
    return this.lines.concat(...Array.from(this.others.values()).map((r) => r.lines));
  }

  get nClean(): number { return this.all.filter((l) => !l.over).length; }
  get nOver(): number { return this.all.filter((l) => l.over).length; }

  /** The key of a line among the runs shown (keys.lineKey). */
  keyOf = (l: Line): string => lineKey(l, this.runId);

  /** The run a line belongs to. */
  runOf(l: Line): string {
    return l.run ? l.run.info.id : this.runId;
  }

  /** The `#line=` value of a link to a line of this run (keys.lineCoords). */
  lineLink(l: Line): string {
    return lineCoords(l);
  }

  // --- what is shown ---

  /** The line a group shows: the one chosen in it, else the user's favourite in it, else its representative. */
  shownOf(g: Group<Line>): Line {
    return g.members.find((m) => this.chosen.has(this.keyOf(m)))
      || g.members.find((m) => this.favourites.has(this.keyOf(m))) || g.rep;
  }

  /** Is the line shown for its group the user's favourite rather than a choice. */
  shownAsFavourite(g: Group<Line>): boolean {
    const l = this.shownOf(g);
    return this.favourites.has(this.keyOf(l)) && !this.chosen.has(this.keyOf(l));
  }

  /** The groups and rows again (drawLines in main.tsx). */
  private compute() {
    const all = this.all, f = this.filters, s = this.state;
    // Runs in another coordinate system are grouped in a local plane around this run (main.tsx `framed`);
    // the groups hold the lines themselves, not their copies in the plane
    const framed = Array.from(this.others.values()).some((r) => r.crs !== this.data.crs);
    const visible = all.filter((l) => (l.over ? s.showOver : s.showClean) && passes(l, f)
      && (!f.favOnly || !this.favouritesOn || this.favourites.has(this.keyOf(l))));
    const planar = framed ? inLocalPlane(visible, this.data.center[0], this.data.center[1]) : visible;
    const original = new Map<Line, Line>(planar.map((p, k) => [p, visible[k]]));
    let groups: Group<Line>[] = s.group
      ? groupLines(planar, s.distance, s.by).map((g) => ({rep: original.get(g.rep)!,
                                                         members: g.members.map((m) => original.get(m)!)}))
      : visible.map((l) => ({rep: l, members: [l]}));
    this.largestGroup = groups.reduce((m, g) => Math.max(m, g.members.length), 1);
    if (s.group && f.groupMin !== null) groups = groups.filter((g) => g.members.length >= f.groupMin!);
    // groupLines gives clear groups first; without grouping the lines come in their order, so the
    // rows (clear first, as main.tsx sorts them) keep that order within each kind
    this.baseRows = groups.map((g) => ({line: this.shownOf(g), n: g.members.length, members: g.members, group: g}))
      .sort((a, b) => +a.group.rep.over - +b.group.rep.over);
    this.groups = this.baseRows.map((r) => r.group);
    this.shown = {spots: groups.length, lines: groups.reduce((n, g) => n + g.members.length, 0)};
    this.ranges = lineRanges(all, this.data.high_above, this.largestGroup, this.favouritesOn);
    this.sortRows();
    this.reselect();
  }

  /** The rows' lines again after a choice (the groups stay). */
  private refreshRows() {
    this.baseRows.forEach((r) => { r.line = this.shownOf(r.group); });
    this.sortRows();
    this.reselect();
  }

  private sortRows() {
    // As LinesList: "lines in the group" without grouping sorts by length
    const key = sortKey(this.list.sort === "n" && !this.state.group ? "len" : this.list.sort);
    this.rows = sortRows(this.baseRows, key, this.list.desc);
  }

  /** The selection on the groups shown now: the group holding the selected line, else none. */
  private reselect() {
    const key = this.selectedKey;
    const group = key === null ? undefined : this.groups.find((g) => g.members.some((m) => this.keyOf(m) === key));
    if (!group) { this.selected = null; this.selectedKey = null; return; }
    const line = this.shownOf(group);
    this.selected = {group, line};
    this.selectedKey = this.keyOf(line);
  }

  /** The group (shown now) holding a line, or undefined. */
  groupOf(l: Line): Group<Line> | undefined {
    const key = this.keyOf(l);
    return this.groups.find((g) => g.members.some((m) => this.keyOf(m) === key));
  }

  // --- changes ---

  setState(next: LinesState) {
    this.state = next;
    this.compute();
    this.notify();
  }

  setFilters(next: Filters) {
    this.filters = next;
    saveFilters(next);
    this.compute();
    this.notify();
  }

  clearFilters() {
    this.setFilters({...NO_FILTERS});
  }

  /** The list's state (sorting, unfolded, in view): kept in the browser as main.tsx's list. */
  setList(next: ListState) {
    this.list = next;
    saveList(next);
    this.sortRows();
    this.notify();
  }

  /** Select a group, or a line (shown for its group: chosen if it is not), or nothing. */
  select(target: Group<Line> | Line | null) {
    if (target === null) {
      this.selectedKey = null;
    } else if ("members" in target) {
      this.selectedKey = this.keyOf(this.shownOf(target));
    } else {
      const group = this.groupOf(target);
      if (!group) return;
      if (this.shownOf(group) !== target) this.setChosen(group, target);
      this.selectedKey = this.keyOf(target);
      this.refreshRows();
      this.notify();
      return;
    }
    this.reselect();
    this.notify();
  }

  /** The position of the selected spot in the rows' order (-1: none selected or not in the rows). */
  get position(): number {
    const sel = this.selected;
    return sel ? this.rows.findIndex((r) => r.group === sel.group) : -1;
  }

  /** Select the next spot of the rows' order (the first without a selection); false at the end. */
  next(): boolean {
    return this.step(1);
  }

  /** Select the previous spot (the last without a selection); false at the start. */
  prev(): boolean {
    return this.step(-1);
  }

  private step(by: 1 | -1): boolean {
    const k = this.position, target = k < 0 ? (by > 0 ? 0 : this.rows.length - 1) : k + by;
    if (target < 0 || target >= this.rows.length) return false;
    this.select(this.rows[target].group);
    return true;
  }

  private setChosen(g: Group<Line>, line: Line) {
    g.members.forEach((m) => this.chosen.delete(this.keyOf(m)));
    this.chosen.add(this.keyOf(line));
  }

  /** Show another line of a group (the representative too is a choice: it wins over a favourite in it);
   * the groups stay those of groupLines. A selected group keeps its selection on the new line. */
  choose(g: Group<Line>, line: Line) {
    if (!g.members.includes(line)) return;
    const wasSelected = this.selected?.group === g;
    this.setChosen(g, line);
    if (wasSelected) this.selectedKey = this.keyOf(line);
    this.refreshRows();
    this.notify();
  }

  /** The index of the shown line among its group's members (the selected group's by default); -1: none. */
  variantIndex(g: Group<Line> | undefined = this.selected?.group): number {
    return g ? g.members.indexOf(this.shownOf(g)) : -1;
  }

  // --- favourites ---

  isFavourite = (l: Line): boolean => this.favourites.has(this.keyOf(l));

  /** The user's favourites of a run; none when the server keeps none for this page. */
  private loadRunFavourites(runId: string) {
    if (!this.wantFavourites) return;
    this.api.loadFavourites(runId).then((found) => {
      found.forEach((coords) => this.favourites.add(runLineKey(runId, coords)));
      this.favouritesOn = true;
      this.compute();
      this.notify();
    }, () => {});
  }

  /** Mark or unmark a favourite at once; rolled back with a note when the server does not save it. */
  toggleFavourite(l: Line): Promise<void> {
    const key = this.keyOf(l), on = !this.favourites.has(key);
    const flip = (to: boolean) => { if (to) this.favourites.add(key); else this.favourites.delete(key); };
    // A line shown stays shown in its group
    const group = this.groupOf(l);
    if (!on && group && this.shownOf(group) === l) this.chosen.add(key);
    flip(on);
    this.note = null;
    this.compute();
    this.notify();
    return this.api.saveFavourite(this.runOf(l), l, on).catch((e) => {
      flip(!on);
      this.note = {text: "The favourite was not saved: " + e.message};
      this.compute();
      this.notify();
    });
  }

  /** The favourite lines of the runs shown. */
  favouriteLines(): Line[] {
    return this.all.filter((l) => this.favourites.has(this.keyOf(l)));
  }

  setNote(note: Note | null) {
    this.note = note;
    this.notify();
  }

  // --- links ---

  /** Show and select the line of a link ("#line=lat1,lon1,lat2,lon2" or the value alone) in its group;
   * returns it, or null with a note (not in this run; hidden by the filters, with an action that
   * clears them and tries again). */
  openLinked(hash: string): Line | null {
    const value = hash.startsWith("#line=") ? hash.slice("#line=".length) : hash.replace(/^#/, "");
    const wanted = decodeURIComponent(value);
    const line = this.lines.find((l) => lineCoords(l) === wanted);
    if (!line) {
      this.note = {text: "The line of the link is not in this run."};
      this.notify();
      return null;
    }
    this.note = null;
    this.chosen.add(this.keyOf(line));
    this.compute();
    if (this.groupOf(line)) {
      this.select(line);
      return line;
    }
    this.note = {text: "The line of the link is hidden by the filters.", action: "clear filters", onAction: () => {
      this.filters = {...NO_FILTERS};
      saveFilters(this.filters);
      this.state = {...this.state, showClean: true, showOver: true};
      this.openLinked(hash);
    }};
    this.notify();
    return null;
  }

  // --- other runs ---

  /** Show another run's lines (lines.js, from others.loadLines) with this run's. */
  addRun(info: RunInfo, loaded: RunLines, color = colorOf(info.id)) {
    const run = {info, color};
    this.others.set(info.id, {info, color, crs: loaded.crs, area: loaded.area,
                              lines: loaded.lines.map((row) => toLine(row, run))});
    this.compute();
    this.notify();
    this.loadRunFavourites(info.id);
  }

  removeRun(id: string) {
    if (!this.others.delete(id)) return;
    this.compute();
    this.notify();
  }

  // --- profiles ---

  /** A line's profile: saved with it, computed before, or computed by the server (lines of this run only;
   * maps of areas keep a profile only for the line saved for each group). */
  async profile(l: Line): Promise<Profile> {
    if (l.prof) return l.prof;
    if (l.run) throw new Error("lines of other runs come without profiles");
    const key = lineCoords(l), known = this.computed.get(key);
    if (known) { l.prof = known; return known; }
    if (!this.canCompute) throw new Error("no profile saved for this line, and this page cannot compute one");
    const p = await this.api.profile(this.runId, key);
    this.computed.set(key, p);
    l.prof = p;
    this.notify();
    return p;
  }

  /** The profile between two points (lat, lon) as a line (anchors on the ground), computed by the server. */
  async measure(a: [number, number], b: [number, number]): Promise<Line> {
    if (!this.canCompute) throw new Error("this page cannot compute profiles");
    const key = [a[0], a[1], b[0], b[1]].map((v) => v.toFixed(7)).join(",");
    return measuredLine(a, b, await this.api.profile(this.runId, key));
  }
}
