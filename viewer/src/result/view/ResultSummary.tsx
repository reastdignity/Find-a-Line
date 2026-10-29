// The head of a result: "31 spots · 48 lines", how many lines the filters hide, the legend of the
// line colours (style.ts) and the model's note (a link to a line, a favourite not saved).

import { Button } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { COLORS } from "../../style";
import type { ResultModel } from "../model";
import { Swatch, useModel } from "./common";

export interface ResultSummaryProps {
  model: ResultModel;
  /** Show the legend (default true). */
  legend?: boolean;
  /** Show model.note with its action (default true). */
  note?: boolean;
}

/** The note of the model (model.note) with its action. */
export function ResultNote({model}: {model: ResultModel}) {
  useModel(model);
  const note = model.note;
  if (!note) return null;
  return <div class="r-note" role="status"><Icon name="info" /><span class="u-grow">{note.text}</span>
    {note.action && <Button kind="ghost" onClick={() => note.onAction?.()}>{note.action}</Button>}
    <button type="button" class="r-note-x" aria-label="Close" onClick={() => model.setNote(null)}>
      <Icon name="close" /></button>
  </div>;
}

export function ResultSummary({model, legend = true, note = true}: ResultSummaryProps) {
  useModel(model);
  const {spots, lines} = model.shown, total = model.all.length, hidden = total - lines;
  const grouped = model.state.group;
  return <div class="r-summary">
    <div class="r-summary-count">
      <span class="u-h">{grouped ? `${spots} ${spots === 1 ? "spot" : "spots"}` : `${lines} ${lines === 1 ? "line" : "lines"}`}</span>
      <span class="u-small">{grouped && `${lines} ${lines === 1 ? "line" : "lines"}`}
        {grouped && hidden > 0 && " · "}{hidden > 0 && `${hidden} hidden by filters`}</span>
    </div>
    {legend && <div class="r-legend">
      <span><Swatch color={COLORS.clean} />Clear</span>
      {model.nOver > 0 && <span><Swatch color={COLORS.over} />Over trees or buildings</span>}
      {model.favouritesOn && <span><Swatch color={COLORS.favourite} class="r-sw-fav" />Favourite</span>}
      {Array.from(model.others.values()).map((r) =>
        <span key={r.info.id} title={r.info.description}><Swatch color={r.color} />{r.info.name}</span>)}
    </div>}
    {note && <ResultNote model={model} />}
  </div>;
}
