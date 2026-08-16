import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jobchain.core import NodeHelperError
from jobchain.store import PENDING, DONE
from jobchain.store import Store, find_node_binary, _column_value, _read_json, _read_optional


class TestStoreDeep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "run")
        self.store = Store(self.home, node_binary="/bin/false")

    def tearDown(self):
        self.tmp.cleanup()

    def test_root_discovery_and_list_runs(self):
        root = os.path.join(self.tmp.name, ".jobchain")
        nested = os.path.join(root, "nested")
        os.makedirs(nested)
        for name in ("b", "a"):
            path = os.path.join(root, name)
            os.makedirs(path)
            with open(os.path.join(path, "config.json"), "w") as h:
                h.write("{}")
        os.makedirs(os.path.join(root, "not-a-run"))
        self.assertEqual(Store.list_runs(root), ["a", "b"])
        self.assertEqual(Store.list_runs(os.path.join(self.tmp.name, "missing")), [])
        self.assertEqual(Store.discover_root(nested), root)
        self.assertEqual(Store.discover_root(self.tmp.name), root)

    def test_node_binary_searches_environment_and_reports_unset(self):
        fake = os.path.join(self.tmp.name, "jobchain-node")
        with open(fake, "w") as h:
            h.write("x")
        os.chmod(fake, 0o755)
        with patch.dict(os.environ, {"JOBCHAIN_NODE": fake}, clear=True):
            self.assertEqual(find_node_binary(), os.path.abspath(fake))
        with patch.dict(os.environ, {}, clear=True), patch("jobchain.store.shutil.which", return_value=None):
            with self.assertRaisesRegex(NodeHelperError, "could not be found"):
                find_node_binary(explicit=None)

    def test_node_binary_explicit_missing_is_reported(self):
        with patch.dict(os.environ, {}, clear=True), patch("jobchain.store.shutil.which", return_value=None):
            with self.assertRaisesRegex(NodeHelperError, "--node-binary"):
                find_node_binary("/does/not/exist")

    def test_load_row_ignores_invalid_generation_directory_names(self):
        self.store.create({})
        self.store.write_row("000001", "A", 2, 0, {"x": 1})
        rowdir = self.store.row_dir("000001")
        os.makedirs(os.path.join(rowdir, "run-bad"))
        os.makedirs(os.path.join(rowdir, "not-a-run"))
        self.assertEqual(self.store.load_row("000001").generation, 1)

    def test_load_run_reads_handoff_and_stage_defaults(self):
        self.store.create({})
        self.store.write_row("000001", "A", 2, 0, {})
        run = self.store.run_dir("000001", 1)
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(self.store.row_dir("000001"), "handoff.seed"), "w") as h:
            h.write("JC_OUT_A=seed\n")
        with open(os.path.join(run, "handoff"), "w") as h:
            h.write("JC_OUT_A=local\nJC_OUT_B=two\n")
        self.store.write_manifest("000001", [("prep", "-", "prep.sh"), ("solve", "afterok", "solve.sh")])
        run2 = self.store.run_dir("000001", 2)
        os.makedirs(run2, exist_ok=True)
        with open(os.path.join(self.store.row_dir("000001"), "gen"), "w") as h:
            h.write("2")
        loaded = self.store.load_row("000001")
        self.assertEqual(loaded.runs[0].handoff, {"A": "local", "B": "two"})
        self.assertEqual(loaded.runs[0].stages[0].status, PENDING)
        self.assertEqual(loaded.runs[0].stages[1].depends, "afterok")

    def test_column_value_returns_empty_when_untyped(self):
        from jobchain.store import RowState
        row = RowState("1", "id", 1, 0, {}, 1, raw_fields=["id", "x"])
        self.assertEqual(_column_value(row, "missing", ["missing"]), "")
        self.assertEqual(_column_value(row, "missing", None), "")

    def test_claim_handles_empty_success_output(self):
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(self.store, "_run_node", return_value=result):
            with self.assertRaises(NodeHelperError):
                self.store.claim()

    def test_claim_returns_none_for_exhausted_queue(self):
        result = SimpleNamespace(returncode=3, stdout="", stderr="")
        with patch.object(self.store, "_run_node", return_value=result):
            self.assertIsNone(self.store.claim())

    def test_claim_reports_helper_failure(self):
        result = SimpleNamespace(returncode=2, stdout="", stderr="boom")
        with patch.object(self.store, "_run_node", return_value=result):
            with self.assertRaisesRegex(NodeHelperError, "boom"):
                self.store.claim()

    def test_mark_builds_all_optional_arguments(self):
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(self.store, "_run_node", return_value=result) as run:
            self.store.mark("/run/1", "solve", status="RUNNING", jobid="42", error="bad")
        self.assertEqual(run.call_args.args[0], ["mark", "--run", "/run/1", "--stage", "solve", "--status", "RUNNING", "--jobid", "42", "--error", "bad"])

    def test_mark_reports_failure(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="bad mark")
        with patch.object(self.store, "_run_node", return_value=result):
            with self.assertRaisesRegex(NodeHelperError, "bad mark"):
                self.store.mark("/run", "s")

    def test_event_logs_warning_on_helper_failure(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="event failed")
        with patch.object(self.store, "_run_node", return_value=result), patch("jobchain.store.get_logger") as logger:
            self.store.event("hello")
            logger.return_value.warning.assert_called_once()

    def test_selftest_returns_success_and_failure(self):
        ok = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
        bad = SimpleNamespace(returncode=1, stdout="", stderr="bad")
        with patch.object(self.store, "_run_node", return_value=ok):
            self.assertEqual(self.store.selftest(), (True, "ok"))
        with patch.object(self.store, "_run_node", return_value=bad):
            self.assertEqual(self.store.selftest(), (False, "bad"))

    def test_read_helpers_return_defaults_on_invalid_data(self):
        path = os.path.join(self.tmp.name, "bad")
        with open(path, "w") as h:
            h.write("not-json")
        self.assertEqual(_read_json(path, {"fallback": True}), {"fallback": True})
        self.assertIsNone(_read_optional(os.path.join(self.tmp.name, "missing")))

    def test_run_terminal_and_jobid_missing_stage(self):
        from jobchain.store import RunState, StageState, RowState
        row = RowState("1", "1", 1, 0, {}, 1, runs=[RunState(1, stages=[StageState("prep", DONE, jobid="9")])])
        self.assertTrue(row.is_terminal)


if __name__ == "__main__":
    unittest.main()
