#!/usr/bin/env python3
"""Concurrency and race-condition tests for jobchain.

These tests use real processes and the real node helper. They focus on
invariants that cannot be established by single-process unit tests.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from jobchain.store import Store

NODE = os.path.join(ROOT, "bin", "jobchain-node")


def _claim_worker(home: str, gate, results) -> None:
    gate.wait()
    try:
        result = Store(home).claim()
        results.put(("won", result[0], result[1]) if result else ("empty", "", ""))
    except Exception as exc:  # pragma: no cover - exercised in child process
        results.put(("error", type(exc).__name__, str(exc)))


def _lock_worker(home: str, gate, release, results) -> None:
    gate.wait()
    store = Store(home)
    try:
        store.acquire_lock()
    except Exception as exc:
        results.put(("lost", type(exc).__name__, str(exc)))
        return
    results.put(("won", os.getpid(), ""))
    release.wait()
    store.release_lock()


def _claim_loop(home: str, gate, started, results) -> None:
    gate.wait()
    store = Store(home)
    while True:
        claimed = store.claim()
        if claimed is None:
            break
        results.put(("won", claimed[0], claimed[1]))
        started.set()


def _make_home(root: str, rows: int, generation: int = 1) -> str:
    home = os.path.join(root, "home")
    rows_dir = os.path.join(home, "rows")
    os.makedirs(rows_dir, exist_ok=True)
    names = []
    for index in range(rows):
        name = f"{index + 1:06d}"
        row = os.path.join(rows_dir, name)
        os.makedirs(row, exist_ok=True)
        with open(os.path.join(row, "gen"), "w", encoding="utf-8") as f:
            f.write(f"{generation}\n")
        with open(os.path.join(row, "meta.json"), "w", encoding="utf-8") as f:
            f.write('{"name":"000001","row_id":"000001","line_num":1,"index":0,"params":{}}')
        with open(os.path.join(row, "env"), "w", encoding="utf-8") as f:
            f.write(f"JC_index='{index}'\nexport JC_index\n")
        with open(os.path.join(row, "manifest"), "w", encoding="utf-8") as f:
            f.write("only\t-\t/bin/true\n")
        names.append(name)
    with open(os.path.join(home, "rows.idx"), "w", encoding="utf-8") as f:
        f.write("".join(f"{name}\n" for name in names))
    return home


def _run(workers, target, *args):
    ctx = multiprocessing.get_context("fork")
    gate = ctx.Event()
    results = ctx.Queue()
    processes = [ctx.Process(target=target, args=(*args, gate, results)) for _ in range(workers)]
    for process in processes:
        process.start()
    gate.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0, f"worker exited {process.exitcode}"
    values = [results.get(timeout=2) for _ in processes]
    return values


def test_python_claim_wrapper_has_one_winner_per_row():
    with tempfile.TemporaryDirectory(prefix="jobchain-concurrency-") as root:
        home = _make_home(root, 50)
        results = _run(32, _claim_worker, home)
        won = [value[1] for value in results if value[0] == "won"]
        assert len(won) == 32
        assert len(set(won)) == 32
        assert not [value for value in results if value[0] == "error"]


def test_setup_lock_has_exactly_one_owner_while_held():
    with tempfile.TemporaryDirectory(prefix="jobchain-concurrency-") as root:
        home = _make_home(root, 1)
        ctx = multiprocessing.get_context("fork")
        gate = ctx.Event()
        release = ctx.Event()
        results = ctx.Queue()
        processes = [ctx.Process(target=_lock_worker, args=(home, gate, release, results)) for _ in range(24)]
        for process in processes:
            process.start()
        gate.set()
        values = [results.get(timeout=5) for _ in processes]
        winners = [value for value in values if value[0] == "won"]
        losers = [value for value in values if value[0] == "lost"]
        assert len(winners) == 1
        assert len(losers) == 23
        release.set()
        for process in processes:
            process.join(5)
            assert process.exitcode == 0


def test_stop_makes_claiming_quiescent():
    with tempfile.TemporaryDirectory(prefix="jobchain-concurrency-") as root:
        home = _make_home(root, 100)
        store = Store(home)
        ctx = multiprocessing.get_context("fork")
        gate = ctx.Event()
        started = ctx.Event()
        results = ctx.Queue()
        processes = [ctx.Process(target=_claim_loop, args=(home, gate, started, results)) for _ in range(12)]
        for process in processes:
            process.start()
        gate.set()
        assert started.wait(5), "no worker reached the claim boundary"
        # Stop after a real claim has occurred. Any claim that already won
        # remains valid; after the stop marker exists, no new claim is allowed.
        store.stop("concurrency test")
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        claimed = []
        while not results.empty():
            value = results.get()
            if value[0] == "won":
                claimed.append(value[1])
        assert len(claimed) == len(set(claimed))
        assert len(claimed) <= 100
        assert Store(home).claim() is None


def test_generation_bump_after_contention_creates_distinct_attempt():
    with tempfile.TemporaryDirectory(prefix="jobchain-concurrency-") as root:
        home = _make_home(root, 1)
        results = _run(16, _claim_worker, home)
        won = [value for value in results if value[0] == "won"]
        assert len(won) == 1
        assert won[0][2].endswith("run-1")
        store = Store(home)
        generation = store.bump_generation("000001")
        assert generation == 2
        results = _run(16, _claim_worker, home)
        won2 = [value for value in results if value[0] == "won"]
        assert len(won2) == 1
        assert won2[0][2].endswith("run-2")
        assert os.path.isdir(os.path.join(home, "rows", "000001", "run-1"))
        assert os.path.isdir(os.path.join(home, "rows", "000001", "run-2"))


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"Concurrency tests: {len(tests)}/{len(tests)} passed")
    print("Process contention: real multiprocessing + real node helper")


if __name__ == "__main__":
    main()
