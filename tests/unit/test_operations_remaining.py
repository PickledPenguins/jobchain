"""Additional unit coverage for remaining operations.py decision paths."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jobchain.operations import (
    PreparedRun, RunResult, _continue_existing, _generate_scripts, open_run,
    _submit_row, _submit_chains, run, doctor,
)
from jobchain.store import DONE, FAILED, PENDING, RUNNING, RowState, RunState, StageState


class TestRunAndExistingBranches(unittest.TestCase):
    def test_open_run_loads_captured_config_and_reuses_store(self):
        store = Mock()
        store.name = "r"
        store.home = "/tmp/r"
        store.require.return_value = None
        with patch("jobchain.operations.os.path.isfile", return_value=True), \
             patch("jobchain.config.load_config", return_value=Mock()), \
             patch("jobchain.operations.prepare") as prepare:
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
        with patch("jobchain.operations.prepare", return_value=prepared), \
             patch("jobchain.operations._prepare_fresh", return_value=fresh), \
             patch("jobchain.operations._submit_chains") as submit:
            result = run(SimpleNamespace(width=2), no_submit=False)
        self.assertEqual(result.phase, "submitted")
        submit.assert_called_once_with(prepared, 2, fresh)

    def test_run_existing_resume_path_submits(self):
        prepared = SimpleNamespace(store=Mock(), config=SimpleNamespace(width=2), scheduler=Mock())
        prepared.store.exists.return_value = True
        prepared.store.load_rows.return_value = []
        prepared.store.load_config.return_value = {"width": 2}
        with patch("jobchain.operations.prepare", return_value=prepared), \
             patch("jobchain.operations._check_inputs_unchanged"), \
             patch("jobchain.operations._submit_chains") as submit:
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
        result = RunResult(store=prepared.store)
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
        with self.assertRaises(Exception):
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
        prepared.scheduler.submit_pipeline.return_value = [("s", SimpleNamespace(success=True, job_id="7", output=""))]
        jobs, reason = _submit_row(prepared, "r1", "/tmp/r1")
        self.assertEqual(jobs, [("s", "7")])
        self.assertEqual(reason, "")
        prepared.store.write_resources.assert_called_once()
        prepared.store.mark.assert_any_call("/tmp/r1", "s", "QUEUED")


class TestPrepareFreshBranches(unittest.TestCase):
    def test_prepare_fresh_strict_validation_rejects_invalid_rows(self):
        prepared = SimpleNamespace(
            config=SimpleNamespace(strict=True, params_path="p", name="r", scheduler="pbs", width=1,
                                   max_attempts=1, work_dir_template="{run.home}", on_complete="", source_text="x",
                                   effective_workers=1),
            schema=SimpleNamespace(name="s", fields=[], unique_fields=[]),
            pipeline=SimpleNamespace(name="p", specs=[], chaining_stage=None, stage_names=[]),
            store=Mock(),
        )
        prepared.store.home = "/tmp/r"
        report = SimpleNamespace(invalid_rows=[SimpleNamespace(line_num=2, reasons=lambda: ["bad"])],
                                 valid_rows=[], rows=[1])
        normalized = SimpleNamespace(rows=[1], changed_count=0, skipped_blank=0, skipped_comment=0)
        with patch("jobchain.operations.normalize_file", return_value=normalized), \
             patch("jobchain.operations.scan", return_value=report):
            with self.assertRaises(Exception):
                _prepare_fresh(prepared)
        prepared.store.create.assert_not_called()


class TestScriptGenerationBranches(unittest.TestCase):
    def test_generate_scripts_empty_rows_is_noop(self):
        prepared = SimpleNamespace(pipeline=SimpleNamespace(specs=[1]), store=Mock(), config=SimpleNamespace(effective_workers=1))
        result = RunResult(store=prepared.store)
        _generate_scripts(prepared, [], result)
        prepared.store.write_manifest.assert_not_called()

    def test_generate_scripts_records_verify_failure(self):
        stage = Mock()
        stage.output_dir.return_value = "/tmp"
        stage.script_name.return_value = "x.sh"
        stage.write_script.return_value = "/tmp/x.sh"
        prepared = SimpleNamespace(
            pipeline=SimpleNamespace(specs=[SimpleNamespace(name="s", position=1, depends="-")], stage=lambda n: stage),
            store=Mock(), config=SimpleNamespace(effective_workers=1),
            run_context=Mock(),
        )
        row = RowState("r", "r", 1, 0, {}, 1, valid=True)
        with patch("jobchain.operations._context_for", return_value=Mock()), \
             patch("jobchain.operations.verify_script", return_value="not executable"):
            with self.assertRaises(Exception):
                _generate_scripts(prepared, [row], RunResult(store=prepared.store))

    def test_generate_scripts_records_render_exception(self):
        stage = Mock()
        stage.write_script.side_effect = RuntimeError("boom")
        prepared = SimpleNamespace(
            pipeline=SimpleNamespace(specs=[SimpleNamespace(name="s", position=1, depends="-")], stage=lambda n: stage),
            store=Mock(), config=SimpleNamespace(effective_workers=1), run_context=Mock(),
        )
        row = RowState("r", "r", 1, 0, {}, 1, valid=True)
        with patch("jobchain.operations._context_for", return_value=Mock()):
            with self.assertRaises(Exception):
                _generate_scripts(prepared, [row], RunResult(store=prepared.store))


if __name__ == "__main__":
    unittest.main()
