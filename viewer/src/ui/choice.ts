// Which interface the app shows: ?ui=old|b|c|kit|kit-result, remembered in localStorage (per browser) until
// another is chosen. The old interface stays the default.

export type UiChoice = "old" | "b" | "c" | "kit" | "kit-result";
const CHOICES: UiChoice[] = ["old", "b", "c", "kit", "kit-result"];
const STORAGE_KEY = "findaline-ui";

/** The interface of this page: the parameter (remembered), else the remembered one, else "old". */
export function chooseUi(search: string = location.search): UiChoice {
  const asked = new URLSearchParams(search).get("ui") as UiChoice | null;
  if (asked && CHOICES.includes(asked)) {
    try { localStorage.setItem(STORAGE_KEY, asked); } catch { /* private window, blocked storage */ }
    return asked;
  }
  try {
    const kept = localStorage.getItem(STORAGE_KEY) as UiChoice | null;
    if (kept && CHOICES.includes(kept)) return kept;
  } catch { /* no storage */ }
  return "old";
}

/** The address of this page in another interface (the hash, a route of the new shells, is dropped). */
export function uiHref(choice: UiChoice): string {
  const params = new URLSearchParams(location.search);
  params.set("ui", choice);
  return `${location.pathname}?${params}`;
}

const THEME_KEY = "findaline-theme";

/** The remembered theme of the new interfaces ("auto" follows the system). */
export function storedTheme(): "auto" | "light" | "dark" {
  try {
    const kept = localStorage.getItem(THEME_KEY);
    if (kept === "light" || kept === "dark") return kept;
  } catch { /* no storage */ }
  return "auto";
}

export function storeTheme(theme: "auto" | "light" | "dark") {
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* no storage */ }
}
