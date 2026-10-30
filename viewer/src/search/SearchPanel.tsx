// The search panel of the new interfaces: where (Point · Area · Along cliffs · File), the kind of
// lines (presets as chips, B, or cards, C) with the key fields, "More settings" by topic, and the
// live estimate with the start (EstimateBar; a shell may place it elsewhere, as C's bottom bar).
// UsageMeters shows the user's quotas. Styles in search.css (s-*), under the kit's .ui root.

import type { ComponentChildren } from "preact";
import { useState } from "preact/hooks";
import type { Field as SchemaField, Preset, Schema, SearchRequest, Usage, UsageItem } from "../app/api";
import { parseValue, withValue } from "../app/request";
import type { PlaceMap } from "../app/PlaceMap";
import { errorText, label, t } from "../i18n";
import { Button, Field, Progress, Range, RangePair, Segmented, StatusChip, type Option } from "../ui/components";
import { Icon } from "../ui/icons";
import { PlaceBox } from "./PlaceBox";
import { fmt, KEY_FIELDS, useSearch, withinLimits, type SearchMode, type SearchState } from "./useSearch";
import "./search.css";

const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

const MODES: Option<SearchMode>[] = [
  {value: "point", label: "Point", icon: "pin"}, {value: "area", label: "Area", icon: "poly"},
  {value: "cliffs", label: "Along cliffs", icon: "cliff"}, {value: "file", label: "File", icon: "file"},
];
const PHONE_MODES: Option<SearchMode>[] = MODES.map((m) => m.value === "cliffs" ? {...m, label: "Cliffs"} : m);

let ids = 0;
const useId = (prefix: string) => useState(() => `${prefix}-${++ids}`)[0];

const unitOf = (name: string) => t(`field.${name}.unit`);

/** The label of a field with its unit. */
const labelOf = (field: SchemaField) => label(field.name) + (unitOf(field.name) ? `, ${unitOf(field.name)}` : "");

/** A value with its unit as the hints write it ("30°", "5 %", "200 m"). */
const withUnit = (value: number, unit: string) => unit === "°" ? `${value}°` : unit ? `${value} ${unit}` : String(value);

/** The site's bounds in words (hint.bounds.*), "" without any; `factor`: at least this many times
 *  the grid size (--group-distance). */
function boundsText(low: number | null | undefined, high: number | null | undefined, unit: string,
                    factor?: number | null): string {
  if (factor) return t("hint.bounds.factor", {factor});
  const min = low ?? null, max = high ?? null;
  if (min !== null && max !== null) return t("hint.bounds.both", {min: withUnit(min, unit), max: withUnit(max, unit)});
  if (min !== null) return t("hint.bounds.min", {min: withUnit(min, unit)});
  if (max !== null) return t("hint.bounds.max", {max: withUnit(max, unit)});
  return "";
}

/** The "?" text of a field: its hint from the catalogue (or `key`'s) and the site's bounds. */
const hintOf = (field: SchemaField, key = `field.${field.name}.help`) =>
  [t(key), boundsText(field.min, field.max, unitOf(field.name), field.min_factor)].filter(Boolean).join(" ");

// ---- where -----------------------------------------------------------------------------------

function BufferField({s}: {s: SearchState}) {
  const field = s.byName.buffer;
  const id = useId("s-buffer");
  if (!field) return null;
  const max = Math.max(field.max ?? 1000, s.buffer);
  return <Field label={label("buffer")} value={`${s.buffer} m`} htmlFor={id} hint={hintOf(field)}>
    <Range id={id} label={label("buffer")} value={s.buffer} min={10} max={max} step={10} onInput={s.setBuffer} />
  </Field>;
}

function Where({s, phone}: {s: SearchState; phone?: boolean}) {
  const radiusField = s.byName.radius;
  const id = useId("s-where");
  const areas = s.shapes > 0 && <p class="s-hint s-areas">
    {s.areas === 1 ? "1 area" : `${s.areas} separate areas`}
    {s.shapes !== s.areas && ` (${s.shapes} shapes; those that overlap or touch are one area)`}</p>;
  return <div class="s-where">
    {s.mode !== "file" && <PlaceBox place={s.place} />}
    <Segmented label="Where" options={phone ? PHONE_MODES : MODES} value={s.mode} onChange={s.setMode} />
    {s.mode === "point" && <>
      <p class="s-hint s-point">{s.point ? <><Icon name="pin" />Centre {s.point[0].toFixed(5)}, {s.point[1].toFixed(5)}
        {" · "}click elsewhere to move it</> : "Click the map to set the centre of the search."}</p>
      {radiusField && <Field label={label("radius")} value={`${s.radius} m`} htmlFor={id} hint={hintOf(radiusField)}>
        <Range id={id} label={label("radius")} value={s.radius} min={50} step={50}
               max={Math.max(radiusField.max ?? 5000, s.radius)} onInput={s.setRadius} />
      </Field>}
    </>}
    {s.mode === "area" && <>
      <Segmented label="Area" value={s.areaKind} onChange={s.setAreaKind}
                 options={[{value: "draw", label: "Draw on the map"}, {value: "osm", label: "Named OSM area"}]} />
      {s.areaKind === "draw" ? <>
        <p class="s-hint">Draw polygons or rectangles with the tools on the map; drag their corners to change them.
          Both anchors of a line lie inside, the webbing may leave the area.</p>
        {areas}
        <BufferField s={s} />
      </> : <Field label={t("hint.area_name.label")} htmlFor={id} hint={t("hint.area_name.help")}>
        <input id={id} class="u-input" type="text" value={s.osmName} placeholder="e.g. Gozo"
               onInput={(e) => s.setOsmName(e.currentTarget.value)} />
      </Field>}
    </>}
    {s.mode === "cliffs" && <>
      <Segmented label="Along" value={s.coastKind} onChange={s.setCoastKind}
                 options={[{value: "cliffs", label: "Cliffs and crags"}, {value: "coast", label: "Coast"}]} />
      <Field label={label("within")} htmlFor={id}
             hint={s.byName.within ? hintOf(s.byName.within) : t("field.within.help")}>
        <div class="s-within">
          <input id={id} class="u-input" type="text" value={s.within} placeholder="OSM name or S,W,N,E"
                 onInput={(e) => s.setWithin(e.currentTarget.value)} />
          <Button onClick={() => s.setWithin(s.place.viewBox())}>Use the map view</Button>
        </div>
      </Field>
      <BufferField s={s} />
    </>}
    {s.mode === "file" && <>
      <Field label={t("hint.area_file.label")} htmlFor={id} hint={t("hint.area_file.help")}
             help={s.file && `${s.file.name}: ${s.file.areas.features.length} shapes`}>
        <input id={id} key={s.fileKey} class="s-file" type="file" accept=".geojson,.json,.kml,.gpx"
               onChange={(e) => s.readFile(e.currentTarget)} />
      </Field>
      {areas}
      <BufferField s={s} />
    </>}
  </div>;
}

// ---- presets and key fields ------------------------------------------------------------------

/** What a preset sets, in words. */
export function describePreset(preset: Preset, byName: Record<string, SchemaField>): string {
  if (!preset.values) return "your own values";
  const v = preset.values;
  const parts = [`${v.min_length}–${v.max_length} m`];
  const diff = byName.max_anchor_diff_m;
  if (v.max_anchor_diff_m !== undefined && (!diff || v.max_anchor_diff_m !== diff.default)) {
    parts.push(`anchors within ${v.max_anchor_diff_m} m of height`);
  }
  return parts.join(", ");
}

function Presets({s, variant}: {s: SearchState; variant: "b" | "c"}) {
  const presets = s.schema.presets;
  if (!presets.length) return null;
  const chosen = presets.find((p) => p.id === s.preset);
  const title = t("hint.presets.label");
  if (variant === "c") {
    return <Field label={title} hint={t("hint.presets.help")}><div class="s-cards" role="radiogroup" aria-label={title}>
      {presets.map((p) => <button type="button" key={p.id} role="radio" aria-checked={p.id === s.preset}
                                  class={cx("s-card", p.id === s.preset && "u-on")} disabled={!p.available}
                                  title={p.reason ?? undefined} onClick={() => s.applyPreset(p.id)}>
        <b>{p.label}</b><span>{p.available ? describePreset(p, s.byName) : p.reason}</span></button>)}
    </div></Field>;
  }
  return <Field label={title} hint={t("hint.presets.help")} help={chosen && (chosen.values ? describePreset(chosen, s.byName)
    : "Your own values: set them below or in More settings.")}>
    <div class="u-chips" role="radiogroup" aria-label={title}>
      {presets.map((p) => <button type="button" key={p.id} role="radio" aria-checked={p.id === s.preset}
                                  class={cx("u-chip", "s-preset", p.id === s.preset && "u-on")} disabled={!p.available}
                                  title={p.reason ?? describePreset(p, s.byName)} onClick={() => s.applyPreset(p.id)}>
        {p.id === s.preset && <Icon name="check" />}{p.label}</button>)}
    </div>
  </Field>;
}

function KeyFields({s}: {s: SearchState}) {
  const {byName} = s;
  const low = Number(s.value("min_length")), high = Number(s.value("max_length"));
  const diff = Number(s.value("max_anchor_diff_m"));
  const lengthMax = Math.max(byName.max_length?.max ?? 1000, high);
  const diffMax = Math.max(100, diff);
  const source = byName.source;
  const sourceId = useId("s-source");
  const changed = (name: string) => name in s.values;
  return <div class="s-keys">
    {byName.min_length && byName.max_length && <Field
      label={<span class={cx(changed("min_length") || changed("max_length") ? "s-changed" : "")}>{t("hint.length.label")}</span>}
      hintLabel={t("hint.length.label")}
      hint={[t("hint.length.help"), boundsText(byName.min_length.min, byName.max_length.max, "m")].filter(Boolean).join(" ")}
      value={`${low} – ${high} m`} help={s.problems.min_length && <span class="s-problem">{s.problems.min_length}</span>}>
      <RangePair label={t("hint.length.label")} low={low} high={high} min={5} max={lengthMax} step={5}
                 onInput={(a, b) => s.setValues(withLengths(s, a, b))} />
    </Field>}
    {byName.max_anchor_diff_m && <Field
      label={<span class={cx(changed("max_anchor_diff_m") && "s-changed")}>{label("max_anchor_diff_m")}</span>}
      hintLabel={label("max_anchor_diff_m")} hint={hintOf(byName.max_anchor_diff_m)} value={`≤ ${diff} m`}>
      <Range label={label("max_anchor_diff_m")} value={diff} min={0} max={diffMax} step={1}
             onInput={(v) => s.setValue("max_anchor_diff_m", v)} />
    </Field>}
    {source && <Field label={<span class={cx(changed("source") && "s-changed")}>{label("source")}</span>} htmlFor={sourceId}
                      hintLabel={label("source")} hint={hintOf(source)}>
      <select id={sourceId} class="u-input" value={String(s.value("source"))}
              onChange={(e) => s.setValue("source", e.currentTarget.value)}>
        {source.choices!.map((c) => <option value={c}>{source.choice_labels?.[c] ?? c}</option>)}
      </select>
    </Field>}
  </div>;
}

/** The changed values with a new length range (a value equal to the default is not kept). */
function withLengths(s: SearchState, low: number, high: number): Record<string, unknown> {
  return withValue(withValue(s.values, s.byName.min_length, low), s.byName.max_length, high);
}

// ---- more settings ---------------------------------------------------------------------------

function ParamInput({field, value, onChange, id, invalid}:
                    {field: SchemaField; value: unknown; onChange(v: unknown): void; id: string; invalid: boolean}) {
  const shown = value === undefined ? field.default : value;
  if (field.type === "boolean") {
    return <input id={id} type="checkbox" class="s-check" checked={!!shown} onChange={(e) => onChange(e.currentTarget.checked)} />;
  }
  if (field.type === "choice") {
    return <select id={id} class="u-input" value={shown === null || shown === undefined ? "" : String(shown)}
                   onChange={(e) => onChange(e.currentTarget.value === "" ? null : e.currentTarget.value)}>
      {field.optional && <option value="">(default)</option>}
      {field.choices!.map((c) => <option value={c}>{field.choice_labels?.[c] ?? c}</option>)}
    </select>;
  }
  const text = shown === null || shown === undefined ? "" : String(shown);
  const changed = (e: Event) => onChange(parseValue(field, (e.currentTarget as HTMLInputElement).value));
  // A number field keeps what is being typed (defaultValue, read when it is left: the page is drawn
  // again on every event of the queue); a new value from elsewhere (reset) gives it a new key
  return field.type === "number" || field.type === "integer"
    ? <input id={id} key={`${field.name}:${text}`} class={cx("u-input", invalid && "s-invalid")} type="number"
             step={field.type === "integer" ? 1 : "any"} defaultValue={text} min={field.min ?? undefined}
             max={field.max ?? undefined} placeholder={field.optional ? "default" : ""} onChange={changed}
             aria-invalid={invalid || undefined} />
    : <input id={id} class="u-input" type="text" value={text} placeholder={field.optional ? "default" : ""} onInput={changed} />;
}

function Setting({s, field}: {s: SearchState; field: SchemaField}) {
  const id = useId("s-f");
  const changed = field.name in s.values;
  const problem = s.problems[field.name];
  return <div class={cx("s-setting", changed && "s-is-changed")} data-field={field.name}>
    <Field htmlFor={id} label={<span class={cx(changed && "s-changed")}>{labelOf(field)}</span>}
           hintLabel={label(field.name)} hint={hintOf(field)}
           value={changed ? <button type="button" class="s-reset" title="Back to the default"
                                    onClick={() => {
                                      const next = {...s.values};
                                      delete next[field.name];
                                      s.setValues(next);
                                      // the button goes with the change: the focus to the field
                                      setTimeout(() => document.getElementById(id)?.focus());
                                    }}>
             reset</button> : undefined}
           help={problem && <span class="s-problem">{problem}</span>}>
      <ParamInput id={id} field={field} value={s.values[field.name]} invalid={!!problem}
                  onChange={(v) => s.setValue(field.name, v)} />
    </Field>
  </div>;
}

/** A topic of More settings: open at first when it is "lines" or has changed values, then as the user
 *  leaves it (a reset does not fold it under the pointer). */
function Topic({s, topic, title, fields}: {s: SearchState; topic: string; title: string; fields: SchemaField[]}) {
  const count = fields.filter((f) => f.name in s.values).length;
  const [open, setOpen] = useState(() => topic === "lines" || count > 0);
  return <details class="s-topic" open={open}
                  onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
    <summary><span class="u-grow">{title}</span>{count > 0 && <span class="u-badge">{count}</span>}</summary>
    <div class="s-topic-body">{fields.map((f) => <Setting key={f.name} s={s} field={f} />)}</div>
  </details>;
}

function MoreSettings({s, first}: {s: SearchState; first?: ComponentChildren}) {
  const [open, setOpen] = useState(false);
  const topics = Object.entries(s.schema.topics).filter(([t]) => t !== "where");
  const fields = s.schema.fields.filter((f) => f.topic && f.topic !== "where" && !KEY_FIELDS.includes(f.name));
  const changed = fields.filter((f) => f.name in s.values).length;
  return <div class="s-more">
    <button type="button" class="s-more-head" aria-expanded={open} onClick={() => setOpen(!open)}>
      <Icon name="filter" /><span class="u-grow">More settings · height, anchors, data, rigging model, output</span>
      {changed > 0 && <span class="u-badge">{changed} changed</span>}
      <span class="u-badge u-n">{fields.length}</span><Icon name={open ? "down" : "next"} />
    </button>
    {open && <div class="s-topics">
      {first && <div class="s-topic s-topic-body s-first">{first}</div>}
      {topics.map(([topic, title]) => {
        const these = fields.filter((f) => f.topic === topic);
        if (!these.length) return null;
        return <Topic key={topic} s={s} topic={topic} title={title} fields={these} />;
      })}
    </div>}
  </div>;
}

// ---- estimate and start ----------------------------------------------------------------------

/** The live estimate and the start: a box over a big "Find lines" button (B, phones) or one line
 *  with "Start search" (C's bottom bar). */
export function EstimateBar({search: s, usage, layout = "box", label}:
                            {search: SearchState; usage?: Usage | null; layout?: "box" | "bar"; label?: string}) {
  const {status, estimate, message} = s.estimate;
  const limit = withinLimits(usage ?? null, estimate, s.km2);
  const state = status === "ready" ? (limit && !limit.ok ? "warn" : "ok")
    : status === "refused" ? "bad" : status === "paused" || status === "unavailable" ? "warn" : "idle";
  const line = status === "incomplete" ? message
    : status === "waiting" || status === "loading" ? "Estimating…"
    : status === "paused" ? `Estimate paused: ${message}.${s.estimate.retrying ? " Trying again in a moment." : ""}`
    : status === "refused" ? message
    : status === "unavailable" ? `No estimate: ${message}`
    : limit ? limit.text : "Ready to search";
  const stats = estimate && <>
    {s.km2 !== null && <div class="s-stat"><b>{fmt(s.km2)} km²</b><span>area</span></div>}
    <div class="s-stat"><b>{estimate.source}</b><span>{estimate.resolution} m grid</span></div>
    <div class="s-stat"><b>~{fmt(estimate.download_mb)} MB</b><span>download</span></div>
    {estimate.tiles > 1 && <div class="s-stat"><b>{estimate.tiles}</b><span>tiles</span></div>}
  </>;
  const warnings = estimate ? [...estimate.reasons,
    ...estimate.messages.filter(([kind]) => kind === "warning")
      .map(([, text, info]) => info ? errorText(info, text) : text)] : [];
  const statusLine = <div class={cx("s-est-line", `s-${state}`)} role="status">
    <Icon name={state === "ok" ? "check" : state === "idle" ? (status === "incomplete" ? "info" : "clock") : "warn"} />
    <span class="u-grow">{line}</span>
    {(status === "paused" && !s.estimate.retrying || status === "unavailable") &&
      <button type="button" class="s-link" onClick={s.estimate.retry}>Try again</button>}
  </div>;
  const startText = label ?? (layout === "bar" ? "Start search" : "Find lines");
  const button = <Button kind="primary" large full={layout === "box"} disabled={!s.canStart} onClick={s.start}
                         iconRight={layout === "bar" ? "next" : undefined} class="s-start">
    {s.busy ? "Starting…" : startText}</Button>;
  if (layout === "bar") {
    return <div class={cx("s-bar", `s-${state}`)}>
      <StatusChip status={state === "ok" ? "done" : state === "bad" ? "failed" : state === "warn" ? "shared" : "waiting"}>
        {state === "ok" ? "Ready" : state === "bad" ? "Refused" : status === "loading" || status === "waiting"
          ? "Estimating" : state === "warn" ? "Check" : "Not ready"}</StatusChip>
      {stats}
      <span class="u-grow s-bar-line">{line}{warnings.length > 0 && ` · ${warnings.length} warning(s)`}</span>
      {s.error && <span class="s-problem">{s.error}</span>}
      {button}
    </div>;
  }
  return <div class="s-estimate">
    <div class={cx("s-est", `s-${state}`)}>
      {stats && <div class="s-est-row">{stats}</div>}
      {statusLine}
      {warnings.length > 0 && <ul class="s-warnings">{warnings.map((w) => <li>{w}</li>)}</ul>}
    </div>
    {s.error && <p class="s-problem" role="alert">{s.error}</p>}
    {button}
  </div>;
}

// ---- usage -----------------------------------------------------------------------------------

const METERS: Record<keyof Usage["usage"], [string, (v: number) => string, string]> = {
  km2: ["Area today", fmt, " km²"], runs_gb: ["Storage", fmt, " GB"], jobs_day: ["Searches today", String, ""],
  jobs_active: ["Waiting or running", String, ""], download_mb: ["Downloads today", fmt, " MB"],
  nominatim: ["Place lookups today", String, ""],
};

/** The user's quotas as meters ("Area today · 3.1 of 250 km²"); without quotas, what is used. */
export function UsageMeters({usage, items = ["km2", "runs_gb", "jobs_day", "jobs_active"], compact}:
                            {usage: Usage | null; items?: (keyof Usage["usage"])[]; compact?: boolean}) {
  if (!usage) return null;
  const meter = (key: keyof Usage["usage"], item: UsageItem) => {
    const [title, show, unit] = METERS[key];
    if (item.used === null) return null;
    const text = item.limit === null ? `${title} · ${show(item.used)}${unit}`
      : `${title} · ${show(item.used)} of ${show(item.limit)}${unit}`;
    return <div class="s-meter" key={key} data-usage={key}>
      {item.limit === null ? <span>{text}</span> : <Progress value={item.limit ? item.used / item.limit : 1} label={text} />}
    </div>;
  };
  return <div class={cx("s-meters", compact && "s-compact")}>
    {items.map((key) => meter(key, usage.usage[key]))}
    {!usage.limited && <span class="u-small s-nolimits">No daily limits</span>}
  </div>;
}

// ---- the panel -------------------------------------------------------------------------------

export interface SearchPanelProps {
  place: PlaceMap;
  schema: Schema;
  onStarted(jobId: string): void;
  initial?: SearchRequest | null;  // edit & run again
  variant: "b" | "c";              // presets as chips (B) or cards (C)
  /** The state from useSearch(), when the shell shows the EstimateBar elsewhere (C's bottom bar). */
  search?: SearchState;
  /** Show the estimate and the start inside the panel (default: yes). */
  estimate?: boolean;
  usage?: Usage | null;            // for "within your daily limit"
  phone?: boolean;                 // shorter mode labels
  footer?: ComponentChildren;      // after the panel's content (e.g. the user's searches)
}

/** The panel; with `search` it shows that state, otherwise it keeps its own. */
export function SearchPanel(props: SearchPanelProps) {
  return props.search ? <PanelBody {...props} s={props.search} /> : <OwnSearch {...props} />;
}

function OwnSearch(props: SearchPanelProps) {
  const s = useSearch({place: props.place, schema: props.schema, onStarted: props.onStarted, initial: props.initial});
  return <PanelBody {...props} s={s} />;
}

function PanelBody({s, variant, estimate = true, usage, phone, footer}: SearchPanelProps & {s: SearchState}) {
  const nameField = s.byName.name;
  const nameId = useId("s-name");
  // The run's name: under Where in C, in More settings in B (its panel stays short)
  const name = nameField && <Field label={label("name")} htmlFor={nameId} hint={hintOf(nameField)}>
    <input id={nameId} class="u-input" type="text" value={s.name} placeholder="from OpenStreetMap"
           onInput={(e) => s.setName(e.currentTarget.value)} />
  </Field>;
  return <div class={cx("s-panel", `s-${variant}`)}>
    {variant === "c" && <h2 class="u-h">Where</h2>}
    <Where s={s} phone={phone} />
    {variant === "c" && name}
    {variant === "c" && <h2 class="u-h s-gap">Lines</h2>}
    <Presets s={s} variant={variant} />
    <KeyFields s={s} />
    <MoreSettings s={s} first={variant === "b" && name} />
    <div class="s-row-end">
      <Button kind="ghost" icon="close" onClick={s.clear}>Clear the place</Button>
    </div>
    {estimate && <EstimateBar search={s} usage={usage} />}
    {footer}
  </div>;
}
