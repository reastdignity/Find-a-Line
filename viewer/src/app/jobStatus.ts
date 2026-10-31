// Statuses of jobs that need no page (useJobs.ts imports the API, which reads the page): the finished
// ones, and whether a listed run is still being saved by its job.

import type { Job } from "./api";
import { timeOf } from "../time";

export const FINISHED: Job["status"][] = ["done", "failed", "cancelled"];

/** Whether a listed run is still being saved by a search that is not finished: a run is listed once its
 *  run.json is written, before its map, while its job learns its run_id only when it is done. Such a run
 *  has no map yet and was made after the unfinished job was queued; its job's card stands for it. */
export function beingSaved(run: {has_map: boolean; created: string}, jobs: Pick<Job, "status" | "run_id" | "created">[]): boolean {
  return !run.has_map && jobs.some((j) => !FINISHED.includes(j.status) && !j.run_id
                                          && timeOf(run.created) >= timeOf(j.created));
}
