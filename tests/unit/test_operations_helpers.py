"""Focused unit coverage for operations.py helpers and decision logic."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jobchain.operations import (
    _check_params_digest,
    RunResult,
    _describe_changes,
    _digest,
    _directory_size,
    _existing_output,
    _identifier_for,
    _pipeline_document,
    _raw_fields,
    _read_completions,
    _record_submissions,
    _schema_document,
    _run_hook,
    _create_row_state,
    _generate_scripts,
    _submit_chains,
    _submit_row,
    _submit_selected,
    execute_rerun,
    cancel,
    check_completion,
    doctor,
    plan_rerun,
)
from jobchain.schema import Field, Schema
from jobchain.schema import Int, Str, Bool
from jobchain.store import DONE, FAILED, PENDING, RUNNING, RowState, RunState, StageState
from jobchain.scheduler import FINISHED, ALIVE


class TestDigestAndFilesystemHelpers(unittest.TestCase):
    def test_digest_matches_content_and_changes_after_edit(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "input")
            with open(path, "wb") as handle:
                handle.write(b"abc")
            first = _digest(path)
            with open(path, "wb") as handle:
                handle.write(b"abd")
            self.assertNotEqual(first, _digest(path))

    def test_digest_missing_file_returns_empty(self):
        self.assertEqual(_digest("/definitely/missing/jobchain-file"), "")

    def test_directory_size_counts_files_and_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "nested"))
            with open(os.path.join(root, "a"), "wb") as handle:
                handle.write(b"123")
            with open(os.path.join(root, "nested", "b"), "wb") as handle:
                handle.write(b"4567")
            self.assertEqual(_directory_size(root), (2, 7))
            self.assertEqual(_directory_size(root, depth=1), (2, 7))

    def test_directory_size_missing_path_is_empty(self):
        self.assertEqual(_directory_size("/definitely/missing/jobchain-dir"), (0, 0))

    def test_existing_output_prefers_nonempty_child_directories(self):
        with tempfile.TemporaryDirectory() as root:
            child = os.path.join(root, "results")
            os.makedirs(child)
            with open(os.path.join(child, "out"), "w", encoding="utf-8") as handle:
                handle.write("hello")
            row = SimpleNamespace(work_dir=root)
            found = _existing_output(row)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0][0], child)
            self.assertEqual(found[0][1:], (1, 5))

    def test_existing_output_handles_missing_work_directory(self):
        self.assertEqual(_existing_output(SimpleNamespace(work_dir="/missing")), [])


class TestRowHelpers(unittest.TestCase):
    def setUp(self):
        self.schema = Schema(
            name="s",
            fields=[Field("rid", [Str()]), Field("count", [Int()])],
            id_field="rid",
        )

    def test_identifier_uses_typed_id(self):
        result = SimpleNamespace(raw_fields=[])
        self.assertEqual(_identifier_for(self.schema, result, {"rid": 42}, "000001"), "42")

    def test_identifier_recovers_raw_id_for_invalid_row(self):
        result = SimpleNamespace(raw_fields=["bad-id", "x"])
        self.assertEqual(_identifier_for(self.schema, result, {}, "000001"), "bad-id")

    def test_identifier_falls_back_to_row_name(self):
        result = SimpleNamespace(raw_fields=["", "x"])
        self.assertEqual(_identifier_for(self.schema, result, {}, "000001"), "000001")

    def test_raw_fields_preserves_invalid_raw_values(self):
        row = SimpleNamespace(params={}, raw_fields=["x"])
        self.assertEqual(_raw_fields(self.schema, row), ["x", ""])

    def test_raw_fields_serializes_typed_values(self):
        row = SimpleNamespace(params={"rid": "x", "count": 4}, raw_fields=[])
        self.assertEqual(_raw_fields(self.schema, row), ["x", "4"])

    def test_raw_fields_serializes_booleans(self):
        schema = Schema(name="s", fields=[Field("enabled", [Bool()])])
        row = SimpleNamespace(params={"enabled": True}, raw_fields=[])
        self.assertEqual(_raw_fields(schema, row), ["true"])

    def test_describe_changes_rejects_unknown_columns(self):
        row = SimpleNamespace(params={"rid": "a", "count": 1})
        with self.assertRaises(Exception):
            _describe_changes(self.schema, row, {"missing": "x"})

    def test_describe_changes_returns_old_and_new_values(self):
        row = SimpleNamespace(params={"rid": "a", "count": 1})
        self.assertEqual(
            _describe_changes(self.schema, row, {"count": "2"}),
            {"count": (1, "2")},
        )


class TestCapturedConfigurationHelpers(unittest.TestCase):
    def test_schema_document_keeps_inline_schema(self):
        prepared = SimpleNamespace(config=SimpleNamespace(schema_source={"fields": []}))
        self.assertEqual(_schema_document(prepared), {"fields": []})

    def test_schema_document_absolutizes_relative_path(self):
        prepared = SimpleNamespace(config=SimpleNamespace(
            schema_source="schema.yaml", base_dir="/tmp/project"))
        self.assertEqual(_schema_document(prepared), "/tmp/project/schema.yaml")

    def test_schema_document_preserves_absolute_path(self):
        prepared = SimpleNamespace(config=SimpleNamespace(
            schema_source="/tmp/schema.yaml", base_dir="/tmp/project"))
        self.assertEqual(_schema_document(prepared), "/tmp/schema.yaml")

    def test_pipeline_document_handles_none(self):
        prepared = SimpleNamespace(config=SimpleNamespace(pipeline_source=None))
        self.assertIsNone(_pipeline_document(prepared))

    def test_pipeline_document_keeps_inline_document_and_absolutizes_module(self):
        prepared = SimpleNamespace(config=SimpleNamespace(
            pipeline_source={"stage_module": "stages.py", "stages": []},
            base_dir="/tmp/project"))
        self.assertEqual(
            _pipeline_document(prepared),
            {"stage_module": "/tmp/project/stages.py", "stages": []},
        )

    def test_pipeline_document_preserves_absolute_module(self):
        prepared = SimpleNamespace(config=SimpleNamespace(
            pipeline_source={"stage_module": "/tmp/stages.py"},
            base_dir="/tmp/project"))
        self.assertEqual(_pipeline_document(prepared), {"stage_module": "/tmp/stages.py"})


class TestSubmissionRecording(unittest.TestCase):
    def test_record_submissions_records_successful_jobs(self):
        store = Mock()
        results = [
            ("prep", SimpleNamespace(success=True, job_id="1", output="")),
            ("solve", SimpleNamespace(success=True, job_id="2", output="")),
        ]
        jobs, failure = _record_submissions(store, "run", results)
        self.assertEqual(jobs, [("prep", "1"), ("solve", "2")])
        self.assertEqual(failure, "")
        self.assertEqual(store.mark.call_count, 2)

    def test_record_submissions_cancels_prior_jobs_after_rejection(self):
        store = Mock()
        scheduler = Mock()
        results = [
            ("prep", SimpleNamespace(success=True, job_id="1", output="")),
            ("solve", SimpleNamespace(success=False, job_id="", output="boom")),
        ]
        jobs, failure = _record_submissions(store, "run", results, scheduler)
        self.assertEqual(jobs, [("prep", "1")])
        self.assertEqual(failure, "boom")
        scheduler.cancel.assert_called_once_with("1")
        self.assertEqual(store.mark.call_count, 3)


class TestCompletionHelpers(unittest.TestCase):
    def test_read_completions_handles_missing_file(self):
        store = SimpleNamespace(completions_path="/missing/completions")
        self.assertEqual(_read_completions(store), [])

    def test_read_completions_reads_dates_only(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "completions")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("2026-01-01T00:00:00 completion=1 done=2\n")
                handle.write("\n")
                handle.write("2026-01-02T00:00:00 completion=2 done=3\n")
            self.assertEqual(_read_completions(SimpleNamespace(completions_path=path)),
                             ["2026-01-01T00:00:00", "2026-01-02T00:00:00"])

    def test_check_params_digest_ignores_missing_recorded_digest(self):
        result = SimpleNamespace(findings=[])
        _check_params_digest(Mock(), {"params": "/missing", "params_digest": ""}, result)
        self.assertEqual(result.findings, [])

    def test_check_params_digest_reports_missing_file(self):
        result = SimpleNamespace(findings=[])
        _check_params_digest(Mock(), {"params": "/missing", "params_digest": "abc"}, result)
        self.assertIn("no longer exists", result.findings[0].detail)

    def test_check_params_digest_reports_external_change(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "params")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("one")
            result = SimpleNamespace(findings=[])
            _check_params_digest(Mock(), {"params": path, "params_digest": "wrong"}, result)
            self.assertIn("changed since", result.findings[0].detail)

    def test_run_hook_does_not_raise_when_command_fails(self):
        store = SimpleNamespace(name="run", home="/tmp/run")
        payload = {"completion": 1, "rows": {"done": 1, "failed": 0}}
        with patch("jobchain.operations.subprocess.run", side_effect=RuntimeError("no shell")):
            _run_hook(store, "false", payload)


class TestRerunPlanning(unittest.TestCase):
    def _prepared(self):
        pipeline = SimpleNamespace(stage_names=["prep", "solve", "archive"])
        store = Mock()
        store.load_config.return_value = {"max_attempts": 3}
        config = SimpleNamespace(work_dir_template="{run.home}/{row_name}")
        schema = self.schema = Schema(
            name="s", fields=[Field("rid", [Str()]), Field("count", [Int()])], id_field="rid")
        return SimpleNamespace(pipeline=pipeline, store=store, config=config, schema=schema)

    def _row(self, status=PENDING, attempts=0):
        runs = [RunState(generation=i + 1, stages=[
            StageState(name="prep", status=status),
            StageState(name="solve", status=status),
            StageState(name="archive", status=status),
        ]) for i in range(max(1, attempts))]
        return RowState(name="000001", row_id="a", line_num=2, index=0,
                        params={"rid": "a", "count": 1}, generation=1,
                        runs=runs, valid=True)

    def test_plan_defaults_to_all_stages_and_new_generation(self):
        prepared = self._prepared()
        plan = plan_rerun(prepared, [self._row()])
        self.assertEqual(plan.stages, ["prep", "solve", "archive"])
        self.assertTrue(plan.new_generation)

    def test_plan_from_stage_keeps_generation(self):
        prepared = self._prepared()
        plan = plan_rerun(prepared, [self._row()], from_stage="solve")
        self.assertEqual(plan.stages, ["solve", "archive"])
        self.assertFalse(plan.new_generation)

    def test_plan_rejects_unknown_stage(self):
        prepared = self._prepared()
        with self.assertRaises(Exception):
            plan_rerun(prepared, [self._row()], stages=["missing"])

    def test_plan_skips_active_rows_without_force(self):
        prepared = self._prepared()
        plan = plan_rerun(prepared, [self._row(status="RUNNING")])
        self.assertFalse(plan.rows)
        self.assertTrue(plan.skipped)

    def test_plan_skips_attempt_limit_without_force(self):
        prepared = self._prepared()
        plan = plan_rerun(prepared, [self._row(attempts=3)])
        self.assertFalse(plan.rows)
        self.assertIn("limit", plan.skipped[0][1])


if __name__ == "__main__":
    unittest.main()

class TestOperationDecisionBranches(unittest.TestCase):
    def _prepared(self, rows=None, config=None, scheduler=None, stopped=False):
        store = Mock()
        store.name = "run"
        store.home = "/tmp/run"
        store.stopped = stopped
        store.load_rows.return_value = rows or []
        store.load_config.return_value = config or {"width": 2, "chaining_stage": "solve"}
        store.run_dir.side_effect = lambda name, generation: f"/tmp/run/{name}/g{generation}"
        pipeline = SimpleNamespace(stage_names=["prep", "solve"], specs=[], stage=lambda n: None)
        prepared = SimpleNamespace(store=store, scheduler=scheduler or Mock(), config=SimpleNamespace(work_dir_template="{run.home}/{row_name}"), pipeline=pipeline, schema=None)
        return prepared

    def _row(self, name="000001", status=RUNNING, jobid="123", script=""):
        stage = StageState(name="solve", status=status, jobid=jobid, script=script)
        run = RunState(generation=1, stages=[stage])
        return RowState(name=name, row_id=name, line_num=2, index=0, params={}, generation=1, runs=[run], valid=True)

    def test_cancel_dry_run_collects_active_job_ids_without_scheduler_calls(self):
        scheduler = Mock()
        prepared = self._prepared(scheduler=scheduler)
        result = cancel(prepared, [self._row()], dry_run=True)
        self.assertEqual(result.cancelled, [("000001", ["123"])])
        scheduler.cancel.assert_not_called()

    def test_cancel_skips_never_claimed_and_terminal_rows(self):
        scheduler = Mock()
        prepared = self._prepared(scheduler=scheduler)
        pending = RowState(name="p", row_id="p", line_num=1, index=0, params={}, generation=1, runs=[], valid=True)
        done = self._row(name="d", status=DONE, jobid="999")
        result = cancel(prepared, [pending, done])
        self.assertEqual(len(result.cancelled), 0)
        self.assertEqual(len(result.skipped), 2)

    def test_cancel_stop_sets_stopped_and_cancels(self):
        scheduler = Mock()
        scheduler.cancel.return_value = (True, "")
        prepared = self._prepared(scheduler=scheduler)
        result = cancel(prepared, [self._row()], stop=True)
        self.assertTrue(result.stopped)
        scheduler.cancel.assert_called_once_with("123")

    def test_cancel_records_scheduler_failure(self):
        scheduler = Mock()
        scheduler.cancel.return_value = (False, "scheduler rejected")
        prepared = self._prepared(scheduler=scheduler)
        result = cancel(prepared, [self._row()])
        self.assertEqual(result.cancelled, [("000001", ["123"])])

    def test_doctor_reports_missing_job_and_script(self):
        scheduler = Mock()
        scheduler.job_state.return_value = FINISHED
        row = self._row(script="/missing/script.sh")
        prepared = self._prepared(rows=[row], scheduler=scheduler)
        result = doctor(prepared)
        self.assertFalse(result.ok)
        self.assertTrue(any("no longer known" in f.detail for f in result.findings))
        self.assertTrue(any("script no longer exists" in f.detail for f in result.findings))

    def test_doctor_repairs_missing_job(self):
        scheduler = Mock()
        scheduler.job_state.return_value = FINISHED
        row = self._row()
        prepared = self._prepared(rows=[row], scheduler=scheduler)
        result = doctor(prepared, repair=True)
        self.assertTrue(any(f.repaired for f in result.findings))
        prepared.store.mark.assert_called()

    def test_doctor_reports_unsubmitted_stage(self):
        row = self._row(jobid="")
        prepared = self._prepared(rows=[row])
        result = doctor(prepared)
        self.assertTrue(any("no job id" in f.detail for f in result.findings))

    def test_doctor_reports_stopped_run(self):
        prepared = self._prepared(stopped=True)
        result = doctor(prepared)
        self.assertTrue(result.stopped)
        self.assertTrue(any("stopped" in f.detail for f in result.findings))

    def test_doctor_reports_invalid_rows(self):
        row = self._row()
        row.valid = False
        prepared = self._prepared(rows=[row])
        result = doctor(prepared)
        self.assertTrue(any("failed validation" in f.detail for f in result.findings))

    def test_check_completion_returns_none_for_empty_or_outstanding(self):
        store = Mock()
        store.done_path = "/tmp/nonexistent-jobchain-done"
        store.completions_path = "/tmp/nonexistent-jobchain-completions"
        store.load_rows.return_value = []
        self.assertIsNone(check_completion(store))
        row = self._row(status=RUNNING)
        store.load_rows.return_value = [row]
        self.assertIsNone(check_completion(store))

    def test_check_completion_writes_marker_for_terminal_rows(self):
        with tempfile.TemporaryDirectory() as root:
            store = Mock()
            store.name = "run"
            store.done_path = os.path.join(root, "done.json")
            store.completions_path = os.path.join(root, "completions")
            row = self._row(status=DONE, jobid="123")
            store.load_rows.return_value = [row]
            payload = check_completion(store)
            self.assertEqual(payload["rows"]["done"], 1)
            self.assertTrue(os.path.isfile(store.done_path))
            self.assertTrue(os.path.isfile(store.completions_path))


class TestGenerationAndSubmissionHelpers(unittest.TestCase):
    def _prepared(self, rows=None):
        store = Mock()
        store.home = "/tmp/jobchain"
        store.name = "run"
        store.stopped = False
        store.load_rows.return_value = rows or []
        store.load_config.return_value = {"width": 2, "max_in_flight": 0, "chaining_stage": "solve"}
        store.claim.side_effect = [("000001", "/tmp/jobchain/000001"), None]
        scheduler = Mock()
        scheduler.submit_pipeline.return_value = []
        stage = Mock()
        stage.output_dir.return_value = "/tmp/jobchain/000001"
        stage.script_name.return_value = "job.sh"
        def write_script(params, ctx):
            os.makedirs(os.path.dirname(ctx.script_path), exist_ok=True)
            with open(ctx.script_path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\n")
            return ctx.script_path
        stage.write_script.side_effect = write_script
        stage.effective_resources.return_value = {}
        spec = SimpleNamespace(name="solve", position=1, depends="-", chains_next=False)
        pipeline = SimpleNamespace(specs=[spec], stage_names=["solve"], stage=lambda n: stage)
        config = SimpleNamespace(effective_workers=1, max_in_flight=0, width=2,
                                work_dir_template="{run.home}/{row_name}")
        run_context = Mock()
        run_context.work_dir.return_value = "/tmp/jobchain/000001"
        store.run_dir.side_effect = lambda name, generation: f"/tmp/jobchain/{name}/g{generation}"
        return SimpleNamespace(store=store, scheduler=scheduler, pipeline=pipeline,
                               config=config, run_context=run_context)

    def test_create_row_state_writes_valid_and_invalid_rows(self):
        store = Mock()
        store.home = "/tmp/run"
        store.load_row.side_effect = lambda name: SimpleNamespace(name=name)
        schema = Schema(name="s", fields=[Field("id", [Str()])], id_field="id")
        config = SimpleNamespace(work_dir_template="{run.home}/{row_name}", name="run")
        prepared = SimpleNamespace(store=store, schema=schema, config=config)
        report = SimpleNamespace(rows=[
            SimpleNamespace(index=0, line_num=2, ok=True, record={"id": "a"}, raw_fields=[], reasons=lambda: [], failure_id=lambda: ""),
            SimpleNamespace(index=1, line_num=3, ok=False, record=None, raw_fields=["bad"], reasons=lambda: ["bad"], failure_id=lambda: "f1"),
        ])
        rows = _create_row_state(prepared, report)
        self.assertEqual(len(rows), 2)
        self.assertEqual(store.write_row.call_count, 2)
        store.write_index.assert_called_once_with(["000001", "000002"])

    def test_generate_scripts_returns_for_empty_rows(self):
        prepared = self._prepared()
        result = SimpleNamespace(scripts_written=0)
        _generate_scripts(prepared, [], result)
        self.assertEqual(result.scripts_written, 0)

    def test_generate_scripts_writes_manifest(self):
        prepared = self._prepared()
        row = SimpleNamespace(name="000001", index=0, generation=1, params={})
        result = SimpleNamespace(scripts_written=0)
        _generate_scripts(prepared, [row], result)
        prepared.store.write_manifest.assert_called_once()
        self.assertEqual(result.scripts_written, 1)

    def test_submit_row_rejects_missing_manifest(self):
        prepared = self._prepared()
        prepared.store.read_manifest.return_value = []
        jobs, error = _submit_row(prepared, "000001", "/tmp/run")
        self.assertEqual(jobs, [])
        self.assertIn("no manifest", error)

    def test_submit_row_records_scheduler_results(self):
        prepared = self._prepared()
        prepared.store.read_manifest.return_value = [("solve", "-", "/tmp/job.sh")]
        prepared.store.load_row.return_value = SimpleNamespace(params={})
        prepared.scheduler.submit_pipeline.return_value = [("solve", SimpleNamespace(success=True, job_id="9", output=""))]
        jobs, error = _submit_row(prepared, "000001", "/tmp/run")
        self.assertEqual(jobs, [("solve", "9")])
        self.assertEqual(error, "")

    def test_submit_chains_stopped_run_is_rejected(self):
        prepared = self._prepared()
        prepared.store.stopped = True
        with self.assertRaises(Exception):
            _submit_chains(prepared, 1, SimpleNamespace(submitted=[], failures=[], exhausted=False))

    def test_submit_chains_stops_when_no_rows_can_be_claimed(self):
        prepared = self._prepared()
        prepared.store.claim.return_value = None
        prepared.store.claim.side_effect = None
        result = SimpleNamespace(submitted=[], failures=[], exhausted=False)
        _submit_chains(prepared, 2, result)
        self.assertTrue(result.exhausted)

    def test_submit_selected_rejects_missing_manifest(self):
        prepared = self._prepared()
        prepared.store.read_manifest.return_value = []
        jobs, error = _submit_selected(prepared, "000001", ["solve"], False)
        self.assertEqual(jobs, [])
        self.assertIn("no manifest", error)

    def test_submit_selected_skips_unrequested_manifest_entries(self):
        prepared = self._prepared()
        prepared.store.load_row.return_value = SimpleNamespace(generation=1)
        prepared.store.read_manifest.return_value = [
            ("prep", "-", "/tmp/prep.sh"), ("solve", "1", "/tmp/solve.sh")]
        prepared.scheduler.submit_pipeline.return_value = []
        jobs, error = _submit_selected(prepared, "000001", ["missing"], False)
        self.assertEqual(jobs, [])
        self.assertEqual(error, "")


class TestCompletionBranches(unittest.TestCase):
    def test_check_completion_removes_stale_done_marker(self):
        with tempfile.TemporaryDirectory() as root:
            store = Mock()
            store.done_path = os.path.join(root, "done")
            store.completions_path = os.path.join(root, "completions")
            open(store.done_path, "w").close()
            row = RowState(name="r", row_id="r", line_num=1, index=0, params={}, generation=1,
                           runs=[RunState(generation=1, stages=[StageState(name="s", status=RUNNING)])], valid=True)
            store.load_rows.return_value = [row]
            self.assertIsNone(check_completion(store))
            self.assertFalse(os.path.exists(store.done_path))

    def test_check_completion_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            store = Mock()
            store.name = "run"
            store.done_path = os.path.join(root, "done")
            store.completions_path = os.path.join(root, "completions")
            row = RowState(name="r", row_id="r", line_num=1, index=0, params={}, generation=1,
                           runs=[RunState(generation=1, stages=[StageState(name="s", status=DONE)])], valid=True)
            store.load_rows.return_value = [row]
            first = check_completion(store)
            second = check_completion(store)
            self.assertIsNotNone(first)
            self.assertIsNone(second)

class TestOperationsEdgeBranches(unittest.TestCase):
    def test_directory_size_depth_zero_does_not_recurse(self):
        with tempfile.TemporaryDirectory() as root:
            nested = os.path.join(root, "nested")
            os.mkdir(nested)
            with open(os.path.join(root, "top"), "wb") as f:
                f.write(b"12")
            with open(os.path.join(nested, "deep"), "wb") as f:
                f.write(b"1234")
            self.assertEqual(_directory_size(root, depth=0), (1, 2))

    def test_existing_output_ignores_empty_children(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "empty"))
            row = SimpleNamespace(work_dir=root)
            self.assertEqual(_existing_output(row), [])

    def test_raw_fields_uses_empty_for_missing_parameter(self):
        schema = Schema(name="s", fields=[Field("a", [Str()]), Field("b", [Int()])])
        row = SimpleNamespace(params={"a": "x"}, raw_fields=[])
        self.assertEqual(_raw_fields(schema, row), ["x", ""])

    def test_describe_changes_reports_equal_value(self):
        schema = Schema(name="s", fields=[Field("a", [Int()])])
        row = SimpleNamespace(params={"a": 2})
        self.assertEqual(_describe_changes(schema, row, {"a": "2"}), {"a": (2, "2")})

    def test_check_params_digest_handles_current_digest(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "params")
            with open(path, "w", encoding="utf-8") as f:
                f.write("same")
            result = SimpleNamespace(findings=[])
            _check_params_digest(Mock(), {"params": path, "params_digest": _digest(path)}, result)
            self.assertEqual(result.findings, [])

    def test_read_completions_ignores_malformed_lines(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "completions")
            with open(path, "w", encoding="utf-8") as f:
                f.write("bad line\n2026-01-01T00:00:00 completion=1 done=1\n")
            self.assertEqual(_read_completions(SimpleNamespace(completions_path=path)),
                             ["bad", "2026-01-01T00:00:00"])

    def test_run_hook_success_path(self):
        store = SimpleNamespace(name="run", home="/tmp/run")
        with patch("jobchain.operations.subprocess.run") as run_mock:
            _run_hook(store, "echo ok", {"completion": 1, "rows": {"done": 1, "failed": 0}})
        run_mock.assert_called_once()

    def test_check_completion_ignores_invalid_row_without_run(self):
        with tempfile.TemporaryDirectory() as root:
            store = Mock()
            store.done_path = os.path.join(root, "done")
            store.completions_path = os.path.join(root, "completions")
            row = RowState(name="r", row_id="r", line_num=1, index=0, params={}, generation=1,
                           runs=[], valid=False)
            store.load_rows.return_value = [row]
            payload = check_completion(store)
            self.assertEqual(payload["rows"]["invalid"], 1)

    def test_cancel_dry_run_does_not_require_scheduler(self):
        store = Mock()
        store.stopped = False
        prepared = SimpleNamespace(store=store, scheduler=None)
        row = RowState(name="r", row_id="r", line_num=1, index=0, params={}, generation=1,
                       runs=[RunState(generation=1, stages=[StageState(name="s", status=RUNNING, jobid="1")])], valid=True)
        result = cancel(prepared, [row], dry_run=True)
        self.assertEqual(result.cancelled, [("r", ["1"])])

class TestRunLoadingAndLifecycle(unittest.TestCase):
    def test_prepare_without_pipeline_uses_single_job(self):
        config = SimpleNamespace(
            schema_source={"fields": [{"name": "x", "validators": ["str"]}]},
            params_path="/tmp/params", base_dir="/tmp", pipeline_source=None,
            scheduler="pbs", name="r", work_dir_template="{run.home}/work",
            log_dir_template="{run.home}/logs",
            home=lambda root=None: os.path.join(root or "/tmp", "r"),
        )
        with patch("jobchain.operations.load_schema_source") as load_schema, \
             patch("jobchain.operations.Store") as store_cls, \
             patch("jobchain.operations.single_job_pipeline") as single, \
             patch("jobchain.operations.Scheduler") as scheduler_cls, \
             patch("jobchain.operations.RunContext") as context_cls:
            schema = SimpleNamespace(fields=[], name="s")
            pipeline = Mock(spec=["construct"])
            load_schema.return_value = schema
            single.return_value = pipeline
            store_cls.return_value.home = "/tmp/r"
            context_cls.return_value = Mock()
            result = __import__("jobchain.operations", fromlist=["prepare"]).prepare(config)
            single.assert_called_once()
            pipeline.construct.assert_called_once()
            self.assertIs(result.pipeline, pipeline)
            scheduler_cls.assert_called_once_with("pbs")

    def test_prepare_dry_run_uses_null_scheduler(self):
        config = SimpleNamespace(schema_source={}, params_path="/tmp/p", base_dir="/tmp",
                                 pipeline_source={"stages": []}, scheduler="slurm", name="r",
                                 work_dir_template="{run.home}", log_dir_template="{run.home}/logs",
                                 home=lambda root=None: os.path.join(root or "/tmp", "r"))
        with patch("jobchain.operations.load_schema_source", return_value=SimpleNamespace(fields=[], name="s")), \
             patch("jobchain.operations.load_pipeline_source", return_value=Mock()), \
             patch("jobchain.operations.Store") as store_cls, \
             patch("jobchain.operations.NullScheduler") as null_cls, \
             patch("jobchain.operations.RunContext") as context_cls:
            store_cls.return_value.home = "/tmp/r"
            context_cls.return_value = Mock()
            __import__("jobchain.operations", fromlist=["prepare"]).prepare(config, dry_run=True)
            null_cls.assert_called_once_with("slurm")

    def test_open_run_requires_captured_configuration(self):
        from jobchain.operations import open_run
        store = Mock()
        store.name = "r"
        store.home = "/tmp/r"
        store.require.return_value = None
        with patch("os.path.isfile", return_value=False):
            with self.assertRaises(Exception):
                open_run(store)

    def test_run_check_only_does_not_submit(self):
        from jobchain.operations import run
        report = SimpleNamespace(invalid_rows=[], valid_rows=[1])
        prepared = SimpleNamespace(store=Mock(), schema=Mock(), pipeline=Mock(), config=Mock())
        prepared.store.exists.return_value = False
        with patch("jobchain.operations.prepare", return_value=prepared), \
             patch("jobchain.operations._validate_only", return_value=report), \
             patch("jobchain.operations._submit_chains") as submit:
            result = run(Mock(), check_only=True)
        self.assertEqual(result.phase, "check")
        submit.assert_not_called()

    def test_run_force_destroys_existing_store(self):
        from jobchain.operations import run
        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=1), scheduler=Mock())
        prepared.store.exists.return_value = True
        fresh = RunResult(store=prepared.store)
        with patch("jobchain.operations.prepare", return_value=prepared), \
             patch("jobchain.operations._prepare_fresh", return_value=fresh), \
             patch("jobchain.operations._submit_chains"):
            run(Mock(), force=True, no_submit=True)
        prepared.store.destroy.assert_called_once()

    def test_run_no_submit_returns_prepared_phase(self):
        from jobchain.operations import run
        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=1), scheduler=Mock())
        prepared.store.exists.return_value = False
        fresh = RunResult(store=prepared.store)
        with patch("jobchain.operations.prepare", return_value=prepared), \
             patch("jobchain.operations._prepare_fresh", return_value=fresh):
            result = run(Mock(), no_submit=True)
        self.assertEqual(result.phase, "prepared")

    def test_continue_existing_conflict_when_claimed_and_active(self):
        from jobchain.operations import _continue_existing
        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=1), scheduler=Mock())
        row = self._row_claimed()
        prepared.store.load_rows.return_value = [row]
        with self.assertRaises(Exception):
            _continue_existing(prepared, False, False, False, False, None)
        prepared.store.acquire_lock.assert_called_once()
        prepared.store.release_lock.assert_called_once()

    def _row_claimed(self):
        stage = StageState(name="solve", status=RUNNING, jobid="1")
        return RowState(name="r", row_id="r", line_num=1, index=0, params={}, generation=1,
                        runs=[RunState(generation=1, stages=[stage])], valid=True)

    def test_check_inputs_unchanged_accepts_matching_digest(self):
        from jobchain.operations import _check_inputs_unchanged
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "p")
            with open(path, "w") as f:
                f.write("x")
            prepared = SimpleNamespace(config=SimpleNamespace(params_path=path), store=Mock())
            prepared.store.load_config.return_value = {"params_digest": _digest(path)}
            _check_inputs_unchanged(prepared)

    def test_check_inputs_unchanged_rejects_changed_file(self):
        from jobchain.operations import _check_inputs_unchanged
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "p")
            with open(path, "w") as f:
                f.write("x")
            prepared = SimpleNamespace(config=SimpleNamespace(params_path=path), store=Mock())
            prepared.store.load_config.return_value = {"params_digest": "wrong"}
            with self.assertRaises(Exception):
                _check_inputs_unchanged(prepared)
