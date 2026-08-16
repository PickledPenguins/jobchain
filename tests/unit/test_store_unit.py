"""Focused unit coverage for the on-disk Store contract."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from jobchain.core import NodeHelperError, StateError
from jobchain.store import Store, DONE, PENDING, RowState, RunState, StageState


class TestStorePathsAndDiscovery(unittest.TestCase):
    def test_root_for_uses_parameter_directory(self):
        self.assertEqual(Store.root_for("/tmp/x/params.csv"), "/tmp/x/.jobchain")

    def test_discover_root_walks_upward(self):
        with tempfile.TemporaryDirectory() as root:
            nested = os.path.join(root, "a", "b")
            os.makedirs(nested)
            os.makedirs(os.path.join(root, ".jobchain"))
            self.assertEqual(Store.discover_root(nested), os.path.join(root, ".jobchain"))

    def test_discover_root_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(Store.discover_root(root))

    def test_list_runs_only_accepts_directories_with_config(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "good"))
            os.makedirs(os.path.join(root, "empty"))
            with open(os.path.join(root, "good", "config.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            with open(os.path.join(root, "file"), "w", encoding="utf-8") as f:
                f.write("")
            self.assertEqual(Store.list_runs(root), ["good"])

    def test_path_properties(self):
        store = Store("/tmp/project/.jobchain/run1", node_binary="node")
        self.assertEqual(store.name, "run1")
        self.assertTrue(store.rows_dir.endswith("/rows"))
        self.assertTrue(store.index_path.endswith("/rows.idx"))
        self.assertTrue(store.config_path.endswith("/config.json"))
        self.assertTrue(store.events_path.endswith("/events.log"))
        self.assertTrue(store.lock_path.endswith("/lock"))
        self.assertTrue(store.stop_path.endswith("/stopped"))
        self.assertTrue(store.done_path.endswith("/done.json"))
        self.assertTrue(store.completions_path.endswith("/completions.log"))
        self.assertTrue(store.log_path.endswith("/jobchain.log"))
        self.assertTrue(store.row_dir("000001").endswith("/rows/000001"))
        self.assertTrue(store.run_dir("000001", 2).endswith("/rows/000001/run-2"))


class TestStoreLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "run")
        self.store = Store(self.home, node_binary="/bin/false")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_store_is_not_existing_and_require_fails(self):
        self.assertFalse(self.store.exists())
        with self.assertRaises(StateError):
            self.store.require()

    def test_create_and_update_config(self):
        self.store.create({"params": "params.csv", "width": 4})
        self.assertTrue(self.store.exists())
        config = self.store.load_config()
        self.assertEqual(config["params"], "params.csv")
        self.assertEqual(config["width"], 4)
        self.assertIn("version", config)
        changed = self.store.update_config(width=8, workers=2)
        self.assertEqual(changed["width"], 8)
        self.assertEqual(self.store.load_config()["workers"], 2)

    def test_load_config_rejects_invalid_json(self):
        os.makedirs(self.home)
        with open(self.store.config_path, "w", encoding="utf-8") as f:
            f.write("not json")
        with self.assertRaises(StateError):
            self.store.load_config()

    def test_write_text_file_returns_path(self):
        os.makedirs(self.home)
        path = self.store.write_text_file("hello", "world")
        self.assertEqual(path, os.path.join(self.home, "hello"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "world")

    def test_lock_reports_existing_owner_and_release_is_idempotent(self):
        self.store.acquire_lock()
        with self.assertRaisesRegex(StateError, "another jobchain process"):
            self.store.acquire_lock()
        self.store.release_lock()
        self.store.release_lock()
        self.store.acquire_lock()
        self.store.release_lock()

    def test_stop_resume(self):
        self.store.create({})
        self.assertFalse(self.store.stopped)
        self.store.stop("maintenance")
        self.assertTrue(self.store.stopped)
        self.store.resume()
        self.assertFalse(self.store.stopped)

    def test_write_and_read_index(self):
        self.store.create({})
        self.store.write_index(["000001", "000002", "000002", ""])
        self.assertEqual(self.store.read_index(), ["000001", "000002", "000002"])

    def test_missing_index_is_state_error(self):
        self.store.create({})
        with self.assertRaises(StateError):
            self.store.read_index()

    def test_manifest_round_trip_skips_blank_and_malformed_lines(self):
        self.store.create({})
        os.makedirs(self.store.row_dir("000001"))
        with open(os.path.join(self.store.row_dir("000001"), "manifest"), "w", encoding="utf-8") as f:
            f.write("prep\t-\tprep.sh\n\nmalformed\ttoo\nmix\tafterok\tsolve.sh\n")
        self.assertEqual(self.store.read_manifest("000001"),
                         [("prep", "-", "prep.sh"), ("mix", "afterok", "solve.sh")])

    def test_hold_and_release(self):
        self.store.create({})
        self.store.write_row("000001", "a", 2, 0, {"x": 1})
        self.store.hold("000001")
        self.assertTrue(os.path.exists(os.path.join(self.store.row_dir("000001"), "hold")))
        self.store.release("000001")
        self.store.release("000001")
        self.assertFalse(os.path.exists(os.path.join(self.store.row_dir("000001"), "hold")))

    def test_write_row_loads_identity_and_parameters(self):
        self.store.create({})
        self.store.write_row("000001", "row-A", 7, 2, {"x": 3}, valid=False,
                             invalid_reasons=["bad"], failure_id="4", work_dir="/tmp/w",
                             raw_fields=["row-A", "3"])
        row = self.store.load_row("000001")
        self.assertEqual(row.row_id, "row-A")
        self.assertEqual(row.line_num, 7)
        self.assertEqual(row.index, 2)
        self.assertEqual(row.params, {"x": 3})
        self.assertFalse(row.valid)
        self.assertEqual(row.invalid_reasons, ["bad"])
        self.assertEqual(row.failure_id, "4")
        self.assertEqual(row.work_dir, "/tmp/w")
        self.assertEqual(row.raw_fields, ["row-A", "3"])

    def test_bump_generation_clears_done(self):
        self.store.create({})
        self.store.write_row("000001", "a", 2, 0, {"x": 1}, generation=1)
        with open(self.store.done_path, "w", encoding="utf-8") as f:
            f.write("done")
        self.assertEqual(self.store.bump_generation("000001"), 2)
        with open(os.path.join(self.store.row_dir("000001"), "gen"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "2")
        self.assertFalse(os.path.exists(self.store.done_path))

    def test_handoff_seed_is_sorted_and_can_be_cleared(self):
        self.store.create({})
        self.store.write_row("000001", "a", 2, 0, {})
        self.store.seed_handoff("000001", {"z": "last", "a": "first"})
        with open(os.path.join(self.store.row_dir("000001"), "handoff.seed"), encoding="utf-8") as f:
            text = f.read()
        self.assertLess(text.index("JC_OUT_a"), text.index("JC_OUT_z"))
        self.store.clear_handoff_seed("000001")
        self.assertFalse(os.path.exists(os.path.join(self.store.row_dir("000001"), "handoff.seed")))
        self.store.seed_handoff("000001", {})

    def test_destroy_removes_run(self):
        self.store.create({})
        self.assertTrue(self.store.exists())
        self.store.destroy()
        self.assertFalse(os.path.exists(self.home))
        self.store.destroy()


class TestStoreResolutionAndSerialization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "run"), node_binary="/bin/false")
        self.store.create({})
        for i, (name, rowid, value) in enumerate((("000001", "A", "x"), ("000002", "B", "y")), start=0):
            self.store.write_row(name, rowid, i + 2, i, {"id": rowid, "value": value})
        self.store.write_index(["000001", "000002"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_by_name_number_line_and_row_id(self):
        self.assertEqual(self.store.resolve_row("000001").row_id, "A")
        self.assertEqual(self.store.resolve_row("1").row_id, "A")
        self.assertEqual(self.store.resolve_row("line:3").row_id, "B")
        self.assertEqual(self.store.resolve_row("B").name, "000002")

    def test_resolve_rejects_invalid_line(self):
        with self.assertRaises(StateError):
            self.store.resolve_row("line:nope")
        with self.assertRaises(StateError):
            self.store.resolve_row("line:99")

    def test_resolve_by_unique_column_and_rejects_nonunique_column(self):
        self.assertEqual(self.store.resolve_row("id=A", ["id"]).name, "000001")
        with self.assertRaises(StateError):
            self.store.resolve_row("value=x", ["id"])
        with self.assertRaises(StateError):
            self.store.resolve_row("id=missing", ["id"])

    def test_load_rows_and_summary(self):
        rows = self.store.load_rows()
        self.assertEqual([r.name for r in rows], ["000001", "000002"])
        self.assertEqual(self.store.summary(), {PENDING: 2})

    def test_read_events_empty(self):
        self.assertEqual(self.store.read_events(), [])

    def test_resources_filters_empty_values(self):
        self.store.write_resources("/tmp/run", "solve", {"ncpus": 4, "mem": "", "gpu": None, "tags": []})
        with open("/tmp/run/resources.solve.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"ncpus": 4})


if __name__ == "__main__":
    unittest.main()

class TestStoreDerivedState(unittest.TestCase):
    def test_stage_lookup_returns_stage_or_none(self):
        run = RunState(1, stages=[StageState("prep"), StageState("solve")])
        self.assertEqual(run.stage("solve").name, "solve")
        self.assertIsNone(run.stage("missing"))

    def test_row_status_and_stage_reached_cover_pending_and_queued(self):
        row = RowState("1", "1", 2, 0, {}, 1, runs=[RunState(1, stages=[StageState("prep")])])
        self.assertEqual(row.status, PENDING)
        self.assertEqual(row.stage_reached, "prep")
        self.assertIsNone(row.jobid)
        row.runs[0].stages[0].status = "QUEUED"
        row.runs[0].stages[0].jobid = "42"
        self.assertEqual(row.status, "QUEUED")
        self.assertEqual(row.jobid, "42")

    def test_row_stage_reached_uses_last_nonpending_stage(self):
        row = RowState("1", "1", 2, 0, {}, 1, runs=[RunState(1, stages=[
            StageState("prep", DONE), StageState("solve"), StageState("archive")])])
        self.assertEqual(row.stage_reached, "prep")

    def test_row_terminal_detects_done_failure_and_cancel(self):
        for status, error in ((DONE, None), ("FAILED", "exit 1"), ("CANCELLED", None)):
            row = RowState("1", "1", 2, 0, {}, 1, runs=[RunState(1, stages=[StageState("solve", status, error=error)])])
            self.assertTrue(row.is_terminal)

    def test_failure_code_extracts_numeric_code(self):
        from jobchain.store import _code_of
        self.assertEqual(_code_of("exit code 17 from scheduler"), "17")
        self.assertEqual(_code_of("nothing numeric"), "error")
        self.assertEqual(_code_of(None), "unknown")


class TestStoreParsingHelpers(unittest.TestCase):
    def test_render_env_quotes_values_and_handles_none_and_bool(self):
        from jobchain.store import render_env
        text = render_env({"z": "it's here", "flag": True, "off": False, "none": None})
        self.assertIn("JC_z='it'\\''s here'", text)
        self.assertIn("JC_flag='1'", text)
        self.assertIn("JC_off='0'", text)
        self.assertIn("JC_none=''", text)

    def test_handoff_parser_ignores_nonhandoff_lines_and_unquotes(self):
        from jobchain.store import _parse_handoff
        self.assertEqual(_parse_handoff("x=y\nJC_OUT_a='it'\\''s'\nJC_OUT_b=plain\n"),
                         {"a": "it's", "b": "plain"})

    def test_assignment_parser_strips_keys_and_values(self):
        from jobchain.store import _parse_assignments
        self.assertEqual(_parse_assignments(" A = one\nB= two\nignored\n"),
                         {"A": "one", "B": "two"})

    def test_row_name_and_padding(self):
        from jobchain.store import row_name, _pad
        self.assertEqual(row_name(0), "000001")
        self.assertEqual(_pad("42"), "000042")

    def test_missing_or_invalid_row_metadata_raises_state_error(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(os.path.join(root, "run"), node_binary="/bin/false")
            store.create({})
            os.makedirs(store.row_dir("000001"))
            with self.assertRaises(StateError):
                store.load_row("000001")
            with open(os.path.join(store.row_dir("000001"), "meta.json"), "w", encoding="utf-8") as f:
                f.write("broken")
            with self.assertRaises(StateError):
                store.load_row("000001")

    def test_node_execution_oserror_becomes_node_helper_error(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, node_binary="/does/not/exist")
            with self.assertRaises(NodeHelperError):
                store._run_node(["claim"])


if __name__ == "__main__":
    unittest.main()
