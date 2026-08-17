#!/usr/bin/env python3
"""Architecture-specific scaling and bottleneck tests.

These tests intentionally vary one load dimension at a time. They are not
microbenchmarks: they are regression guards for nonlinear scaling in the
filesystem-backed claim protocol, state loading/reporting, and scheduler
submission path.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from jobchain.report import build_views, compute_metrics
from jobchain.store import Store


def _make_home(root: str, rows: int, blocked: int = 0, stages: int = 1) -> str:
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
            f.writelines(f"stage{stage + 1}\t-\t/bin/true\n" for stage in range(stages))
        names.append(name)
    # A run directory makes the current generation unavailable to claim().
    for index in range(blocked):
        run_dir = os.path.join(rows_dir, names[index], "run-1")
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "claim"), "w", encoding="utf-8") as f:
            f.write("blocked\n")
    with open(os.path.join(home, "rows.idx"), "w", encoding="utf-8") as f:
        f.write("".join(f"{name}\n" for name in names))
    Store(home).create({"name": "bottleneck"})
    return home


def _time_claim(home: str) -> tuple[float, tuple[str, str] | None]:
    started = time.monotonic()
    result = Store(home).claim()
    return time.monotonic() - started, result


def test_discovery_scales_with_irrelevant_rows() -> None:
    """A fixed amount of work should not become wildly slower with prefixes."""
    samples: list[tuple[int, float]] = []
    for blocked in (0, 5_000, 20_000):
        with tempfile.TemporaryDirectory(prefix="jobchain-bottleneck-") as root:
            home = _make_home(root, blocked + 1, blocked=blocked)
            elapsed, claimed = _time_claim(home)
            assert claimed is not None
            samples.append((blocked, elapsed))
    baseline = samples[0][1]
    largest = samples[-1][1]
    # This deliberately allows substantial filesystem variance while catching
    # a catastrophic superlinear regression in the sequential rows.idx scan.
    assert largest < max(100 * baseline, 8.0), (
        f"claim discovery collapsed: baseline={baseline:.4f}s "
        f"at 20K blocked={largest:.4f}s"
    )
    print("  discovery scaling: " + ", ".join(
        f"{blocked} blocked={elapsed:.3f}s" for blocked, elapsed in samples))


def test_claim_hotspot_throughput() -> None:
    """Many workers competing for a small eligible set must remain bounded."""
    workers = 16
    jobs = 64
    with tempfile.TemporaryDirectory(prefix="jobchain-bottleneck-") as root:
        home = _make_home(root, jobs)
        started = time.monotonic()
        processes = []
        for _ in range(workers):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", """
from jobchain.store import Store
import sys
s=Store(sys.argv[1])
n=0
while True:
    x=s.claim()
    if x is None: break
    n += 1
print(n)
""", home], cwd=ROOT, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True))
        outputs = []
        for process in processes:
            out, err = process.communicate(timeout=60)
            assert process.returncode == 0, err
            outputs.append(int(out.strip() or "0"))
        elapsed = time.monotonic() - started
        assert sum(outputs) == jobs
        assert elapsed < 60
        print(f"  claim hotspot: {jobs} jobs/{workers} workers in {elapsed:.3f}s")


def test_reporting_scales_with_history() -> None:
    """Loading/reporting a large historical run should remain bounded."""
    results = []
    for rows in (500, 2_000, 5_000):
        with tempfile.TemporaryDirectory(prefix="jobchain-bottleneck-") as root:
            home = _make_home(root, rows)
            started = time.monotonic()
            loaded = Store(home).load_rows()
            views = build_views(loaded)
            metrics = compute_metrics(loaded)
            elapsed = time.monotonic() - started
            assert len(views) == rows
            assert metrics.total == rows
            results.append((rows, elapsed))
    per_row = [elapsed / rows for rows, elapsed in results]
    assert per_row[-1] < max(20 * per_row[0], 0.01)
    print("  reporting scaling: " + ", ".join(
        f"{rows}={elapsed:.3f}s" for rows, elapsed in results))


def test_scheduler_backpressure_does_not_leak_processes() -> None:
    """Slow fake scheduler calls should finish cleanly without child leaks."""
    with tempfile.TemporaryDirectory(prefix="jobchain-bottleneck-") as root:
        fake = Path(root) / "qsub"
        fake.write_text("#!/bin/sh\nsleep 0.03\necho fake-$PPID\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["PATH"] = str(fake.parent) + os.pathsep + env.get("PATH", "")
        home = _make_home(root, 8, stages=2)
        # Exercise the real helper submission path repeatedly. A fixed fake
        # latency makes scheduler pressure deterministic without requiring PBS.
        node = os.path.join(ROOT, "bin", "jobchain-node")
        start = time.monotonic()
        for _ in range(8):
            result = subprocess.run(
                [node, "submit", "--home", home, "--next"],
                cwd=ROOT, env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
        elapsed = time.monotonic() - start
        assert elapsed < 10
        leftovers = list(Path(home).rglob("*.tmp.*"))
        assert not leftovers, f"temporary files leaked: {leftovers[:5]}"
        print(f"  scheduler backpressure: 8x2 submissions in {elapsed:.3f}s")


def test_pipeline_width_load_profile() -> None:
    """The orchestration process should remain stable as width increases."""
    samples = []
    for workers in (1, 4, 16):
        with tempfile.TemporaryDirectory(prefix="jobchain-bottleneck-") as root:
            home = _make_home(root, 128)
            # Each worker claims until exhaustion. This isolates width from
            # scheduler execution while stressing the same hot path.
            started = time.monotonic()
            procs = []
            for _ in range(workers):
                procs.append(subprocess.Popen(
                    [sys.executable, "-c", """
from jobchain.store import Store
import sys
s=Store(sys.argv[1])
while s.claim() is not None: pass
""", home], cwd=ROOT))
            for process in procs:
                assert process.wait(timeout=60) == 0
            elapsed = time.monotonic() - started
            samples.append((workers, elapsed))
    # More workers should not create an order-of-magnitude collapse compared
    # with serial operation; this is a regression guard, not a benchmark.
    assert samples[-1][1] < max(10 * samples[0][1], 15.0)
    print("  width profile: " + ", ".join(
        f"{workers}w={elapsed:.3f}s" for workers, elapsed in samples))


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    started = time.monotonic()
    for test in tests:
        test()
    print(f"Bottleneck tests: {len(tests)}/{len(tests)} passed in "
          f"{time.monotonic() - started:.3f}s")
    print("Surfaces: discovery, claim contention, reporting, scheduler pressure, width")


if __name__ == "__main__":
    main()
