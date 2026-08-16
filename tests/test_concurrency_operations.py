"""Process-level concurrency tests for run preparation and selection.

The compiled node helper already has direct claim-race coverage in
``test_node.py``. These tests exercise the higher-level run setup boundary,
where multiple independent CLI processes may touch the same .jobchain tree.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import time
import unittest

from tests.helpers import TempProject, require_node_binary


def _run_cli_process(cwd: str, project_root: str, args: list[str], barrier_file: str) -> tuple[int, str, str]:
    """Start a real CLI process after the parent releases all workers."""
    deadline = time.time() + 30
    while not os.path.exists(barrier_file):
        if time.time() >= deadline:
            return 70, "", "timed out waiting for test barrier"
        time.sleep(0.01)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [project_root, env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    completed = subprocess.run(
        [sys.executable, "-m", "jobchain", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


class TestConcurrentRunPreparation(TempProject):
    def setUp(self) -> None:
        super().setUp()
        require_node_binary()
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)

    def _run_workers(self, run_names: list[str]) -> list[tuple[int, str, str]]:
        barrier = self.path("START")
        args = [["run", "config.yaml", "--no-submit", "--run-name", name]
                for name in run_names]
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(len(args)) as pool:
            pending = [pool.apply_async(_run_cli_process,
                                        (self.tmp, os.path.dirname(os.path.dirname(__file__)), item, barrier))
                       for item in args]
            # All workers are waiting here before the marker is created.
            time.sleep(0.15)
            open(barrier, "w", encoding="ascii").close()
            return [item.get(timeout=90) for item in pending]

    def test_same_run_concurrent_preparation_is_consistent(self):
        # State-aware invocations may serialize through the preparation lock
        # and then reuse the prepared run. The invariant is one coherent run.
        results = self._run_workers(["same"] * 8)
        codes = [code for code, _, _ in results]
        self.assertTrue(all(code in (0, 6) for code in codes), results)
        errors = "\n".join(stderr for _, _, stderr in results)
        self.assertNotIn("rows.idx", errors)
        self.assertNotIn("No such file or directory", errors)
        self.assertTrue(os.path.isfile(
            self.path(".jobchain", "same", "rows.idx")))

    def test_same_run_never_creates_multiple_indexes(self):
        self._run_workers(["same"] * 6)
        index = self.path(".jobchain", "same", "rows.idx")
        self.assertTrue(os.path.isfile(index))
        with open(index, encoding="utf-8") as handle:
            rows = [line for line in handle if line.strip()]
        self.assertEqual(len(rows), 4)

    def test_independent_run_names_can_prepare_concurrently(self):
        names = [f"run-{index}" for index in range(6)]
        results = self._run_workers(names)
        self.assertTrue(all(code == 0 for code, _, _ in results), results)
        for name in names:
            self.assertTrue(os.path.isdir(self.path(".jobchain", name)))
            self.assertTrue(os.path.isfile(
                self.path(".jobchain", name, "rows.idx")))

    def test_concurrent_preparation_does_not_duplicate_row_directories(self):
        self._run_workers(["same"] * 8)
        rows = self.path(".jobchain", "same", "rows")
        names = sorted(name for name in os.listdir(rows)
                       if os.path.isdir(os.path.join(rows, name)))
        self.assertEqual(names, ["000001", "000002", "000003", "000004"])

    def test_preparation_lock_is_removed_after_success(self):
        result = self.run_cli("run", "config.yaml", "--no-submit",
                              "--run-name", "first", expect=0)
        self.assertEqual(result.returncode, 0)
        lock = self.path(".jobchain", "first", "lock")
        self.assertFalse(os.path.exists(lock))


class TestConcurrencyExample(TempProject):
    def test_documented_concurrency_example_is_valid(self):
        root = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "examples", "15_concurrency")
        self.write("config.yaml", self.read(os.path.join(root, "config.yaml")))
        self.write("params.psv", self.read(os.path.join(root, "params.psv")))
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.assertTrue(os.path.isfile(self.path(".jobchain", "concurrency-demo",
                                                 "rows.idx")))


if __name__ == "__main__":
    unittest.main()
