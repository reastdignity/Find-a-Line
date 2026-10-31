"""
Run by hand: how many cores a search keeps busy, stage by stage (docs/performance.md).

Runs findaline.py in this process with the given options, follows the progress bars
(progress.subscribe) and samples the CPU time and memory of the process every 0.1 s.
Writes <out>/<name>.csv (the samples), <name>-stages.csv (per stage: wall time, CPU time,
mean cores, peak memory), <name>.png (a timeline) and prints the table. --tracemalloc adds
the peak of Python's heap per stage (numpy arrays included, memory-mapped grids not): what
the lines take, apart from the grids (slower).

    uv run tools/perf_profile.py --out <scratch> --name gozo -- --area coast --buffer 100 ...

The options after "--" are findaline.py's; --no-plot, --yes and --ortho never are added. Give
--data-dir there to keep the runs out of the repository's runs/ (the cache of the search is used).
"""

import argparse
import csv
import os
import runpy
import sys
import threading
import time
import tracemalloc

import psutil

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from progress import progress  # noqa: E402

INTERVAL = 0.1


class Recorder:
    """The samples and the events of the bars, both against time.monotonic()."""

    def __init__(self):
        self.process = psutil.Process()
        self.samples = []  # (t, cpu seconds, rss bytes)
        self.events = []  # (t, label): the activity from then on
        self.step = ""
        self.stopped = threading.Event()
        self.label = "Start: (between bars)"  # the stage running now
        self.heap = {}  # label -> peak of the traced heap (--tracemalloc)

    def cpu(self) -> float:
        times = self.process.cpu_times()
        return times.user + times.system

    def sample(self):
        self.samples.append((time.monotonic(), self.cpu(), self.process.memory_info().rss))

    def run(self):
        while not self.stopped.wait(INTERVAL):
            self.sample()

    def mark(self, label):
        """A new stage starts (None: the end): the heap's peak of the one that ends."""
        if tracemalloc.is_tracing():
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.reset_peak()
            self.heap[self.label] = max(self.heap.get(self.label, 0), peak)
        self.label = label
        if label is not None:
            self.events.append((time.monotonic(), label))

    def listener(self, state):
        if state["bar"] == "":
            if state["step"] != self.step:
                self.step = state["step"]
                self.mark(f"{self._short(self.step)}: (between bars)")
            return
        if state["count"] == 0 and not state["done"] and state["stage"]:
            self.mark(f"{self._short(state['step'])}: {state['bar']} {state['stage']}")
        elif state["done"]:
            self.mark(f"{self._short(state['step'])}: (between bars)")

    def _short(self, step: str) -> str:
        """"Tile 3/15 · Step 3/5: evaluating anchor pairs" -> "Step 3"; tiles are summed up."""
        if "Step " not in step:
            return "Between steps" if self.events else "Start"  # between tiles, after the last step
        return "Step " + step.split("Step ", 1)[1].split("/", 1)[0]


def cpu_at(samples, t: float) -> float:
    """CPU seconds at time t, interpolated between the samples."""
    for (t0, c0, _), (t1, c1, _) in zip(samples, samples[1:]):
        if t0 <= t <= t1:
            return c0 + (c1 - c0) * (t - t0) / (t1 - t0) if t1 > t0 else c0
    return samples[0][1] if t < samples[0][0] else samples[-1][1]


def stages(rec: Recorder):
    """Per label: wall seconds, CPU seconds and peak memory, in order of first appearance."""
    events = [(rec.samples[0][0], "Start: (between bars)")] + rec.events + [(rec.samples[-1][0], None)]
    table = {}
    for (t0, label), (t1, _) in zip(events, events[1:]):
        if t1 <= t0:
            continue
        row = table.setdefault(label, [0.0, 0.0, 0])
        row[0] += t1 - t0
        row[1] += cpu_at(rec.samples, t1) - cpu_at(rec.samples, t0)
        # The sampler may be late on busy cores: the nearest sample stands in for a short stage
        peak = max((s[2] for s in rec.samples if t0 <= s[0] <= t1),
                   default=min(rec.samples, key=lambda s: abs(s[0] - t0))[2])
        row[2] = max(row[2], peak)
    return table


def plot(rec: Recorder, path: str, title: str, cores: int):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t0 = rec.samples[0][0]
    xs, ys = [], []
    for (ta, ca, _), (tb, cb, _) in zip(rec.samples, rec.samples[1:]):
        xs.append((ta + tb) / 2 - t0)
        ys.append((cb - ca) / (tb - ta) if tb > ta else 0)
    fig, ax = plt.subplots(figsize=(11, 3.6))
    steps = [(t - t0, label.split(":", 1)[0]) for t, label in rec.events]
    boundaries = []
    for t, step in steps:
        if not boundaries or boundaries[-1][1] != step:
            boundaries.append((t, step))
    end = rec.samples[-1][0] - t0
    for n, (t, step) in enumerate(boundaries):
        t_end = boundaries[n + 1][0] if n + 1 < len(boundaries) else end
        ax.axvspan(t, t_end, color=f"C{n % 10}", alpha=0.12, lw=0)
        ax.text((t + t_end) / 2, cores * 1.02, step.replace("Step ", "S"), ha="center", va="bottom", fontsize=7)
    ax.plot(xs, ys, color="#333", lw=1)
    ax.axhline(cores, color="#c33", lw=0.8, ls="--")
    ax.set_xlim(0, end)
    ax.set_ylim(0, cores * 1.12)
    ax.set_xlabel("seconds")
    ax.set_ylabel("cores busy")
    ax.set_title(title, fontsize=10, pad=14)
    fig.tight_layout()
    fig.savefig(path, dpi=110)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="folder for the CSV files and the chart")
    parser.add_argument("--name", required=True, help="name of the files")
    parser.add_argument("--cores", type=int, help="run on this many cores only (the CPU affinity, and "
                        "--threads for the line search), like a worker container with that many cpus")
    parser.add_argument("--tracemalloc", action="store_true", help="also the peak of the Python heap per stage")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="-- and the options of findaline.py")
    args = parser.parse_args()
    search_args = args.args[1:] if args.args[:1] == ["--"] else args.args
    os.makedirs(args.out, exist_ok=True)
    os.environ.setdefault("FINDALINE_PROGRESS", "text")

    cores = psutil.cpu_count()
    if args.cores:
        # One logical CPU per physical core while there are enough (SMT siblings are neighbours)
        step = 2 if 2 * args.cores <= psutil.cpu_count() and psutil.cpu_count(False) < psutil.cpu_count() else 1
        psutil.Process().cpu_affinity(list(range(0, step * args.cores, step)))
        search_args += ["--threads", str(args.cores)]
        cores = args.cores

    rec = Recorder()
    if args.tracemalloc:
        tracemalloc.start()
    progress.subscribe(rec.listener)
    rec.sample()
    sampler = threading.Thread(target=rec.run, daemon=True)
    sampler.start()
    sys.argv = [os.path.join(HERE, "findaline.py"), *search_args, "--no-plot", "--yes", "--ortho", "never"]
    started = time.monotonic()
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    except SystemExit as e:
        if e.code not in (None, 0):
            print(f"findaline.py ended with {e.code!r}", file=sys.stderr)
    finally:
        rec.stopped.set()
        sampler.join()
        rec.sample()
        rec.mark(None)
    wall = time.monotonic() - started

    with open(os.path.join(args.out, f"{args.name}.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["t", "cpu_s", "rss_mb"])
        t0 = rec.samples[0][0]
        for t, c, rss in rec.samples:
            writer.writerow([f"{t - t0:.2f}", f"{c:.2f}", f"{rss / 2**20:.0f}"])
    table = stages(rec)
    total_cpu = rec.samples[-1][1] - rec.samples[0][1]
    with open(os.path.join(args.out, f"{args.name}-stages.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["stage", "wall_s", "cpu_s", "cores", "share_of_wall", "peak_rss_mb", "peak_heap_mb"])
        for label, (w, c, peak) in table.items():
            writer.writerow([label, f"{w:.1f}", f"{c:.1f}", f"{c / w:.2f}" if w else "",
                             f"{w / wall:.3f}", f"{peak / 2**20:.0f}",
                             f"{rec.heap[label] / 2**20:.0f}" if label in rec.heap else ""])
    heap = lambda label: f"{rec.heap[label] / 2**20:8.0f}" if label in rec.heap else ""
    print(f"\n{'stage':62} {'wall s':>8} {'cpu s':>8} {'cores':>6} {'wall %':>7} {'peak MB':>8}"
          + (f" {'heap MB':>8}" if rec.heap else ""))
    for label, (w, c, peak) in sorted(table.items(), key=lambda kv: -kv[1][0]):
        if w < 0.2 and not rec.heap:
            continue
        print(f"{label[:62]:62} {w:8.1f} {c:8.1f} {c / w:6.2f} {100 * w / wall:6.1f}% {peak / 2**20:8.0f} "
              + heap(label))
    peak = max(s[2] for s in rec.samples)
    print(f"{'total':62} {wall:8.1f} {total_cpu:8.1f} {total_cpu / wall:6.2f} {100:6.1f}% {peak / 2**20:8.0f}"
          + (f" {max(rec.heap.values()) / 2**20:8.0f}" if rec.heap else ""))
    plot(rec, os.path.join(args.out, f"{args.name}.png"),
         f"{args.name}: {wall:.0f} s, mean {total_cpu / wall:.1f} of {cores} cores", cores)


if __name__ == "__main__":
    main()
