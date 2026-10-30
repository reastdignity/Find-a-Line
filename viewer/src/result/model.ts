// The result of a run without its UI: lines, filters, groups, the list's rows and order, the chosen
// line of each group, favourites, the selection, other runs shown with it, links to lines and
// profiles. The same logic as start() in main.tsx (filters first, then groupLines, then the group
// size; the same rows in the same order), for the app's shells, which draw it with ResultMap and
// their own panels. Plain TypeScript (no DOM but fetch), so tests run it under Node.

import { dh, drop, lowerAnchor, NO_FILTERS, passes, prominence, range as valueRange, saveFilters, savedFilters,
         wallAngle, type Filters } from "../filters";
import { groupLines, type Group } from "../grouping";
import { detailMessage } from "../i18n";
import { addFolder, loadRunFavourites, moveFavourite, saveFavourite, type FavouriteFolder,
         type RunFavourites } from "../favourites";
import { FolderTree } from "../folders";
import { lineCoords, lineKey, runLineKey } from "../keys";
import { hasSavedList, saveList, savedList, sortKey, sortRows, type ListState, type Row } from "../list";
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
  /** The user's favourites of a run with their folders (the lines alone: no folders known). */
  loadFavourites(runId: string): Promise<RunFavourites | string[]>;
  saveFavourite(runId: string, line: Line, on: boolean, folder?: number | null): Promise<void>;
  moveFavourite?(runId: string, line: string, folder: number | null): Promise<void>;
  addFolder?(name: string, parent: number | null): Promise<number>;
  /** The terrain profile of `line` ("lat1,lon1,lat2,lon2") of a run, computed by the server. */
  profile(runId: string, line: string): Promise<Profile>;
}

/** web.py /api/profile, with the header it wants (as fetchProfile in main.tsx). */
export async function fetchProfile(runId: string, line: string): Promise<Profile> {
  const response = await fetch("/api/profile?run=" + encodeURIComponent(runId) + "&line=" + line,
                               {headers: {"X-Findaline-Profile": "1"}});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(detailMessage(body.detail, response.statusText || "error"));
  return body as Profile;
}

export const SERVER_API: ResultApi = {
  loadFavourites: loadRunFavourites, saveFavourite: (runId, l, on, folder) => saveFavourite(runId, l, on, undefined, folder),
  moveFavourite: (runId, line, folder) => moveFavourite(runId, line, folder), addFolder: (name, parent) => addFolder(name, parent),
  profile: fetchProfile};

const LAST_FOLDER = "findaline.lastFolder";  // the folder a new favourite goes to: this browser's convenience

export interface ResultOptions {
  /** Load and mark the user's favourites (default as main.tsx: FINDALINE_APP.favourites, else from a server). */
  favourites?: boolean;
  /** May the server compute profiles and measure (default: FINDALINE_APP.profiles, else from a server). */
  profiles?: boolean;
  api?: ResultApi;
  /** The list's sorting while the user has kept no choice of their own (default: list.ts LIST_DEFAULTS);
   *  not saved until the user changes the list. */
  list?: Partial<ListState>;
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
  /** The profile of the selected line was closed (ProfileDock); selecting again, or another line, shows it. */
  profileClosed = false;

  // Runs of hundreds of thousands of lines: what goes through every line (keys are strings built
  // from the coordinates) is done only when it changes, and lines are known by the objects
  private readonly favourites = new Set<string>();  // keyOf
  private readonly favouriteSet = new Set<Line>();     // the lines of `favourites` among the runs shown
  private readonly inFolder = new Map<string, number | null>();  // keyOf -> the favourite's folder
  /** The user's folders of favourites (from the server with the run's favourites). */
  folders: FavouriteFolder[] = [];
  tree = new FolderTree([]);
  private readonly chosen = new Set<Line>();        // the line shown of a group, chosen by the user
  private readonly computed = new Map<string, Profile>();  // by lineCoords (this run's lines only)
  private readonly listeners = new Set<() => void>();
  private readonly api: ResultApi;
  private readonly wantFavourites: boolean;
  readonly canCompute: boolean;
  private selectedLine: Line | null = null;
  private allLines: Line[] = [];
  private counts = {clean: 0, over: 0};
  private grouped: Group<Line>[] = [];  // groupLines' groups, before the group size filter
  private valueRanges: {of: Line[]; ranges: FilterRanges} | null = null;  // lineRanges of `all`

  constructor(data: RunData, options: ResultOptions = {}) {
    this.data = data;
    this.runId = data.run.id;
    this.lines = data.lines.map((row) => toLine(row));
    this.gather();
    this.api = options.api || SERVER_API;
    const app = appFlags();
    this.wantFavourites = options.favourites ?? (app ? app.favourites === true : fromServer());
    this.canCompute = options.profiles ?? (app ? app.profiles : fromServer());
    this.state = {group: true, distance: data.distance, by: data.by, showClean: true, showOver: true};
    this.filters = savedFilters();
    this.list = options.list && !hasSavedList() ? {...savedList(), ...options.list} : savedList();
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
    return this.allLines;
  }

  get nClean(): number { return this.counts.clean; }
  get nOver(): number { return this.counts.over; }

  /** `all` and its counts again (the runs shown changed). */
  private gather() {
    this.allLines = this.lines.concat(...Array.from(this.others.values()).map((r) => r.lines));
    const over = this.allLines.reduce((n, l) => n + +l.over, 0);
    this.counts = {clean: this.allLines.length - over, over};
  }

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
    if (this.chosen.size === 0 && this.favouriteSet.size === 0) return g.rep;
    return g.members.find((m) => this.chosen.has(m)) || g.members.find((m) => this.favouriteSet.has(m)) || g.rep;
  }

  /** Is the line shown for its group the user's favourite rather than a choice. */
  shownAsFavourite(g: Group<Line>): boolean {
    const l = this.shownOf(g);
    return this.favouriteSet.has(l) && !this.chosen.has(l);
  }

  /** The groups and rows again (drawLines in main.tsx): after a change of the filters, of the grouping
   *  or of the runs shown. */
  private compute() {
    const all = this.all, f = this.filters, s = this.state;
    // Runs in another coordinate system are grouped in a local plane around this run (main.tsx `framed`);
    // the groups hold the lines themselves, not their copies in the plane
    const framed = Array.from(this.others.values()).some((r) => r.crs !== this.data.crs);
    const visible = all.filter((l) => (l.over ? s.showOver : s.showClean) && passes(l, f)
      && (!f.favOnly || !this.favouritesOn || this.favouriteSet.has(l)));
    const planar = framed ? inLocalPlane(visible, this.data.center[0], this.data.center[1]) : visible;
    const original = framed ? new Map<Line, Line>(planar.map((p, k) => [p, visible[k]])) : null;
    this.grouped = s.group
      ? groupLines(planar, s.distance, s.by).map((g) => original ? {rep: original.get(g.rep)!,
                                                                    members: g.members.map((m) => original.get(m)!)} : g)
      : visible.map((l) => ({rep: l, members: [l]}));
    this.largestGroup = this.grouped.reduce((m, g) => Math.max(m, g.members.length), 1);
    this.ranges = this.rangesNow();
    this.showGroups();
  }

  /** The filters' ranges: of the values only when the lines changed. */
  private rangesNow(): FilterRanges {
    if (this.valueRanges?.of !== this.all) {
      this.valueRanges = {of: this.all, ranges: lineRanges(this.all, this.data.high_above, 1, false)};
    }
    const largest = this.largestGroup;
    return {...this.valueRanges.ranges, group: largest > 1 ? {min: 1, max: largest} : null, favourites: this.favouritesOn};
  }

  /** The groups shown (the group size filter) and their rows again. */
  private showGroups() {
    const f = this.filters, s = this.state;
    let groups = this.grouped;
    if (s.group && f.groupMin !== null) groups = groups.filter((g) => g.members.length >= f.groupMin!);
    // groupLines gives clear groups first; without grouping the lines come in their order, so the
    // rows (clear first, as main.tsx sorts them) keep that order within each kind
    this.baseRows = groups.map((g) => ({line: this.shownOf(g), n: g.members.length, members: g.members, group: g}))
      .sort((a, b) => +a.group.rep.over - +b.group.rep.over);
    this.groups = this.baseRows.map((r) => r.group);
    this.shown = {spots: groups.length, lines: groups.reduce((n, g) => n + g.members.length, 0)};
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
    const group = this.selectedLine && this.groupOf(this.selectedLine);
    if (!group) { this.selected = null; this.selectedLine = null; return; }
    const line = this.shownOf(group);
    this.selected = {group, line};
    this.selectedLine = line;
  }

  /** The group (shown now) holding a line, or undefined. */
  groupOf(l: Line): Group<Line> | undefined {
    return this.groups.find((g) => g.members.includes(l));
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
    this.profileClosed = false;
    if (target === null) {
      this.selectedLine = null;
    } else if ("members" in target) {
      this.selectedLine = this.shownOf(target);
    } else {
      const group = this.groupOf(target);
      if (!group) return;
      if (this.shownOf(group) !== target) this.setChosen(group, target);
      this.selectedLine = target;
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
    g.members.forEach((m) => this.chosen.delete(m));
    this.chosen.add(line);
  }

  /** Show another line of a group (the representative too is a choice: it wins over a favourite in it);
   * the groups stay those of groupLines. A selected group keeps its selection on the new line. */
  choose(g: Group<Line>, line: Line) {
    if (!g.members.includes(line)) return;
    const wasSelected = this.selected?.group === g;
    this.profileClosed = false;
    this.setChosen(g, line);
    if (wasSelected) this.selectedLine = line;
    this.refreshRows();
    this.notify();
  }

  /** Close the selected line's profile (until a line is selected again: select, choose, showProfile). */
  closeProfile() {
    this.profileClosed = true;
    this.notify();
  }

  showProfile() {
    this.profileClosed = false;
    this.notify();
  }

  /** The index of the shown line among its group's members (the selected group's by default); -1: none. */
  variantIndex(g: Group<Line> | undefined = this.selected?.group): number {
    return g ? g.members.indexOf(this.shownOf(g)) : -1;
  }

  // --- favourites ---

  isFavourite = (l: Line): boolean => this.favourites.has(this.keyOf(l));

  /** The folder of a favourite (null: the top level); undefined: not a favourite. */
  folderOf(l: Line): number | null | undefined {
    const key = this.keyOf(l);
    return this.favourites.has(key) ? this.tree.known(this.inFolder.get(key)) : undefined;
  }

  /** The folder a new favourite goes to: the last one chosen in this browser, if it still exists. */
  get lastFolder(): number | null {
    try {
      const v = localStorage.getItem(LAST_FOLDER);
      return this.tree.known(v === null ? null : Number(v));
    } catch {
      return null;
    }
  }

  private setLastFolder(folder: number | null) {
    try {
      if (folder === null) localStorage.removeItem(LAST_FOLDER); else localStorage.setItem(LAST_FOLDER, String(folder));
    } catch { /* no storage: the top level next time */ }
  }

  /** The user's favourites of a run; none when the server keeps none for this page. */
  private loadRunFavourites(runId: string) {
    if (!this.wantFavourites) return;
    this.api.loadFavourites(runId).then((found) => {
      const body: {lines: string[]; in: Record<string, number | null>; folders: FavouriteFolder[] | null} =
        Array.isArray(found) ? {lines: found, in: {}, folders: null} : found;
      body.lines.forEach((coords) => {
        const key = runLineKey(runId, coords);
        this.favourites.add(key);
        this.inFolder.set(key, body.in[coords] ?? null);
      });
      if (body.folders) this.setFolders(body.folders);
      this.markFavourites(runId, body.lines);
      const regroup = this.filters.favOnly;
      this.favouritesOn = true;
      if (regroup) this.compute();
      else { this.ranges = this.rangesNow(); this.refreshRows(); }
      this.notify();
    }, () => {});
  }

  private setFolders(folders: FavouriteFolder[]) {
    this.folders = folders;
    this.tree = new FolderTree(folders);
  }

  /** A new folder of favourites in `parent` (null: the top level); its id, or null with a note. */
  async newFolder(name: string, parent: number | null): Promise<number | null> {
    if (!this.api.addFolder) return null;
    try {
      const id = await this.api.addFolder(name, parent);
      this.setFolders([...this.folders, {id, parent_id: parent, name: name.trim(), created: ""}]);
      this.notify();
      return id;
    } catch (e: any) {
      this.setNote({text: "The folder was not made: " + e.message});
      return null;
    }
  }

  /** Put a line into a folder of favourites: marked there when it is not a favourite, else moved. */
  saveTo(l: Line, folder: number | null): Promise<void> {
    this.setLastFolder(folder);
    const key = this.keyOf(l);
    if (!this.favourites.has(key)) return this.toggleFavourite(l, folder);
    const before = this.inFolder.get(key) ?? null;
    if (before === folder || !this.api.moveFavourite) return Promise.resolve();
    this.inFolder.set(key, folder);
    this.notify();
    return this.api.moveFavourite(this.runOf(l), lineCoords(l), folder).catch((e) => {
      this.inFolder.set(key, before);
      this.setNote({text: "The favourite was not moved: " + e.message});
    });
  }

  /** Mark or unmark a favourite at once (a new one into `folder`, by default the last folder used);
   *  rolled back with a note when the server does not save it. */
  toggleFavourite(l: Line, folder: number | null = this.lastFolder): Promise<void> {
    const key = this.keyOf(l), on = !this.favourites.has(key);
    const flip = (to: boolean) => {
      if (to) { this.favourites.add(key); this.favouriteSet.add(l); this.inFolder.set(key, folder); }
      else { this.favourites.delete(key); this.favouriteSet.delete(l); this.inFolder.delete(key); }
    };
    // A line shown stays shown in its group
    const group = this.groupOf(l);
    if (!on && group && this.shownOf(group) === l) this.chosen.add(l);
    flip(on);
    this.note = null;
    this.afterFavourite();
    return this.api.saveFavourite(this.runOf(l), l, on, on ? folder : undefined).catch((e) => {
      flip(!on);
      this.note = {text: "The favourite was not saved: " + e.message};
      this.afterFavourite();
    });
  }

  /** The groups (only favourites shown) or the lines shown again after a favourite changed. */
  private afterFavourite() {
    if (this.filters.favOnly) this.compute(); else this.refreshRows();
    this.notify();
  }

  /** The lines of a run whose coordinates (keys.lineCoords) are among `found` become favourites
   *  (keys are built only for the lines whose first latitude is among theirs). */
  private markFavourites(runId: string, found: string[]) {
    const lines = runId === this.runId ? this.lines : this.others.get(runId)?.lines || [];
    const wanted = new Set(found), lat1 = new Set(found.map((coords) => Number(coords.split(",")[0])));
    for (const l of lines) if (lat1.has(l.lat1) && wanted.has(lineCoords(l))) this.favouriteSet.add(l);
  }

  /** The favourite lines of the runs shown. */
  favouriteLines(): Line[] {
    return this.all.filter((l) => this.favouriteSet.has(l));
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
    this.chosen.add(line);
    this.refreshRows();
    if (this.groupOf(line)) {
      this.select(line);
      return line;
    }
    this.note = {text: "The line of the link is hidden by the filters.", action: "clear filters", onAction: () => {
      this.filters = {...NO_FILTERS};
      saveFilters(this.filters);
      this.state = {...this.state, showClean: true, showOver: true};
      this.compute();
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
    this.gather();
    this.compute();
    this.notify();
    this.loadRunFavourites(info.id);
  }

  removeRun(id: string) {
    if (!this.others.delete(id)) return;
    this.gather();
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
