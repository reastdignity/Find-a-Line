// Popups can be dragged by their top bar (.fal-drag), so they don't cover the line.
// The offset is kept when the map is zoomed or panned, and reset when the popup closes.

import L from "leaflet";

type Dragged = L.Popup & {_falOffset?: L.Point; _falDraggable?: L.Draggable; _container?: HTMLElement};

function makeDraggable(popup: Dragged) {
  const container = popup.getElement(), handle = container?.querySelector<HTMLElement>(".fal-drag");
  if (!container || !handle || popup._falDraggable) return;
  popup._falOffset = L.point(0, 0);
  const draggable = new L.Draggable(container, handle);
  let start: L.Point;
  draggable.on("dragstart", () => { start = L.DomUtil.getPosition(container); });
  draggable.on("drag", () => {
    popup._falOffset = popup._falOffset!.add(L.DomUtil.getPosition(container).subtract(start));
    start = L.DomUtil.getPosition(container);
    L.DomUtil.addClass(container, "fal-dragged");
  });
  draggable.enable();
  popup._falDraggable = draggable;
}

export function draggablePopups(map: L.Map) {
  // Patched on the prototype: popups register these as event handlers when they open
  for (const name of ["_updatePosition", "_animateZoom"]) {
    const proto = L.Popup.prototype as any, original = proto[name];
    proto[name] = function (this: Dragged, ...args: unknown[]) {
      original.apply(this, args);
      if (this._falOffset && this._container) {
        L.DomUtil.setPosition(this._container, L.DomUtil.getPosition(this._container).add(this._falOffset));
      }
    };
  }
  map.on("popupopen", (e) => makeDraggable(e.popup as Dragged));
  map.on("popupclose", (e) => {
    const popup = e.popup as Dragged;
    popup._falOffset = L.point(0, 0);  // reopened popups start next to the line again
    const element = popup.getElement();
    if (element) L.DomUtil.removeClass(element, "fal-dragged");
  });
}
