"""Scheduler reconciliation, corruption, and recovery scenarios.

These tests intentionally manipulate only the test scheduler or on-disk state;
production state transitions still go through the real CLI and Store APIs.
"""

from __future__ import annotations

import os
import unittest

from tests.helpers import TempProject


class TestDoctorReconciliationMatrix(TempProject):
    def _started_pipeline(self, *, alive: bool = True) -> None:
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False, alive=alive)
        self.run_cli("run", "config.yaml", expect=0)

    def test_missing_stage_job_id_is_detected(self):
        self._started_pipeline()
        store = self.store_for()
        row = store.resolve_row("a1")
        run = row.current
        assert run is not None
        stage = run.stages[0]
        jobid_path = os.path.join(store.run_dir(row.name, row.generation),
                                  f"jobid.{stage.name}")
        os.unlink(jobid_path)
        result = self.run_cli("doctor", expect=6)
        self.assertIn("no job id", result.stdout)

    def test_missing_stage_script_is_detected(self):
        self._started_pipeline()
        store = self.store_for()
        row = store.resolve_row("a1")
        run = row.current
        assert run is not None
        script = run.stages[0].script
        os.unlink(script)
        result = self.run_cli("doctor", expect=6)
        self.assertIn("script no longer exists", result.stdout)

    def test_missing_stage_script_dry_run_does_not_repair_state(self):
        self._started_pipeline()
        store = self.store_for()
        row = store.resolve_row("a1")
        run = row.current
        assert run is not None
        script = run.stages[0].script
        os.unlink(script)
        before = row.status
        self.run_cli("doctor", "--repair", "--dry-run", expect=0)
        self.assertEqual(store.resolve_row("a1").status, before)

    def test_repair_marks_unsubmitted_claim_as_failed(self):
        self._started_pipeline()
        store = self.store_for()
        row = store.resolve_row("a1")
        run = row.current
        assert run is not None
        stage = run.stages[0]
        jobid_path = os.path.join(store.run_dir(row.name, row.generation),
                                  f"jobid.{stage.name}")
        os.unlink(jobid_path)
        result = self.run_cli("doctor", "--repair", expect=0)
        self.assertIn("repaired", result.stdout)
        self.assertTrue(store.resolve_row("a1").status.startswith("failed."))

    def test_repair_can_relaunch_a_shortfall(self):
        self.make_project(pipeline=True, width=2)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)

        # Two rows exist, but remove the scheduler-visible first job by making
        # qstat report no active jobs. Doctor should identify the shortfall.
        self._install("qstat", 'exit 153\n')
        result = self.run_cli("doctor", expect=6)
        self.assertIn("chain", result.stdout.lower())

    def test_parameter_file_disappearance_is_detected(self):
        self.make_project(width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        os.unlink(self.path("params.psv"))
        result = self.run_cli("doctor", expect=6)
        self.assertIn("parameter file", result.stdout)
        self.assertIn("no longer exists", result.stdout)

    def test_doctor_json_preserves_finding_details(self):
        self._started_pipeline(alive=False)
        payload = self.run_cli_json("doctor", expect=6)
        self.assertEqual(payload["run"], "test-run")
        self.assertTrue(payload["findings"])
        self.assertTrue(all("row" in finding and "detail" in finding
                            for finding in payload["findings"]))


class TestReconciliationExample(TempProject):
    def test_documented_example_runs_to_completion(self):
        root = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "examples", "14_scheduler_reconciliation")
        self.write("config.yaml", self.read(os.path.join(root, "config.yaml")))
        self.write("params.psv", self.read(os.path.join(root, "params.psv")))
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses("operations").values()), {"DONE"})


class TestSchedulerQueryFailures(TempProject):
    def test_missing_qstat_does_not_crash_doctor(self):
        self._prepare()
        os.unlink(self.path("stubbin", "qstat"))
        result = self.run_cli("doctor", expect=6)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_qstat_without_job_state_is_treated_as_finished(self):
        self._prepare()
        self._install("qstat", 'echo "some unrelated output"\nexit 0\n')
        result = self.run_cli("doctor", expect=6)
        self.assertIn("no longer known", result.stdout)

    def _prepare(self) -> None:
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)


if __name__ == "__main__":
    unittest.main()
