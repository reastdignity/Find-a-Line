// The texts of the interface: the message catalogue locales/<language>.json at the root of the
// repository, shared with the server (messages.py reads the same file and formats it the same way).
// Keys are flat and namespaced ("field.radius.label", "choice.nmpt.off", "ui.try"); `{{name}}` is
// replaced by a value, and a key with `count` takes its plural form ("<key>_one", "<key>_other",
// "<key>_zero" for 0) where the catalogue has one. i18next's core only: no React bindings, the
// texts are read when a component draws.
import i18next, { type TOptions } from "i18next";
import en from "../../locales/en.json";
import { detailText } from "./errors";

type Texts = Record<string, string>;

/** A `t` of its own catalogue (tests give their own samples). Initialised synchronously: the texts
 * are in the bundle, so `t` works as soon as the module is loaded. */
export function translator(texts: Texts, language = "en"): (key: string, params?: TOptions) => string {
  const instance = i18next.createInstance();
  instance.init({
    lng: language, fallbackLng: "en", resources: {[language]: {translation: texts}},
    initAsync: false,
    keySeparator: false, nsSeparator: false,  // flat keys with dots, as Python reads them
    interpolation: {escapeValue: false},       // Preact escapes what it draws
    returnNull: false,
  });
  return (key, params) => instance.t(key, params) as string;
}

// English only for now; a language would be chosen here later (`?lang=`, navigator.language) and its
// catalogue imported next to en.json, with English as the fallback.
export const t = translator(en as Texts);

/** The label of a field of the search (field.<name>.label). */
export const label = (field: string): string => t(`field.${field}.label`);

/** The parameters of a coded error or note (messages.py UserError, Note): {code, params}. */
export interface ErrorInfo { code: string; params?: Record<string, unknown> }

/** The text of a coded error ("error.<code>") in the page's language, as messages.Coded.web_text: a
 *  parameter "field" gives the label ("label"). `fallback` (the server's English text) when the
 *  catalogue has no such code (a newer server). */
export function errorText(info: ErrorInfo, fallback?: string): string {
  const key = `error.${info.code}`;
  const params: Record<string, unknown> = {...(info.params || {})};
  if (typeof params.field === "string" && params.label === undefined) params.label = label(params.field);
  const text = t(key, params);
  return text === key && fallback ? fallback : text;
}

/** A value of a field with its unit as the texts write it (messages.quantity): "2000 m", "45°", "50". */
export function quantity(field: string, value: number): string {
  const unit = t(`field.${field}.unit`);
  if (!unit || unit === `field.${field}.unit`) return String(value);
  return unit === "°" ? `${value}${unit}` : `${value} ${unit}`;
}

/** The text of an error answer's detail (web.py error_detail) in the page's language: a coded one's
 *  text, else its message or the plain text (errors.ts detailText). */
export function detailMessage(detail: unknown, fallback = "error"): string {
  const coded = detail as {code?: unknown; params?: Record<string, unknown>; message?: string} | null;
  if (coded && typeof coded === "object" && typeof coded.code === "string") {
    return errorText({code: coded.code, params: coded.params}, detailText(detail, fallback));
  }
  return detailText(detail, fallback);
}
