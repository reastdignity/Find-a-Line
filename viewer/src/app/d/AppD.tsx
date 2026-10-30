// Prototype D (?ui=d): C's home, new search, favourites and account (ShellC and its pages, app/c) with
// B's way of working with a finished run (result/view/ResultWorkspace, shared with B) on its own
// full-screen map (result/view/RunOnMap, shared with a run's map.html): the pill "‹ Home", Edit & run
// again and the run's actions on top, the panel on the right with the list and the selected line's
// details, the profile docked at the bottom; on phones C's bottom navigation and B's sheet ("Spot k of
// N" with ‹ ›). The selected line is kept in C's hash (#run=<id>&line=…, replaced), and a copied link
// opens it in D.

import { Button } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { uiHref, type UiChoice } from "../../ui/choice";
import type { Line } from "../../types";
import type { ResultModel } from "../../result/model";
import { orthoForApp } from "../../result/load";
import { RunOnMap } from "../../result/view/RunOnMap";
import { ShellC, type ResultPageProps } from "../c/AppC";
import { editHash, go, runHash } from "../c/route";
import "../c/c.css";

const TRIES: UiChoice[] = ["b", "c"];

/** The link "Copy link" gives: this page in D with the line's hash, whichever interface is remembered. */
const linkOf = (model: ResultModel, l: Line) => location.origin + uiHref("d") + runHash(model.runOf(l), model.lineLink(l));
const back = {label: "Home", text: true, onClick: () => go("#home")};

/** The result on its own map, with Edit & run again. */
function ResultD({id, line, nav, editJob, wide}: ResultPageProps) {
  const actions = (compact: boolean) => editJob && (compact
    ? <a class="u-ib u-round" href={editHash(editJob)} aria-label="Edit & run again" title="Edit & run again">
        <Icon name="edit" /></a>
    : <Button icon="edit" href={editHash(editJob)}>Edit &amp; run again</Button>);
  return <RunOnMap runId={id} line={line} nav={nav} wide={wide} back={back} hashOf={runHash} linkOf={linkOf}
                   actions={actions} ortho={(data) => orthoForApp(data.ortho, data.run.id)} />;
}

export function AppD() {
  return <ShellC ui="u-app-d d-app" tries={TRIES} result={(props) => <ResultD key={props.id} {...props} />} />;
}
