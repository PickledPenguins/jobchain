"""Regression tests for the asynchronous scheduler test fixture."""

from __future__ import annotations

import os
import subprocess
import time

from .helpers import TempProject


class TestSchedulerFixtureLifecycle(TempProject):
    """The stub scheduler must not leak jobs across test cases."""

    def test_wait_for_jobs_reaches_true_quiescence(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()

        state = self.path("stubbin", "state")
        active_markers = [
            name for name in os.listdir(state)
            if name.startswith("running.")
            or name.startswith("pid.")
            or name.startswith("qpid.")
        ]
        self.assertEqual(active_markers, [])


    def test_async_submission_client_returns_without_waiting_for_job(self):
        """The qsub stub must behave like a real asynchronous scheduler client."""
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()

        # The job sleeps far longer than any plausible process-launch jitter
        # on a loaded machine, so a generous elapsed threshold still clearly
        # distinguishes "qsub returned immediately" from "qsub waited for
        # the job," without the test flaking under contention the way a
        # threshold close to the sleep duration would.
        job_seconds = 2.0
        script = self.write_executable(
            "slow.sh",
            f"#!/bin/sh\nsleep {job_seconds}\n",
        )
        started = time.monotonic()
        result = subprocess.run(
            [os.path.join(self.bin_dir, "qsub"), script],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=job_seconds + 5,
            check=False,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, job_seconds / 2)
        self.wait_for_jobs()

    def test_dependency_chain_reaches_quiescence_repeatedly(self):
        """Repeated asynchronous pipelines must not accumulate stale markers."""
        for _ in range(3):
            self.make_project(pipeline=True, width=1, name=f"test-run-{_}")
            self.install_scheduler()
            self.run_cli("run", "config.yaml", expect=0)
            self.wait_for_jobs()

            state = self.path("stubbin", "state")
            active = [
                name for name in os.listdir(state)
                if name.startswith("running.")
                or name.startswith("pid.")
                or name.startswith("qpid.")
            ]
            self.assertEqual(active, [])

    def test_scheduler_cleanup_is_safe_when_a_job_is_still_active(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)

        # Do not call wait_for_jobs: teardown must clean up an asynchronous
        # scheduler job even when the test itself finishes early.
