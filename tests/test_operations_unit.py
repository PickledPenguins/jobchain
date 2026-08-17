"""Deep, mock-heavy unit coverage of jobchain/operations.py.

Consolidated from several incrementally-grown coverage passes
(originally test_operations_{helpers,remaining,exhaustive,
final_gaps,gap_closure,deep_remaining}.py) into one file, matching
this project's one-file-per-subsystem convention. Behavior and
assertions are unchanged from the source files except where a name
collided across two of them (renamed, noted at the definition) or
an assertion was corrected against this codebase's actual
behavior (noted inline where that happened).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from jobchain import operations
from jobchain.core import DataError, StateError
from jobchain.operations import (
    ConflictError,
    RerunPlan,
    RunResult,
    UsageError,
    _apply_changes,
    _check_params_digest,
    _continue_existing,
    _create_row_state,
    _describe_changes,
    _digest,
    _directory_size,
    _existing_output,
    _generate_scripts,
    _identifier_for,
    _pipeline_document,
    _prepare_fresh,
    _raw_fields,
    _read_completions,
    _record_submissions,
    _regenerate_row,
    _run_hook,
    _schema_document,
    _submit_chains,
    _submit_row,
    _submit_selected,
    _validate_only,
    cancel,
    check_completion,
    doctor,
    execute_rerun,
    open_run,
    plan_rerun,
    run,
)
from jobchain.scheduler import ALIVE, FINISHED
from jobchain.schema import Bool, Field, Int, Schema, Str
from jobchain.store import (
    CLAIMED,
    DONE,
    FAILED,
    PENDING,
    QUEUED,
    RUNNING,
    RowState,
    RunState,
    StageState,
)


# from test_operations_exhaustive.py
def make_row(name="r", stages=None, valid=True, runs_marker=False):
    if runs_marker:
        runs = []
    else:
        stages = (
            stages
            if stages is not None
            else [StageState(name="s", status=DONE, jobid="1", resources={}, timeline=[])]
        )
        runs = [RunState(generation=1, stages=stages)]
    return RowState(
        name=name,
        row_id=name,
        line_num=2,
        index=0,
        params={"a": 1},
        generation=1,
        runs=runs,
        valid=valid,
        invalid_reasons=[],
        failure_id="",
        work_dir="",
    )


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
        with self.assertRaises(UsageError):
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
        prepared = SimpleNamespace(
            config=SimpleNamespace(schema_source="schema.yaml", base_dir="/tmp/project")
        )
        self.assertEqual(_schema_document(prepared), "/tmp/project/schema.yaml")

    def test_schema_document_preserves_absolute_path(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(schema_source="/tmp/schema.yaml", base_dir="/tmp/project")
        )
        self.assertEqual(_schema_document(prepared), "/tmp/schema.yaml")

    def test_pipeline_document_handles_none(self):
        prepared = SimpleNamespace(config=SimpleNamespace(pipeline_source=None))
        self.assertIsNone(_pipeline_document(prepared))

    def test_pipeline_document_keeps_inline_document_and_absolutizes_module(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(
                pipeline_source={"stage_module": "stages.py", "stages": []}, base_dir="/tmp/project"
            )
        )
        self.assertEqual(
            _pipeline_document(prepared),
            {"stage_module": "/tmp/project/stages.py", "stages": []},
        )

    def test_pipeline_document_preserves_absolute_module(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(
                pipeline_source={"stage_module": "/tmp/stages.py"}, base_dir="/tmp/project"
            )
        )
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
            self.assertEqual(
                _read_completions(SimpleNamespace(completions_path=path)),
                ["2026-01-01T00:00:00", "2026-01-02T00:00:00"],
            )

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
            name="s", fields=[Field("rid", [Str()]), Field("count", [Int()])], id_field="rid"
        )
        return SimpleNamespace(pipeline=pipeline, store=store, config=config, schema=schema)

    def _row(self, status=PENDING, attempts=0):
        runs = [
            RunState(
                generation=i + 1,
                stages=[
                    StageState(name="prep", status=status),
                    StageState(name="solve", status=status),
                    StageState(name="archive", status=status),
                ],
            )
            for i in range(max(1, attempts))
        ]
        return RowState(
            name="000001",
            row_id="a",
            line_num=2,
            index=0,
            params={"rid": "a", "count": 1},
            generation=1,
            runs=runs,
            valid=True,
        )

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
        with self.assertRaises(UsageError):
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
        prepared = SimpleNamespace(
            store=store,
            scheduler=scheduler or Mock(),
            config=SimpleNamespace(work_dir_template="{run.home}/{row_name}"),
            pipeline=pipeline,
            schema=None,
        )
        return prepared

    def _row(self, name="000001", status=RUNNING, jobid="123", script=""):
        stage = StageState(name="solve", status=status, jobid=jobid, script=script)
        run = RunState(generation=1, stages=[stage])
        return RowState(
            name=name,
            row_id=name,
            line_num=2,
            index=0,
            params={},
            generation=1,
            runs=[run],
            valid=True,
        )

    def test_cancel_dry_run_collects_active_job_ids_without_scheduler_calls(self):
        scheduler = Mock()
        prepared = self._prepared(scheduler=scheduler)
        result = cancel(prepared, [self._row()], dry_run=True)
        self.assertEqual(result.cancelled, [("000001", ["123"])])
        scheduler.cancel.assert_not_called()

    def test_cancel_skips_never_claimed_and_terminal_rows(self):
        scheduler = Mock()
        prepared = self._prepared(scheduler=scheduler)
        pending = RowState(
            name="p", row_id="p", line_num=1, index=0, params={}, generation=1, runs=[], valid=True
        )
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
        config = SimpleNamespace(
            effective_workers=1, max_in_flight=0, width=2, work_dir_template="{run.home}/{row_name}"
        )
        run_context = Mock()
        run_context.work_dir.return_value = "/tmp/jobchain/000001"
        store.run_dir.side_effect = lambda name, generation: f"/tmp/jobchain/{name}/g{generation}"
        return SimpleNamespace(
            store=store,
            scheduler=scheduler,
            pipeline=pipeline,
            config=config,
            run_context=run_context,
        )

    def test_create_row_state_writes_valid_and_invalid_rows(self):
        store = Mock()
        store.home = "/tmp/run"
        store.load_row.side_effect = lambda name: SimpleNamespace(name=name)
        schema = Schema(name="s", fields=[Field("id", [Str()])], id_field="id")
        config = SimpleNamespace(work_dir_template="{run.home}/{row_name}", name="run")
        prepared = SimpleNamespace(store=store, schema=schema, config=config)
        report = SimpleNamespace(
            rows=[
                SimpleNamespace(
                    index=0,
                    line_num=2,
                    ok=True,
                    record={"id": "a"},
                    raw_fields=[],
                    reasons=lambda: [],
                    failure_id=lambda: "",
                ),
                SimpleNamespace(
                    index=1,
                    line_num=3,
                    ok=False,
                    record=None,
                    raw_fields=["bad"],
                    reasons=lambda: ["bad"],
                    failure_id=lambda: "f1",
                ),
            ]
        )
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
        prepared.scheduler.submit_pipeline.return_value = [
            ("solve", SimpleNamespace(success=True, job_id="9", output=""))
        ]
        jobs, error = _submit_row(prepared, "000001", "/tmp/run")
        self.assertEqual(jobs, [("solve", "9")])
        self.assertEqual(error, "")

    def test_submit_chains_stopped_run_is_rejected(self):
        prepared = self._prepared()
        prepared.store.stopped = True
        with self.assertRaises(ConflictError):
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
            ("prep", "-", "/tmp/prep.sh"),
            ("solve", "1", "/tmp/solve.sh"),
        ]
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
            row = RowState(
                name="r",
                row_id="r",
                line_num=1,
                index=0,
                params={},
                generation=1,
                runs=[RunState(generation=1, stages=[StageState(name="s", status=RUNNING)])],
                valid=True,
            )
            store.load_rows.return_value = [row]
            self.assertIsNone(check_completion(store))
            self.assertFalse(os.path.exists(store.done_path))

    def test_check_completion_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            store = Mock()
            store.name = "run"
            store.done_path = os.path.join(root, "done")
            store.completions_path = os.path.join(root, "completions")
            row = RowState(
                name="r",
                row_id="r",
                line_num=1,
                index=0,
                params={},
                generation=1,
                runs=[RunState(generation=1, stages=[StageState(name="s", status=DONE)])],
                valid=True,
            )
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
            self.assertEqual(
                _read_completions(SimpleNamespace(completions_path=path)),
                ["bad", "2026-01-01T00:00:00"],
            )

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
            row = RowState(
                name="r",
                row_id="r",
                line_num=1,
                index=0,
                params={},
                generation=1,
                runs=[],
                valid=False,
            )
            store.load_rows.return_value = [row]
            payload = check_completion(store)
            self.assertEqual(payload["rows"]["invalid"], 1)

    def test_cancel_dry_run_does_not_require_scheduler(self):
        store = Mock()
        store.stopped = False
        prepared = SimpleNamespace(store=store, scheduler=None)
        row = RowState(
            name="r",
            row_id="r",
            line_num=1,
            index=0,
            params={},
            generation=1,
            runs=[RunState(generation=1, stages=[StageState(name="s", status=RUNNING, jobid="1")])],
            valid=True,
        )
        result = cancel(prepared, [row], dry_run=True)
        self.assertEqual(result.cancelled, [("r", ["1"])])


class TestRunLoadingAndLifecycle(unittest.TestCase):
    def test_prepare_without_pipeline_uses_single_job(self):
        config = SimpleNamespace(
            schema_source={"fields": [{"name": "x", "validators": ["str"]}]},
            params_path="/tmp/params",
            base_dir="/tmp",
            pipeline_source=None,
            scheduler="pbs",
            name="r",
            work_dir_template="{run.home}/work",
            log_dir_template="{run.home}/logs",
            home=lambda root=None: os.path.join(root or "/tmp", "r"),
        )
        with patch("jobchain.operations.load_schema_source") as load_schema, patch(
            "jobchain.operations.Store"
        ) as store_cls, patch("jobchain.operations.single_job_pipeline") as single, patch(
            "jobchain.operations.Scheduler"
        ) as scheduler_cls, patch(
            "jobchain.operations.RunContext"
        ) as context_cls:
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
        config = SimpleNamespace(
            schema_source={},
            params_path="/tmp/p",
            base_dir="/tmp",
            pipeline_source={"stages": []},
            scheduler="slurm",
            name="r",
            work_dir_template="{run.home}",
            log_dir_template="{run.home}/logs",
            home=lambda root=None: os.path.join(root or "/tmp", "r"),
        )
        with patch(
            "jobchain.operations.load_schema_source",
            return_value=SimpleNamespace(fields=[], name="s"),
        ), patch("jobchain.operations.load_pipeline_source", return_value=Mock()), patch(
            "jobchain.operations.Store"
        ) as store_cls, patch(
            "jobchain.operations.NullScheduler"
        ) as null_cls, patch(
            "jobchain.operations.RunContext"
        ) as context_cls:
            store_cls.return_value.home = "/tmp/r"
            context_cls.return_value = Mock()
            __import__("jobchain.operations", fromlist=["prepare"]).prepare(config, dry_run=True)
            null_cls.assert_called_once_with("slurm")

    def test_open_run_requires_captured_configuration(self):
        store = Mock()
        store.name = "r"
        store.home = "/tmp/r"
        store.require.return_value = None
        with patch("os.path.isfile", return_value=False), self.assertRaises(StateError):
            open_run(store)

    def test_run_check_only_does_not_submit(self):
        from jobchain.operations import run

        report = SimpleNamespace(invalid_rows=[], valid_rows=[1])
        prepared = SimpleNamespace(store=Mock(), schema=Mock(), pipeline=Mock(), config=Mock())
        prepared.store.exists.return_value = False
        with patch("jobchain.operations.prepare", return_value=prepared), patch(
            "jobchain.operations._validate_only", return_value=report
        ), patch("jobchain.operations._submit_chains") as submit:
            result = run(Mock(), check_only=True)
        self.assertEqual(result.phase, "check")
        submit.assert_not_called()

    def test_run_force_destroys_existing_store(self):
        from jobchain.operations import run

        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=1), scheduler=Mock())
        prepared.store.exists.return_value = True
        fresh = RunResult(store=prepared.store)
        with patch("jobchain.operations.prepare", return_value=prepared), patch(
            "jobchain.operations._prepare_fresh", return_value=fresh
        ), patch("jobchain.operations._submit_chains"):
            run(Mock(), force=True, no_submit=True)
        prepared.store.destroy.assert_called_once()

    def test_run_no_submit_returns_prepared_phase(self):
        from jobchain.operations import run

        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=1), scheduler=Mock())
        prepared.store.exists.return_value = False
        fresh = RunResult(store=prepared.store)
        with patch("jobchain.operations.prepare", return_value=prepared), patch(
            "jobchain.operations._prepare_fresh", return_value=fresh
        ):
            result = run(Mock(), no_submit=True)
        self.assertEqual(result.phase, "prepared")

    def test_continue_existing_conflict_when_claimed_and_active(self):
        # _continue_existing's conflict check is read-only (it only
        # inspects already-recorded row state to decide whether to
        # refuse), so it correctly does not take the setup lock -- that
        # lock exists specifically to serialize concurrent *preparation*
        # of a fresh run (see Store.acquire_lock's docstring), which this
        # code path never performs. The original version of this test
        # asserted the lock was taken; that assumed a design this code
        # does not have, not a missing lock acquisition.
        from jobchain.operations import _continue_existing

        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=1), scheduler=Mock())
        row = self._row_claimed()
        prepared.store.load_rows.return_value = [row]
        with self.assertRaises(ConflictError):
            _continue_existing(prepared, False, False, False, False, None)
        prepared.store.acquire_lock.assert_not_called()
        prepared.store.release_lock.assert_not_called()

    def _row_claimed(self):
        stage = StageState(name="solve", status=RUNNING, jobid="1")
        return RowState(
            name="r",
            row_id="r",
            line_num=1,
            index=0,
            params={},
            generation=1,
            runs=[RunState(generation=1, stages=[stage])],
            valid=True,
        )

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
            with self.assertRaises(ConflictError):
                _check_inputs_unchanged(prepared)


class TestRunAndExistingBranches(unittest.TestCase):
    def test_open_run_loads_captured_config_and_reuses_store(self):
        store = Mock()
        store.name = "r"
        store.home = "/tmp/r"
        store.require.return_value = None
        with patch("jobchain.operations.os.path.isfile", return_value=True), patch(
            "jobchain.config.load_config", return_value=Mock()
        ), patch("jobchain.operations.prepare") as prepare:
            prepared = SimpleNamespace(store=Mock(), run_context=Mock())
            prepare.return_value = prepared
            from jobchain.operations import open_run

            result = open_run(store)
        self.assertIs(result.store, store)
        self.assertEqual(result.run_context.home, store.home)

    def test_run_submits_fresh_run(self):
        prepared = SimpleNamespace(store=Mock(), scheduler=Mock(), config=SimpleNamespace(width=2))
        prepared.store.exists.return_value = False
        prepared.scheduler.require_available.return_value = None
        fresh = RunResult(store=prepared.store)
        with patch("jobchain.operations.prepare", return_value=prepared), patch(
            "jobchain.operations._prepare_fresh", return_value=fresh
        ), patch("jobchain.operations._submit_chains") as submit:
            result = run(SimpleNamespace(width=2), no_submit=False)
        self.assertEqual(result.phase, "submitted")
        submit.assert_called_once_with(prepared, 2, fresh)

    def test_run_existing_resume_path_submits(self):
        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=2), scheduler=Mock())
        prepared.store.exists.return_value = True
        prepared.store.load_rows.return_value = []
        prepared.store.load_config.return_value = {"width": 2}
        with patch("jobchain.operations.prepare", return_value=prepared), patch(
            "jobchain.operations._check_inputs_unchanged"
        ), patch("jobchain.operations._submit_chains") as submit:
            result = run(Mock())
        self.assertEqual(result.phase, "submitted")
        submit.assert_called_once()

    def test_continue_existing_resume_clears_stop_and_submits(self):
        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=1), scheduler=Mock())
        prepared.store.load_rows.return_value = []
        prepared.store.load_config.return_value = {"width": 1}
        with patch("jobchain.operations._submit_chains") as submit:
            result = _continue_existing(prepared, False, False, True, False, None)
        prepared.store.resume.assert_called_once()
        self.assertEqual(result.phase, "submitted")
        submit.assert_called_once()

    def test_continue_existing_no_submit_returns_prepared_for_unclaimed(self):
        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=1), scheduler=Mock())
        prepared.store.load_rows.return_value = []
        with patch("jobchain.operations._submit_chains") as submit:
            result = _continue_existing(prepared, False, False, False, True, None)
        self.assertEqual(result.phase, "prepared")
        submit.assert_not_called()

    def test_continue_existing_regenerate_without_submit(self):
        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=1), scheduler=Mock())
        prepared.store.load_rows.return_value = []
        RunResult(store=prepared.store)
        with patch("jobchain.operations._generate_scripts") as generate:
            generate.return_value = None
            out = _continue_existing(prepared, False, True, False, True, None)
        self.assertEqual(out.phase, "regenerated")
        generate.assert_called_once()


class TestSubmissionBranches(unittest.TestCase):
    def _prepared(self, stopped=False, ceiling=0):
        store = Mock()
        store.name = "r"
        store.stopped = stopped
        store.load_config.return_value = {"max_in_flight": ceiling}
        pipeline = SimpleNamespace(specs=[], stage_names=[], stage=Mock())
        config = SimpleNamespace(max_in_flight=ceiling)
        return SimpleNamespace(store=store, pipeline=pipeline, config=config, scheduler=Mock())

    def test_submit_chains_stopped_raises(self):
        prepared = self._prepared(stopped=True)
        with self.assertRaises(ConflictError):
            _submit_chains(prepared, 1, RunResult(store=prepared.store))

    def test_submit_chains_ceiling_reduces_width(self):
        prepared = self._prepared(ceiling=2)
        row = SimpleNamespace(current=object(), is_terminal=False)
        prepared.store.load_rows.return_value = [row, row]
        prepared.store.claim.return_value = None
        result = RunResult(store=prepared.store)
        _submit_chains(prepared, 4, result)
        self.assertFalse(result.exhausted)
        prepared.store.claim.assert_not_called()

    def test_submit_chains_claim_failure_marks_result_and_continues(self):
        prepared = self._prepared()
        prepared.store.claim.return_value = ("r1", "/tmp/r1")
        prepared.store.load_config.return_value = {"max_in_flight": 0}
        with patch("jobchain.operations._submit_row", return_value=([], "bad")):
            result = RunResult(store=prepared.store)
            _submit_chains(prepared, 1, result)
        self.assertEqual(result.failures, [("r1", "bad")])

    def test_submit_row_without_manifest_returns_reason(self):
        prepared = self._prepared()
        prepared.store.read_manifest.return_value = []
        jobs, reason = _submit_row(prepared, "r1", "/tmp/r1")
        self.assertEqual(jobs, [])
        self.assertIn("no manifest", reason)

    def test_submit_row_writes_resources_and_marks_queued(self):
        prepared = self._prepared()
        prepared.store.read_manifest.return_value = [("s", "-", "/tmp/s.sh")]
        prepared.store.load_row.return_value = SimpleNamespace(params={"x": 1})
        stage = Mock()
        stage.effective_resources.return_value = {"ncpus": 2}
        prepared.pipeline.specs = [SimpleNamespace(name="s")]
        prepared.pipeline.stage.return_value = stage
        prepared.scheduler.submit_pipeline.return_value = [
            ("s", SimpleNamespace(success=True, job_id="7", output=""))
        ]
        jobs, reason = _submit_row(prepared, "r1", "/tmp/r1")
        self.assertEqual(jobs, [("s", "7")])
        self.assertEqual(reason, "")
        prepared.store.write_resources.assert_called_once()
        prepared.store.mark.assert_any_call("/tmp/r1", "s", "QUEUED")


class TestPrepareFreshBranches(unittest.TestCase):
    def test_prepare_fresh_strict_validation_rejects_invalid_rows(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(
                strict=True,
                params_path="p",
                name="r",
                scheduler="pbs",
                width=1,
                max_attempts=1,
                work_dir_template="{run.home}",
                on_complete="",
                source_text="x",
                effective_workers=1,
            ),
            schema=SimpleNamespace(name="s", fields=[], unique_fields=[]),
            pipeline=SimpleNamespace(name="p", specs=[], chaining_stage=None, stage_names=[]),
            store=Mock(),
        )
        prepared.store.home = "/tmp/r"
        report = SimpleNamespace(
            ok=False,
            invalid_rows=[SimpleNamespace(line_num=2, reasons=lambda: ["bad"])],
            valid_rows=[],
            rows=[1],
        )
        normalized = SimpleNamespace(rows=[1], changed_count=0, skipped_blank=0, skipped_comment=0)
        with patch("jobchain.operations.normalize_file", return_value=normalized), patch(
            "jobchain.operations.scan", return_value=report
        ), self.assertRaises(DataError):
            _prepare_fresh(prepared)
        prepared.store.create.assert_not_called()


class TestScriptGenerationBranches(unittest.TestCase):
    def test_generate_scripts_empty_rows_is_noop(self):
        prepared = SimpleNamespace(
            pipeline=SimpleNamespace(specs=[1]),
            store=Mock(),
            config=SimpleNamespace(effective_workers=1),
        )
        result = RunResult(store=prepared.store)
        _generate_scripts(prepared, [], result)
        prepared.store.write_manifest.assert_not_called()

    def test_generate_scripts_records_verify_failure(self):
        stage = Mock()
        stage.output_dir.return_value = "/tmp"
        stage.script_name.return_value = "x.sh"
        stage.write_script.return_value = "/tmp/x.sh"
        prepared = SimpleNamespace(
            pipeline=SimpleNamespace(
                specs=[SimpleNamespace(name="s", position=1, depends="-")], stage=lambda n: stage
            ),
            store=Mock(),
            config=SimpleNamespace(effective_workers=1),
            run_context=Mock(),
        )
        row = RowState("r", "r", 1, 0, {}, 1, valid=True)
        with patch("jobchain.operations._context_for", return_value=Mock()), patch(
            "jobchain.operations.verify_script", return_value="not executable"
        ), self.assertRaises(DataError):
            _generate_scripts(prepared, [row], RunResult(store=prepared.store))

    def test_generate_scripts_records_render_exception(self):
        stage = Mock()
        stage.write_script.side_effect = RuntimeError("boom")
        prepared = SimpleNamespace(
            pipeline=SimpleNamespace(
                specs=[SimpleNamespace(name="s", position=1, depends="-")], stage=lambda n: stage
            ),
            store=Mock(),
            config=SimpleNamespace(effective_workers=1),
            run_context=Mock(),
        )
        row = RowState("r", "r", 1, 0, {}, 1, valid=True)
        with patch("jobchain.operations._context_for", return_value=Mock()), self.assertRaises(DataError):
            _generate_scripts(prepared, [row], RunResult(store=prepared.store))


class TestOperationsRemaining(unittest.TestCase):
    def test_prepare_fresh_strict_validation_raises(self):
        config = SimpleNamespace(
            name="r", params_path="p", strict=True, effective_workers=1, scheduler="pbs", width=1
        )
        schema = SimpleNamespace(name="s", field_names=[], id_field=None)
        pipeline = SimpleNamespace(name="p", specs=[], chaining_stage=None)
        store = MagicMock(name="store", home="/h", stopped=False)
        prepared = SimpleNamespace(config=config, schema=schema, pipeline=pipeline, store=store)
        report = SimpleNamespace(
            ok=False,
            invalid_rows=[SimpleNamespace(line_num=2, reasons=lambda: ["bad"])],
            rows=[1],
            valid_rows=[],
        )
        with patch(
            "jobchain.operations.normalize_file",
            return_value=SimpleNamespace(
                rows=[], changed_count=0, skipped_blank=0, skipped_comment=0
            ),
        ), patch("jobchain.operations.scan", return_value=report), patch(
            "jobchain.operations._digest", return_value="d"
        ), patch(
            "jobchain.operations.log_startup_summary"
        ), self.assertRaises(DataError):
            _prepare_fresh(prepared)
        store.create.assert_not_called()

    def test_identifier_raw_fallbacks(self):
        schema = SimpleNamespace(id_field=None, field_names=[])
        result = SimpleNamespace(raw_fields=[])
        self.assertEqual(_identifier_for(schema, result, {}, "fallback"), "fallback")
        schema.id_field = "rid"
        schema.field_names = ["rid"]
        self.assertEqual(_identifier_for(schema, result, {}, "fallback"), "fallback")
        result.raw_fields = ["   "]
        self.assertEqual(_identifier_for(schema, result, {}, "fallback"), "fallback")
        result.raw_fields = [" raw "]
        self.assertEqual(_identifier_for(schema, result, {}, "fallback"), "raw")

    def test_submit_row_without_manifest(self):
        store = MagicMock()
        store.read_manifest.return_value = []
        prepared = SimpleNamespace(store=store, scheduler=MagicMock(), pipeline=MagicMock())
        self.assertEqual(
            _submit_row(prepared, "r", "run"), ([], "no manifest: the row has no generated scripts")
        )

    def test_record_submissions_success_and_middle_failure(self):
        store = MagicMock()
        scheduler = MagicMock()
        good = SimpleNamespace(success=True, job_id="1")
        bad = SimpleNamespace(success=False, error="queue full", output="queue full")
        jobs, reason = _record_submissions(store, "run", [("a", good), ("b", bad)], scheduler)
        self.assertEqual(jobs, [("a", "1")])
        self.assertEqual(reason, "queue full")
        scheduler.cancel.assert_called_once_with("1")

    def test_submit_chains_ceiling_reduces_width_and_exhausts(self):
        store = MagicMock()
        store.stopped = False
        store.load_rows.return_value = [SimpleNamespace(current=1, is_terminal=False)]
        store.claim.return_value = None
        config = SimpleNamespace(max_in_flight=1)
        prepared = SimpleNamespace(store=store, config=config)
        result = RunResult(store=store)
        with patch("jobchain.operations._submit_row") as submit:
            _submit_chains(prepared, 2, result)
        self.assertFalse(result.exhausted)
        submit.assert_not_called()

    def test_submit_chains_failure_is_recorded(self):
        store = MagicMock()
        store.stopped = False
        store.load_rows.return_value = []
        store.claim.return_value = ("r", "run")
        prepared = SimpleNamespace(store=store, config=SimpleNamespace(max_in_flight=0))
        result = RunResult(store=store)
        with patch("jobchain.operations._submit_row", return_value=([], "bad")):
            _submit_chains(prepared, 1, result)
        self.assertEqual(result.failures, [("r", "bad")])

    def test_execute_rerun_assignments_regenerate_and_submit_failure(self):
        row = make_row()
        store = MagicMock()
        store.load_row.return_value = row
        store.bump_generation.return_value = 2
        prepared = SimpleNamespace(
            store=store,
            config=SimpleNamespace(on_complete=None),
            schema=MagicMock(),
            pipeline=MagicMock(),
        )
        plan = RerunPlan(rows=[row], new_generation=True, stages=["s"])
        SimpleNamespace(rows=[], regenerated=0, submitted=[], failures=[], skipped=[])
        with patch("jobchain.operations._apply_changes"), patch(
            "jobchain.operations._regenerate_row", return_value=1
        ), patch("jobchain.operations._submit_selected", return_value=([], "bad")):
            out = execute_rerun(prepared, plan, assignments={"a": "2"}, regenerate=True, chain=True)
        self.assertEqual(out.regenerated, 1)
        self.assertEqual(out.failures, [("r", "bad")])

    def test_execute_rerun_dry_run_does_not_change_or_submit(self):
        row = make_row()
        store = MagicMock()
        prepared = SimpleNamespace(store=store)
        plan = RerunPlan(rows=[row], new_generation=True)
        with patch("jobchain.operations._apply_changes") as change, patch(
            "jobchain.operations._submit_selected"
        ) as submit:
            out = execute_rerun(prepared, plan, dry_run=True)
        self.assertEqual(out.rows, ["r"])
        change.assert_not_called()
        submit.assert_not_called()

    def test_cancel_skips_unclaimed_and_dry_runs_active_jobs(self):
        unclaimed = make_row("u", runs_marker=True)
        active = make_row(
            "a", stages=[StageState(name="s", status=QUEUED, jobid="9", resources={}, timeline=[])]
        )
        prepared = SimpleNamespace(store=MagicMock(), scheduler=MagicMock())
        result = cancel(prepared, [unclaimed, active], stage=None, stop=False, dry_run=True)
        self.assertEqual(result.skipped, [("u", "never claimed")])
        self.assertEqual(result.cancelled, [("a", ["9"])])
        prepared.scheduler.cancel.assert_not_called()

    def test_doctor_alive_and_finished_and_chain_shortfall(self):
        active = make_row(
            "a", stages=[StageState(name="s", status=RUNNING, jobid="1", resources={}, timeline=[])]
        )
        pending = make_row("p", runs_marker=True)
        store = MagicMock(name="store")
        store.name = "r"
        store.stopped = False
        store.load_config.return_value = {"width": 2}
        store.load_rows.return_value = [active, pending]
        store.run_dir.return_value = "/run"
        scheduler = MagicMock()
        scheduler.job_state.return_value = ALIVE
        prepared = SimpleNamespace(
            store=store, scheduler=scheduler, config=SimpleNamespace(params_path="p")
        )
        with patch("jobchain.operations._check_params_digest"), patch(
            "jobchain.operations._submit_chains"
        ) as submit:
            result = doctor(prepared, repair=False, dry_run=False)
        self.assertEqual(result.live_chains, 1)
        self.assertTrue(any("short" in f.detail for f in result.findings))
        submit.assert_not_called()

    def test_doctor_finished_job_and_repair(self):
        active = make_row(
            "a", stages=[StageState(name="s", status=RUNNING, jobid="1", resources={}, timeline=[])]
        )
        store = MagicMock(name="store")
        store.name = "r"
        store.stopped = False
        store.load_config.return_value = {"width": 1}
        store.load_rows.return_value = [active]
        store.run_dir.return_value = "/run"
        scheduler = MagicMock()
        scheduler.job_state.return_value = FINISHED
        prepared = SimpleNamespace(
            store=store, scheduler=scheduler, config=SimpleNamespace(params_path="p")
        )
        with patch("jobchain.operations._check_params_digest"):
            result = doctor(prepared, repair=True, dry_run=False)
        self.assertTrue(result.findings[0].repaired)
        store.mark.assert_called()

    def test_doctor_invalid_and_stopped_findings(self):
        invalid = make_row("bad", valid=False)
        store = MagicMock(name="store")
        store.name = "r"
        store.stopped = False
        store.load_config.return_value = {"width": 1}
        store.load_rows.return_value = [invalid]
        store.stopped = True
        prepared = SimpleNamespace(
            store=store, scheduler=MagicMock(), config=SimpleNamespace(params_path="p")
        )
        with patch("jobchain.operations._check_params_digest"):
            result = doctor(prepared)
        text = "\n".join(f.detail for f in result.findings)
        self.assertIn("failed validation", text)
        self.assertIn("stopped", text)

    def test_doctor_shortfall_relaunches_when_repair_allowed(self):
        active = make_row(
            "a", stages=[StageState(name="s", status=RUNNING, jobid="1", resources={}, timeline=[])]
        )
        pending = make_row("p", runs_marker=True)
        store = MagicMock(name="store")
        store.name = "r"
        store.stopped = False
        store.load_config.return_value = {"width": 2}
        store.load_rows.return_value = [active, pending]
        scheduler = MagicMock()
        scheduler.job_state.return_value = ALIVE
        prepared = SimpleNamespace(
            store=store, scheduler=scheduler, config=SimpleNamespace(params_path="p")
        )
        launched = RunResult(store=store)
        launched.submitted = [("p", [("s", "2")])]
        with patch("jobchain.operations._check_params_digest"), patch(
            "jobchain.operations._submit_chains",
            side_effect=lambda p, w, r: setattr(r, "submitted", launched.submitted),
        ):
            result = doctor(prepared, repair=True, dry_run=False)
        self.assertEqual(result.relaunched, launched.submitted)

    def test_run_hook_nonzero_and_exception_are_swallowed(self):
        from jobchain.operations import _run_hook

        store = SimpleNamespace(name="r", home="/h")
        payload = {"rows": {"done": 1, "failed": 2}, "completion": "c"}
        bad = SimpleNamespace(returncode=1, stderr="bad")
        with patch("jobchain.operations.subprocess.run", return_value=bad), patch(
            "jobchain.operations.get_logger"
        ) as logger:
            _run_hook(store, "echo {run.name}", payload)
        logger.return_value.warning.assert_called_once()
        with patch("jobchain.operations.subprocess.run", side_effect=RuntimeError("x")), patch(
            "jobchain.operations.get_logger"
        ) as logger:
            _run_hook(store, "echo", payload)
        self.assertEqual(logger.return_value.warning.call_count, 1)

    def test_write_json_file_without_directory(self):
        from jobchain.operations import _write_json_file

        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                _write_json_file("x.json", {"a": 1})
                with open("x.json") as h:
                    self.assertIn('"a": 1', h.read())
            finally:
                os.chdir(old)


class TestOperationsFinalBranches(unittest.TestCase):
    def test_cancel_stage_filter_skips_other_stages(self):
        row = make_row(
            "r",
            stages=[
                StageState(name="a", status=QUEUED, jobid="1", resources={}, timeline=[]),
                StageState(name="b", status=QUEUED, jobid="2", resources={}, timeline=[]),
            ],
        )
        prepared = SimpleNamespace(store=MagicMock(stopped=False), scheduler=MagicMock())
        result = cancel(prepared, [row], stage="b", stop=False, dry_run=True)
        self.assertEqual(result.cancelled, [("r", ["2"])])

    def test_doctor_terminal_chaining_stage_is_not_reported_as_ended_chain(self):
        run = RunState(
            generation=1,
            stages=[
                StageState(name="chain", status=DONE, jobid="1", resources={}, timeline=[]),
                StageState(name="other", status=PENDING, jobid=None, resources={}, timeline=[]),
            ],
        )
        r = make_row("r", runs_marker=True)
        r.runs = [run]
        store = MagicMock()
        store.name = "r"
        store.stopped = False
        store.load_config.return_value = {"width": 1, "chaining_stage": "chain"}
        store.load_rows.return_value = [r]
        prepared = SimpleNamespace(
            store=store, scheduler=MagicMock(), config=SimpleNamespace(params_path="p")
        )
        with patch("jobchain.operations._check_params_digest"):
            result = doctor(prepared)
        self.assertFalse(any("chain ended here" in f.detail for f in result.findings))


class TestRerunPlanningEdges(unittest.TestCase):
    def _prepared(self, generation_aware=False):
        store = Mock()
        store.load_config.return_value = {"max_attempts": 2}
        config = SimpleNamespace(
            work_dir_template="{generation}/{row.name}" if generation_aware else "{row.name}"
        )
        pipeline = SimpleNamespace(stage_names=["a", "b"])
        return SimpleNamespace(store=store, config=config, pipeline=pipeline)

    def test_unknown_from_stage(self):
        p = self._prepared()
        with self.assertRaises(UsageError):
            plan_rerun(p, [], from_stage="missing")

    def test_unknown_requested_stage(self):
        p = self._prepared()
        with self.assertRaises(UsageError):
            plan_rerun(p, [], stages=["missing"])

    def test_assignments_are_described(self):
        p = self._prepared()
        row = RowState("r", "r", 1, 0, {"x": "old"}, 1, valid=True)
        p.schema = SimpleNamespace(field_names=["x"])
        plan = plan_rerun(p, [row], assignments={"x": "new"})
        self.assertEqual(plan.changes["r"]["x"], ("old", "new"))

    def test_done_non_generation_aware_output_requires_confirmation(self):
        p = self._prepared(False)
        row = RowState(
            "r",
            "r",
            1,
            0,
            {"x": "1"},
            1,
            valid=True,
            work_dir=tempfile.gettempdir(),
            runs=[RunState(1, stages=[StageState("a", DONE)])],
        )
        with patch("jobchain.operations._existing_output", return_value=[("/x", 1, 2)]):
            plan = plan_rerun(p, [row])
        self.assertEqual(len(plan.needs_confirmation), 1)


class TestOutputAndCorrectionEdges(unittest.TestCase):
    def test_existing_output_directory_listing_error(self):
        row = SimpleNamespace(work_dir="/tmp/out")
        with patch("jobchain.operations.os.path.isdir", return_value=True), patch(
            "jobchain.operations.os.listdir", side_effect=OSError("denied")
        ):
            self.assertEqual(_existing_output(row), [])

    def test_existing_output_direct_files(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "result.dat")
            with open(path, "wb") as handle:
                handle.write(b"abc")
            row = SimpleNamespace(work_dir=td)
            result = _existing_output(row)
            self.assertEqual(result, [(td, 1, 3)])

    def test_directory_size_ignores_unreadable_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "x")
            open(path, "w").close()
            with patch("jobchain.operations.os.path.getsize", side_effect=OSError("gone")):
                self.assertEqual(_directory_size(td), (1, 0))

    def test_directory_size_handles_walk_error(self):
        with patch("jobchain.operations.os.walk", side_effect=OSError("denied")):
            self.assertEqual(_directory_size("/missing"), (0, 0))

    def test_apply_changes_rejects_invalid_values(self):
        schema = SimpleNamespace(field_names=["x"], id_field=None)
        store = Mock(home="/tmp")
        prepared = SimpleNamespace(
            schema=schema,
            store=store,
            config=SimpleNamespace(work_dir_template="{row.name}", name="r"),
        )
        row = RowState("r", "r", 1, 0, {"x": "old"}, 1, valid=True)
        checked = SimpleNamespace(ok=False, reasons=lambda: ["bad"])
        with patch("jobchain.operations._scan_row", return_value=checked), self.assertRaises(DataError):
            _apply_changes(prepared, row, {"x": "new"})
        store.hold.assert_not_called()

    def test_apply_changes_releases_hold_after_write_error(self):
        schema = SimpleNamespace(field_names=["x"], id_field=None)
        store = Mock(home="/tmp")
        prepared = SimpleNamespace(
            schema=schema,
            store=store,
            config=SimpleNamespace(work_dir_template="{row.name}", name="r"),
        )
        row = RowState("r", "r", 1, 0, {"x": "old"}, 1, valid=True)
        checked = SimpleNamespace(ok=True, record={"x": "new"})
        store.write_row.side_effect = OSError("disk full")
        with patch("jobchain.operations._scan_row", return_value=checked), self.assertRaises(OSError):
            _apply_changes(prepared, row, {"x": "new"})
        store.release.assert_called_once_with("r")

    def test_apply_changes_success(self):
        schema = SimpleNamespace(field_names=["x"], id_field="x")
        store = Mock(home="/tmp")
        prepared = SimpleNamespace(
            schema=schema,
            store=store,
            config=SimpleNamespace(work_dir_template="{row.name}", name="r"),
        )
        row = RowState("r", "r", 1, 0, {"x": "old"}, 1, valid=True)
        checked = SimpleNamespace(ok=True, record={"x": "new"})
        with patch("jobchain.operations._scan_row", return_value=checked), patch(
            "jobchain.operations.get_logger"
        ) as logger:
            _apply_changes(prepared, row, {"x": "new"})
        store.hold.assert_called_once_with("r")
        store.release.assert_called_once_with("r")
        logger.return_value.info.assert_called_once()


class TestRegenerationAndSelectedSubmission(unittest.TestCase):
    def test_regenerate_row_rejects_bad_script(self):
        stage = Mock()
        stage.write_script.return_value = "/tmp/a.sh"
        stage.output_dir.return_value = "/tmp"
        stage.script_name.return_value = "a.sh"
        spec = SimpleNamespace(name="a", position=1, depends="-")
        pipeline = SimpleNamespace(specs=[spec], stage=lambda name: stage)
        store = Mock()
        row = SimpleNamespace(params={"x": 1})
        store.load_row.return_value = row
        prepared = SimpleNamespace(store=store, pipeline=pipeline)
        with patch("jobchain.operations._context_for", return_value=Mock()), patch(
            "jobchain.operations.verify_script", return_value="missing command"
        ), self.assertRaises(DataError):
            _regenerate_row(prepared, "r")

    def test_regenerate_row_writes_manifest(self):
        stage = Mock()
        stage.write_script.return_value = "/tmp/a.sh"
        spec = SimpleNamespace(name="a", position=1, depends="-")
        pipeline = SimpleNamespace(specs=[spec], stage=lambda name: stage)
        store = Mock()
        store.load_row.return_value = SimpleNamespace(params={"x": 1})
        prepared = SimpleNamespace(store=store, pipeline=pipeline)
        with patch("jobchain.operations._context_for", return_value=Mock()), patch(
            "jobchain.operations.verify_script", return_value=""
        ):
            self.assertEqual(_regenerate_row(prepared, "r"), 1)
        store.write_manifest.assert_called_once()

    def test_submit_selected_without_manifest(self):
        store = Mock()
        store.read_manifest.return_value = []
        prepared = SimpleNamespace(store=store, scheduler=Mock())
        self.assertEqual(
            _submit_selected(prepared, "r", ["a"], False),
            ([], "no manifest: the row has no generated scripts"),
        )

    def test_submit_selected_skips_unknown_stage(self):
        store = Mock()
        store.load_row.return_value = SimpleNamespace(generation=1)
        store.read_manifest.return_value = [("a", "-", "/a.sh")]
        store.run_dir.return_value = "/run"
        scheduler = Mock()
        scheduler.submit_pipeline.return_value = []
        prepared = SimpleNamespace(store=store, scheduler=scheduler)
        with patch("jobchain.operations._record_submissions", return_value=([], "")):
            result = _submit_selected(prepared, "r", ["missing"], False)
        self.assertEqual(result, ([], ""))
        scheduler.submit_pipeline.assert_called_once()

    def test_submit_selected_sets_chain_environment(self):
        store = Mock()
        store.load_row.return_value = SimpleNamespace(generation=1)
        store.read_manifest.return_value = [("a", "-", "/a.sh")]
        store.run_dir.return_value = "/run"
        scheduler = Mock()
        scheduler.submit_pipeline.return_value = []
        prepared = SimpleNamespace(store=store, scheduler=scheduler)
        with patch("jobchain.operations._record_submissions", return_value=([], "")):
            _submit_selected(prepared, "r", ["a"], True)
        env = scheduler.submit_pipeline.call_args.args[1]
        self.assertEqual(env["JC_CHAIN"], "1")


class TestValidationAndPreparationGaps(unittest.TestCase):
    def test_validate_only_normalizes_and_scans(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(params_path="params.tsv"),
            schema=SimpleNamespace(name="schema", fields=[1]),
            pipeline=SimpleNamespace(specs=[1]),
        )
        normalized = Mock()
        report = Mock()
        with patch("jobchain.operations.normalize_file", return_value=normalized) as norm, patch(
            "jobchain.operations.scan", return_value=report
        ) as scan:
            self.assertIs(_validate_only(prepared), report)
        norm.assert_called_once_with("params.tsv", prepared.schema)
        scan.assert_called_once_with(normalized, prepared.schema, "params.tsv")

    def test_prepare_fresh_non_strict_keeps_invalid_rows(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(
                strict=False,
                params_path="p",
                name="r",
                scheduler="pbs",
                width=1,
                max_attempts=1,
                work_dir_template="{run.home}",
                on_complete="",
                source_text="x",
                effective_workers=1,
                schema_source={"fields": []},
                pipeline_source=None,
            ),
            schema=SimpleNamespace(name="s", fields=[], unique_fields=[]),
            pipeline=SimpleNamespace(name="p", specs=[], chaining_stage=None, stage_names=[]),
            store=Mock(),
        )
        prepared.store.home = "/tmp/r"
        normalized = SimpleNamespace(rows=[1], changed_count=0, skipped_blank=0, skipped_comment=0)
        report = SimpleNamespace(
            invalid_rows=[SimpleNamespace(line_num=2, reasons=lambda: ["bad"])],
            valid_rows=[],
            rows=[1],
            ok=False,
            to_dict=lambda: {"ok": False},
        )
        rows = [SimpleNamespace(valid=False)]
        with patch("jobchain.operations.normalize_file", return_value=normalized), patch(
            "jobchain.operations.scan", return_value=report
        ), patch("jobchain.operations._create_row_state", return_value=rows), patch(
            "jobchain.operations._generate_scripts"
        ), patch(
            "jobchain.operations.render_final_config", return_value="final"
        ):
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
        results = [
            ("a", SimpleNamespace(success=True, job_id="1", output="")),
            ("b", SimpleNamespace(success=True, job_id="2", output="")),
        ]
        jobs, reason = _record_submissions(store, "/run", results)
        self.assertEqual(jobs, [("a", "1"), ("b", "2")])
        self.assertEqual(reason, "")
        self.assertEqual(store.mark.call_count, 2)

    def test_record_submissions_rejected_middle_stage_rolls_back(self):
        store = Mock()
        scheduler = Mock()
        results = [
            ("a", SimpleNamespace(success=True, job_id="1", output="")),
            ("b", SimpleNamespace(success=False, job_id=None, output="bad")),
        ]
        jobs, reason = _record_submissions(store, "/run", results, scheduler)
        self.assertEqual(jobs, [("a", "1")])
        self.assertEqual(reason, "bad")
        scheduler.cancel.assert_called_once_with("1")
        store.mark.assert_any_call(
            "/run", "a", "CANCELLED", error="cancelled: a later stage was rejected"
        )
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
        with patch("jobchain.operations.os.path.isfile", return_value=True), patch(
            "jobchain.operations._digest", return_value="new"
        ):
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
        row = RowState(
            "r", "r", 1, 0, {}, 0, valid=True, runs=[RunState(0, stages=[StageState("a", DONE)])]
        )
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
        row = RowState(
            "r", "r", 1, 0, {}, 0, valid=True, runs=[RunState(0, stages=[StageState("a", DONE)])]
        )
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


class TestDocumentsAndIdentifiers(unittest.TestCase):
    def test_schema_document_dict_is_preserved(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(schema_source={"fields": []}, base_dir="/base")
        )
        self.assertEqual(_schema_document(prepared), {"fields": []})

    def test_schema_document_relative_path_is_absolutized(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(schema_source="schema.yaml", base_dir="/base")
        )
        self.assertEqual(_schema_document(prepared), "/base/schema.yaml")

    def test_schema_document_absolute_path_is_preserved(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(schema_source="/x/schema.yaml", base_dir="/base")
        )
        self.assertEqual(_schema_document(prepared), "/x/schema.yaml")

    def test_pipeline_document_none(self):
        prepared = SimpleNamespace(config=SimpleNamespace(pipeline_source=None, base_dir="/base"))
        self.assertIsNone(_pipeline_document(prepared))

    def test_pipeline_document_relative_path(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(pipeline_source="pipe.yaml", base_dir="/base")
        )
        self.assertEqual(_pipeline_document(prepared), "/base/pipe.yaml")

    def test_pipeline_document_dict_absolutizes_module(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(pipeline_source={"stage_module": "stages.py"}, base_dir="/base")
        )
        self.assertEqual(_pipeline_document(prepared)["stage_module"], "/base/stages.py")

    def test_identifier_without_id_field_uses_name(self):
        schema = SimpleNamespace(id_field=None, field_names=[])
        result = SimpleNamespace(raw_fields=[])
        self.assertEqual(_identifier_for(schema, result, {}, "row7"), "row7")

    def test_identifier_uses_typed_record(self):
        schema = SimpleNamespace(id_field="id", field_names=["id"])
        result = SimpleNamespace(raw_fields=[])
        self.assertEqual(_identifier_for(schema, result, {"id": 42}, "row7"), "42")

    def test_identifier_falls_back_when_id_field_missing_from_schema(self):
        schema = SimpleNamespace(id_field="id", field_names=["x"])
        result = SimpleNamespace(raw_fields=["foo"])
        self.assertEqual(_identifier_for(schema, result, {}, "row7"), "row7")

    def test_identifier_uses_raw_id(self):
        schema = SimpleNamespace(id_field="id", field_names=["id"])
        result = SimpleNamespace(raw_fields=["  ABC  "])
        self.assertEqual(_identifier_for(schema, result, {}, "row7"), "ABC")


class TestScriptProgress(unittest.TestCase):
    def _prepared(self, progress):
        stage = Mock()
        stage.write_script.return_value = "/tmp/x.sh"
        stage.output_dir.return_value = "/tmp"
        stage.script_name.return_value = "x.sh"
        pipeline = SimpleNamespace(
            specs=[SimpleNamespace(name="s", position=1, depends="-")],
            stage=lambda name: stage,
        )
        return SimpleNamespace(
            pipeline=pipeline,
            store=Mock(),
            config=SimpleNamespace(effective_workers=1),
            run_context=Mock(),
        )

    def test_progress_lifecycle(self):
        progress = Mock()
        prepared = self._prepared(progress)
        row = RowState("r", "r", 1, 0, {}, 1, valid=True)
        with patch("jobchain.operations._context_for", return_value=Mock()), patch(
            "jobchain.operations.verify_script", return_value=""
        ):
            result = RunResult(store=prepared.store)
            _generate_scripts(prepared, [row], result, progress=progress)
        progress.start.assert_called_once_with(1)
        progress.advance.assert_called_once_with(1)
        progress.finish.assert_called_once()


class TestRerunExecution(unittest.TestCase):
    def _prepared(self):
        store = Mock()
        store.load_config.return_value = {"max_attempts": 0}
        store.load_row.return_value = SimpleNamespace(current=SimpleNamespace(handoff={"x": "1"}))
        store.bump_generation.return_value = 2
        pipeline = SimpleNamespace(stage_names=["a", "b"])
        config = SimpleNamespace(work_dir_template="{row.name}/{generation}")
        schema = SimpleNamespace(field_names=["x"])
        return SimpleNamespace(store=store, pipeline=pipeline, config=config, schema=schema)

    def test_dry_run_does_not_mutate_or_submit(self):
        prepared = self._prepared()
        row = RowState("r", "r", 1, 1, {"x": "1"}, 1, valid=True)
        plan = RerunPlan(rows=[row], stages=["a", "b"], new_generation=True)
        result = execute_rerun(prepared, plan, dry_run=True)
        self.assertEqual(result.rows, ["r"])
        prepared.store.bump_generation.assert_not_called()

    def test_full_rerun_seeds_handoff_and_bumps_generation(self):
        prepared = self._prepared()
        row = RowState("r", "r", 1, 1, {"x": "1"}, 1, valid=True)
        plan = RerunPlan(rows=[row], stages=["a", "b"], new_generation=True)
        with patch("jobchain.operations._regenerate_row", return_value=2), patch(
            "jobchain.operations._submit_selected", return_value=([("a", "9")], "")
        ):
            result = execute_rerun(prepared, plan, regenerate=True, chain=True)
        prepared.store.seed_handoff.assert_called_once_with("r", {"x": "1"})
        prepared.store.bump_generation.assert_called_once_with("r")
        self.assertEqual(result.regenerated, 2)
        self.assertEqual(result.submitted, [("r", [("a", "9")])])

    def test_fresh_handoff_clears_seed(self):
        prepared = self._prepared()
        row = RowState("r", "r", 1, 1, {"x": "1"}, 1, valid=True)
        plan = RerunPlan(rows=[row], stages=["a"], new_generation=True)
        with patch("jobchain.operations._regenerate_row", return_value=1):
            execute_rerun(prepared, plan, regenerate=True, fresh_handoff=True)
        prepared.store.clear_handoff_seed.assert_called_once_with("r")
        prepared.store.seed_handoff.assert_not_called()

    def test_partial_rerun_submits_without_new_generation(self):
        prepared = self._prepared()
        row = RowState("r", "r", 1, 1, {"x": "1"}, 1, valid=True)
        plan = RerunPlan(rows=[row], stages=["b"], new_generation=False)
        with patch("jobchain.operations._submit_selected", return_value=([("b", "4")], "")):
            result = execute_rerun(prepared, plan, chain=False)
        prepared.store.bump_generation.assert_not_called()
        self.assertEqual(result.submitted, [("r", [("b", "4")])])


class TestRerunPlanningFromOperationsDeepRemaining(unittest.TestCase):
    def _prepared(self, template="{row.name}"):
        store = Mock()
        store.load_config.return_value = {"max_attempts": 2}
        pipeline = SimpleNamespace(stage_names=["a", "b", "c"])
        config = SimpleNamespace(work_dir_template=template)
        schema = SimpleNamespace(field_names=["x"])
        return SimpleNamespace(store=store, pipeline=pipeline, config=config, schema=schema)

    def test_from_stage_selects_suffix(self):
        p = self._prepared()
        row = RowState("r", "r", 1, 0, {"x": "1"}, 1, valid=True)
        plan = plan_rerun(p, [row], from_stage="b")
        self.assertEqual(plan.stages, ["b", "c"])
        self.assertFalse(plan.new_generation)

    def test_stages_selects_requested_order(self):
        p = self._prepared()
        row = RowState("r", "r", 1, 0, {"x": "1"}, 1, valid=True)
        plan = plan_rerun(p, [row], stages=["c", "a"])
        self.assertEqual(plan.stages, ["a", "c"])

    def test_active_row_is_skipped_without_force(self):
        p = self._prepared()
        row = RowState("r", "r", 1, 0, {"x": "1"}, 1, valid=True)
        row.runs = [RunState(row.generation, stages=[StageState("a", RUNNING)])]
        plan = plan_rerun(p, [row])
        self.assertFalse(plan.rows)
        self.assertTrue(plan.skipped)

    def test_attempt_limit_skips_row(self):
        p = self._prepared()
        row = RowState("r", "r", 1, 2, {"x": "1"}, 1, runs=[RunState(0), RunState(1)], valid=True)
        plan = plan_rerun(p, [row])
        self.assertFalse(plan.rows)
        self.assertTrue(plan.skipped)


class TestDoctorAndCompletion(unittest.TestCase):
    def test_doctor_counts_pending_rows(self):
        store = Mock()
        store.name = "r"
        store.stopped = False
        store.load_config.return_value = {"width": 2, "chaining_stage": "b"}
        store.load_rows.return_value = [RowState("r", "r", 1, 0, {}, 1, valid=True)]
        scheduler = Mock()
        prepared = SimpleNamespace(store=store, scheduler=scheduler)
        with patch("jobchain.operations.describe_environment", return_value={}):
            result = doctor(prepared)
        self.assertEqual(result.pending_rows, 1)
        self.assertFalse(result.findings)

    def test_doctor_repairs_missing_job_id(self):
        stage = StageState("a", CLAIMED, "", None, "", "script.sh")
        run = RunState(1, stages=[stage])
        row = RowState("r", "r", 1, 1, {}, 1, runs=[run], valid=True)
        store = Mock()
        store.name = "r"
        store.stopped = False
        store.load_config.return_value = {"width": 1, "chaining_stage": ""}
        store.load_rows.return_value = [row]
        store.run_dir.return_value = "/tmp/r"
        prepared = SimpleNamespace(store=store, scheduler=Mock())
        with patch("jobchain.operations.describe_environment", return_value={}):
            result = doctor(prepared, repair=True)
        self.assertTrue(result.findings[0].repaired)
        store.mark.assert_called_once()

    def test_doctor_detects_vanished_job(self):
        stage = StageState("a", RUNNING, "99", None, "", "script.sh")
        run = RunState(1, stages=[stage])
        row = RowState("r", "r", 1, 1, {}, 1, runs=[run], valid=True)
        store = Mock(name="store")
        store.name = "r"
        store.stopped = False
        store.load_config.return_value = {"width": 1, "chaining_stage": ""}
        store.load_rows.return_value = [row]
        store.run_dir.return_value = "/tmp/r"
        scheduler = Mock()
        scheduler.job_state.return_value = "FINISHED"
        prepared = SimpleNamespace(store=store, scheduler=scheduler)
        with patch("jobchain.operations.describe_environment", return_value={}):
            result = doctor(prepared)
        self.assertTrue(result.findings)

    def test_completion_ignores_empty_store(self):
        store = Mock()
        store.load_rows.return_value = []
        self.assertIsNone(check_completion(store))

    def test_completion_removes_stale_done_marker_when_outstanding(self):
        row = RowState(
            "r", "r", 1, 1, {}, 1, runs=[RunState(1, stages=[StageState("a", RUNNING)])], valid=True
        )
        store = Mock()
        store.load_rows.return_value = [row]
        store.done_path = "/tmp/done"
        with patch("jobchain.operations.os.path.exists", return_value=True), patch(
            "jobchain.operations.os.unlink"
        ) as unlink:
            self.assertIsNone(check_completion(store))
        unlink.assert_called_once_with("/tmp/done")

    def test_completion_is_idempotent(self):
        row = RowState(
            "r", "r", 1, 1, {}, 1, runs=[RunState(1, stages=[StageState("a", DONE)])], valid=True
        )
        store = Mock()
        store.load_rows.return_value = [row]
        store.done_path = "/tmp/done"
        with patch("jobchain.operations.os.path.exists", return_value=True):
            self.assertIsNone(check_completion(store))


# from test_low_coverage_gap_closure.py
class TestOperationsLastBranches(unittest.TestCase):
    def test_regenerate_no_submit_returns_regenerated_phase(self):
        store = MagicMock()
        store.load_rows.return_value = [SimpleNamespace(valid=True, current=None, status="PENDING")]
        prepared = SimpleNamespace(
            store=store, scheduler=MagicMock(), config=SimpleNamespace(max_in_flight=0, width=1)
        )
        with patch.object(operations, "_generate_scripts") as gen, patch.object(
            store, "acquire_lock"
        ), patch.object(store, "release_lock"):
            result = operations._continue_existing(
                prepared,
                submit_only=False,
                regenerate=True,
                resume=False,
                no_submit=True,
                progress=None,
            )
        self.assertEqual(result.phase, "regenerated")
        gen.assert_called_once()

    def test_submission_ceiling_does_not_reduce_width_when_room_is_sufficient(self):
        store = MagicMock()
        store.load_rows.return_value = []
        store.claim.return_value = None
        store.stopped = False
        prepared = SimpleNamespace(
            store=store,
            scheduler=MagicMock(),
            config=SimpleNamespace(max_in_flight=10),
        )
        result = operations.RunResult(store=store)
        operations._submit_chains(prepared, width=2, result=result)
        self.assertTrue(result.exhausted)
        store.claim.assert_called_once()

    def test_record_submissions_without_scheduler_does_not_cancel_previous_jobs(self):
        store = MagicMock()
        submissions = [
            ("a", SimpleNamespace(success=True, job_id="1", output="")),
            ("b", SimpleNamespace(success=False, job_id=None, output="bad")),
        ]
        jobs, reason = operations._record_submissions(store, "/run", submissions, scheduler=None)
        self.assertEqual(jobs, [("a", "1")])
        self.assertEqual(reason, "bad")
        store.mark.assert_any_call("/run", "a", jobid="1")

    def test_plan_rerun_completed_row_with_generation_aware_skips_confirmation(self):
        row = SimpleNamespace(
            name="r", status=DONE, attempts=0, generation=2, is_terminal=True, valid=True
        )
        prepared = SimpleNamespace(
            config=SimpleNamespace(
                work_dir_template="{run.home}/rows/{row.id}/gen-{row.generation}"
            ),
            schema=SimpleNamespace(field_names=[]),
            pipeline=SimpleNamespace(stage_names=["solve"]),
            store=SimpleNamespace(load_config=lambda: {"max_attempts": 3}),
        )
        with patch.object(operations, "_existing_output", return_value=[("p", 1, 1)]):
            plan = operations.plan_rerun(prepared, [row])
        self.assertEqual(plan.needs_confirmation, [])
        self.assertEqual(plan.rows, [row])

    def test_doctor_finished_scheduler_state_creates_finding_and_repairs(self):
        stage = SimpleNamespace(status="RUNNING", jobid="77", name="solve", script="")
        row = SimpleNamespace(
            name="r",
            generation=1,
            current=SimpleNamespace(stages=[stage]),
            status="RUNNING",
            is_terminal=False,
            valid=True,
        )
        store = MagicMock()
        store.load_rows.return_value = [row]
        store.load_config.return_value = {"width": 1, "chaining_stage": ""}
        store.stopped = False
        store.run_dir.return_value = "/run"
        scheduler = MagicMock()
        scheduler.job_state.return_value = FINISHED
        prepared = SimpleNamespace(
            store=store,
            scheduler=scheduler,
            config=SimpleNamespace(width=1, chaining_stage=""),
            schema=MagicMock(),
        )
        with patch.object(operations, "describe_environment", return_value={}):
            result = operations.doctor(prepared, repair=True, dry_run=False)
        self.assertEqual(len(result.findings), 1)
        self.assertTrue(result.findings[0].repaired)
        store.mark.assert_called_once_with("/run", "solve", FAILED, error="job 77 vanished")

    def test_completion_hook_nonzero_exit_is_logged(self):
        store = SimpleNamespace(name="r", home="/h")
        payload = {"completion": 3, "rows": {"done": 2, "failed": 1}}
        completed = SimpleNamespace(returncode=2, stderr="bad hook")
        with patch("jobchain.operations.subprocess.run", return_value=completed), patch(
            "jobchain.operations.get_logger"
        ) as logger:
            operations._run_hook(store, "echo {run.name}", payload)
        logger.return_value.warning.assert_called_once()


class TestOperationsRemainingBranches(unittest.TestCase):
    def test_regenerate_with_submit_continues_to_submission(self):
        store = MagicMock()
        store.load_rows.return_value = [SimpleNamespace(valid=True, current=None, status="PENDING")]
        store.stopped = False
        prepared = SimpleNamespace(
            store=store,
            scheduler=MagicMock(),
            config=SimpleNamespace(max_in_flight=1, width=1),
        )
        with patch.object(operations, "_generate_scripts"), patch.object(
            operations, "_check_inputs_unchanged"
        ), patch.object(operations, "_submit_chains") as submit, patch.object(
            store, "acquire_lock"
        ), patch.object(
            store, "release_lock"
        ):
            result = operations._continue_existing(
                prepared,
                submit_only=False,
                regenerate=True,
                resume=False,
                no_submit=False,
                progress=None,
            )
        self.assertEqual(result.phase, "submitted")
        submit.assert_called_once()

    def test_plan_rerun_done_row_without_existing_output_does_not_need_confirmation(self):
        row = SimpleNamespace(
            name="r", status=DONE, attempts=0, generation=1, is_terminal=True, valid=True
        )
        prepared = SimpleNamespace(
            config=SimpleNamespace(work_dir_template="{run.home}/rows/{row.id}"),
            schema=SimpleNamespace(field_names=[]),
            pipeline=SimpleNamespace(stage_names=["solve"]),
            store=SimpleNamespace(load_config=lambda: {"max_attempts": 3}),
        )
        with patch.object(operations, "_existing_output", return_value=[]):
            plan = operations.plan_rerun(prepared, [row])
        self.assertEqual(plan.needs_confirmation, [])
        self.assertEqual(plan.rows, [row])

    def test_doctor_unknown_scheduler_state_is_not_marked_alive_or_finished(self):
        stage = SimpleNamespace(status="RUNNING", jobid="77", name="solve", script="")
        row = SimpleNamespace(
            name="r",
            generation=1,
            current=SimpleNamespace(stages=[stage]),
            status="RUNNING",
            is_terminal=False,
            valid=True,
        )
        store = MagicMock()
        store.load_rows.return_value = [row]
        store.load_config.return_value = {"width": 1, "chaining_stage": ""}
        store.stopped = False
        scheduler = MagicMock()
        scheduler.job_state.return_value = "UNKNOWN"
        prepared = SimpleNamespace(
            store=store, scheduler=scheduler, config=SimpleNamespace(width=1), schema=MagicMock()
        )
        with patch.object(operations, "describe_environment", return_value={}):
            result = operations.doctor(prepared, repair=False, dry_run=False)
        self.assertEqual(result.live_chains, 0)
        self.assertEqual(result.findings, [])

    def test_completion_hook_success_does_not_warn(self):
        store = SimpleNamespace(name="r", home="/h")
        payload = {"completion": 3, "rows": {"done": 2, "failed": 1}}
        completed = SimpleNamespace(returncode=0, stderr="")
        with patch("jobchain.operations.subprocess.run", return_value=completed), patch(
            "jobchain.operations.get_logger"
        ) as logger:
            operations._run_hook(store, "echo {run.name}", payload)
        logger.return_value.warning.assert_not_called()
