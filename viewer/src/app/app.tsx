// The app (python findaline.py web): one of the interfaces of ui/choice.ts (?ui=b|c|d, D the default, or a
// demo of the kit), with the beta notice and Report a problem around it.

import "leaflet/dist/leaflet.css";
import L from "leaflet";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import { render } from "preact";
import "./base.css";
import "../ui/pages.css";
import { AppB } from "./b/AppB";
import { BetaNotice, ReportDialog } from "./ReportDialog";
import { AppC } from "./c/AppC";
import { AppD } from "./d/AppD";
import { Gallery } from "../ui/Gallery";
import { KitResult } from "../result/view/KitResult";
import { KitSearch } from "../search/KitSearch";
import { chooseUi } from "../ui/choice";

L.Icon.Default.mergeOptions({iconRetinaUrl, iconUrl, shadowUrl});
delete (L.Icon.Default.prototype as any)._getIconUrl;

const SHELLS = {b: AppB, c: AppC, d: AppD, kit: Gallery, "kit-result": KitResult, "kit-search": KitSearch};
const choice = chooseUi(), Shell = SHELLS[choice];
// The beta notice after a sign-in and Report a problem (the public site), whichever interface is shown
const dialogs = ["b", "c", "d"].includes(choice) && <><BetaNotice /><ReportDialog /></>;
render(<><Shell />{dialogs}</>, document.getElementById("app")!);
