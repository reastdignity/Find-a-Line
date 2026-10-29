// The routes of prototype B in the page's hash, so the server needs no new routes and back/forward
// work: #search, #searches, #favourites, #admin, #osm and #run=<id>&line=<lat1,lon1,lat2,lon2> (the
// link LineDetails copies: result/view/LineDetails.tsx appLink).

export type Section = "search" | "searches" | "favourites" | "admin" | "osm";
export const SECTIONS: Section[] = ["search", "searches", "favourites", "admin", "osm"];

export type Route = {section: Section} | {section: "run"; run: string; line: string | null};

/** The route of a hash; anything unknown is the search. */
export function parseRoute(hash: string): Route {
  const text = hash.replace(/^#/, "");
  if (text.startsWith("run=")) {
    const params = new URLSearchParams(text);
    const run = params.get("run");
    if (run) return {section: "run", run, line: params.get("line")};
  }
  const section = SECTIONS.find((s) => s === text);
  return {section: section ?? "search"};
}

/** The hash of a route. */
export function routeHash(route: Route): string {
  if (route.section !== "run") return "#" + route.section;
  return `#run=${encodeURIComponent(route.run)}` + (route.line ? `&line=${encodeURIComponent(route.line)}` : "");
}
