"""Deep unit coverage for operations state, rerun, doctor, and completion paths."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jobchain.operations import (
    DoctorResult, RerunPlan, RunResult, _directory_size, _existing_output,
    _identifier_for, _pipeline_document, _schema_document, _generate_scripts,
    execute_rerun, plan_rerun, doctor, check_completion,
)
from jobchain.store import DONE, FAILED, PENDING, RUNNING, CLAIMED, RowState, StageState, RunState


class TestDocumentsAndIdentifiers(unittest.TestCase):
    def test_schema_document_dict_is_preserved(self):
        prepared = SimpleNamespace(config=SimpleNamespace(schema_source={"fields": []}, base_dir="/base"))
        self.assertEqual(_schema_document(prepared), {"fields": []})

    def test_schema_document_relative_path_is_absolutized(self):
        prepared = SimpleNamespace(config=SimpleNamespace(schema_source="schema.yaml", base_dir="/base"))
        self.assertEqual(_schema_document(prepared), "/base/schema.yaml")

    def test_schema_document_absolute_path_is_preserved(self):
        prepared = SimpleNamespace(config=SimpleNamespace(schema_source="/x/schema.yaml", base_dir="/base"))
        self.assertEqual(_schema_document(prepared), "/x/schema.yaml")

    def test_pipeline_document_none(self):
        prepared = SimpleNamespace(config=SimpleNamespace(pipeline_source=None, base_dir="/base"))
        self.assertIsNone(_pipeline_document(prepared))

    def test_pipeline_document_relative_path(self):
        prepared = SimpleNamespace(config=SimpleNamespace(pipeline_source="pipe.yaml", base_dir="/base"))
        self.assertEqual(_pipeline_document(prepared), "/base/pipe.yaml")

    def test_pipeline_document_dict_absolutizes_module(self):
        prepared = SimpleNamespace(config=SimpleNamespace(pipeline_source={"stage_module": "stages.py"}, base_dir="/base"))
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


class TestRerunPlanning(unittest.TestCase):
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
        store.name = "r"; store.stopped = False
        store.load_config.return_value = {"width": 1, "chaining_stage": ""}
        store.load_rows.return_value = [row]
        store.run_dir.return_value = "/tmp/r"
        scheduler = Mock(); scheduler.job_state.return_value = "FINISHED"
        prepared = SimpleNamespace(store=store, scheduler=scheduler)
        with patch("jobchain.operations.describe_environment", return_value={}):
            result = doctor(prepared)
        self.assertTrue(result.findings)

    def test_completion_ignores_empty_store(self):
        store = Mock(); store.load_rows.return_value = []
        self.assertIsNone(check_completion(store))

    def test_completion_removes_stale_done_marker_when_outstanding(self):
        row = RowState("r", "r", 1, 1, {}, 1, runs=[RunState(1, stages=[StageState("a", RUNNING)])], valid=True)
        store = Mock(); store.load_rows.return_value = [row]; store.done_path = "/tmp/done"
        with patch("jobchain.operations.os.path.exists", return_value=True), patch("jobchain.operations.os.unlink") as unlink:
            self.assertIsNone(check_completion(store))
        unlink.assert_called_once_with("/tmp/done")

    def test_completion_is_idempotent(self):
        row = RowState("r", "r", 1, 1, {}, 1, runs=[RunState(1, stages=[StageState("a", DONE)])], valid=True)
        store = Mock(); store.load_rows.return_value = [row]; store.done_path = "/tmp/done"
        with patch("jobchain.operations.os.path.exists", return_value=True):
            self.assertIsNone(check_completion(store))


if __name__ == "__main__":
    unittest.main()
