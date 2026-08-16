"""Targeted unit tests for remaining operations.py decision paths."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jobchain.operations import (
    RunResult, _validate_only, _prepare_fresh, _submit_chains,
    _record_submissions, _check_params_digest, check_completion, doctor,
    Finding,
)
from jobchain.store import RowState, RunState, StageState, DONE, RUNNING, CLAIMED, FAILED


class TestValidationAndPreparationGaps(unittest.TestCase):
    def test_validate_only_normalizes_and_scans(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(params_path="params.tsv"),
            schema=SimpleNamespace(name="schema", fields=[1]),
            pipeline=SimpleNamespace(specs=[1]),
        )
        normalized = Mock()
        report = Mock()
        with patch("jobchain.operations.normalize_file", return_value=normalized) as norm, \
             patch("jobchain.operations.scan", return_value=report) as scan:
            self.assertIs(_validate_only(prepared), report)
        norm.assert_called_once_with("params.tsv", prepared.schema)
        scan.assert_called_once_with(normalized, prepared.schema, "params.tsv")

    def test_prepare_fresh_non_strict_keeps_invalid_rows(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(strict=False, params_path="p", name="r", scheduler="pbs", width=1,
                                   max_attempts=1, work_dir_template="{run.home}", on_complete="",
                                   source_text="x", effective_workers=1, schema_source={"fields": []}, pipeline_source=None),
            schema=SimpleNamespace(name="s", fields=[], unique_fields=[]),
            pipeline=SimpleNamespace(name="p", specs=[], chaining_stage=None, stage_names=[]),
            store=Mock(),
        )
        prepared.store.home = "/tmp/r"
        normalized = SimpleNamespace(rows=[1], changed_count=0, skipped_blank=0, skipped_comment=0)
        report = SimpleNamespace(
            invalid_rows=[SimpleNamespace(line_num=2, reasons=lambda: ["bad"])],
            valid_rows=[], rows=[1], ok=False,
            to_dict=lambda: {"ok": False},
        )
        rows = [SimpleNamespace(valid=False)]
        with patch("jobchain.operations.normalize_file", return_value=normalized), \
             patch("jobchain.operations.scan", return_value=report), \
             patch("jobchain.operations._create_row_state", return_value=rows), \
             patch("jobchain.operations._generate_scripts"), \
             patch("jobchain.operations.render_final_config", return_value="final"):
            result = _prepare_fresh(prepared)
        self.assertEqual(result.rows_invalid, 1)
        prepared.store.create.assert_called_once()


class TestSubmissionGapClosure(unittest.TestCase):
    def test_submit_chains_records_successful_submission(self):
        store = Mock()
        store.name = "run"
        store.stopped = False
        store.load_config.return_value = {"max_in_flight": 0}
        store.claim.return_value = ("row1", "/tmp/row1")
        store.claim.side_effect = [("row1", "/tmp/row1"), None]
        prepared = SimpleNamespace(store=store, config=SimpleNamespace(max_in_flight=0))
        result = RunResult(store=store)
        with patch("jobchain.operations._submit_row", return_value=([("a", "10")], "")):
            _submit_chains(prepared, 2, result)
        self.assertEqual(result.submitted, [("row1", [("a", "10")])])
        self.assertTrue(result.exhausted)

    def test_record_submissions_all_success(self):
        store = Mock()
        results = [("a", SimpleNamespace(success=True, job_id="1", output="")),
                   ("b", SimpleNamespace(success=True, job_id="2", output=""))]
        jobs, reason = _record_submissions(store, "/run", results)
        self.assertEqual(jobs, [("a", "1"), ("b", "2")])
        self.assertEqual(reason, "")
        self.assertEqual(store.mark.call_count, 2)

    def test_record_submissions_rejected_middle_stage_rolls_back(self):
        store = Mock()
        scheduler = Mock()
        results = [("a", SimpleNamespace(success=True, job_id="1", output="")),
                   ("b", SimpleNamespace(success=False, job_id=None, output="bad"))]
        jobs, reason = _record_submissions(store, "/run", results, scheduler)
        self.assertEqual(jobs, [("a", "1")])
        self.assertEqual(reason, "bad")
        scheduler.cancel.assert_called_once_with("1")
        store.mark.assert_any_call("/run", "a", "CANCELLED", error="cancelled: a later stage was rejected")
        store.mark.assert_any_call("/run", "b", FAILED, error="bad")


class TestDoctorAndDigestGaps(unittest.TestCase):
    def test_digest_missing_parameter_file_adds_finding(self):
        store = Mock()
        result = SimpleNamespace(findings=[])
        with patch("jobchain.operations.os.path.isfile", return_value=False):
            from jobchain.operations import _check_params_digest
            _check_params_digest(store, {"params": "/missing", "params_digest": "abc"}, result)
        self.assertEqual(len(result.findings), 1)
        self.assertIn("no longer exists", result.findings[0].detail)

    def test_digest_changed_parameter_file_adds_finding(self):
        result = SimpleNamespace(findings=[])
        with patch("jobchain.operations.os.path.isfile", return_value=True), \
             patch("jobchain.operations._digest", return_value="new"):
            _check_params_digest(Mock(), {"params": "/p", "params_digest": "old"}, result)
        self.assertEqual(len(result.findings), 1)
        self.assertIn("has changed", result.findings[0].detail)

    def test_doctor_counts_finished_row(self):
        run = RunState(1, stages=[StageState("a", DONE, "1")])
        row = RowState("r", "r", 1, 1, {}, 1, runs=[run], valid=True)
        store = Mock(name="store")
        store.name = "r"
        store.stopped = False
        store.load_config.return_value = {"width": 1, "chaining_stage": ""}
        store.load_rows.return_value = [row]
        scheduler = Mock()
        prepared = SimpleNamespace(store=store, scheduler=scheduler)
        with patch("jobchain.operations.describe_environment", return_value={}):
            result = doctor(prepared)
        self.assertEqual(result.finished_rows, 1)
        scheduler.job_state.assert_not_called()


class TestCompletionGapClosure(unittest.TestCase):
    def test_completion_uses_previous_completion_number(self):
        row = RowState("r", "r", 1, 0, {}, 0, valid=True, runs=[RunState(0, stages=[StageState("a", DONE)])])
        store = Mock()
        store.name = "run"
        store.load_rows.return_value = [row]
        store.done_path = "/tmp/done"
        store.completions_path = "/tmp/completions"
        with tempfile.TemporaryDirectory() as td:
            store.done_path = os.path.join(td, "done.json")
            store.completions_path = os.path.join(td, "completions")
            with open(store.completions_path, "w", encoding="utf-8") as f:
                f.write("2026-01-01T00:00:00 completion=1 done=1 failed=0\n")
            with patch("jobchain.operations.time.strftime", return_value="2026-01-02T00:00:00"):
                payload = check_completion(store)
        self.assertEqual(payload["completion"], 2)
        self.assertEqual(payload["first_completed_at"], "2026-01-01T00:00:00")

    def test_completion_hook_is_invoked(self):
        row = RowState("r", "r", 1, 0, {}, 0, valid=True, runs=[RunState(0, stages=[StageState("a", DONE)])])
        store = Mock()
        store.name = "run"
        store.home = "/tmp/run"
        store.load_rows.return_value = [row]
        with tempfile.TemporaryDirectory() as td:
            store.done_path = os.path.join(td, "done.json")
            store.completions_path = os.path.join(td, "completions")
            with patch("jobchain.operations._run_hook") as hook:
                check_completion(store, "echo done")
            hook.assert_called_once()


if __name__ == "__main__":
    unittest.main()
