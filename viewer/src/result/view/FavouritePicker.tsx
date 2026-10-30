// The favourite button of a line's details with its folders (as a browser's "bookmark this page"): a
// click opens the user's folders; picking one marks the line there, or moves it there when it is a
// favourite already; "New folder" makes one in the folder highlighted and saves the line into it;
// "Remove" unmarks it. The list's star (LinesTable) adds to the last folder used without asking.

import { useState } from "preact/hooks";
import { Button } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { FOLDER_DEPTH_MAX } from "../../folders";
import type { Line } from "../../types";
import type { ResultModel } from "../model";
import { cx, Popover, useModel } from "./common";

export function FavouritePicker({model, line}: {model: ResultModel; line: Line}) {
  useModel(model);
  const [open, setOpen] = useState(false);
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const tree = model.tree, folder = model.folderOf(line), favourite = folder !== undefined;
  const here = favourite ? folder : model.lastFolder;  // highlighted: where it is, or where it would go
  const where = favourite ? (folder === null ? "Favourites" : tree.path(folder)) : "";

  const pick = (to: number | null) => {
    setOpen(false);
    setNaming(false);
    model.saveTo(line, to);
  };
  const create = async () => {
    const parent = here !== null && tree.depth(here) < FOLDER_DEPTH_MAX ? here : null;
    if (!name.trim() || busy) return;
    setBusy(true);
    const id = await model.newFolder(name, parent);
    setBusy(false);
    if (id === null) return;
    setName("");
    pick(id);
  };
  const remove = () => {
    setOpen(false);
    if (favourite) model.toggleFavourite(line);
  };
  const row = (id: number | null, label: string, depth: number) =>
    <li key={id ?? "top"}>
      <button type="button" class={cx("r-folder", id === here && "u-on")} aria-current={id === folder ? "true" : undefined}
              style={{paddingLeft: `${8 + 14 * depth}px`}} onClick={() => pick(id)}>
        <Icon name={id === null ? "star" : "folder"} /><span class="u-grow">{label}</span>
        {id === folder && <Icon name="check" />}</button></li>;

  return <Popover label="Favourite folders" open={open} onOpen={(v) => { setOpen(v); if (!v) setNaming(false); }}
                  button={(on, toggle) =>
                    <Button class={cx("r-fav", favourite && "r-on")} aria-expanded={on} onClick={toggle}
                            title={favourite ? `In ${where}` : "Add to favourites"}>
                      <Icon name="star" fav={favourite} />{favourite ? "Favourite" : "Add to favourites"}
                      <Icon name="down" /></Button>}>
    {() => <div class="r-picker">
      <b class="u-h4">{favourite ? "Move to a folder" : "Save to a folder"}</b>
      <ul class="r-folders" role="list">
        {row(null, "Favourites (top level)", 0)}
        {tree.flat().map(({folder: f, depth}) => row(f.id, f.name, depth))}
      </ul>
      {naming
        ? <form class="r-newfolder" onSubmit={(e) => { e.preventDefault(); create(); }}>
            <input class="u-input" value={name} placeholder="Folder name" aria-label="New folder's name" maxLength={100}
                   ref={(el) => el?.focus()} onInput={(e) => setName(e.currentTarget.value)}
                   onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setNaming(false); } }} />
            <Button kind="primary" type="submit" disabled={!name.trim() || busy}>Create</Button>
          </form>
        : <Button kind="ghost" icon="plus" onClick={() => setNaming(true)}>
            New folder{here !== null ? ` in ${tree.byId.get(here)?.name}` : ""}</Button>}
      {favourite && <Button kind="danger" class="r-unfav" onClick={remove}>Remove from favourites</Button>}
    </div>}
  </Popover>;
}
