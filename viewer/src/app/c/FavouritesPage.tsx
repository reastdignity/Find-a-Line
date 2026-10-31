// C's (and D's) page of favourite lines in folders, as a browser's bookmarks manager: on desktops the
// folder tree on the left (the top level "Favourites", folders folded and unfolded, with counts) and
// the chosen folder on the right (its folders, then its lines sorted by any value, each opening its
// line in the result); on phones one column with the path and the folders to go into. New folder,
// and a ⋯ menu on every folder (Rename, Move to…, Delete with what is in it) and line (Open, Move to…,
// Remove); folders and lines are also dragged onto a folder (Pragmatic drag and drop; the targets a
// move may not go to are not offered, as jobs.Jobs.change_folder refuses them). The chosen folder is
// in the hash (#favourites&folder=<id>, replaced). Export: the chosen folder with its folders.

import type { ComponentChildren } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { draggable, dropTargetForElements } from "@atlaskit/pragmatic-drag-and-drop/adapter/element-adapter";
import { combine } from "@atlaskit/pragmatic-drag-and-drop/utils/combine";
import { Button, IconButton } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { Popover } from "../../result/view";
import { exportFavourites, favouriteLine, type Favourite, type FavouriteFolder } from "../../favourites";
import { FolderTree } from "../../folders";
import { formatValue, sortKey, sortRows, SORT_KEYS, type Row } from "../../list";
import { api } from "../api";
import { menuKeys } from "./Home";
import { favouritesHash, folderOfHash, runHash } from "./route";
import { day } from "../../time";

const ADDED = "added";  // the server's order: marked last first
type FavouriteRow = Row & {fav: Favourite};
/** What is dragged: a line (its run and lineCoords) or a folder. */
type Dragged = {kind: "fav"; run: string; line: string} | {kind: "folder"; id: number};
const favKey = (f: {run_id: string; line: string}) => f.run_id + "|" + f.line;

/** The page's state and its changes, given to every row (and the drag-and-drop targets). */
interface Shelf {
  tree: FolderTree;
  items: Favourite[];
  current: number | null;
  choose(id: number | null): void;
  canDrop(what: Dragged, target: number | null): boolean;
  drop(what: Dragged, target: number | null): void;
  moveFolder(id: number, target: number | null): void;
  rename(id: number, name: string): void;
  askDelete(id: number): void;
  removeFav(f: Favourite): void;
}

/** A row that can be dragged (`drag`) and/or dropped on (`target`: the folder it stands for). */
function useDnd(shelf: Shelf, drag: Dragged | null, target: number | null | undefined) {
  const ref = useRef<HTMLElement>(null);
  const [over, setOver] = useState(false);
  const [dragging, setDragging] = useState(false);
  const latest = useRef({shelf, drag, target});
  latest.current = {shelf, drag, target};
  const dropping = target !== undefined;
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const parts: (() => void)[] = [];
    if (drag) {
      parts.push(draggable({element, getInitialData: () => ({...latest.current.drag!}),
                            onDragStart: () => setDragging(true), onDrop: () => setDragging(false)}));
    }
    if (dropping) {
      parts.push(dropTargetForElements({
        element,
        getData: () => ({target: latest.current.target}),
        canDrop: ({source}) => latest.current.shelf.canDrop(source.data as Dragged, latest.current.target!),
        onDragEnter: () => setOver(true), onDragLeave: () => setOver(false),
        onDrop: ({source}) => { setOver(false); latest.current.shelf.drop(source.data as Dragged, latest.current.target!); },
      }));
    }
    return combine(...parts);
  }, [!!drag, dropping]);
  return {ref, cls: (over ? " c-drop-over" : "") + (dragging ? " c-dragging" : "")};
}

/** "Move to…": the folders a thing may go to (the others shown, not offered). */
function MoveTargets({tree, can, onPick}: {tree: FolderTree; can(target: number | null): boolean;
                                           onPick(target: number | null): void}) {
  const item = (id: number | null, name: string, depth: number) =>
    <button key={id ?? "top"} type="button" role="menuitem" class="c-menu-item c-move-target" disabled={!can(id)}
            style={{paddingLeft: `${10 + 14 * depth}px`}} onClick={() => onPick(id)}>
      <Icon name={id === null ? "star" : "folder"} />{name}</button>;
  return <>{item(null, "Favourites (top level)", 0)}{tree.flat().map(({folder, depth}) => item(folder.id, folder.name, depth))}</>;
}

/** The ⋯ menu of a folder or a line, with its "Move to…" view. */
function RowMenu({label, children, move}: {label: string; children: (close: () => void) => ComponentChildren;
                                          move: {tree: FolderTree; can(t: number | null): boolean; pick(t: number | null): void}}) {
  const [open, setOpen] = useState(false);
  const [moving, setMoving] = useState(false);
  const close = () => { setOpen(false); setMoving(false); };
  return <Popover label={label} align="right" open={open} onOpen={(v) => { setOpen(v); if (!v) setMoving(false); }}
                  button={(on, toggle) => <IconButton icon="more" label={label} plain expanded={on} class="c-row-menu"
                                                      onClick={toggle} />}>
    <div class="c-menu" role="menu" onKeyDown={menuKeys}>
      {moving
        ? <><span class="u-small c-menu-head">Move to</span>
            <MoveTargets tree={move.tree} can={move.can} onPick={(t) => { close(); move.pick(t); }} /></>
        : <>{children(close)}
            <button type="button" role="menuitem" class="c-menu-item" onClick={() => setMoving(true)}>
              <Icon name="folder" />Move to…</button></>}
    </div>
  </Popover>;
}

/** A folder's name being typed (new or renamed): Enter saves, Esc gives up. */
function NameField({initial = "", label, onDone}: {initial?: string; label: string; onDone(name: string | null): void}) {
  const [name, setName] = useState(initial);
  return <form class="c-folder-name" onSubmit={(e) => { e.preventDefault(); onDone(name.trim() || null); }}>
    <input class="u-input" value={name} aria-label={label} maxLength={100} ref={(el) => el?.focus()}
           onInput={(e) => setName(e.currentTarget.value)}
           onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onDone(null); } }} />
    <Button kind="primary" type="submit" disabled={!name.trim()}>Save</Button>
    <Button kind="ghost" onClick={() => onDone(null)}>Cancel</Button>
  </form>;
}

/** A folder of the tree on the left, with the folders in it when unfolded. */
function TreeNode({shelf, folder, depth, counts, unfolded, toggle}:
                  {shelf: Shelf; folder: FavouriteFolder; depth: number; counts: Map<number | null, number>;
                   unfolded: Set<number>; toggle(id: number): void}) {
  const dnd = useDnd(shelf, {kind: "folder", id: folder.id}, folder.id);
  const kids = shelf.tree.children(folder.id), open = unfolded.has(folder.id);
  return <li>
    <div ref={dnd.ref as any} class={"c-tree-row" + (shelf.current === folder.id ? " u-on" : "") + dnd.cls}
         style={{paddingLeft: `${4 + 14 * depth}px`}}>
      {kids.length ? <button type="button" class="c-tree-fold" aria-expanded={open}
                             aria-label={`${open ? "Fold" : "Unfold"} ${folder.name}`} onClick={() => toggle(folder.id)}>
          <Icon name={open ? "down" : "next"} /></button> : <span class="c-tree-fold" />}
      <button type="button" class="c-tree-name" aria-current={shelf.current === folder.id ? "true" : undefined}
              onClick={() => shelf.choose(folder.id)}>
        <Icon name="folder" /><span class="u-grow">{folder.name}</span>
        <span class="u-small">{counts.get(folder.id) || 0}</span></button>
    </div>
    {open && kids.length > 0 && <ul class="c-tree">{kids.map((k) =>
      <TreeNode key={k.id} shelf={shelf} folder={k} depth={depth + 1} counts={counts} unfolded={unfolded} toggle={toggle} />)}</ul>}
  </li>;
}

/** The top level of the tree ("Favourites"): a target too. */
function TreeTop({shelf, total}: {shelf: Shelf; total: number}) {
  const dnd = useDnd(shelf, null, null);
  return <div ref={dnd.ref as any} class={"c-tree-row" + (shelf.current === null ? " u-on" : "") + dnd.cls}>
    <span class="c-tree-fold" />
    <button type="button" class="c-tree-name" aria-current={shelf.current === null ? "true" : undefined}
            onClick={() => shelf.choose(null)}>
      <Icon name="star" fav /><span class="u-grow">Favourites</span><span class="u-small">{total}</span></button>
  </div>;
}

/** A folder in the chosen folder's contents. */
function FolderRow({shelf, folder, count}: {shelf: Shelf; folder: FavouriteFolder; count: number}) {
  const dnd = useDnd(shelf, {kind: "folder", id: folder.id}, folder.id);
  const [renaming, setRenaming] = useState(false);
  if (renaming) {
    return <li class="c-fav c-folder-row"><NameField initial={folder.name} label="The folder's name"
                                                     onDone={(name) => { setRenaming(false); if (name) shelf.rename(folder.id, name); }} /></li>;
  }
  return <li ref={dnd.ref as any} class={"c-fav c-folder-row" + dnd.cls} data-folder={folder.id}>
    <button type="button" class="c-fav-open c-folder-open" onClick={() => shelf.choose(folder.id)}>
      <Icon name="folder" /><span class="u-grow"><b>{folder.name}</b>
        <span class="u-small c-fav-run">{count === 1 ? "1 line" : `${count} lines`}</span></span>
      <Icon name="next" /></button>
    <RowMenu label={`Actions of ${folder.name}`}
             move={{tree: shelf.tree, can: (t) => shelf.tree.canMove(folder.id, t), pick: (t) => shelf.moveFolder(folder.id, t)}}>
      {(close) => <>
        <button type="button" role="menuitem" class="c-menu-item" onClick={() => { close(); setRenaming(true); }}>
          <Icon name="edit" />Rename</button>
        <button type="button" role="menuitem" class="c-menu-item c-danger" onClick={() => { close(); shelf.askDelete(folder.id); }}>
          <Icon name="close" />Delete…</button>
      </>}
    </RowMenu>
  </li>;
}

/** A favourite line in the chosen folder. */
function FavRow({shelf, r, extra}: {shelf: Shelf; r: FavouriteRow; extra: ReturnType<typeof sortKey> | null}) {
  const f = r.fav;
  const dnd = useDnd(shelf, {kind: "fav", run: f.run_id, line: f.line}, undefined);
  const here = shelf.tree.known(f.folder_id);
  return <li ref={dnd.ref as any} class={"c-fav" + dnd.cls} data-line={favKey(f)}>
    <button type="button" class="c-fav-star" aria-label="Remove from favourites" title="Remove from favourites"
            onClick={() => shelf.removeFav(f)}><Icon name="star" fav /></button>
    <a class="c-fav-open" href={runHash(f.run_id, f.line)} draggable={false}>
      <span class="u-grow">
        <b>{formatValue(sortKey("len"), r)} m</b> · {formatValue(sortKey("clr"), r)} m clear
        {extra && <span class="u-small"> · {extra.head} {formatValue(extra, r)}{extra.unit}</span>}
        {r.line.over && <span class="c-fav-over"> · over trees</span>}
        <span class="u-small c-fav-run">{f.run_name} · {day(f.run_created)}</span>
      </span>
    </a>
    <RowMenu label="Actions of the line"
             move={{tree: shelf.tree, can: (t) => t !== here, pick: (t) => shelf.drop({kind: "fav", run: f.run_id, line: f.line}, t)}}>
      {(close) => <>
        <a role="menuitem" class="c-menu-item" href={runHash(f.run_id, f.line)} onClick={close}><Icon name="next" />Open</a>
        <button type="button" role="menuitem" class="c-menu-item c-danger" onClick={() => { close(); shelf.removeFav(f); }}>
          <Icon name="close" />Remove</button>
      </>}
    </RowMenu>
  </li>;
}

export function FavouritesPage({wide}: {wide: boolean}) {
  const [items, setItems] = useState<Favourite[] | null>(null);
  const [folders, setFolders] = useState<FavouriteFolder[]>([]);
  const [error, setError] = useState("");    // the list could not be loaded
  const [failed, setFailed] = useState("");  // a change failed: said above the list, which stays
  const [sort, setSort] = useState({key: ADDED, desc: true});
  const [unfolded, setUnfolded] = useState<Set<number>>(new Set());
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<number | null>(null);
  const [, setHash] = useState(location.hash);
  useEffect(() => {
    api.favourites().then((r) => { setItems(r.favourites); setFolders(r.folders); }, (e) => setError(e.message));
    const changed = () => setHash(location.hash);
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  const tree = useMemo(() => new FolderTree(folders), [folders]);
  const current = tree.known(folderOfHash(location.hash));
  const all = items || [];
  const counts = useMemo(() => tree.counts(all), [tree, items]);
  // The path to the chosen folder is unfolded in the tree
  useEffect(() => {
    const path = tree.ancestry(current).slice(0, -1).map((f) => f.id);
    if (path.some((id) => !unfolded.has(id))) setUnfolded(new Set([...unfolded, ...path]));
  }, [current, tree]);
  const confirmRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (deleting !== null) confirmRef.current?.querySelector<HTMLElement>(".c-panel-first")?.focus(); }, [deleting]);

  const fail = (e: Error) => setFailed(e.message);
  const ok = () => setFailed("");
  const choose = (id: number | null) => { setAdding(false); setDeleting(null); location.replace(favouritesHash(id)); };
  const setFolderOf = (run: string, line: string, folder: number | null) =>
    setItems((now) => (now || []).map((f) => f.run_id === run && f.line === line ? {...f, folder_id: folder} : f));
  const shelf: Shelf = {
    tree, items: all, current, choose,
    canDrop: (what, target) => what.kind === "folder" ? tree.canMove(what.id, target)
      : tree.known(all.find((f) => f.run_id === what.run && f.line === what.line)?.folder_id) !== target,
    drop: (what, target) => {
      if (!shelf.canDrop(what, target)) return;
      if (what.kind === "folder") { shelf.moveFolder(what.id, target); return; }
      const before = all.find((f) => f.run_id === what.run && f.line === what.line)?.folder_id ?? null;
      setFolderOf(what.run, what.line, target);
      api.moveFavourite(what.run, what.line, target).then(ok, (e) => { setFolderOf(what.run, what.line, before); fail(e); });
    },
    moveFolder: (id, target) => {
      if (!tree.canMove(id, target)) return;
      api.changeFolder(id, {parent: target}).then(() => {
        ok();
        setFolders((now) => now.map((f) => f.id === id ? {...f, parent_id: target} : f));
      }, fail);
    },
    rename: (id, name) => api.changeFolder(id, {name}).then(() => {
      ok();
      setFolders((now) => now.map((f) => f.id === id ? {...f, name} : f));
    }, fail),
    askDelete: (id) => setDeleting(id),
    removeFav: (f) => api.unfavourite(f.run_id, f.line).then(() => {
      ok();
      setItems((now) => (now || []).filter((x) => x !== f));
    }, fail),
  };
  const create = (name: string | null) => {
    setAdding(false);
    if (!name) return;
    api.addFolder(name, current).then(({id}) => {
      ok();
      setFolders((now) => [...now, {id, parent_id: current, name, created: ""}]);
    }, fail);
  };
  const remove = (id: number) => {
    const gone = new Set(tree.subtree(id)), parent = tree.known(tree.byId.get(id)?.parent_id);
    api.deleteFolder(id).then(() => {
      ok();
      setDeleting(null);
      setFolders((now) => now.filter((f) => !gone.has(f.id)));
      setItems((now) => (now || []).filter((f) => f.folder_id === null || !gone.has(f.folder_id)));
      if (current !== null && gone.has(current)) choose(parent);
      else setTimeout(() => document.querySelector<HTMLElement>(".c-new-folder")?.focus());
    }, fail);
  };
  const toggle = (id: number) => {
    const next = new Set(unfolded);
    if (next.has(id)) next.delete(id); else next.add(id);
    setUnfolded(next);
  };

  if (error) return <p class="c-error">{error}</p>;
  if (!items) return <p class="u-small">Loading…</p>;
  const rows: FavouriteRow[] = all.filter((f) => tree.known(f.folder_id) === current)
    .map((fav) => ({line: favouriteLine(fav), n: 1, fav}));
  const key = sort.key === ADDED ? null : sortKey(sort.key);
  const sorted = key ? sortRows(rows, key, sort.desc) : sort.desc ? rows : rows.slice().reverse();
  const extra = key && !["len", "clr"].includes(key.key) ? key : sortKey("hmax");
  const inside = current === null ? null : new Set(tree.subtree(current));
  const exported = () => all.filter((f) => inside === null || (f.folder_id !== null && inside.has(f.folder_id)))
    .map((f) => ({line: favouriteLine(f), runId: f.run_id, runName: f.run_name, folder: tree.path(tree.known(f.folder_id))}));
  const kids = tree.children(current), path = tree.ancestry(current);
  const deleted = deleting === null ? null : tree.byId.get(deleting);

  const contents = <section class="c-shelf-main" aria-label="The folder's contents">
    <nav class="c-crumbs" aria-label="Folder path">
      <button type="button" class="c-link" onClick={() => choose(null)}>Favourites</button>
      {path.map((f) => <span key={f.id}> / {f.id === current ? <b>{f.name}</b>
        : <button type="button" class="c-link" onClick={() => choose(f.id)}>{f.name}</button>}</span>)}
    </nav>
    {failed && <p class="c-error u-small" role="alert">{failed}</p>}
    {deleted && <div class="c-card-panel c-confirm" role="alertdialog" aria-label="Delete the folder" ref={confirmRef}
                     onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); setDeleting(null); } }}>
      <span>Delete “{deleted.name}”
        {tree.subtree(deleted.id).length > 1 && `, ${tree.subtree(deleted.id).length - 1} folders in it`} and
        {" "}{counts.get(deleted.id) || 0} favourite {counts.get(deleted.id) === 1 ? "line" : "lines"}? This cannot be undone.</span>
      <div class="u-row">
        <Button kind="danger" class="c-confirm-delete" onClick={() => remove(deleted.id)}>Delete</Button>
        <Button kind="ghost" class="c-panel-first" onClick={() => setDeleting(null)}>Keep it</Button>
      </div>
    </div>}
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
      {tree.canAdd(current) && <Button icon="plus" class="c-new-folder" onClick={() => setAdding(true)}>New folder</Button>}
    </div>
    {adding && <NameField label="New folder's name" onDone={create} />}
    <ul class="c-trip-list c-shelf-list">
      {kids.map((f) => <FolderRow key={f.id} shelf={shelf} folder={f} count={counts.get(f.id) || 0} />)}
      {sorted.map((r) => <FavRow key={favKey(r.fav)} shelf={shelf} r={r} extra={extra} />)}
    </ul>
    {!kids.length && !sorted.length && <p class="u-small c-trip-empty">{current === null && !all.length
      ? <>No favourite lines yet: mark them with <Icon name="star" /> in a result.</> : "This folder is empty."}</p>}
    {exported().length > 0 && <>
      <div class="u-h4">Export {current === null ? "all of them" : "this folder"}</div>
      <div class="u-row">
        <Button icon="download" onClick={() => exportFavourites(exported(), "geojson")}>GeoJSON</Button>
        <Button icon="download" onClick={() => exportFavourites(exported(), "csv")}>CSV</Button>
      </div>
    </>}
  </section>;

  if (!wide) return <div class="c-shelf c-shelf-phone">{contents}</div>;
  return <div class="c-shelf">
    <aside class="c-shelf-tree" aria-label="Folders">
      <TreeTop shelf={shelf} total={all.length} />
      <ul class="c-tree">{tree.children(null).map((f) =>
        <TreeNode key={f.id} shelf={shelf} folder={f} depth={1} counts={counts} unfolded={unfolded} toggle={toggle} />)}</ul>
    </aside>
    {contents}
  </div>;
}
