"""Deep, mock-heavy unit coverage of jobchain/store.py.

Consolidated from test_store_{unit,deep,exhaustive}.py into one file,
matching this project's one-file-per-subsystem convention.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jobchain.core import NodeHelperError, StateError
from jobchain.store import (
    CANCELLED,
    DONE,
    PENDING,
    ManifestEntry,
    RowState,
    RowStatus,
    RunState,
    StageState,
    Store,
    _column_value,
    _read_json,
    _read_optional,
    _write_text,
    find_node_binary,
)


# from test_store_exhaustive.py
def stage(name="s", status=DONE, jobid="1"):
    return StageState(
        name=name, status=status, jobid=jobid, depends="afterok", resources={}, timeline=[]
    )


# from test_store_exhaustive.py
def row(rid="001", runs=None):
    if runs is None:
        runs = [RunState(generation=1, stages=[stage()])]
    return RowState(
        name=rid,
        row_id=rid,
        line_num=2,
        index=0,
        params={"rid": rid},
        generation=1,
        runs=runs,
        valid=True,
        invalid_reasons=[],
        failure_id="",
        work_dir="",
    )


class TestRowStatusIsTransparentlyAString(unittest.TestCase):
    """RowStatus is a str-mixin Enum specifically so every existing
    comparison, dict key, and JSON output keeps working unchanged. These
    two properties are exactly what that choice depends on.
    """

    def test_serializes_as_its_plain_string_value(self):
        self.assertEqual(json.dumps({"s": RowStatus.DONE}), '{"s": "DONE"}')

    def test_round_trips_from_the_value_read_off_disk(self):
        self.assertIs(RowStatus("DONE"), RowStatus.DONE)
        self.assertEqual(RowStatus.DONE, "DONE")

    def test_str_and_fstring_format_as_the_plain_value_not_the_member_name(self):
        # A plain `str, Enum` mixin's str()/f-string formatting renders
        # "RowStatus.DONE" rather than "DONE" on some Python versions --
        # exactly the kind of call site report.py's f"status={terminal}"
        # relies on. This is what RowStatus's __str__/__format__ overrides
        # guard against; catches a regression if either is ever removed.
        self.assertEqual(str(RowStatus.DONE), "DONE")
        self.assertEqual(f"{RowStatus.DONE}", "DONE")
        self.assertEqual("{}".format(RowStatus.DONE), "DONE")


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
        with open(
            os.path.join(self.store.row_dir("000001"), "manifest"), "w", encoding="utf-8"
        ) as f:
            f.write("prep\t-\tprep.sh\n\nmalformed\ttoo\nmix\tafterok\tsolve.sh\n")
        self.assertEqual(
            self.store.read_manifest("000001"),
            [("prep", "-", "prep.sh"), ("mix", "afterok", "solve.sh")],
        )

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
        self.store.write_row(
            "000001",
            "row-A",
            7,
            2,
            {"x": 3},
            valid=False,
            invalid_reasons=["bad"],
            failure_id="4",
            work_dir="/tmp/w",
            raw_fields=["row-A", "3"],
        )
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
        with open(
            os.path.join(self.store.row_dir("000001"), "handoff.seed"), encoding="utf-8"
        ) as f:
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
        for i, (name, rowid, value) in enumerate(
            (("000001", "A", "x"), ("000002", "B", "y")), start=0
        ):
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
        self.store.write_resources(
            "/tmp/run", "solve", {"ncpus": 4, "mem": "", "gpu": None, "tags": []}
        )
        with open("/tmp/run/resources.solve.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"ncpus": 4})


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
        row = RowState(
            "1",
            "1",
            2,
            0,
            {},
            1,
            runs=[
                RunState(
                    1, stages=[StageState("prep", DONE), StageState("solve"), StageState("archive")]
                )
            ],
        )
        self.assertEqual(row.stage_reached, "prep")

    def test_row_terminal_detects_done_failure_and_cancel(self):
        for status, error in ((DONE, None), ("FAILED", "exit 1"), ("CANCELLED", None)):
            row = RowState(
                "1",
                "1",
                2,
                0,
                {},
                1,
                runs=[RunState(1, stages=[StageState("solve", status, error=error)])],
            )
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

        self.assertEqual(
            _parse_handoff("x=y\nJC_OUT_a='it'\\''s'\nJC_OUT_b=plain\n"),
            {"a": "it's", "b": "plain"},
        )

    def test_assignment_parser_strips_keys_and_values(self):
        from jobchain.store import _parse_assignments

        self.assertEqual(
            _parse_assignments(" A = one\nB= two\nignored\n"), {"A": "one", "B": "two"}
        )

    def test_row_name_and_padding(self):
        from jobchain.store import _pad, row_name

        self.assertEqual(row_name(0), "000001")
        self.assertEqual(_pad("42"), "000042")

    def test_missing_or_invalid_row_metadata_raises_state_error(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(os.path.join(root, "run"), node_binary="/bin/false")
            store.create({})
            os.makedirs(store.row_dir("000001"))
            with self.assertRaises(StateError):
                store.load_row("000001")
            with open(
                os.path.join(store.row_dir("000001"), "meta.json"), "w", encoding="utf-8"
            ) as f:
                f.write("broken")
            with self.assertRaises(StateError):
                store.load_row("000001")

    def test_node_execution_oserror_becomes_node_helper_error(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root, node_binary="/does/not/exist")
            with self.assertRaises(NodeHelperError):
                store._run_node(["claim"])


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
        # os.access is also patched away: besides JOBCHAIN_NODE and PATH,
        # find_node_binary tries a real, compiled bin/jobchain-node
        # sitting alongside the installed package, which exists in this
        # checkout, so leaving it reachable would make the "not found"
        # path untestable no matter how the other two candidates are
        # mocked.
        with patch.dict(os.environ, {}, clear=True), patch(
            "jobchain.store.shutil.which", return_value=None
        ), patch("jobchain.store.os.access", return_value=False), self.assertRaisesRegex(
            NodeHelperError, "could not be found"
        ):
            find_node_binary(explicit=None)

    def test_node_binary_explicit_missing_is_reported(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "jobchain.store.shutil.which", return_value=None
        ), patch("jobchain.store.os.access", return_value=False), self.assertRaisesRegex(
            NodeHelperError, "--node-binary"
        ):
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
        self.store.write_manifest(
            "000001", [ManifestEntry("prep", "-", "prep.sh"),
                      ManifestEntry("solve", "afterok", "solve.sh")]
        )
        run2 = self.store.run_dir("000001", 2)
        os.makedirs(run2, exist_ok=True)
        with open(os.path.join(self.store.row_dir("000001"), "gen"), "w") as h:
            h.write("2")
        loaded = self.store.load_row("000001")
        self.assertEqual(loaded.runs[0].handoff, {"A": "local", "B": "two"})
        self.assertEqual(loaded.runs[0].stages[0].status, PENDING)
        self.assertEqual(loaded.runs[0].stages[1].depends, "afterok")

    def test_column_value_falls_back_to_raw_fields_by_position_when_untyped(self):
        from jobchain.store import RowState

        row = RowState("1", "id", 1, 0, {}, 1, raw_fields=["id", "x"])
        # An unvalidated row has no typed params, so the column's raw text is
        # located by its position in the schema's field order instead.
        self.assertEqual(_column_value(row, "id", ["id", "other"]), "id")
        self.assertEqual(_column_value(row, "other", ["id", "other"]), "x")
        # A column absent from field_names, or no field_names at all (no
        # schema to consult), cannot be positioned, so it falls back to "".
        self.assertEqual(_column_value(row, "missing", ["id", "other"]), "")
        self.assertEqual(_column_value(row, "id", None), "")

    def test_claim_handles_empty_success_output(self):
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(self.store, "_run_node", return_value=result), self.assertRaises(
            NodeHelperError
        ):
            self.store.claim()

    def test_claim_returns_none_for_exhausted_queue(self):
        result = SimpleNamespace(returncode=3, stdout="", stderr="")
        with patch.object(self.store, "_run_node", return_value=result):
            self.assertIsNone(self.store.claim())

    def test_claim_reports_helper_failure(self):
        result = SimpleNamespace(returncode=2, stdout="", stderr="boom")
        with patch.object(self.store, "_run_node", return_value=result), self.assertRaisesRegex(
            NodeHelperError, "boom"
        ):
            self.store.claim()

    def test_mark_builds_all_optional_arguments(self):
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(self.store, "_run_node", return_value=result) as run:
            self.store.mark("/run/1", "solve", status="RUNNING", jobid="42", error="bad")
        self.assertEqual(
            run.call_args.args[0],
            [
                "mark",
                "--run",
                "/run/1",
                "--stage",
                "solve",
                "--status",
                "RUNNING",
                "--jobid",
                "42",
                "--error",
                "bad",
            ],
        )

    def test_mark_reports_failure(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="bad mark")
        with patch.object(self.store, "_run_node", return_value=result), self.assertRaisesRegex(
            NodeHelperError, "bad mark"
        ):
            self.store.mark("/run", "s")

    def test_event_logs_warning_on_helper_failure(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr="event failed")
        with patch.object(self.store, "_run_node", return_value=result), patch(
            "jobchain.store.core.get_logger"
        ) as logger:
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
        from jobchain.store import RowState, RunState, StageState

        row = RowState(
            "1", "1", 1, 0, {}, 1, runs=[RunState(1, stages=[StageState("prep", DONE, jobid="9")])]
        )
        self.assertTrue(row.is_terminal)


class TestStoreRemaining(unittest.TestCase):
    def test_jobid_none_and_unmatched_stage(self):
        r = row(runs=[])
        self.assertIsNone(r.jobid)
        r.runs = [RunState(generation=1, stages=[])]
        self.assertIsNone(r.jobid)

    def test_node_binary_lazy_lookup(self):
        store = Store("/tmp/jobchain-store-test")
        with patch("jobchain.store.core.find_node_binary", return_value="/node") as find:
            self.assertEqual(store.node_binary, "/node")
            self.assertEqual(store.node_binary, "/node")
        find.assert_called_once()

    def test_handoff_seed_deletes_existing_when_empty(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            os.makedirs(store.row_dir("r"), exist_ok=True)
            path = os.path.join(store.row_dir("r"), "handoff.seed")
            open(path, "w").close()
            store.seed_handoff("r", {})
            self.assertFalse(os.path.exists(path))

    def test_load_row_skips_non_directory_run_entries(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            rd = store.row_dir("r")
            os.makedirs(rd, exist_ok=True)
            with open(os.path.join(rd, "meta.json"), "w") as h:
                h.write('{"name":"r","row_id":"r","line_num":1,"index":0,"params":{}}')
            with open(os.path.join(rd, "gen"), "w") as h:
                h.write("1")
            with open(os.path.join(rd, "run-1"), "w") as h:
                h.write("not a directory")
            self.assertEqual(store.load_row("r").runs, [])

    def test_resolve_unique_duplicate_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            rows = [row("001"), row("002")]
            rows[0].params["x"] = "same"
            rows[1].params["x"] = "same"
            with self.assertRaises(StateError):
                store.resolve_row("x=same", ["x"])
            with self.assertRaises(StateError):
                store.resolve_row("x=none", ["x"])

    def test_resolve_padded_numeric_name(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            r = row("000001")
            with patch.object(store, "load_rows", return_value=[r]):
                self.assertIs(store.resolve_row("1", []), r)

    def test_resolve_row_id_fallback_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            r = row("abc")
            r.row_id = "external"
            with patch.object(store, "load_rows", return_value=[r]):
                self.assertIs(store.resolve_row("external", []), r)
                with self.assertRaises(StateError):
                    store.resolve_row("missing", [])

    def test_event_failure_only_warns(self):
        store = Store("/tmp/x")
        result = SimpleNamespace(returncode=1, stderr="bad", stdout="")
        with patch.object(store, "_run_node", return_value=result), patch(
            "jobchain.store.core.get_logger"
        ) as logger:
            store.event("x")
        logger.return_value.warning.assert_called_once()

    def test_write_text_without_parent_directory(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                _write_text("file.txt", "hello")
                with open("file.txt") as h:
                    self.assertEqual(h.read(), "hello")
            finally:
                os.chdir(old)

    def test_row_state_terminal_and_stage_reached_paths(self):
        r = row(runs=[RunState(generation=1, stages=[stage("a", PENDING), stage("b", PENDING)])])
        self.assertEqual(r.stage_reached, "a")
        r.runs = [RunState(generation=1, stages=[stage("a", CANCELLED), stage("b", PENDING)])]
        self.assertEqual(r.stage_reached, "a")
        self.assertTrue(r.is_terminal)

    def test_find_node_binary_failure_is_propagated(self):
        with patch(
            "jobchain.store.core.find_node_binary", side_effect=StateError("missing")
        ), self.assertRaises(StateError):
            _ = Store("/tmp/x").node_binary


class TestStoreFinalBranches(unittest.TestCase):
    def test_unique_column_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            a = row("001")
            b = row("002")
            a.params["x"] = "same"
            b.params["x"] = "same"
            with patch.object(store, "load_rows", return_value=[a, b]), self.assertRaises(
                StateError
            ):
                store.resolve_row("x=same", ["x"])


# from test_low_coverage_gap_closure.py
class TestStoreLastBranches(unittest.TestCase):
    def test_jobid_returns_none_when_reached_stage_has_no_matching_stage(self):
        row = RowState(
            "r",
            "id",
            1,
            0,
            {},
            1,
            runs=[SimpleNamespace(generation=1, stages=[SimpleNamespace(name="other", jobid="1")])],
        )
        with patch.object(
            type(row),
            "stage_reached",
            new_callable=unittest.mock.PropertyMock,
            return_value="missing",
        ):
            self.assertIsNone(row.jobid)

    def test_resume_without_stop_marker_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(d)
            s.resume()
            self.assertFalse(os.path.exists(s.stop_path))

    def test_clear_done_without_marker_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(d)
            s.clear_done()
            self.assertFalse(os.path.exists(s.done_path))

    def test_clear_handoff_without_seed_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(d)
            os.makedirs(s.row_dir("r"))
            s.clear_handoff_seed("r")
            self.assertFalse(os.path.exists(os.path.join(s.row_dir("r"), "handoff.seed")))

    def test_numeric_identifier_falls_through_when_padded_name_absent(self):
        rows = [SimpleNamespace(name="abc", row_id="1234", line_num=1)]
        with tempfile.TemporaryDirectory() as d:
            s = Store(d)
            with patch.object(s, "load_rows", return_value=rows):
                self.assertIs(rows[0], s.resolve_row("1234", []))

    def test_event_success_does_not_warn(self):
        result = SimpleNamespace(returncode=0, stderr="")
        with tempfile.TemporaryDirectory() as d, patch.object(
            Store, "_run_node", return_value=result
        ), patch("jobchain.store.core.get_logger") as logger:
            Store(d).event("hello")
        logger.return_value.warning.assert_not_called()

    def test_mark_success_does_not_raise(self):
        result = SimpleNamespace(returncode=0, stderr="")
        with tempfile.TemporaryDirectory() as d, patch.object(
            Store, "_run_node", return_value=result
        ):
            Store(d).mark("run", "stage", status=DONE, jobid="7", error="")
