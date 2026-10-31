// Times of the server (jobs, runs, users, reports) in the viewer's own time zone. The server stores
// them in UTC with their offset ("2026-10-31T06:17:52+00:00"); older ones have no offset and are
// the server's local time, read here as the browser's (the same zone for the local app).

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const pad = (n: number) => String(n).padStart(2, "0");

/** The moment of a stored time; null when there is none or it cannot be read. */
export function instant(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const date = new Date(iso.trim().replace(" ", "T"));
  return isNaN(date.getTime()) ? null : date;
}

/** Milliseconds of a stored time, for comparing and sorting (0 when unknown). */
export function timeOf(iso: string | null | undefined): number {
  return instant(iso)?.getTime() ?? 0;
}

/** "31 Oct 2026, 07:17" in the viewer's time zone; `empty` when there is no time. */
export function when(iso: string | null | undefined, empty = ""): string {
  const d = instant(iso);
  if (!d) return iso || empty;
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "2026-10-31" in the viewer's time zone. */
export function day(iso: string | null | undefined, empty = ""): string {
  const d = instant(iso);
  if (!d) return iso ? iso.slice(0, 10) : empty;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** "07:17" today, "2026-10-30 07:17" on another day (the viewer's time zone). */
export function shortWhen(iso: string | null | undefined, now: Date = new Date()): string {
  const d = instant(iso);
  if (!d) return iso || "";
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return day(iso) === day(now.toISOString()) ? time : `${day(iso)} ${time}`;
}
