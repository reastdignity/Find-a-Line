// "How to read the result": the intro of a result (ui/Intro.tsx), once per browser, mounted by the
// FilterBar (one in every layout). Spots and lines, the colours of the lines (style.ts COLORS, the
// swatches of the legend), what filters do, how to make the map easy to read, and checking on the spot.
// Texts in locales/en.json (intro.result.*).

import { t } from "../../i18n";
import { COLORS } from "../../style";
import { Intro, Rich, type IntroState } from "../../ui/Intro";
import { Swatch } from "./common";

const EASY = ["group", "within", "clearance", "length", "spot", "off"];

export function ResultIntro({state}: {state: IntroState}) {
  return <Intro state={state} title={t("intro.result.title")} class="n-intro-result">
    <p><Rich text={t("intro.result.spots")} /></p>
    <p><Rich text={t("intro.result.colours")} /></p>
    <ul class="n-colours">
      <li><Swatch color={COLORS.clean} /><span>{t("intro.result.colour_clean")}</span></li>
      <li><Swatch color={COLORS.over} /><span>{t("intro.result.colour_over")}</span></li>
      <li><Swatch color={COLORS.favourite} class="r-sw-fav" /><span>{t("intro.result.colour_favourite")}</span></li>
    </ul>
    <p><Rich text={t("intro.result.filters")} /></p>
    <p><Rich text={t("intro.result.easy")} /></p>
    <ul class="n-easy">{EASY.map((k) => <li key={k}><Rich text={t(`intro.result.easy_${k}`)} /></li>)}</ul>
    <p><Rich text={t("intro.result.check")} /></p>
  </Intro>;
}
