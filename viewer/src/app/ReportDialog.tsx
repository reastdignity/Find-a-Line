// "Report a problem" (the public site): a native <dialog> opened by report.openReport from anywhere in
// the app: the kind of problem, the text, and what is sent with it (the page, the run and line, the
// window's size; the server adds the browser's User-Agent), then "Thank you". POST /api/reports.

import { useEffect, useRef, useState } from "preact/hooks";
import { Button, Segmented, type Option } from "../ui/components";
import { currentContext, REPORT_EVENT, type ReportCategory, type ReportContext, type ReportRequest } from "../report";
import { api } from "./api";
import "./notices.css";

const TEXT_MAX = 4000;  // jobs.REPORT_TEXT_MAX
const CATEGORIES: Option<ReportCategory>[] = [
  {value: "results", label: "Results"}, {value: "water", label: "Water level"},
  {value: "interface", label: "Interface"}, {value: "other", label: "Other"},
];
const HINTS: Record<ReportCategory, string> = {
  results: "Which lines did you expect (where, how long, how high), and what did you get instead?",
  water: "Where is the water surface at a wrong height, and how high should it be?",
  interface: "What did you do, what did you expect, and what happened?",
  other: "What would you like to tell us?",
};

export function ReportDialog() {
  const ref = useRef<HTMLDialogElement>(null);
  const [category, setCategory] = useState<ReportCategory>("results");
  const [text, setText] = useState("");
  const [context, setContext] = useState<ReportContext>({});
  const [about, setAbout] = useState("");
  const [state, setState] = useState<"form" | "sending" | "sent">("form");
  const [error, setError] = useState("");

  useEffect(() => {
    const open = (e: Event) => {
      const request = (e as CustomEvent<ReportRequest>).detail || {};
      setCategory(request.category || "results");
      setContext({...currentContext(), ...request.context});
      setAbout(request.about || "");
      setText("");
      setError("");
      setState("form");
      if (!ref.current?.open) ref.current?.showModal();
    };
    window.addEventListener(REPORT_EVENT, open);
    return () => window.removeEventListener(REPORT_EVENT, open);
  }, []);

  const close = () => ref.current?.close();
  const send = async () => {
    if (!text.trim() || state === "sending") return;
    setState("sending");
    setError("");
    try {
      await api.report({category, text, context});
      setState("sent");
    } catch (e: any) {
      setError(e.message);
      setState("form");
    }
  };
  const attached = [
    context.page && `the page (${context.page})`,
    about && `what it is about: ${about}`,
    context.run && `the run${context.line ? " and the line" : ""}`,
    context.viewport && `the window's size (${context.viewport})`,
    "your browser's identification (User-Agent)",
  ].filter(Boolean) as string[];

  return <dialog ref={ref} class="ui u-dialog n-report" aria-labelledby="n-report-title">
    {state === "sent" ? <>
      <h2 class="u-h" id="n-report-title">Thank you</h2>
      <p>Your report was sent to the administrators of this site.</p>
      <div class="n-actions"><Button kind="primary" onClick={close}>Close</Button></div>
    </> : <form method="dialog" onSubmit={(e) => { e.preventDefault(); send(); }}>
      <h2 class="u-h" id="n-report-title">Report a problem</h2>
      <Segmented label="Kind of problem" options={CATEGORIES} value={category} onChange={setCategory} />
      <label class="n-field"><span class="u-small">{HINTS[category]}</span>
        <textarea class="u-input n-text" rows={7} maxLength={TEXT_MAX} value={text} aria-label="Your report"
                  onInput={(e) => setText(e.currentTarget.value)} />
        <span class="u-small n-count">{text.length} / {TEXT_MAX}</span></label>
      <div class="u-small n-attached"><b>Sent with it:</b> {attached.join(", ")}.</div>
      {error && <p class="c-error n-error" role="alert">{error}</p>}
      <div class="n-actions">
        <Button kind="ghost" onClick={close}>Cancel</Button>
        <Button kind="primary" type="submit" disabled={!text.trim() || state === "sending"}>
          {state === "sending" ? "Sending…" : "Send"}</Button>
      </div>
    </form>}
  </dialog>;
}

/** The notice after a sign-in and the report's dialog: what every shell mounts. */
export { BetaNotice } from "./BetaNotice";
