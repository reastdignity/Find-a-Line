// The text of an error answer of the server (web.py error_detail), without the message catalogue: the
// map of a run (main.tsx, favourites.ts) shows the server's English text. The app renders coded
// errors in its language (app/api.ts ApiError, i18n.ts errorText).

/** The detail of an error answer as a text: a plain text, or the "message" of {message, code, params}. */
export function detailText(detail: unknown, fallback = "error"): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && typeof (detail as {message?: unknown}).message === "string") {
    return (detail as {message: string}).message;
  }
  return fallback;
}
