// C's home (c1, c4 on phones): "New search", an example run, the user's searches as cards (the jobs of
// useJobs merged with the saved runs: status, progress, mini map, a menu), the favourite lines as a
// trip list and the usage meters aside.

import { useEffect, useRef, useState } from "preact/hooks";
import { Button, Chip, Chips, IconButton, Panel, Progress, StatusChip } from "../../ui/components";
import { Icon } from "../../ui/icons";
import { LinkField, Popover } from "../../result/view";
import { forgetRun } from "../../result/load";
import { UsageMeters, type UsageState } from "../../search";
import { api, type RunListed } from "../api";
import { jobProgress, type JobsState, type JobView } from "../useJobs";
import { editHash, go, runHash } from "./route";
import { Thumb, type ThumbSource } from "./Thumb";
import { Trip } from "./Trip";

export interface RunsState {
  runs: RunListed[] | null;
  public: boolean;
  error: string;
  reload(): void;
  change(id: string, change: Partial<RunListed> | null): void;  // null: removed
}

/** A card: a job, its run, or both (a finished job's run). */
export interface Card {
  key: string;
  job?: JobView;
  run?: RunListed;
  created: string;
}

/** The jobs and the user's own runs as cards, newest first. */
export function cardsOf(jobs: JobView[], runs: RunListed[], publicSite: boolean): Card[] {
  const own = runs.filter((r) => r.own || !publicSite);
  const byId = new Map(own.map((r) => [r.id, r]));
  const used = new Set<string>();
  const cards: Card[] = jobs.map((job) => {
    const run = job.run_id ? byId.get(job.run_id) : undefined;
    if (run) used.add(run.id);
    return {key: "job:" + job.id, job, run, created: run?.created || job.created};
  });
  for (const run of own) if (!used.has(run.id)) cards.push({key: "run:" + run.id, run, created: run.created});
  return cards.sort((a, b) => b.created.localeCompare(a.created));
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "27 Oct 2026, 10:41" from an ISO time (as written, no time zone arithmetic). */
export function when(iso: string): string {
  const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)/.exec(iso);
  if (!m) return iso.slice(0, 16);
  return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}, ${m[4]}:${m[5]}`;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

type CardState = {status: "running" | "waiting" | "done" | "failed" | "shared"; chip: string; meta: string;
                  fraction: number | null; step: string; unfinished: boolean};

function stateOf(card: Card): CardState {
  const {job, run} = card;
  if (job && job.status !== "done") {
    const p = jobProgress(job);
    if (p.state === "failed") {
      return {status: "failed", chip: job.status === "cancelled" ? "cancelled" : "failed", unfinished: false,
              meta: job.error || (job.status === "cancelled" ? "Cancelled" : "Failed"), fraction: null, step: ""};
    }
    const pct = p.fraction === null ? "" : ` · ${Math.round(p.fraction * 100)}%`;
    return {status: p.state === "running" ? "running" : "waiting", unfinished: true,
            chip: p.state === "running" ? "running" + pct : "waiting",
            meta: p.state === "running" ? p.label : job.status === "ready" ? p.label : "waiting in the queue",
            fraction: p.fraction, step: p.step};
  }
  if (run) {
    return {status: run.shared ? "shared" : "done", chip: run.shared ? "shared" : "done", unfinished: false,
            meta: `${plural(run.summary.spots, "spot", "spots")} · ${plural(run.summary.lines, "line", "lines")}`,
            fraction: null, step: ""};
  }
  return {status: "done", chip: "done", meta: job?.run_id ? "Saved" : "Finished", fraction: null, step: "",
          unfinished: false};
}

function thumbOf(card: Card): ThumbSource {
  if (card.run && card.run.has_map) return {kind: "run", id: card.run.id, lines: card.run.summary.lines};
  if (card.job) return {kind: "job", id: card.job.id};
  return {kind: "none"};
}

/** A search's card: the mini map and the name open a finished one; the menu edits, shares, deletes. */
function SearchCard({card, runs, jobs, publicSite, highlight}:
                    {card: Card; runs: RunsState; jobs: JobsState; publicSite: boolean; highlight: boolean}) {
  const {job, run} = card;
  const st = stateOf(card);
  const [panel, setPanel] = useState<"" | "share" | "delete">("");
  const [menu, setMenu] = useState(false);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const ref = useRef<HTMLElement>(null);
  useEffect(() => { if (highlight) ref.current?.scrollIntoView?.({block: "nearest"}); }, [highlight]);
  const open = run && run.has_map ? runHash(run.id) : null;
  const name = run?.name || job?.title || "Search";
  const canShare = publicSite && !!run && run.own && run.has_map;
  const canDelete = publicSite && !!run && run.own;
  const act = (then: () => void) => () => { setMenu(false); setError(""); then(); };
  const share = () => api.share(run!.id).then((r) => { setUrl(r.url); runs.change(run!.id, {shared: true}); },
                                              (e) => setError(e.message));
  const unshare = () => api.unshare(run!.id).then(() => { setUrl(""); runs.change(run!.id, {shared: false}); },
                                                  (e) => setError(e.message));
  const remove = () => api.deleteRun(run!.id).then(() => { forgetRun(run!.id); runs.change(run!.id, null); },
                                                   (e) => setError(e.message));
  const cancel = () => api.cancel(job!.id).then(() => jobs.refresh(), (e) => setError(e.message));
  const items = [
    open && <a key="open" class="c-menu-item" href={open}><Icon name="layers" />Open the result</a>,
    job && <a key="edit" class="c-menu-item" href={editHash(job.id)}><Icon name="edit" />Edit &amp; run again</a>,
    canShare && <button key="share" type="button" class="c-menu-item" onClick={act(() => setPanel("share"))}>
      <Icon name="share" />Share…</button>,
    job && st.unfinished && <button key="cancel" type="button" class="c-menu-item" onClick={act(cancel)}>
      <Icon name="close" />Cancel the search</button>,
    canDelete && <button key="delete" type="button" class="c-menu-item c-danger" onClick={act(() => setPanel("delete"))}>
      <Icon name="close" />Delete…</button>,
  ].filter(Boolean);

  const body = <>
    <span class="c-card-name">{name}</span>
    <span class="u-small c-card-meta">{st.meta}{!st.unfinished && ` · ${when(card.created)}`}</span>
  </>;
  return <article ref={ref} class={`c-card${highlight ? " c-card-new" : ""}`} data-job={job?.id} data-run={run?.id}
                  data-status={st.status}>
    {open ? <a class="c-card-thumb" href={open} tabIndex={-1} aria-hidden="true"><Thumb source={thumbOf(card)} /></a>
      : <div class="c-card-thumb"><Thumb source={thumbOf(card)} /></div>}
    <div class="c-card-body">
      <div class="c-card-head">
        <StatusChip status={st.status}>{st.chip}</StatusChip>
        {run?.demo && <span class="u-badge u-n" title="an example: its map is open to everyone">example</span>}
        <span class="u-grow" />
        {items.length > 0 && <Popover label="Search actions" align="right" open={menu} onOpen={setMenu}
                                      button={(on, toggle) => <IconButton icon="more" label="Actions" plain on={on}
                                                                          class="c-card-menu" onClick={toggle} />}>
          <div class="c-menu" role="menu">{items}</div>
        </Popover>}
      </div>
      {open ? <a class="c-card-open" href={open}>{body}</a> : <div class="c-card-open">{body}</div>}
      {st.unfinished && <Progress value={st.status === "running" ? st.fraction : null} label={st.step || undefined} />}
      {panel === "share" && run && <div class="c-card-panel">
        <span class="u-small">A link opens this run's map for anyone who has it. It is shown once: copy it now.</span>
        {url && <LinkField text={url} />}
        <div class="u-row">
          <Button kind={run.shared ? "default" : "primary"} icon="link" onClick={share}>
            {run.shared ? "New link" : "Make a link"}</Button>
          {run.shared && <Button kind="danger" onClick={unshare}>Stop sharing</Button>}
          <Button kind="ghost" onClick={() => { setPanel(""); setUrl(""); }}>Close</Button>
        </div>
      </div>}
      {panel === "delete" && run && <div class="c-card-panel c-confirm" role="alertdialog" aria-label="Delete the run">
        <span>Delete “{run.name}” and its files? This cannot be undone.</span>
        <div class="u-row">
          <Button kind="danger" class="c-confirm-delete" onClick={remove}>Delete</Button>
          <Button kind="ghost" onClick={() => setPanel("")}>Keep it</Button>
        </div>
      </div>}
      {error && <p class="c-error u-small" role="alert">{error}</p>}
    </div>
  </article>;
}

type Show = "all" | "running" | "shared";

export interface HomeProps {
  jobs: JobsState;
  runs: RunsState;
  usage: UsageState;
  highlight: string;
  wide: boolean;
}

export function Home({jobs, runs, usage, highlight, wide}: HomeProps) {
  const [show, setShow] = useState<Show>("all");
  useEffect(() => { runs.reload(); usage.refresh(); }, []);
  const listed = runs.runs || [];
  const cards = cardsOf(jobs.jobs, listed, runs.public);
  const running = cards.filter((c) => stateOf(c).unfinished);
  const shared = cards.filter((c) => c.run?.shared);
  const shown = show === "running" ? running : show === "shared" ? shared : cards;
  const example = runs.public ? listed.find((r) => r.demo && r.has_map)
    : listed.filter((r) => r.has_map).sort((a, b) => b.created.localeCompare(a.created))[0];

  return <div class="c-home">
    <div class="c-home-main">
      {!jobs.worker && <p class="c-error c-no-worker" role="alert">The worker is not running: searches wait in the
        queue until it starts (<code>findaline.py worker</code>, or <code>findaline.py web</code> without
        --no-worker).</p>}
      {jobs.error && <p class="c-error">{jobs.error}</p>}
      {runs.error && <p class="c-error">{runs.error}</p>}
      <div class="c-starts">
        <a class="c-new" href="#new">
          <span class="c-new-icon"><Icon name="plus" /></span>
          <span><span class="u-h c-new-title">New search</span>
            {wide && <span class="c-new-text">Click a place, draw an area, or follow cliffs and coasts.</span>}</span>
        </a>
        {example && wide && <a class="c-example" href={runHash(example.id)} data-run={example.id}>
          <span class="c-example-icon"><Icon name="rope" /></span>
          <span><b>{runs.public ? "Explore an example" : "Open the latest result"}</b>
            <span class="u-small">{example.name} · {plural(example.summary.lines, "line", "lines")}</span></span>
        </a>}
      </div>
      <div class="c-section-head">
        <h1 class="u-h">Your searches</h1>
        <span class="u-small">{cards.length}</span>
        <span class="u-grow" />
        {wide ? <Chips>
          <Chip on={show === "all"} onClick={() => setShow("all")}>All</Chip>
          <Chip on={show === "running"} onClick={() => setShow("running")}>Running{running.length ? ` · ${running.length}` : ""}</Chip>
          <Chip on={show === "shared"} onClick={() => setShow("shared")}>Shared{shared.length ? ` · ${shared.length}` : ""}</Chip>
        </Chips> : <select class="u-input c-show" aria-label="Show" value={show}
                           onChange={(e) => setShow(e.currentTarget.value as Show)}>
          <option value="all">All</option><option value="running">Running</option><option value="shared">Shared</option>
        </select>}
      </div>
      {runs.runs === null ? <p class="u-small">Loading…</p> : shown.length === 0
        ? <p class="u-small c-empty">{show === "all" ? "No searches yet: start one with New search." :
            show === "running" ? "Nothing is running." : "No shared runs."}</p>
        : <div class="c-cards">{shown.map((c) =>
            <SearchCard key={c.key} card={c} runs={runs} jobs={jobs} publicSite={runs.public}
                        highlight={!!highlight && c.job?.id === highlight} />)}</div>}
      {!wide && example && <a class="c-example c-example-phone" href={runHash(example.id)} data-run={example.id}>
        <span class="c-example-icon"><Icon name="rope" /></span>
        <span><b>{runs.public ? "Explore an example" : "Open the latest result"}</b>
          <span class="u-small">{example.name}</span></span></a>}
    </div>
    {wide && <aside class="c-home-aside">
      <Panel title="Favourite lines" class="c-favs"><Trip limit={8} /></Panel>
      {usage.usage && <Panel title="Today" class="c-usage"><UsageMeters usage={usage.usage} /></Panel>}
    </aside>}
  </div>;
}
