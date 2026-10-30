// The folders of favourite lines as a tree (as a browser's bookmarks): children by name, a folder's
// path, what is inside it, and which moves the server allows (jobs.Jobs.change_folder: never into
// itself or one of its folders, at most FOLDER_DEPTH_MAX deep). Pure: tests/test_viewer.py runs it.

import type { FavouriteFolder } from "./favourites";

export const FOLDER_DEPTH_MAX = 8;  // jobs.FOLDER_DEPTH_MAX

export class FolderTree {
  readonly byId = new Map<number, FavouriteFolder>();
  private readonly kids = new Map<number | null, FavouriteFolder[]>();

  constructor(folders: FavouriteFolder[]) {
    for (const f of folders) this.byId.set(f.id, f);
    for (const f of folders) {
      // A parent that is not known (deleted meanwhile) counts as the top level
      const parent = f.parent_id !== null && this.byId.has(f.parent_id) ? f.parent_id : null;
      if (!this.kids.has(parent)) this.kids.set(parent, []);
      this.kids.get(parent)!.push(f);
    }
    for (const list of this.kids.values()) {
      list.sort((a, b) => a.name.localeCompare(b.name, undefined, {sensitivity: "base"}) || a.id - b.id);
    }
  }

  /** The folders in `parent` (null: the top level), by name. */
  children(parent: number | null): FavouriteFolder[] {
    return this.kids.get(parent) || [];
  }

  /** A known folder, or the top level (null) for an unknown id. */
  known(id: number | null | undefined): number | null {
    return id !== null && id !== undefined && this.byId.has(id) ? id : null;
  }

  /** The folders from the top level down to `id` (empty for the top level). */
  ancestry(id: number | null): FavouriteFolder[] {
    const path: FavouriteFolder[] = [];
    for (let f = id === null ? undefined : this.byId.get(id); f; f = f.parent_id === null ? undefined : this.byId.get(f.parent_id)) {
      path.unshift(f);
      if (path.length > FOLDER_DEPTH_MAX + 1) break;  // a loop the server would never make
    }
    return path;
  }

  /** "Trips / Tatras"; "" for the top level. */
  path(id: number | null): string {
    return this.ancestry(id).map((f) => f.name).join(" / ");
  }

  /** How deep a folder is: the top level 0, a folder in it 1. */
  depth(id: number | null): number {
    return this.ancestry(id).length;
  }

  /** A folder and every folder inside it. */
  subtree(id: number): number[] {
    const found: number[] = [], todo = [id];
    while (todo.length) {
      const f = todo.pop()!;
      found.push(f);
      todo.push(...this.children(f).map((c) => c.id));
    }
    return found;
  }

  /** The levels a folder takes with its folders (1: none inside). */
  height(id: number): number {
    const base = this.depth(id);
    return Math.max(...this.subtree(id).map((f) => this.depth(f))) - base + 1;
  }

  /** May folder `id` go into `target` (null: the top level)? Not into itself or its own folders, not
   *  deeper than FOLDER_DEPTH_MAX, not where it is already. */
  canMove(id: number, target: number | null): boolean {
    const folder = this.byId.get(id);
    if (!folder || (target !== null && !this.byId.has(target))) return false;
    if (this.known(folder.parent_id) === target) return false;
    if (target !== null && this.subtree(id).includes(target)) return false;
    return this.depth(target) + this.height(id) <= FOLDER_DEPTH_MAX;
  }

  /** May a new folder go into `parent`? */
  canAdd(parent: number | null): boolean {
    return (parent === null || this.byId.has(parent)) && this.depth(parent) + 1 <= FOLDER_DEPTH_MAX;
  }

  /** Every folder in tree order (a folder, then what is in it), with its depth: for pickers. */
  flat(): {folder: FavouriteFolder; depth: number}[] {
    const out: {folder: FavouriteFolder; depth: number}[] = [];
    const walk = (parent: number | null, depth: number) => {
      for (const f of this.children(parent)) {
        out.push({folder: f, depth});
        walk(f.id, depth + 1);
      }
    };
    walk(null, 1);
    return out;
  }

  /** How many of `items` are in each folder with its folders (the key null: all of them). */
  counts(items: {folder_id: number | null}[]): Map<number | null, number> {
    const counts = new Map<number | null, number>([[null, items.length]]);
    for (const item of items) {
      for (const f of this.ancestry(this.known(item.folder_id))) counts.set(f.id, (counts.get(f.id) || 0) + 1);
    }
    return counts;
  }
}
