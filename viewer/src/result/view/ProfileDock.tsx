// The terrain profile of the selected line (or of two points measured on the map) in a dock: the
// old map's ProfilePanel (Profile.tsx) with the profile from model.profile (saved, computed before or
// asked from the server), a loading and an error state; the point under the cursor or the finger is
// shown on the map (ResultMap.hoverAt).

import { useEffect, useState } from "preact/hooks";
import { useMedia } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { ProfilePanel } from "../../Profile";
import type { Line } from "../../types";
import type { ResultModel } from "../model";
import type { ResultMap } from "../ResultMap";
import { cx, num, useModel } from "./common";

export interface ProfileDockProps {
  model: ResultModel;
  /** The map layer, for the point under the cursor. */
  view?: ResultMap | null;
  /** The line whose profile is shown (default: the selected one). */
  line?: Line | null;
  /** Two points measured on the map (RunActions' onMeasured): shown instead of the line while set. */
  measured?: Line | null;
  /** The measured profile was closed (the shell clears it and ResultMap.clearMeasure). */
  onCloseMeasured?(): void;
  /** The line's profile was closed (it opens again for the next line). */
  onClose?(): void;
  class?: string;
}

type State = {line: Line; status: "loading"} | {line: Line; status: "error"; error: string} |
  {line: Line; status: "ready"};

export function ProfileDock({model, view, line, measured, onCloseMeasured, onClose, class: cls}: ProfileDockProps) {
  useModel(model);
  const target = line === undefined ? model.selected?.line || null : line;
  const [state, setState] = useState<State | null>(null);
  const [closed, setClosed] = useState<Line | null>(null);
  const [rigging, setRigging] = useState(false);
  const touch = useMedia("(pointer: coarse)");

  useEffect(() => {
    if (!target) { setState(null); return; }
    if (target.prof) { setState({line: target, status: "ready"}); return; }
    let live = true;
    setState({line: target, status: "loading"});
    model.profile(target).then(() => { if (live) setState({line: target, status: "ready"}); },
                               (e) => { if (live) setState({line: target, status: "error", error: e.message}); });
    return () => { live = false; };
  }, [target]);
  useEffect(() => () => view?.hoverAt(null), [view]);

  const shown = measured || (target && closed !== target ? target : null);
  if (!shown) return null;
  const hover = (at: [number, number] | null) => view?.hoverAt(at);
  const close = () => {
    view?.hoverAt(null);
    if (measured) onCloseMeasured?.();
    else { setClosed(target); onClose?.(); }
  };
  const ready = measured || (state && state.line === target && state.status === "ready");
  return <section class={cx("r-dock", rigging && "r-rigging", cls)} aria-label="Profile">
    <div class="r-dock-bar">
      <b>{measured ? "Measured profile" : `Profile · ${num(shown.len)} m`}</b>
      <span class="u-small r-hint">{touch ? "tap or drag along the chart" : "move the mouse along the chart"}</span>
      <span class="u-grow" />
      {ready && <button type="button" class={cx("u-chip", rigging && "u-on")} aria-pressed={rigging}
                        onClick={() => setRigging(!rigging)}>Rigging and layers<Icon name="down" /></button>}
      {!ready && <button type="button" class="r-note-x" aria-label="Close the profile" onClick={close}>
        <Icon name="close" /></button>}
    </div>
    {ready ? <div class="r-profile">
      <ProfilePanel line={shown} measured={!!measured} model={model.data.model} slope={model.data.slope}
                    leashFall={model.data.leash_fall} rigging={model.data.rigging} onHover={hover} onClose={close} />
    </div> : state && state.status === "error"
      ? <p class="r-dock-msg r-error"><Icon name="warn" />No profile: {state.error}</p>
      : <p class="r-dock-msg" aria-busy="true"><span class="r-spin" />Computing the terrain profile…</p>}
  </section>;
}
