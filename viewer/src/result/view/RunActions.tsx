// The actions of a run: Measure (two points on the map; the profile opens in ProfileDock), Compare
// (other runs nearby shown with this one), Share (the public site: a link shown once) and Download
// (the run's lines.csv and lines.geojson, which web.RUN_FILES serves).

import { useEffect, useState } from "preact/hooks";
import { Button, IconButton } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { api, user, type RunListed } from "../../app/api";
import { listOthers, loadLines, NEARBY_KM, colorOf } from "../../others";
import type { Line, RunInfo } from "../../types";
import { loadRunList } from "../load";
import type { ResultModel } from "../model";
import type { ResultMap } from "../ResultMap";
import { LinkField } from "./LineDetails";
import { Popover, Swatch, useModel } from "./common";

export interface RunActionsProps {
  model: ResultModel;
  view?: ResultMap | null;
  /** A measured line (or null when measuring starts again or fails): the shell shows it in ProfileDock. */
  onMeasured?(line: Line | null): void;
  /** Buttons with icons only (phones). */
  compact?: boolean;
  /** Which actions (default all that can work here). */
  show?: ("measure" | "compare" | "share" | "download")[];
  /** The folder of the runs' files (default "/runs/"). */
  base?: string;
}

const runFile = (base: string, id: string, file: string) => base + encodeURIComponent(id) + "/" + file;

/** Measure: the next two clicks on the map; the profile between them is computed by the server. */
function Measure({model, view, onMeasured, compact}: RunActionsProps) {
  const [measuring, setMeasuring] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!measuring) return;
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setMeasuring(false); };  // ResultMap cancels too
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [measuring]);
  if (!view || !model.canCompute) return null;
  const toggle = () => {
    if (measuring) { view.cancelMeasure(); view.clearMeasure(); setMeasuring(false); return; }
    setError("");
    onMeasured?.(null);
    setMeasuring(true);
    view.startMeasure((a, b) => {
      setMeasuring(false);
      model.measure([a.lat, a.lng], [b.lat, b.lng]).then((l) => onMeasured?.(l), (e) => {
        view.clearMeasure();
        setError("Not computed: " + e.message);
      });
    });
  };
  const label = measuring ? "Cancel measuring" : "Measure";
  return <span class="r-act">
    {compact ? <IconButton icon="ruler" label={label} on={measuring} onClick={toggle} />
      : <Button icon="ruler" class={measuring ? "r-on" : ""} aria-pressed={measuring} onClick={toggle}>
          {measuring ? "Cancel" : "Measure"}</Button>}
    {measuring && <span class="r-tip u-small">Click two points on the map</span>}
    {error && <span class="r-tip r-error u-small" role="alert">{error}</span>}
  </span>;
}

interface OtherItem { run: RunInfo; km: number; status: "off" | "loading" | "on" | "error" }

/** Compare: other runs with lines, nearest first (the ones within NEARBY_KM unfolded). */
function Compare({model, view, compact, base = "/runs/"}: RunActionsProps) {
  const [items, setItems] = useState<OtherItem[] | null>(null);
  const load = () => {
    if (items) return;
    loadRunList(base + "runs.js").then((runs) => {
      const b = view ? view.areaBounds() : null;
      const box = b ? [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]
        : model.data.run.bbox || [model.data.center[1], model.data.center[0], model.data.center[1], model.data.center[0]];
      setItems(listOthers(runs.filter((r) => r.has_map !== false), model.runId, box)
        .map(({run, km}) => ({run, km, status: model.others.has(run.id) ? "on" : "off"})));
    });
  };
  const change = (item: OtherItem, status: OtherItem["status"]) =>
    setItems((all) => all && all.map((i) => i.run.id === item.run.id ? {...i, status} : i));
  const toggle = (item: OtherItem) => {
    if (item.status === "on" || item.status === "loading") { model.removeRun(item.run.id); change(item, "off"); return; }
    change(item, "loading");
    loadLines(item.run.id, base).then((lines) => { model.addRun(item.run, lines); change(item, "on"); },
                                      () => change(item, "error"));
  };
  const row = (item: OtherItem) => <label class="r-check r-other" key={item.run.id} title={item.run.description}>
    <input type="checkbox" checked={item.status === "on" || item.status === "loading"} onChange={() => toggle(item)} />
    <Swatch color={colorOf(item.run.id)} />
    <span class="u-grow">{item.run.name}<span class="u-small"> · {item.run.created.slice(0, 10)} · {item.km < 1 ? "here"
      : num0(item.km) + " km"}{item.status === "loading" ? " · loading…" : item.status === "error" ? " · no lines" : ""}</span></span>
  </label>;
  return <Popover label="Compare with other runs" align="right"
                  button={(open, toggleOpen) => compact
                    ? <IconButton icon="compare" label="Compare" on={open} onClick={() => { load(); toggleOpen(); }} />
                    : <Button icon="compare" aria-expanded={open} onClick={() => { load(); toggleOpen(); }}>Compare</Button>}>
    {() => {
      if (!items) return <p class="u-small">Loading the runs…</p>;
      if (!items.length) return <p class="u-small">No other runs with lines.</p>;
      const near = items.filter((i) => i.km <= NEARBY_KM), far = items.filter((i) => i.km > NEARBY_KM);
      return <div class="r-others">
        <b class="u-h4">Show other runs with this one</b>
        {near.length ? near.map(row) : <p class="u-small">None within {NEARBY_KM} km.</p>}
        {far.length > 0 && <details open={far.some((i) => i.status !== "off")}>
          <summary class="u-small">Farther away ({far.length})</summary>{far.map(row)}</details>}
      </div>;
    }}
  </Popover>;
}

const num0 = (v: number) => v.toFixed(0);

/** Share (the public site, the owner's runs): a link that opens the run's map for anyone who has it. */
function Share({model, compact}: RunActionsProps) {
  return <ShareRun runId={model.runId} compact={compact} />;
}

export interface ShareRunProps {
  runId: string;
  /** The run as /api/runs lists it, when the caller has it (else it is asked for). */
  run?: RunListed | null;
  /** Buttons with icons only. */
  compact?: boolean;
  /** Which side the popover opens to (default right). */
  align?: "left" | "right";
  /** The run was shared or its sharing stopped. */
  onChange?(shared: boolean): void;
}

/** Share of one run without its result (e.g. a card of a list of runs): nothing unless the run is the
 *  user's own on the public site. */
export function ShareRun({runId, run: given, compact, align = "right", onChange}: ShareRunProps) {
  const [run, setRun] = useState<RunListed | null>(null);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (user.local) return;
    if (given !== undefined) { setRun(given && given.own && given.has_map ? given : null); return; }
    api.runs().then((r) => setRun(r.public ? r.runs.find((x) => x.id === runId && x.own && x.has_map) || null
      : null), () => {});
  }, [runId, given]);
  if (!run || user.local) return null;
  const changed = (shared: boolean) => { setRun({...run, shared}); onChange?.(shared); };
  const share = () => api.share(run.id).then((r) => { setUrl(r.url); setError(""); changed(true); },
                                             (e) => setError(e.message));
  const stop = () => api.unshare(run.id).then(() => { setUrl(""); setError(""); changed(false); },
                                              (e) => setError(e.message));
  return <Popover label="Share" align={align}
                  button={(open, toggle) => compact ? <IconButton icon="share" label="Share" on={open} onClick={toggle} />
                    : <Button icon="share" aria-expanded={open} onClick={toggle}>Share</Button>}>
    <div class="r-share">
      <b class="u-h4">Share this run</b>
      <p class="u-small">A link opens the run's map for anyone who has it. It is shown once: copy it now.</p>
      {url && <LinkField text={url} />}
      <div class="u-row">
        <Button kind={run.shared ? "default" : "primary"} icon="link" onClick={share}>
          {run.shared ? "New link" : "Make a link"}</Button>
        {run.shared && <Button kind="danger" onClick={stop}>Stop sharing</Button>}
      </div>
      {run.shared && !url && <p class="u-small">Shared by a link. A new link replaces the old one.</p>}
      {error && <p class="r-error u-small" role="alert">{error}</p>}
    </div>
  </Popover>;
}

/** Download: the lines of the run as CSV and GeoJSON. */
function Download({model, compact, base = "/runs/"}: RunActionsProps) {
  const id = model.runId;
  return <Popover label="Download" align="right"
                  button={(open, toggle) => compact ? <IconButton icon="download" label="Download" on={open} onClick={toggle} />
                    : <Button icon="download" aria-expanded={open} onClick={toggle}>Download</Button>}>
    <div class="r-downloads">
      <b class="u-h4">Download the lines</b>
      <a class="u-btn" href={runFile(base, id, "lines.csv")} download={`${id}.csv`}><Icon name="download" />
        CSV <span class="u-small">table, every line</span></a>
      <a class="u-btn" href={runFile(base, id, "lines.geojson")} download={`${id}.geojson`}><Icon name="download" />
        GeoJSON <span class="u-small">for GIS and map apps</span></a>
    </div>
  </Popover>;
}

export function RunActions(props: RunActionsProps) {
  useModel(props.model);
  const show = props.show || ["measure", "compare", "share", "download"];
  return <div class="r-actions">
    {show.includes("measure") && <Measure {...props} />}
    {show.includes("compare") && <Compare {...props} />}
    {show.includes("share") && <Share {...props} />}
    {show.includes("download") && <Download {...props} />}
  </div>;
}
