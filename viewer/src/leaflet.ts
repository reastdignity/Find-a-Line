// Leaflet for a bundle (the app's and a run's map): its CSS, and the default marker's images, which it
// finds through the CSS path, which a bundle does not have. Imported once by each entry.

import "leaflet/dist/leaflet.css";
import L from "leaflet";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";

L.Icon.Default.mergeOptions({iconRetinaUrl, iconUrl, shadowUrl});
delete (L.Icon.Default.prototype as any)._getIconUrl;
