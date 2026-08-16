#!/usr/bin/env python3
"""Load and scalability tests for jobchain's hot state paths.

These are intentionally bounded stress tests rather than microbenchmarks. They
verify that larger realistic state sets remain correct under process load and
that throughput does not collapse unexpectedly.
"""
from __future__ import annotations

import multiprocessing
import os
import queue
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from jobchain.store import Store


def _make_home(root: str, rows: int) -> str:
    home = os.path.join(root, "home")
    rows_dir = os.path.join(home, "rows")
    os.makedirs(rows_dir, exist_ok=True)
    names = []
    for index in range(rows):
        name = f"{index + 1:06d}"
        row = os.path.join(rows_dir, name)
        os.makedirs(row, exist_ok=True)
        with open(os.path.join(row, "gen"), "w", encoding="utf-8") as f:
            f.write("1\n")
        with open(os.path.join(row, "meta.json"), "w", encoding="utf-8") as f:
            f.write(
                '{"name":"%s","row_id":"row-%d","line_num":%d,'
                '"index":%d,"params":{"value":%d}}' %
                (name, index, index + 1, index, index)
            )
        with open(os.path.join(row, "env"), "w", encoding="utf-8") as f:
            f.write(f"JC_index='{index}'\nexport JC_index\n")
        with open(os.path.join(row, "manifest"), "w", encoding="utf-8") as f:
            f.write("only\t-\t/bin/true\n")
        names.append(name)
    with open(os.path.join(home, "rows.idx"), "w", encoding="utf-8") as f:
        f.write("".join(f"{name}\n" for name in names))
    return home


def _claim_worker(home: str, results) -> None:
    store = Store(home)
    count = 0
    try:
        while True:
            claimed = store.claim()
            if claimed is None:
                break
            count += 1
        results.put(("ok", count))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))


def _run_claim_load(home: str, workers: int) -> tuple[int, float]:
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()
    processes = [ctx.Process(target=_claim_worker, args=(home, results))
                 for _ in range(workers)]
    started = time.monotonic()
    for process in processes:
        process.start()
    for process in processes:
        process.join(60)
        assert process.exitcode == 0, f"worker exited {process.exitcode}"
    elapsed = time.monotonic() - started
    values = [results.get(timeout=5) for _ in processes]
    errors = [value for value in values if value[0] == "error"]
    assert not errors, errors
    return sum(value[1] for value in values), elapsed


def test_large_state_load():
    rows = 5000
    with tempfile.TemporaryDirectory(prefix="jobchain-load-") as root:
        home = _make_home(root, rows)
        Store(home).create({"name": "load"})
        started = time.monotonic()
        loaded = Store(home).load_rows()
        elapsed = time.monotonic() - started
        assert len(loaded) == rows
        assert len({row.name for row in loaded}) == rows
        # A generous ceiling catches catastrophic O(N^2) regressions without
        # turning this into a machine-specific benchmark.
        assert elapsed < 15, f"loading {rows} rows took {elapsed:.2f}s"
        print(f"  state load: {rows} rows in {elapsed:.3f}s")


def test_claim_throughput_under_process_load():
    rows = 2000
    workers = 24
    with tempfile.TemporaryDirectory(prefix="jobchain-load-") as root:
        home = _make_home(root, rows)
        Store(home).create({"name": "load"})
        claimed, elapsed = _run_claim_load(home, workers)
        assert claimed == rows
        assert Store(home).claim() is None
        throughput = claimed / elapsed if elapsed else float("inf")
        # This is deliberately conservative; the assertion is a regression
        # guard against a pathological slowdown, not a hardware benchmark.
        assert elapsed < 45, f"claiming {rows} rows took {elapsed:.2f}s"
        print(f"  claim load: {claimed} rows, {workers} workers, "
              f"{elapsed:.3f}s ({throughput:.1f} claims/s)")


def test_repeated_medium_load_is_stable():
    rows = 500
    workers = 8
    samples = []
    for _ in range(3):
        with tempfile.TemporaryDirectory(prefix="jobchain-load-") as root:
            home = _make_home(root, rows)
            claimed, elapsed = _run_claim_load(home, workers)
            assert claimed == rows
            samples.append(elapsed)
    fastest = min(samples)
    slowest = max(samples)
    assert slowest < 30
    # Avoid asserting an exact ratio on noisy CI, but catch an extreme collapse.
    assert slowest < max(5 * fastest, 1.0)
    print("  stability load: " + ", ".join(f"{s:.3f}s" for s in samples))


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    started = time.monotonic()
    for test in tests:
        test()
    print(f"Load tests: {len(tests)}/{len(tests)} passed in {time.monotonic() - started:.3f}s")
    print("Workload: 5,000-row state load + 2,000-row/24-worker contention + stability runs")


if __name__ == "__main__":
    main()
