// "How a search works": the intro of the search panel (ui/Intro.tsx), once per browser. What the
// search does, the user's limits (/api/usage: the quotas and the limits of one search), why narrow
// settings are fast and wide ones give too much, and five examples. Texts in locales/en.json
// (intro.search.*); the labels of the fields come from the catalogue, so the examples name them as the
// form does.

import type { Usage } from "../app/api";
import { user } from "../app/api";
import { label, t } from "../i18n";
import { Intro, Rich, type IntroState } from "../ui/Intro";
import { fmt } from "./useSearch";

/** A number as the texts write it: 10000 → "10,000", 3.5 → "3.5". */
const n = (value: number) => Number(fmt(value)).toLocaleString("en");

/** The lines of "Your limits"; one line without any. */
export function limitLines(usage: Usage): string[] {
  if (!usage.limited) return [t(user.local ? "intro.search.limits_local" : "intro.search.limits_none")];
  const q = usage.usage, one = usage.search;
  const lines: string[] = [];
  const search: string[] = [];
  if (one?.area_km2 != null) {
    search.push(t("intro.search.limit_area", {area: n(one.area_km2)})
      + (one.radius_m != null ? " " + t("intro.search.limit_circle", {radius: n(one.radius_m / 1000)}) : ""));
  } else if (one?.radius_m != null) search.push(t("intro.search.limit_radius", {radius: n(one.radius_m / 1000)}));
  if (one?.max_length_m != null) search.push(t("intro.search.limit_length", {length: n(one.max_length_m)}));
  if (search.length || one?.bbox_km != null) {
    lines.push([search.length ? t("intro.search.limit_one", {parts: search.join(", ")}) : "",
                one?.bbox_km != null ? t("intro.search.limit_bbox", {bbox: n(one.bbox_km)}) : ""]
      .filter(Boolean).join(" "));
  }
  const day: string[] = [];
  if (q.jobs_day.limit != null) day.push(t("intro.search.limit_jobs", {count: q.jobs_day.limit}));
  if (q.km2.limit != null) day.push(t("intro.search.limit_km2", {km2: n(q.km2.limit)}));
  if (q.download_mb.limit != null) day.push(t("intro.search.limit_mb", {mb: n(q.download_mb.limit)}));
  if (day.length) lines.push(t("intro.search.limit_day", {hours: usage.hours, parts: day.join(", ")}));
  const hold: string[] = [];
  if (q.jobs_active.limit != null) hold.push(t("intro.search.limit_active", {count: q.jobs_active.limit}));
  if (q.runs_gb.limit != null) hold.push(t("intro.search.limit_gb", {gb: n(q.runs_gb.limit)}));
  if (hold.length) lines.push(hold.join(" "));
  lines.push(t("intro.search.limit_meters"));
  return lines;
}

/** The labels the examples name, in <b> (Rich). */
function names(): Record<string, string> {
  return {
    length: t("hint.length.label"), diff: label("max_anchor_diff_m"), prom: label("min_prominence"),
    span: label("prominence_span"), height: label("min_height"), share: label("min_high_share"),
    highAbove: label("high_above"), cliffs: t("intro.search.mode_cliffs"), coast: t("intro.search.coast"),
    angle: label("max_wall_angle"), drop: label("min_drop"), preset: t("preset.short"), nmpt: label("nmpt"),
    reject: t("choice.nmpt.reject"),
  };
}

export function SearchIntro({state, usage}: {state: IntroState; usage: Usage | null}) {
  const values = names();
  return <Intro state={state} title={t("intro.search.title")} class="n-intro-search">
    <p><Rich text={t("intro.search.what")} /></p>
    <h3>{t("intro.search.limits")}</h3>
    {!usage ? <p class="u-small">{t("intro.search.limits_loading")}</p>
      : usage.limited ? <ul class="n-limits">{limitLines(usage).map((line) => <li>{line}</li>)}</ul>
      : <p class="n-limits">{limitLines(usage)[0]}</p>}
    <p><Rich text={t("intro.search.narrow")} /></p>
    <p><Rich text={t("intro.search.wide")} /></p>
    <h3>{t("intro.search.examples")}</h3>
    <ul class="n-examples">
      {["long", "needle", "exposure", "sea", "forest"].map((k) =>
        <li key={k}><Rich text={t(`intro.search.example_${k}`, values)} /></li>)}
    </ul>
  </Intro>;
}
