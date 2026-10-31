"""
Run by hand: jobs.sqlite under the load of many jobs and pages (docs/performance.md).

In a new data folder under <out>, with the real jobs.Jobs: K processes stand for the phase
processes of K jobs running at once (a progress event every 0.3 s, PROGRESS_INTERVAL, and an
info event every 2 s), one for the worker (heartbeat and interrupted() every 5 s, the claims
every 0.5 s, cancel_requested every 0.5 s) and one for the quick lane (quick_claim every 0.05 s),
while M threads of one process stand for M open pages of the web server (events of their user
and worker_alive every 0.5 s, as web.py's event stream). Prints the latency of each kind of
operation (median, 99th percentile, maximum), errors, events written per second and the size
of the database and its WAL file. --burst: the writers write events without pauses (the ceiling).

    uv run tools/perf_sqlite.py --out <scratch> --jobs 1 4 8 16 --pages 50 --seconds 30
"""

import argparse
import multiprocessing
import os
import shutil
import statistics
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import jobs as jb  # noqa: E402


def _timed(queue, kind: str, call):
    t = time.perf_counter()
    try:
        call()
        queue.put((kind, time.perf_counter() - t, None))
    except Exception as e:  # noqa: BLE001 - counted, not raised
        queue.put((kind, time.perf_counter() - t, repr(e)))


def _every(seconds: float, until: float, step):
    nxt = time.monotonic()
    while time.monotonic() < until:
        step()
        nxt += seconds
        time.sleep(max(0.0, nxt - time.monotonic()))


def phase_process(data: str, job_id: str, until: float, queue, burst: bool):
    jobs = jb.Jobs(data)
    n = [0]

    def step():
        n[0] += 1
        _timed(queue, "event (phase)", lambda: jobs.event(job_id, "progress", {
            "bar": "Compute", "stage": "checking lines", "count": n[0], "total": 10000, "unit": "line",
            "extra": {}, "done": False, "step": "Tile 3/15 · Step 3/5: evaluating anchor pairs"}))
        if n[0] % 7 == 0:
            _timed(queue, "event (phase)", lambda: jobs.event(job_id, "info", {"text": "[Compute] a line of text"}))
    _every(0 if burst else jb.PROGRESS_INTERVAL, until, step)


def worker_process(data: str, until: float, queue):
    jobs = jb.Jobs(data)
    jobs.register("w")
    last = [0.0]

    def step():
        if time.monotonic() - last[0] >= jb.HEARTBEAT:
            last[0] = time.monotonic()
            _timed(queue, "heartbeat", lambda: jobs.heartbeat("w"))
            _timed(queue, "interrupted()", lambda: jobs.interrupted())
        _timed(queue, "cancel_requested", lambda: jobs.cancel_requested(["x"]))
        _timed(queue, "claim", lambda: jobs.claim("nothing", "downloading", "w"))
    _every(jb.TICK, until, step)


def quick_process(data: str, until: float, queue):
    jobs = jb.Jobs(data)
    _every(jb.QUICK_POLL, until, lambda: _timed(queue, "quick_claim (idle)", lambda: jobs.quick_claim("w")))


def pages(data: str, count: int, users: int, until: float, queue):
    jobs = jb.Jobs(data)

    def page(user: int):
        seq = jobs.last_seq(user_id=user)

        def step():
            nonlocal seq
            found = []
            _timed(queue, "page: events+alive", lambda: (found.extend(jobs.events(seq, user_id=user)),
                                                          jobs.worker_alive()))
            if found:
                seq = found[-1]["seq"]
        _every(jb.TICK, until, step)
    threads = [threading.Thread(target=page, args=(n % users + 1,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def one(out: str, k: int, m: int, seconds: float, burst: bool):
    data = os.path.join(out, f"sqlite-{k}-{m}{'-burst' if burst else ''}")
    shutil.rmtree(data, ignore_errors=True)
    os.makedirs(data)
    jobs = jb.Jobs(data)
    ids = [jobs.add({"lat": 0, "lon": 0}, f"job {n}", user_id=n % max(1, m // 3) + 1) for n in range(k)]
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    until = time.monotonic() + 3 + seconds  # the processes start meanwhile
    procs = [ctx.Process(target=phase_process, args=(data, job, until, queue, burst)) for job in ids]
    procs += [ctx.Process(target=worker_process, args=(data, until, queue)),
              ctx.Process(target=quick_process, args=(data, until, queue))]
    for p in procs:
        p.start()
    reader = threading.Thread(target=pages, args=(data, m, max(1, m // 3), until, queue))
    reader.start()
    results = {}
    errors = {}
    while any(p.is_alive() for p in procs) or reader.is_alive() or not queue.empty():
        try:
            kind, dt, error = queue.get(timeout=0.5)
        except Exception:  # noqa: BLE001 - queue.Empty
            continue
        results.setdefault(kind, []).append(dt)
        if error:
            errors[kind] = errors.get(kind, 0) + 1
    reader.join()
    size = lambda name: os.path.getsize(os.path.join(data, name)) / 1e6 if os.path.exists(
        os.path.join(data, name)) else 0
    events = len(results.get("event (phase)", []))
    print(f"\n{k} jobs, {m} pages{', burst' if burst else ''}: {events / seconds:.0f} events/s; "
          f"jobs.sqlite {size('jobs.sqlite'):.1f} MB, WAL {size('jobs.sqlite-wal'):.1f} MB")
    print(f"  {'operation':24} {'count':>7} {'median ms':>10} {'p99 ms':>8} {'max ms':>8} {'errors':>7}")
    for kind, values in sorted(results.items()):
        values.sort()
        p99 = values[min(len(values) - 1, int(0.99 * len(values)))]
        print(f"  {kind:24} {len(values):7} {1000 * statistics.median(values):10.1f} {1000 * p99:8.1f} "
              f"{1000 * values[-1]:8.1f} {errors.get(kind, 0):7}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--jobs", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--pages", type=int, nargs="+", default=[30])
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--burst", action="store_true", help="the phase processes write without pauses")
    args = parser.parse_args()
    for k in args.jobs:
        for m in args.pages:
            one(args.out, k, m, args.seconds, args.burst)


if __name__ == "__main__":
    main()
