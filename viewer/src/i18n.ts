// The texts of the interface: the message catalogue locales/<language>.json at the root of the
// repository, shared with the server (messages.py reads the same file and formats it the same way).
// Keys are flat and namespaced ("field.radius.label", "choice.nmpt.off", "ui.try"); `{{name}}` is
// replaced by a value, and a key with `count` takes its plural form ("<key>_one", "<key>_other",
// "<key>_zero" for 0) where the catalogue has one. i18next's core only: no React bindings, the
// texts are read when a component draws.
import i18next, { type TOptions } from "i18next";
import en from "../../locales/en.json";

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
