from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jobchain.operations import (
    DataError, UsageError, _apply_changes, _existing_output, _directory_size,
    _regenerate_row, _submit_selected, execute_rerun, plan_rerun,
)
from jobchain.store import DONE, FAILED, RowState, RunState, StageState


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
        row = RowState("r", "r", 1, 0, {"x": "1"}, 1, valid=True,
                       work_dir=tempfile.gettempdir(),
                       runs=[RunState(1, stages=[StageState("a", DONE)])])
        with patch("jobchain.operations._existing_output", return_value=[("/x", 1, 2)]):
            plan = plan_rerun(p, [row])
        self.assertEqual(len(plan.needs_confirmation), 1)


class TestOutputAndCorrectionEdges(unittest.TestCase):
    def test_existing_output_directory_listing_error(self):
        row = SimpleNamespace(work_dir="/tmp/out")
        with patch("jobchain.operations.os.path.isdir", return_value=True), \
             patch("jobchain.operations.os.listdir", side_effect=OSError("denied")):
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
        with patch("jobchain.operations._scan_row", return_value=checked):
            with self.assertRaises(DataError):
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
        with patch("jobchain.operations._scan_row", return_value=checked):
            with self.assertRaises(OSError):
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
        with patch("jobchain.operations._scan_row", return_value=checked), \
             patch("jobchain.operations.get_logger") as logger:
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
        with patch("jobchain.operations._context_for", return_value=Mock()), \
             patch("jobchain.operations.verify_script", return_value="missing command"):
            with self.assertRaises(DataError):
                _regenerate_row(prepared, "r")

    def test_regenerate_row_writes_manifest(self):
        stage = Mock()
        stage.write_script.return_value = "/tmp/a.sh"
        spec = SimpleNamespace(name="a", position=1, depends="-")
        pipeline = SimpleNamespace(specs=[spec], stage=lambda name: stage)
        store = Mock()
        store.load_row.return_value = SimpleNamespace(params={"x": 1})
        prepared = SimpleNamespace(store=store, pipeline=pipeline)
        with patch("jobchain.operations._context_for", return_value=Mock()), \
             patch("jobchain.operations.verify_script", return_value=""):
            self.assertEqual(_regenerate_row(prepared, "r"), 1)
        store.write_manifest.assert_called_once()

    def test_submit_selected_without_manifest(self):
        store = Mock()
        store.read_manifest.return_value = []
        prepared = SimpleNamespace(store=store, scheduler=Mock())
        self.assertEqual(_submit_selected(prepared, "r", ["a"], False),
                         ([], "no manifest: the row has no generated scripts"))

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


if __name__ == "__main__":
    unittest.main()
