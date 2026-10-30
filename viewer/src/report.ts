// "Report a problem" (the public site): any part of the app opens the report's dialog (app/ReportDialog,
// mounted by each shell) with what it knows (a run, a line, a category), without props through the
// shells: a window event. The page's own context (address, interface, window) is added when it opens.

import { user } from "./app/api";
import { chooseUi } from "./ui/choice";

export type ReportCategory = "results" | "water" | "interface" | "other";

/** What a report is sent with (web.checked_report: values not as expected are left out there). */
export interface ReportContext {
  page?: string;
  ui?: string;
  run?: string;
  line?: string;
  viewport?: string;
}

export interface ReportRequest {
  category?: ReportCategory;
  context?: ReportContext;
  /** Shown in the dialog for what the report is about (e.g. the run's name). */
  about?: string;
}

export const REPORT_EVENT = "findaline-report";
export const ISSUES_URL = "https://github.com/reastdignity/Find-a-Line/issues/new/choose";

/** Reports are sent on the public site only (locally the one user is the operator). */
export function canReport(): boolean {
  return !user.local;
}

/** The page's own context: its address (path and hash), the interface and the window's size. */
export function currentContext(): ReportContext {
  const ui = chooseUi();
  return {page: (location.pathname + location.search + location.hash).slice(0, 300),
          ...(["b", "c", "d", "old"].includes(ui) ? {ui} : {}), viewport: `${innerWidth}x${innerHeight}`};
}

/** Open the report's dialog. */
export function openReport(request: ReportRequest = {}) {
  window.dispatchEvent(new CustomEvent<ReportRequest>(REPORT_EVENT, {detail: request}));
}

/** The operator's contact of the public site (FINDALINE_CONTACT; "" locally). */
export function siteContact(): string {
  return document.querySelector<HTMLMetaElement>("meta[name=findaline-contact]")?.content || "";
}
