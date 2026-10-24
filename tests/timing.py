"""Wall-clock budgets that hold on a loaded host and still catch a real slowdown.

`assert_fast` times the command under test and a baseline (a trivial `trk` call that shares its
process start-up but not its code path) in interleaved rounds, so both see the same host load. The
command passes when its median is under the budget, or, when the host is too loaded for an absolute
reading, when it is no more than `RATIO` times the baseline's median. On a quiet host the baseline
sits well under budget / RATIO, so only the absolute budget binds there. A failing reading is
measured once more before the assertion fails.
"""

import statistics
import time

RUNS = 7
RATIO = 1.25


def _medians(target, baseline, runs, clock):
    timings = {"target": [], "baseline": []}
    for _ in range(runs):
        for name, run in (("baseline", baseline), ("target", target)):
            start = clock()
            run()
            timings[name].append(clock() - start)
    return statistics.median(timings["target"]), statistics.median(timings["baseline"]), timings


def assert_fast(target, baseline, budget, *, runs=RUNS, ratio=RATIO, clock=time.perf_counter):
    """`target()`'s median wall clock is under `budget` seconds, or under `ratio` × `baseline()`'s."""
    readings = []
    for _ in range(2):
        took, base, timings = _medians(target, baseline, runs, clock)
        if took < budget or took <= ratio * base:
            return
        readings.append({name: [round(t * 1000) for t in times] for name, times in timings.items()})
    raise AssertionError(f"median over {budget * 1000:.0f} ms and over {ratio} x the baseline, twice (ms): {readings}")
