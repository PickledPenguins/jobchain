"""Exhaustive unit coverage of the CLI orchestration layer.

These tests deliberately mock the lower layers.  The CLI's responsibility is
argument routing, presentation, exit-code translation, and command sequencing;
those concerns should be testable without repeatedly constructing scheduler
runs. End-to-end command behavior remains covered by tests.test_cli.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jobchain import cli
from jobchain.core import (
    EXIT_INTERNAL, EXIT_OK, EXIT_USAGE, ConflictError, JobChainError,
    StateError, UsageError,
)


def ns(**kwargs):
    """Create a permissive argparse namespace for one command path."""
    return argparse.Namespace(**kwargs)


class TestMainAndParser(unittest.TestCase):
    def test_parser_has_all_commands(self):
        parser = cli.build_parser()
        self.assertEqual(
            set(parser._subparsers._group_actions[0].choices),
            {"run", "status", "show", "rerun", "cancel", "doctor", "logs", "export"},
        )

    def test_main_without_command_prints_help(self):
        with patch.object(cli, "configure_logging"):
            self.assertEqual(cli.main([]), EXIT_USAGE)

    def test_main_dispatches_handler(self):
        handler = MagicMock(return_value=17)
        with patch.dict(cli._HANDLERS, {"status": handler}), \
             patch.object(cli, "configure_logging"):
            result = cli.main(["status"])
        self.assertEqual(result, 17)
        handler.assert_called_once()

    def test_main_translates_jobchain_error(self):
        with patch.dict(cli._HANDLERS, {"status": MagicMock(side_effect=StateError("bad"))}), \
             patch.object(cli, "configure_logging"), patch.object(cli, "get_logger") as logger:
            self.assertEqual(cli.main(["status"]), StateError("bad").exit_code)
            logger.return_value.error.assert_called_once()

    def test_main_translates_keyboard_interrupt(self):
        with patch.dict(cli._HANDLERS, {"status": MagicMock(side_effect=KeyboardInterrupt)}), \
             patch.object(cli, "configure_logging"), patch.object(cli, "get_logger") as logger:
            self.assertEqual(cli.main(["status"]), EXIT_USAGE)
            logger.return_value.error.assert_called_once_with("interrupted")

    def test_main_translates_unexpected_exception(self):
        with patch.dict(cli._HANDLERS, {"status": MagicMock(side_effect=RuntimeError("boom"))}), \
             patch.object(cli, "configure_logging"), patch.object(cli, "get_logger") as logger, \
             patch("traceback.print_exc") as trace:
            self.assertEqual(cli.main(["status"]), EXIT_INTERNAL)
            logger.return_value.error.assert_called_once()
            trace.assert_called_once()


class TestSharedHelpers(unittest.TestCase):
    def test_emit_prints_every_line(self):
        with patch("builtins.print") as p:
            cli._emit(["a", "b"])
        self.assertEqual([c.args[0] for c in p.call_args_list], ["a", "b"])

    def test_emit_json_is_sorted_and_indented(self):
        with patch("builtins.print") as p:
            cli._emit_json({"b": 2, "a": 1})
        payload = json.loads(p.call_args.args[0])
        self.assertEqual(payload, {"a": 1, "b": 2})
        self.assertIn("\n", p.call_args.args[0])

    def test_select_store_requires_root(self):
        with patch.object(cli.Store, "discover_root", return_value=None):
            with self.assertRaises(StateError):
                cli._select_store(ns(run_selector=None))

    def test_select_store_requires_runs(self):
        with patch.object(cli.Store, "discover_root", return_value="/r"), \
             patch.object(cli.Store, "list_runs", return_value=[]):
            with self.assertRaises(StateError):
                cli._select_store(ns(run_selector=None))

    def test_select_store_uses_explicit_selector(self):
        with patch.object(cli.Store, "discover_root", return_value="/r"), \
             patch.object(cli.Store, "list_runs", return_value=["a", "b"]):
            result = cli._select_store(ns(run_selector="b"))
        self.assertEqual(result.home, os.path.join("/r", "b"))

    def test_select_store_uses_environment_selector(self):
        with patch.dict(os.environ, {"JOBCHAIN_RUN": "a"}), \
             patch.object(cli.Store, "discover_root", return_value="/r"), \
             patch.object(cli.Store, "list_runs", return_value=["a", "b"]):
            result = cli._select_store(ns(run_selector=None))
        self.assertEqual(result.home, os.path.join("/r", "a"))

    def test_select_store_rejects_unknown_selector(self):
        with patch.object(cli.Store, "discover_root", return_value="/r"), \
             patch.object(cli.Store, "list_runs", return_value=["a"]):
            with self.assertRaises(StateError):
                cli._select_store(ns(run_selector="missing"))

    def test_select_store_automatically_uses_single_run(self):
        with patch.object(cli.Store, "discover_root", return_value="/r"), \
             patch.object(cli.Store, "list_runs", return_value=["a"]):
            result = cli._select_store(ns(run_selector=None))
        self.assertEqual(result.home, os.path.join("/r", "a"))

    def test_select_store_lists_ambiguous_runs(self):
        with patch.object(cli.Store, "discover_root", return_value="/r"), \
             patch.object(cli.Store, "list_runs", return_value=["a", "b"]), \
             patch.object(cli, "_run_summary", side_effect=lambda r, n: {"name": n}), \
             patch.object(cli.report, "render_run_list", return_value=["runs"]), \
             patch.object(cli, "_emit") as emit:
            with self.assertRaises(UsageError):
                cli._select_store(ns(run_selector=None))
        self.assertEqual(emit.call_count, 2)

    def test_run_summary_handles_corrupt_run(self):
        store = MagicMock()
        store.load_rows.side_effect = StateError("bad")
        with patch.object(cli, "Store", return_value=store):
            result = cli._run_summary("/r", "x")
        self.assertEqual(result["rows"], 0)
        self.assertEqual(result["started"], "-")

    def test_run_summary_counts_rows(self):
        rows = [SimpleNamespace(status="RUNNING"), SimpleNamespace(status="DONE"),
                SimpleNamespace(status="failed"), SimpleNamespace(status="CANCELLED")]
        store = MagicMock()
        store.load_rows.return_value = rows
        store.load_config.return_value = {"created_at": "2026-08-14T10:00:00Z"}
        with patch.object(cli, "Store", return_value=store), \
             patch.object(cli.report, "summarize", return_value={cli.DONE: 1, "failed": 1, "cancelled": 1}):
            result = cli._run_summary("/r", "x")
        self.assertEqual(result["rows"], 4)
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["active"], 1)

    def test_open_uses_null_scheduler_for_dry_run(self):
        store = MagicMock()
        prepared = SimpleNamespace(store=store, config=SimpleNamespace(scheduler="pbs", on_complete=None))
        with patch.object(cli, "_select_store", return_value=store), \
             patch.object(cli.operations, "open_run", return_value=prepared), \
             patch.object(cli, "_attach_file_log"), \
             patch.object(cli.operations, "check_completion") as complete, \
             patch("jobchain.scheduler.NullScheduler") as null:
            result = cli._open(ns(run_selector=None, dry_run=True))
        self.assertIs(result, prepared)
        null.assert_called_once_with("pbs")
        complete.assert_not_called()

    def test_open_checks_completion_when_not_dry(self):
        store = MagicMock()
        prepared = SimpleNamespace(store=store, config=SimpleNamespace(scheduler="pbs", on_complete="hook"))
        with patch.object(cli, "_select_store", return_value=store), \
             patch.object(cli.operations, "open_run", return_value=prepared), \
             patch.object(cli, "_attach_file_log"), \
             patch.object(cli.operations, "check_completion") as complete:
            cli._open(ns(run_selector=None, dry_run=False))
        complete.assert_called_once_with(store, "hook")

    def test_resolve_rows_by_selectors(self):
        store = MagicMock()
        store.resolve_row.side_effect = ["a", "b"]
        prepared = SimpleNamespace(store=store, schema=SimpleNamespace(unique_fields=["rid"]))
        self.assertEqual(cli._resolve_rows(prepared, ns(rows=["1", "2"], statuses=[])), ["a", "b"])

    def test_resolve_rows_by_status(self):
        rows = [SimpleNamespace(status="DONE"), SimpleNamespace(status="failed"), SimpleNamespace(status="RUNNING")]
        prepared = SimpleNamespace(store=MagicMock(load_rows=MagicMock(return_value=rows)), schema=SimpleNamespace(unique_fields=[]))
        result = cli._resolve_rows(prepared, ns(rows=[], statuses=["fail"]))
        self.assertEqual(result, [rows[1]])

    def test_resolve_rows_all_active(self):
        rows = [SimpleNamespace(current=1, is_terminal=False), SimpleNamespace(current=1, is_terminal=True), SimpleNamespace(current=None, is_terminal=False)]
        prepared = SimpleNamespace(store=MagicMock(load_rows=MagicMock(return_value=rows)), schema=SimpleNamespace(unique_fields=[]))
        self.assertEqual(cli._resolve_rows(prepared, ns(rows=[], statuses=[], all_rows=True)), [rows[0]])

    def test_resolve_rows_default_active(self):
        row = SimpleNamespace(current=1, is_terminal=False)
        prepared = SimpleNamespace(store=MagicMock(load_rows=MagicMock(return_value=[row])), schema=SimpleNamespace(unique_fields=[]))
        self.assertEqual(cli._resolve_rows(prepared, ns(rows=[], statuses=[]), True), [row])

    def test_resolve_rows_requires_selection(self):
        prepared = SimpleNamespace(store=MagicMock(load_rows=MagicMock(return_value=[])), schema=SimpleNamespace(unique_fields=[]))
        with self.assertRaises(UsageError):
            cli._resolve_rows(prepared, ns(rows=[], statuses=[]))

    def test_confirm_skip(self):
        self.assertTrue(cli._confirm("p", "x", True))

    def test_confirm_non_tty_refuses(self):
        with patch.object(cli.sys.stdin, "isatty", return_value=False), patch.object(cli, "_emit") as emit:
            self.assertFalse(cli._confirm("p", "x", False))
        emit.assert_called_once()

    def test_confirm_accepts_expected_text(self):
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="YES"):
            self.assertTrue(cli._confirm("p", "YES", False))

    def test_confirm_rejects_wrong_text(self):
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="NO"):
            self.assertFalse(cli._confirm("p", "YES", False))

    def test_confirm_handles_eof(self):
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=EOFError):
            self.assertFalse(cli._confirm("p", "YES", False))


class TestProgress(unittest.TestCase):
    def test_progress_disabled_is_noop(self):
        p = cli.Progress(enabled=False)
        p.start(10); p.advance(); p.finish()
        self.assertEqual(p.done, 1)

    def test_progress_paints_when_terminal(self):
        fake = io.StringIO()
        with patch.object(cli.sys, "stderr", fake), patch.object(cli.sys.stderr, "isatty", return_value=True):
            p = cli.Progress(enabled=True, width=10)
            p.start(4)
            p.advance(2)
            p.finish()
        self.assertIn("2/4", fake.getvalue())
        self.assertTrue(fake.getvalue().endswith("\n"))

    def test_progress_zero_total_does_not_paint(self):
        fake = io.StringIO()
        with patch.object(cli.sys, "stderr", fake), patch.object(cli.sys.stderr, "isatty", return_value=True):
            cli.Progress(enabled=True).start(0)
        self.assertEqual(fake.getvalue(), "")


class TestRunRendering(unittest.TestCase):
    def test_render_header_for_single_job(self):
        config = SimpleNamespace(name="r", pipeline_source=None, source_path="c", params_path="p", scheduler="pbs", width=2)
        store = SimpleNamespace(home="h")
        lines = cli._render_header(config, store)
        self.assertIn("single job per row", lines[2])

    def test_render_header_for_pipeline(self):
        config = SimpleNamespace(name="r", pipeline_source="p", source_path="c", params_path="p", scheduler="pbs", width=2)
        self.assertIn("pipeline", cli._render_header(config, SimpleNamespace(home="h"))[2])

    def test_report_run_check_success_and_failure(self):
        scan_ok = SimpleNamespace(ok=True, rows=[])
        result = SimpleNamespace(scan_report=scan_ok, phase="check", scripts_written=0, submitted=[], failures=[], rows_invalid=0, exhausted=False)
        with patch.object(cli, "_emit") as emit, patch.object(cli, "format_report", return_value=["report"]):
            self.assertEqual(cli._report_run(result, ns()), EXIT_OK)
            result.scan_report = SimpleNamespace(ok=False, rows=[])
            self.assertEqual(cli._report_run(result, ns()), 3)
        self.assertTrue(emit.called)

    def test_report_run_renders_scripts_submissions_failures_and_invalid(self):
        result = SimpleNamespace(scan_report=None, phase="run", scripts_written=2,
                                 submitted=[("r", [("s", "1")])], failures=[("x", "bad")],
                                 rows_invalid=3, exhausted=False)
        with patch.object(cli, "_emit") as emit:
            self.assertEqual(cli._report_run(result, ns()), 7)
        text = "\n".join(emit.call_args.args[0])
        self.assertIn("generated 2", text)
        self.assertIn("failed to submit", text)
        self.assertIn("NOT submitted", text)

    def test_report_run_exhausted_without_submissions(self):
        result = SimpleNamespace(scan_report=None, phase="run", scripts_written=0, submitted=[], failures=[], rows_invalid=0, exhausted=True)
        with patch.object(cli, "_emit") as emit:
            self.assertEqual(cli._report_run(result, ns()), EXIT_OK)
        self.assertIn("no rows are available to claim", emit.call_args.args[0])

    def test_confirm_discard_returns_true_for_corrupt_store(self):
        store = MagicMock()
        store.load_rows.side_effect = StateError("bad")
        self.assertTrue(cli._confirm_discard(store, False))

    def test_confirm_discard_returns_true_for_empty_store(self):
        store = MagicMock()
        store.load_rows.return_value = []
        self.assertTrue(cli._confirm_discard(store, False))

    def test_confirm_discard_asks_for_finished_and_active(self):
        finished = SimpleNamespace(is_terminal=True, valid=True, current=None)
        active = SimpleNamespace(is_terminal=False, current=1)
        store = MagicMock(name="r")
        store.load_rows.return_value = [finished, active]
        with patch.object(cli, "_confirm", return_value=False) as confirm:
            self.assertFalse(cli._confirm_discard(store, False))
        self.assertIn("permanently deletes", confirm.call_args.args[0])


class TestStatusHelpers(unittest.TestCase):
    def test_status_all_requires_root(self):
        with patch.object(cli.Store, "discover_root", return_value=None):
            with self.assertRaises(StateError):
                cli._status_all(ns(prune_after=None, as_json=False))

    def test_status_all_json(self):
        with patch.object(cli.Store, "discover_root", return_value="/r"), \
             patch.object(cli.Store, "list_runs", return_value=["a"]), \
             patch.object(cli, "_run_summary", return_value={"name": "a"}), \
             patch.object(cli, "_emit_json") as emit:
            self.assertEqual(cli._status_all(ns(prune_after=None, as_json=True)), EXIT_OK)
        emit.assert_called_once()

    def test_status_all_empty_text(self):
        with patch.object(cli.Store, "discover_root", return_value="/r"), \
             patch.object(cli.Store, "list_runs", return_value=[]), patch.object(cli, "_emit") as emit:
            self.assertEqual(cli._status_all(ns(prune_after=None, as_json=False)), EXIT_OK)
        self.assertIn("no runs exist", emit.call_args.args[0])

    def test_prune_json_without_yes_does_not_destroy(self):
        with patch.object(cli.os.path, "isfile", return_value=True), patch.object(cli.os.path, "getmtime", return_value=0), \
             patch.object(cli, "Store") as store, patch.object(cli, "_emit_json") as emit:
            self.assertEqual(cli._prune_runs("/r", ["a"], 1, False, True), EXIT_OK)
        store.assert_called_once_with(os.path.join("/r", "a"))
        store.return_value.destroy.assert_not_called()
        self.assertFalse(emit.call_args.args[0]["pruned"])

    def test_prune_text_no_eligible(self):
        with patch.object(cli.os.path, "isfile", return_value=False), patch.object(cli, "_emit") as emit:
            self.assertEqual(cli._prune_runs("/r", ["a"], 1, False, False), EXIT_OK)
        self.assertIn("no runs", emit.call_args.args[0][0])

    def test_prune_text_requires_yes(self):
        with patch.object(cli.os.path, "isfile", return_value=True), patch.object(cli.os.path, "getmtime", return_value=0), patch.object(cli, "_emit") as emit:
            self.assertEqual(cli._prune_runs("/r", ["a"], 1, False, False), EXIT_USAGE)
        self.assertIn("nothing was removed", emit.call_args.args[0][-1])

    def test_prune_text_with_yes_destroys(self):
        with patch.object(cli.os.path, "isfile", return_value=True), patch.object(cli.os.path, "getmtime", return_value=0), patch.object(cli, "Store") as store, patch.object(cli, "_emit"):
            self.assertEqual(cli._prune_runs("/r", ["a"], 1, True, False), EXIT_OK)
        store.return_value.destroy.assert_called_once()


class TestShowAndOutput(unittest.TestCase):
    def test_show_invalid_json(self):
        row = SimpleNamespace(name="a", row_id="1", line_num=2, invalid_reasons=["bad"], valid=False)
        prepared = SimpleNamespace(store=MagicMock(load_rows=MagicMock(return_value=[row])))
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_emit_json") as emit:
            self.assertEqual(cli.cmd_show(ns(invalid=True, as_json=True)), EXIT_OK)
        self.assertEqual(emit.call_args.args[0][0]["row"], "a")

    def test_show_invalid_text(self):
        prepared = SimpleNamespace(store=MagicMock(load_rows=MagicMock(return_value=[])))
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli.report, "render_invalid", return_value=["bad"]), patch.object(cli, "_emit") as emit:
            self.assertEqual(cli.cmd_show(ns(invalid=True, as_json=False)), EXIT_OK)
        emit.assert_called_once_with(["bad"])

    def test_show_requires_row(self):
        with patch.object(cli, "_open", return_value=SimpleNamespace(store=MagicMock())):
            with self.assertRaises(UsageError):
                cli.cmd_show(ns(invalid=False, row=None))

    def test_show_paths_and_stages(self):
        row = SimpleNamespace(current=SimpleNamespace(stages=[]), name="a", row_id="1", line_num=1, status="DONE", generation=0, valid=True, invalid_reasons=[], params={}, work_dir="w")
        store = MagicMock(home="h")
        prepared = SimpleNamespace(store=store, schema=SimpleNamespace(unique_fields=[]))
        store.resolve_row.return_value = row
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli.report, "render_show", return_value=["x"]) as render, patch.object(cli, "_emit"):
            cli.cmd_show(ns(invalid=False, row="1", output=False, paths=True, stages=True, full=False, as_json=False, history=False))
        render.assert_called_once_with(row, store, ["paths", "stages"], history=False)

    def test_show_full_overrides_section_selection(self):
        row = SimpleNamespace(current=None, name="a")
        store = MagicMock(home="h")
        prepared = SimpleNamespace(store=store, schema=SimpleNamespace(unique_fields=[]))
        store.resolve_row.return_value = row
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli.report, "render_show", return_value=[] ) as render, patch.object(cli, "_emit"):
            cli.cmd_show(ns(invalid=False, row="1", output=False, paths=True, stages=True, full=True, as_json=False, history=True))
        render.assert_called_once_with(row, store, None, history=True)

    def test_show_json(self):
        stage = SimpleNamespace(name="s", status="DONE", jobid="1", depends="", script="x", resources={}, error=None)
        run = SimpleNamespace(stages=[stage], handoff={"x": "y"})
        row = SimpleNamespace(current=run, name="a", row_id="1", line_num=1, status="DONE", generation=2, valid=True, invalid_reasons=[], params={"x": 1}, work_dir="w")
        store = MagicMock(home="h")
        prepared = SimpleNamespace(store=store, schema=SimpleNamespace(unique_fields=[]))
        store.resolve_row.return_value = row
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_emit_json") as emit:
            cli.cmd_show(ns(invalid=False, row="1", output=False, paths=False, stages=False, full=False, as_json=True, history=False))
        self.assertEqual(emit.call_args.args[0]["generation"], 2)

    def test_show_output_uses_stage_reached(self):
        row = SimpleNamespace(name="a", stage_reached="solve")
        with tempfile.TemporaryDirectory() as d:
            prepared = SimpleNamespace(store=SimpleNamespace(home=d))
            logdir = os.path.join(d, "logs", "a")
            os.makedirs(logdir)
            path = os.path.join(logdir, "solve.out")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("hello\n")
            with patch.object(cli, "_emit") as emit:
                self.assertEqual(cli._show_output(prepared, row, None), EXIT_OK)
            self.assertTrue(emit.called)

    def test_show_output_requires_stage(self):
        with self.assertRaises(StateError):
            cli._show_output(SimpleNamespace(store=SimpleNamespace(home="/h")), SimpleNamespace(name="a", stage_reached=None), None)

    def test_show_output_missing_log(self):
        with self.assertRaises(StateError):
            cli._show_output(SimpleNamespace(store=SimpleNamespace(home="/h")), SimpleNamespace(name="a", stage_reached="s"), None)


class TestRerunCancelDoctorLogsExport(unittest.TestCase):
    def _prepared(self):
        store = MagicMock(name="store", home="/h")
        config = SimpleNamespace(on_complete=None, scheduler="pbs")
        return SimpleNamespace(store=store, config=config, schema=SimpleNamespace(unique_fields=[]))

    def test_rerun_rejects_bad_assignment(self):
        with patch.object(cli, "_open", return_value=self._prepared()), patch.object(cli, "_resolve_rows", return_value=[]):
            with self.assertRaises(UsageError):
                cli.cmd_rerun(ns(assignments=["bad"], stage=None, stages=None, from_stage=None, force=False, yes=False, regenerate=False, chain=False, fresh_handoff=False, dry_run=False, as_json=False))

    def test_rerun_completed_confirmation_declined(self):
        prepared = self._prepared()
        plan = SimpleNamespace(needs_confirmation=[(SimpleNamespace(row_id="1", name="a"), [])])
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_resolve_rows", return_value=[]), patch.object(cli.operations, "plan_rerun", return_value=plan), patch.object(cli, "_confirm", return_value=False):
            with self.assertRaises(ConflictError):
                cli.cmd_rerun(ns(assignments=[], stage=None, stages=None, from_stage=None, force=False, yes=False, regenerate=False, chain=False, fresh_handoff=False, dry_run=False, as_json=False))

    def test_rerun_completed_confirmation_with_force_can_decline(self):
        prepared = self._prepared()
        plan = SimpleNamespace(needs_confirmation=[(SimpleNamespace(row_id="1", name="a"), [])])
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_resolve_rows", return_value=[]), patch.object(cli.operations, "plan_rerun", return_value=plan), patch.object(cli, "_confirm", return_value=False), patch.object(cli, "_emit"):
            result = cli.cmd_rerun(ns(assignments=[], stage=None, stages=None, from_stage=None, force=True, yes=False, regenerate=False, chain=False, fresh_handoff=False, dry_run=False, as_json=False))
        self.assertEqual(result, EXIT_USAGE)

    def test_rerun_json_and_text_success_and_failure(self):
        prepared = self._prepared()
        plan = SimpleNamespace(needs_confirmation=[])
        result = SimpleNamespace(rows=["a"], regenerated=["a"], submitted=[("a", [("s", "1")])], skipped=[("b", "bad")], failures=[("c", "boom")])
        base = dict(assignments=["x=1"], stage="s", stages=None, from_stage=None, force=False, yes=True, regenerate=True, chain=True, fresh_handoff=True, dry_run=False)
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_resolve_rows", return_value=[]), patch.object(cli.operations, "plan_rerun", return_value=plan), patch.object(cli.operations, "execute_rerun", return_value=result), patch.object(cli.operations, "check_completion"), patch.object(cli, "_emit_json"):
            self.assertEqual(cli.cmd_rerun(ns(**base, as_json=True)), EXIT_OK)
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_resolve_rows", return_value=[]), patch.object(cli.operations, "plan_rerun", return_value=plan), patch.object(cli.operations, "execute_rerun", return_value=result), patch.object(cli.operations, "check_completion"), patch.object(cli, "_emit"):
            self.assertEqual(cli.cmd_rerun(ns(**base, as_json=False)), 7)

    def test_cancel_stop_only(self):
        prepared = self._prepared()
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_emit") as emit:
            result = cli.cmd_cancel(ns(stop=True, rows=[], statuses=[], all_rows=False, dry_run=False, as_json=False))
        prepared.store.stop.assert_called_once()
        self.assertEqual(result, EXIT_OK)
        self.assertIn("stopped", emit.call_args.args[0][0])

    def test_cancel_json(self):
        prepared = self._prepared()
        result = SimpleNamespace(cancelled=[("a", ["1"])], stopped=True, skipped=[])
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_resolve_rows", return_value=[]), patch.object(cli.operations, "cancel", return_value=result), patch.object(cli, "_emit_json") as emit:
            self.assertEqual(cli.cmd_cancel(ns(stop=False, rows=["a"], statuses=[], all_rows=False, dry_run=True, as_json=True, stage=None)), EXIT_OK)
        self.assertEqual(emit.call_args.args[0]["cancelled"][0]["row"], "a")

    def test_cancel_text_skipped_and_empty(self):
        prepared = self._prepared()
        result = SimpleNamespace(cancelled=[], stopped=False, skipped=[("a", "done")])
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_resolve_rows", return_value=[]), patch.object(cli.operations, "cancel", return_value=result), patch.object(cli, "_emit") as emit:
            cli.cmd_cancel(ns(stop=False, rows=["a"], statuses=[], all_rows=False, dry_run=False, as_json=False, stage=None))
        self.assertIn("skipped", emit.call_args.args[0][0])

    def test_doctor_filesystem(self):
        with patch.object(cli, "_check_filesystem", return_value=8) as check:
            self.assertEqual(cli.cmd_doctor(ns(check_fs=True)), 8)
        check.assert_called_once()

    def test_doctor_all_json(self):
        result = SimpleNamespace(ok=True, run="r", live_chains=1, target_width=1, stopped=False, findings=[], relaunched=[], environment={})
        store = MagicMock()
        with patch.object(cli.Store, "discover_root", return_value="/r"), patch.object(cli.Store, "list_runs", return_value=["a"]), patch.object(cli.operations, "open_run", return_value=MagicMock()), patch.object(cli.operations, "doctor", return_value=result), patch.object(cli, "_doctor_payload", return_value={"run":"r"}), patch.object(cli, "_emit_json") as emit:
            self.assertEqual(cli.cmd_doctor(ns(check_fs=False, all_runs=True, repair=False, dry_run=True, as_json=True)), EXIT_OK)
        emit.assert_called_once_with([{"run": "r"}])

    def test_doctor_single_bad_text_and_repair(self):
        prepared = self._prepared()
        result = SimpleNamespace(ok=False, run="r", live_chains=0, target_width=1, stopped=False, findings=[], relaunched=[], environment={}, total_rows=0, finished_rows=0, active_rows=0, pending_rows=0)
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli.operations, "doctor", return_value=result), patch.object(cli, "_render_doctor", return_value=["x"]), patch.object(cli, "_emit"):
            self.assertEqual(cli.cmd_doctor(ns(check_fs=False, all_runs=False, repair=False, dry_run=False, as_json=False)), 6)
            self.assertEqual(cli.cmd_doctor(ns(check_fs=False, all_runs=False, repair=True, dry_run=False, as_json=False)), EXIT_OK)

    def test_doctor_payload_and_render_shortfall(self):
        finding = SimpleNamespace(row="a", detail="bad", repaired=True)
        result = SimpleNamespace(run="r", live_chains=1, target_width=3, stopped=False, findings=[finding], relaunched=[("a", {"s":"1"})], environment={"x":"y"}, total_rows=2, finished_rows=1, active_rows=1, pending_rows=0)
        payload = cli._doctor_payload(result)
        self.assertEqual(payload["findings"][0]["repaired"], True)
        text = "\n".join(cli._render_doctor(result))
        self.assertIn("SHORTFALL 2", text)
        self.assertIn("relaunched 1", text)

    def test_check_filesystem_json_success_and_text_failure(self):
        with patch.object(cli.Store, "discover_root", return_value="/r"), patch.object(cli.Store, "selftest", return_value=(True, "ok")), patch.object(cli.Store, "destroy") as destroy, patch.object(cli, "_emit_json") as emit:
            # Store is patched to an instance-like object below so root construction is observable.
            pass
        store = MagicMock()
        store.selftest.return_value = (True, "ok")
        with patch.object(cli.Store, "discover_root", return_value="/r"), patch.object(cli, "Store", return_value=store), patch.object(cli, "_emit_json") as emit:
            self.assertEqual(cli._check_filesystem(ns(as_json=True)), EXIT_OK)
        store.destroy.assert_called_once()
        self.assertTrue(emit.call_args.args[0]["ok"])

    def test_logs_missing_and_json_and_follow(self):
        prepared = self._prepared()
        prepared.store.log_path = "/missing"
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_emit") as emit, patch.object(cli.os.path, "isfile", return_value=False):
            self.assertEqual(cli.cmd_logs(ns(follow=False, level=None, stage=None, lines=40, as_json=False)), EXIT_OK)
        self.assertIn("no log", emit.call_args.args[0][0])

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("INFO row stage solve\nWARNING row stage other\n")
            path = f.name
        self.addCleanup = lambda f: None
        prepared.store.log_path = path
        try:
            with patch.object(cli, "_open", return_value=prepared), patch.object(cli, "_emit_json") as emit:
                cli.cmd_logs(ns(follow=False, level="WARNING", stage="other", lines=1, as_json=True))
            self.assertEqual(len(emit.call_args.args[0]["entries"]), 1)
        finally:
            os.unlink(path)

        with patch.object(cli, "_open", return_value=prepared), patch.object(cli.os.path, "isfile", return_value=True), patch.object(cli, "_follow", return_value=4) as follow:
            self.assertEqual(cli.cmd_logs(ns(follow=True, level=None, stage=None, lines=40, as_json=False)), 4)

    def test_follow_returns_on_keyboard_interrupt(self):
        with patch("builtins.open", side_effect=KeyboardInterrupt):
            self.assertEqual(cli._follow("x", lambda x: x), EXIT_OK)

    def test_export_json_and_file_and_stdout(self):
        row = SimpleNamespace(status="DONE")
        prepared = SimpleNamespace(store=MagicMock(load_rows=MagicMock(return_value=[row])), schema=MagicMock())
        with patch.object(cli, "_open", return_value=prepared), patch.object(cli.report, "build_views", return_value=[]), patch.object(cli.report, "views_to_dicts", return_value=[{"x":1}]), patch.object(cli, "_emit_json") as emit:
            self.assertEqual(cli.cmd_export(ns(statuses=[], as_json=True, output=None)), EXIT_OK)
        emit.assert_called_once_with([{"x":1}])

        with patch.object(cli, "_open", return_value=prepared), patch.object(cli.report, "export_rows", return_value=["a|b"]), patch.object(cli, "_emit") as emit:
            self.assertEqual(cli.cmd_export(ns(statuses=[], as_json=False, output=None)), EXIT_OK)
        emit.assert_called_once_with(["a|b"])

        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = f.name
        os.unlink(path)
        try:
            with patch.object(cli, "_open", return_value=prepared), patch.object(cli.report, "export_rows", return_value=["a|b"]), patch.object(cli, "_emit") as emit:
                self.assertEqual(cli.cmd_export(ns(statuses=[], as_json=False, output=path)), EXIT_OK)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "a|b\n")
        finally:
            if os.path.exists(path): os.unlink(path)


if __name__ == "__main__":
    unittest.main()

class TestCliRemainingBranches(unittest.TestCase):
    def _run_args(self, **overrides):
        values = dict(config="config.yaml", width=None, workers=None, run_name=None,
                      strict=False, force=False, yes=False, verbose=0,
                      log_level=None, file_log_level=None, check=False,
                      no_submit=False, submit_only=False, regenerate=False,
                      resume=False, dry_run=False, as_json=False)
        values.update(overrides)
        return ns(**values)

    def test_cmd_run_force_declined(self):
        config = SimpleNamespace(params_path="p", home=lambda root: "/home", terminal_level="info", file_level="debug", name="r", pipeline_source=None, source_path="c")
        store = MagicMock()
        store.exists.return_value = True
        with patch.object(cli, "load_config", return_value=config), \
             patch.object(cli.Store, "discover_root", return_value="/root"), \
             patch.object(cli, "Store", return_value=store), \
             patch.object(cli, "_confirm_discard", return_value=False):
            self.assertEqual(cli.cmd_run(self._run_args(force=True)), EXIT_USAGE)

    def test_cmd_run_text_executes_header_and_completion(self):
        config = SimpleNamespace(params_path="p", home=lambda root: "/home", terminal_level="info", file_level="debug", name="r", pipeline_source=None, source_path="c", on_complete="hook")
        store = MagicMock(name="store")
        store.exists.return_value = False
        result = SimpleNamespace(store=SimpleNamespace(name="r", home="/home"), phase="run", rows_created=2, rows_invalid=0, scripts_written=1, submitted=[], failures=[], scan_report=None, exhausted=False)
        with patch.object(cli, "load_config", return_value=config), patch.object(cli.Store, "discover_root", return_value="/root"), patch.object(cli, "Store", return_value=store), patch.object(cli, "_render_header", return_value=["header"]) as header, patch.object(cli, "_emit") as emit, patch.object(cli.operations, "run", return_value=result), patch.object(cli.operations, "check_completion") as complete, patch.object(cli, "_report_run", return_value=EXIT_OK) as report_run:
            self.assertEqual(cli.cmd_run(self._run_args()), EXIT_OK)
        header.assert_called_once()
        complete.assert_called_once_with(result.store, "hook")
        report_run.assert_called_once()

    def test_cmd_run_json_scan_failure(self):
        config = SimpleNamespace(params_path="p", home=lambda root: "/home", terminal_level="info", file_level="debug", name="r", pipeline_source=None, source_path="c", on_complete=None)
        store = MagicMock(name="store")
        store.exists.return_value = False
        scan = SimpleNamespace(ok=False, to_dict=lambda: {"ok": False})
        result = SimpleNamespace(store=SimpleNamespace(name="r", home="/home"), phase="check", rows_created=0, rows_invalid=1, scripts_written=0, submitted=[], failures=[], scan_report=scan, exhausted=False)
        with patch.object(cli, "load_config", return_value=config), patch.object(cli.Store, "discover_root", return_value="/root"), patch.object(cli, "Store", return_value=store), patch.object(cli.operations, "run", return_value=result), patch.object(cli, "_emit_json") as emit:
            self.assertEqual(cli.cmd_run(self._run_args(as_json=True, check=True)), 3)
        self.assertFalse(emit.call_args.args[0]["scan"]["ok"])

    def test_cmd_run_json_success_without_scan(self):
        config = SimpleNamespace(params_path="p", home=lambda root: "/home", terminal_level="info", file_level="debug", name="r", pipeline_source=None, source_path="c", on_complete=None)
        store = MagicMock(name="store"); store.exists.return_value = False
        result = SimpleNamespace(store=SimpleNamespace(name="r", home="/home"), phase="run", rows_created=1, rows_invalid=0, scripts_written=1, submitted=[("a", [("s", "1")])], failures=[], scan_report=None, exhausted=False)
        with patch.object(cli, "load_config", return_value=config), patch.object(cli.Store, "discover_root", return_value="/root"), patch.object(cli, "Store", return_value=store), patch.object(cli.operations, "run", return_value=result), patch.object(cli.operations, "check_completion"), patch.object(cli, "_emit_json") as emit:
            self.assertEqual(cli.cmd_run(self._run_args(as_json=True)), EXIT_OK)
        self.assertEqual(emit.call_args.args[0]["submitted"][0]["row"], "a")

    def test_cmd_status_all_dispatches(self):
        with patch.object(cli, "_status_all", return_value=4) as status_all:
            self.assertEqual(cli.cmd_status(ns(all_runs=True)), 4)
        status_all.assert_called_once()

    def test_cmd_status_row_text_and_metrics(self):
        row = SimpleNamespace(current=1, is_terminal=False)
        store = MagicMock(name="store", home="/h")
        store.name = "run"
        store.load_rows.return_value = [row]
        store.load_config.return_value = {"width": 2}
        prepared = SimpleNamespace(store=store, schema=SimpleNamespace(unique_fields=["rid"]), config=SimpleNamespace(description="desc"))
        metrics = SimpleNamespace(live_chains=1, target_width=2)
        with patch.object(cli, "_open", return_value=prepared), patch.object(store, "resolve_row", return_value=row), patch.object(cli.report, "build_views", return_value=["v"]), patch.object(cli.report, "filter_views", return_value=["v"]), patch.object(cli.report, "summarize", return_value={}), patch.object(cli.report, "compute_metrics", return_value=metrics), patch.object(cli, "_status_body", return_value=["body"]) as body, patch.object(cli, "_emit") as emit:
            self.assertEqual(cli.cmd_status(ns(all_runs=False, row="a", statuses=None, stage=None, as_json=False, watch=False, metrics=True, summary_only=False)), EXIT_OK)
        body.assert_called_once()
        emit.assert_called_once_with(["body"])

    def test_cmd_status_json_with_metrics(self):
        row = SimpleNamespace(current=None, is_terminal=True)
        store = MagicMock(name="store", home="/h"); store.name="run"; store.stopped=False
        store.load_rows.return_value=[row]; store.load_config.return_value={"width":1}
        prepared=SimpleNamespace(store=store,schema=SimpleNamespace(unique_fields=[]),config=SimpleNamespace(description=""))
        metrics=SimpleNamespace(to_dict=lambda:{"x":1},live_chains=0,target_width=1)
        with patch.object(cli,"_open",return_value=prepared), patch.object(cli.report,"build_views",return_value=[]), patch.object(cli.report,"filter_views",return_value=[]), patch.object(cli.report,"summarize",return_value={}), patch.object(cli.report,"compute_metrics",return_value=metrics), patch.object(cli,"_emit_json") as emit:
            self.assertEqual(cli.cmd_status(ns(all_runs=False,row=None,statuses=None,stage=None,as_json=True,watch=False,metrics=True,summary_only=False)),EXIT_OK)
        self.assertEqual(emit.call_args.args[0]["metrics"],{"x":1})

    def test_cmd_status_watch_dispatches(self):
        prepared=SimpleNamespace(store=MagicMock())
        with patch.object(cli,"_open",return_value=prepared), patch.object(cli.report,"build_views",return_value=[]), patch.object(cli.report,"filter_views",return_value=[]), patch.object(cli.report,"summarize",return_value={}), patch.object(cli.report,"compute_metrics",return_value=SimpleNamespace(live_chains=0,target_width=1)), patch.object(cli,"_watch",return_value=9) as watch:
            # watch is reached after all normal status preparation.
            self.assertEqual(cli.cmd_status(ns(all_runs=False,row=None,statuses=None,stage=None,as_json=False,watch=True,metrics=False,summary_only=False)),9)
        watch.assert_called_once()

    def test_status_body_all_optional_sections(self):
        store=MagicMock(name="store",home="/h"); store.name="r"; store.stopped=True; store.load_rows.return_value=[]
        prepared=SimpleNamespace(store=store,config=SimpleNamespace(description="desc"))
        metrics=SimpleNamespace(live_chains=0,target_width=2)
        with patch.object(cli.report,"render_summary",return_value=["summary"]), patch.object(cli.report,"render_warnings",return_value=["warning"]), patch.object(cli.report,"render_metrics",return_value=["metrics"]), patch.object(cli.report,"render_table",return_value=["table"]):
            lines=cli._status_body(prepared,[],[],{},metrics,ns(metrics=True,summary_only=False))
        self.assertIn("warning",lines); self.assertIn("metrics",lines); self.assertIn("table",lines); self.assertIn("desc",lines[0])

    def test_status_body_omits_warnings_metrics_table(self):
        store=MagicMock(name="store",home="/h"); store.name="r"; store.stopped=False; store.load_rows.return_value=[]
        prepared=SimpleNamespace(store=store,config=SimpleNamespace(description=""))
        metrics=SimpleNamespace(live_chains=0,target_width=1)
        with patch.object(cli.report,"render_summary",return_value=["summary"]), patch.object(cli.report,"render_warnings",return_value=[]), patch.object(cli.report,"render_metrics") as rm, patch.object(cli.report,"render_table") as rt:
            lines=cli._status_body(prepared,[],[],{},metrics,ns(metrics=False,summary_only=True))
        rm.assert_not_called(); rt.assert_not_called(); self.assertIn("summary",lines)

    def test_watch_completes_after_terminal_rows(self):
        row=SimpleNamespace(valid=True,is_terminal=True,current=None)
        store=MagicMock(name="store"); store.load_rows.return_value=[row]; store.load_config.return_value={"width":1}
        prepared=SimpleNamespace(store=store,config=SimpleNamespace(description=""))
        with patch.object(cli.report,"build_views",return_value=[]), patch.object(cli.report,"filter_views",return_value=[]), patch.object(cli.report,"summarize",return_value={}), patch.object(cli.report,"compute_metrics",return_value=SimpleNamespace(live_chains=0,target_width=1)), patch.object(cli,"_status_body",return_value=["body"]), patch.object(cli,"_emit") as emit, patch.object(cli.sys.stdout,"write"), patch.object(cli.sys.stdout,"flush"):
            self.assertEqual(cli._watch(prepared,ns(statuses=None,stage=None,metrics=False,summary_only=False)),EXIT_OK)
        self.assertIn("run complete", emit.call_args_list[-1].args[0])

    def test_watch_handles_interrupt(self):
        store=MagicMock(name="store"); store.load_rows.side_effect=KeyboardInterrupt
        prepared=SimpleNamespace(store=store,config=SimpleNamespace(description=""))
        with patch.object(cli,"_emit") as emit:
            self.assertEqual(cli._watch(prepared,ns(statuses=None,stage=None,metrics=False,summary_only=False)),EXIT_OK)
        self.assertIn("stopped watching", " ".join(emit.call_args.args[0]))

    def test_prune_skips_recent_completed_run_and_prunes_json(self):
        with patch.object(cli.os.path,"isfile",return_value=True), patch.object(cli.os.path,"getmtime",return_value=10**12), patch.object(cli,"_emit_json") as emit:
            self.assertEqual(cli._prune_runs("/r",["a"],100,True,True),EXIT_OK)
        self.assertFalse(emit.call_args.args[0]["eligible"])

    def test_show_output_explicit_stage(self):
        with tempfile.TemporaryDirectory() as d:
            logdir=os.path.join(d,"logs","a"); os.makedirs(logdir)
            with open(os.path.join(logdir,"solve.out"),"w") as h: h.write("x")
            prepared=SimpleNamespace(store=SimpleNamespace(home=d))
            with patch.object(cli,"_emit"):
                self.assertEqual(cli._show_output(prepared,SimpleNamespace(name="a",stage_reached="prep"),"solve"),EXIT_OK)

    def test_rerun_no_output_lines(self):
        prepared=SimpleNamespace(store=MagicMock(),config=SimpleNamespace(on_complete=None))
        plan=SimpleNamespace(needs_confirmation=[]); result=SimpleNamespace(rows=[],regenerated=[],submitted=[],skipped=[],failures=[])
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli,"_resolve_rows",return_value=[]),patch.object(cli.operations,"plan_rerun",return_value=plan),patch.object(cli.operations,"execute_rerun",return_value=result),patch.object(cli.operations,"check_completion"),patch.object(cli,"_emit") as emit:
            self.assertEqual(cli.cmd_rerun(ns(assignments=[],stage=None,stages=None,from_stage=None,force=False,yes=False,regenerate=False,chain=False,fresh_handoff=False,dry_run=False,as_json=False)),EXIT_OK)
        self.assertEqual(emit.call_args.args[0],["no rows matched"])

    def test_cancel_dry_run_stop_only_does_not_stop_store(self):
        prepared=SimpleNamespace(store=MagicMock(name="store")); prepared.store.name="r"
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli,"_emit"):
            self.assertEqual(cli.cmd_cancel(ns(stop=True,rows=[],statuses=[],all_rows=False,dry_run=True,as_json=False)),EXIT_OK)
        prepared.store.stop.assert_not_called()

    def test_cancel_text_reports_stopped(self):
        prepared=SimpleNamespace(store=MagicMock()); result=SimpleNamespace(cancelled=[("a",["1"])],skipped=[],stopped=True)
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli,"_resolve_rows",return_value=[]),patch.object(cli.operations,"cancel",return_value=result),patch.object(cli,"_emit") as emit:
            cli.cmd_cancel(ns(stop=False,rows=["a"],statuses=[],all_rows=False,dry_run=False,as_json=False,stage=None))
        self.assertIn("chain is stopped", "\n".join(emit.call_args.args[0]))

    def test_doctor_all_text_reports_repair_counts_and_bad_result(self):
        finding=SimpleNamespace(repaired=True)
        good=SimpleNamespace(ok=True,run="a",findings=[finding],live_chains=1,target_width=2)
        bad=SimpleNamespace(ok=False,run="b",findings=[],live_chains=0,target_width=2)
        with patch.object(cli.Store,"discover_root",return_value="/r"),patch.object(cli.Store,"list_runs",return_value=["a","b"]),patch.object(cli.operations,"open_run",return_value=MagicMock()),patch.object(cli.operations,"doctor",side_effect=[good,bad]),patch.object(cli,"_emit") as emit:
            self.assertEqual(cli.cmd_doctor(ns(check_fs=False,all_runs=True,repair=False,dry_run=False,as_json=False)),6)
        self.assertEqual(emit.call_count,2)

    def test_doctor_all_no_runs_is_ok(self):
        with patch.object(cli.Store,"discover_root",return_value="/r"),patch.object(cli.Store,"list_runs",return_value=[]),patch.object(cli,"_emit") as emit:
            self.assertEqual(cli.cmd_doctor(ns(check_fs=False,all_runs=True,repair=False,dry_run=False,as_json=False)),EXIT_OK)
        # all([]) is true and no summary lines are required.
        self.assertEqual(emit.call_count,0)

    def test_check_filesystem_uses_fallback_root_and_failure_text(self):
        store=MagicMock(); store.selftest.return_value=(False,"bad fs")
        with patch.object(cli.Store,"discover_root",return_value=None),patch.object(cli,"Store",return_value=store),patch.object(cli,"_emit") as emit:
            self.assertEqual(cli._check_filesystem(ns(as_json=False)),8)
        self.assertIn("cannot safely host",emit.call_args.args[0][-1])

    def test_check_filesystem_json_failure(self):
        store=MagicMock(); store.selftest.return_value=(False,"bad")
        with patch.object(cli.Store,"discover_root",return_value="/r"),patch.object(cli,"Store",return_value=store),patch.object(cli,"_emit_json") as emit:
            self.assertEqual(cli._check_filesystem(ns(as_json=True)),8)
        self.assertFalse(emit.call_args.args[0]["ok"])

    def test_logs_empty_matching_text(self):
        prepared=SimpleNamespace(store=SimpleNamespace(log_path="/x"))
        with tempfile.NamedTemporaryFile(mode="w",delete=False) as f:
            f.write("INFO unrelated\n"); path=f.name
        prepared.store.log_path=path
        try:
            with patch.object(cli,"_open",return_value=prepared),patch.object(cli,"_emit") as emit:
                self.assertEqual(cli.cmd_logs(ns(follow=False,level="ERROR",stage=None,lines=40,as_json=False)),EXIT_OK)
            self.assertEqual(emit.call_args.args[0],["no matching entries"])
        finally: os.unlink(path)

    def test_follow_prints_matching_line_then_sleeps(self):
        class Handle:
            def __init__(self): self.calls=0
            def seek(self,*args): pass
            def readline(self):
                self.calls+=1
                if self.calls==1: return "hello\n"
                raise KeyboardInterrupt
            def __enter__(self): return self
            def __exit__(self,*args): return False
        with patch("builtins.open",return_value=Handle()),patch.object(cli,"time") as clock,patch.object(cli.sys.stdout,"flush"),patch("builtins.print") as print_:
            # matching returns the line once, then the handle interrupts.
            self.assertEqual(cli._follow("x",lambda lines: lines),EXIT_OK)
        print_.assert_called_once_with("hello")

    def test_follow_empty_read_sleeps_before_interrupt(self):
        class Handle:
            def seek(self,*args): pass
            def readline(self): raise KeyboardInterrupt
            def __enter__(self): return self
            def __exit__(self,*args): return False
        with patch("builtins.open",return_value=Handle()),patch.object(cli.time,"sleep") as sleep:
            self.assertEqual(cli._follow("x",lambda lines: lines),EXIT_OK)
        sleep.assert_not_called()

    def test_export_filters_statuses(self):
        rows=[SimpleNamespace(status="DONE"),SimpleNamespace(status="FAILED")]
        prepared=SimpleNamespace(store=MagicMock(load_rows=MagicMock(return_value=rows)),schema=MagicMock())
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli.report,"export_rows",return_value=["x"]) as export,patch.object(cli,"_emit"):
            cli.cmd_export(ns(statuses=["fail"],as_json=False,output=None))
        export.assert_called_once_with(prepared.schema,[rows[1]])

class TestCliLastGaps(unittest.TestCase):
    def test_attach_file_log_uses_argument_and_config_levels(self):
        prepared=SimpleNamespace(store=SimpleNamespace(log_path="/run.log"),config=SimpleNamespace(terminal_level="warning",file_level="debug"))
        args=ns(verbose=2,log_level=None,file_log_level=None)
        with patch.object(cli,"configure_logging") as configure:
            cli._attach_file_log(prepared,args)
        configure.assert_called_once_with(verbosity=2,log_file="/run.log",terminal_level="warning",file_level="debug")

    def test_status_all_delegates_to_prune(self):
        with patch.object(cli.Store,"discover_root",return_value="/r"),patch.object(cli.Store,"list_runs",return_value=["a"]),patch.object(cli,"_prune_runs",return_value=7) as prune:
            result=cli._status_all(ns(prune_after=3,yes=True,as_json=True))
        self.assertEqual(result,7); prune.assert_called_once_with("/r",["a"],3,True,True)

    def test_status_all_renders_run_list(self):
        with patch.object(cli.Store,"discover_root",return_value="/r"),patch.object(cli.Store,"list_runs",return_value=["a"]),patch.object(cli,"_run_summary",return_value={"name":"a"}),patch.object(cli.report,"render_run_list",return_value=["table"]),patch.object(cli,"_emit") as emit:
            self.assertEqual(cli._status_all(ns(prune_after=None,yes=False,as_json=False)),EXIT_OK)
        emit.assert_called_once_with(["table"])

    def test_prune_json_yes_destroys_eligible_runs(self):
        with patch.object(cli.os.path,"isfile",return_value=True),patch.object(cli.os.path,"getmtime",return_value=0),patch.object(cli,"Store") as store,patch.object(cli,"_emit_json"):
            self.assertEqual(cli._prune_runs("/r",["a","b"],1,True,True),EXIT_OK)
        self.assertEqual(store.return_value.destroy.call_count,2)

    def test_show_output_branch(self):
        prepared=SimpleNamespace(store=MagicMock(),schema=SimpleNamespace(unique_fields=[]))
        row=SimpleNamespace(name="a")
        prepared.store.resolve_row.return_value=row
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli,"_show_output",return_value=12) as output:
            result=cli.cmd_show(ns(invalid=False,row="a",output=True,stage="solve",paths=False,stages=False,full=False,as_json=False,history=False))
        self.assertEqual(result,12); output.assert_called_once_with(prepared,row,"solve")

    def test_completed_warning_formats_multiple_directories(self):
        row=SimpleNamespace(name="a",row_id="1")
        entries=[(row,[("/a",2,1024),("/b",3,2048)])]
        with patch.object(cli.report,"_format_size",side_effect=["1 KiB","2 KiB"]):
            lines=cli._render_completed_warning(entries)
        self.assertIn("  /a   2 files, 1 KiB",lines)
        self.assertIn("  /b   3 files, 2 KiB",lines)

    def test_doctor_all_requires_root(self):
        with patch.object(cli.Store,"discover_root",return_value=None):
            with self.assertRaises(StateError):
                cli.cmd_doctor(ns(check_fs=False,all_runs=True,repair=False,dry_run=False,as_json=False))

    def test_doctor_single_json_success(self):
        prepared=MagicMock()
        result=SimpleNamespace(ok=True)
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli.operations,"doctor",return_value=result),patch.object(cli,"_doctor_payload",return_value={"ok":True}) as payload,patch.object(cli,"_emit_json") as emit:
            self.assertEqual(cli.cmd_doctor(ns(check_fs=False,all_runs=False,repair=False,dry_run=False,as_json=True)),EXIT_OK)
        emit.assert_called_once_with({"ok":True}); payload.assert_called_once_with(result)

    def test_doctor_single_json_failure_without_repair(self):
        prepared=MagicMock(); result=SimpleNamespace(ok=False)
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli.operations,"doctor",return_value=result),patch.object(cli,"_doctor_payload",return_value={"ok":False}),patch.object(cli,"_emit_json"):
            self.assertEqual(cli.cmd_doctor(ns(check_fs=False,all_runs=False,repair=False,dry_run=False,as_json=True)),6)

    def test_render_doctor_reports_no_problems(self):
        result=SimpleNamespace(run="r",live_chains=2,target_width=2,total_rows=3,finished_rows=3,active_rows=0,pending_rows=0,findings=[],relaunched=[],environment={})
        self.assertIn("no problems found",cli._render_doctor(result))

    def test_watch_sleeps_for_active_run_then_completes(self):
        active=SimpleNamespace(valid=True,is_terminal=False,current=1)
        done=SimpleNamespace(valid=True,is_terminal=True,current=None)
        store=MagicMock(); store.load_rows.side_effect=[[active],[done]]; store.load_config.return_value={"width":1}
        prepared=SimpleNamespace(store=store,config=SimpleNamespace(description=""))
        metrics=SimpleNamespace(live_chains=1,target_width=1)
        with patch.object(cli.report,"build_views",return_value=[]),patch.object(cli.report,"filter_views",return_value=[]),patch.object(cli.report,"summarize",return_value={}),patch.object(cli.report,"compute_metrics",return_value=metrics),patch.object(cli,"_status_body",return_value=["body"]),patch.object(cli,"_emit"),patch.object(cli.sys.stdout,"write"),patch.object(cli.sys.stdout,"flush"),patch.object(cli.time,"sleep") as sleep:
            self.assertEqual(cli._watch(prepared,ns(statuses=None,stage=None,metrics=False,summary_only=False)),EXIT_OK)
        sleep.assert_called_once_with(cli.WATCH_INTERVAL_SECONDS)

    def test_follow_empty_read_retries_once_then_interrupts(self):
        class Handle:
            def __init__(self): self.calls=0
            def seek(self,*args): pass
            def readline(self):
                self.calls+=1
                if self.calls==1: return ""
                raise KeyboardInterrupt
            def __enter__(self): return self
            def __exit__(self,*args): return False
        with patch("builtins.open",return_value=Handle()),patch.object(cli.time,"sleep") as sleep:
            self.assertEqual(cli._follow("x",lambda lines: lines),EXIT_OK)
        sleep.assert_called_once_with(0.5)

class TestCliFinalBranches(unittest.TestCase):
    def test_status_json_without_metrics(self):
        row=SimpleNamespace(current=None,is_terminal=True)
        store=MagicMock(); store.name="r"; store.home="/h"; store.stopped=False; store.load_rows.return_value=[row]; store.load_config.return_value={"width":1}
        prepared=SimpleNamespace(store=store,schema=SimpleNamespace(unique_fields=[]),config=SimpleNamespace(description=""))
        metrics=SimpleNamespace(live_chains=0,target_width=1)
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli.report,"build_views",return_value=[]),patch.object(cli.report,"filter_views",return_value=[]),patch.object(cli.report,"summarize",return_value={}),patch.object(cli.report,"compute_metrics",return_value=metrics),patch.object(cli,"_emit_json") as emit:
            self.assertEqual(cli.cmd_status(ns(all_runs=False,row=None,statuses=None,stage=None,as_json=True,watch=False,metrics=False,summary_only=False)),EXIT_OK)
        self.assertNotIn("metrics",emit.call_args.args[0])

    def test_rerun_confirmation_accepts(self):
        prepared=SimpleNamespace(store=MagicMock(name="store"),config=SimpleNamespace(on_complete=None))
        row=SimpleNamespace(row_id="1",name="a")
        plan=SimpleNamespace(needs_confirmation=[(row,[])])
        result=SimpleNamespace(rows=["a"],regenerated=[],submitted=[],skipped=[],failures=[])
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli,"_resolve_rows",return_value=[]),patch.object(cli.operations,"plan_rerun",return_value=plan),patch.object(cli,"_confirm",return_value=True) as confirm,patch.object(cli.operations,"execute_rerun",return_value=result),patch.object(cli.operations,"check_completion"),patch.object(cli,"_emit"):
            self.assertEqual(cli.cmd_rerun(ns(assignments=[],stage=None,stages=None,from_stage=None,force=True,yes=False,regenerate=False,chain=False,fresh_handoff=False,dry_run=False,as_json=False)),EXIT_OK)
        confirm.assert_called_once_with("Re-running may overwrite the output above.","1",False)

    def test_rerun_dry_run_skips_completion_check(self):
        prepared=SimpleNamespace(store=MagicMock(name="store"),config=SimpleNamespace(on_complete=None))
        plan=SimpleNamespace(needs_confirmation=[])
        result=SimpleNamespace(rows=["a"],regenerated=[],submitted=[],skipped=[],failures=[])
        with patch.object(cli,"_open",return_value=prepared),patch.object(cli,"_resolve_rows",return_value=[]),patch.object(cli.operations,"plan_rerun",return_value=plan),patch.object(cli.operations,"execute_rerun",return_value=result),patch.object(cli.operations,"check_completion") as complete,patch.object(cli,"_emit"):
            self.assertEqual(cli.cmd_rerun(ns(assignments=[],stage=None,stages=None,from_stage=None,force=False,yes=False,regenerate=False,chain=False,fresh_handoff=False,dry_run=True,as_json=False)),EXIT_OK)
        complete.assert_not_called()

    def test_logs_matching_without_level_or_stage(self):
        prepared=SimpleNamespace(store=SimpleNamespace(log_path="/x"))
        with tempfile.NamedTemporaryFile(mode="w",delete=False) as f:
            f.write("INFO one\nINFO two\n"); path=f.name
        prepared.store.log_path=path
        try:
            with patch.object(cli,"_open",return_value=prepared),patch.object(cli,"_emit") as emit:
                self.assertEqual(cli.cmd_logs(ns(follow=False,level=None,stage=None,lines=1,as_json=False)),EXIT_OK)
            self.assertEqual(emit.call_args.args[0],["INFO two"])
        finally: os.unlink(path)
