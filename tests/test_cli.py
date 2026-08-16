"""Command-level tests: every command, its options, and its output."""

from __future__ import annotations

import os
import unittest

from tests.helpers import TempProject, require_node_binary

BOOM_PARAMS = """\
rid|count|label
a1|5|first
a2|10|boom
a3|15|third
"""


def setUpModule() -> None:
    require_node_binary()


class TestCommandSurface(TempProject):
    def test_the_command_set_stays_small(self):
        # Every command has to earn its place; a growing list is what makes a
        # tool hard to hold in mind.
        result = self.run_cli("--help", expect=0)
        for command in ("run", "status", "show", "rerun", "cancel", "doctor",
                        "logs", "export"):
            self.assertIn(command, result.stdout)
        for removed in ("init", "start", "validate", "explain", "retry",
                        "revise", "plan", "metrics", "reset"):
            self.assertNotIn(f"    {removed} ", result.stdout)

    def test_there_are_no_schema_or_pipeline_options(self):
        # Those paths belong in the run configuration, so that one file is
        # the complete description of a run.
        result = self.run_cli("run", "--help", expect=0)
        self.assertNotIn("--schema", result.stdout)
        self.assertNotIn("--pipeline", result.stdout)

    def test_version(self):
        self.assertIn("0.5", self.run_cli("--version", expect=0).stdout)

    def test_no_command_shows_help(self):
        self.assertIn("COMMAND", self.run_cli(expect=1).stdout)

    def test_help_lists_the_exit_codes(self):
        self.assertIn("exit codes:", self.run_cli("--help", expect=0).stdout)


class TestRunCommand(TempProject):
    def test_check_validates_without_writing(self):
        self.make_project()
        result = self.run_cli("run", "config.yaml", "--check", expect=0)
        self.assertIn("4 valid", result.stdout)
        self.assertFalse(os.path.exists(self.path(".jobchain")))

    def test_no_submit_prepares_without_submitting(self):
        self.make_project(pipeline=True)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.assertEqual(self.submissions(), [])
        self.assertTrue(os.path.isfile(self.path(".jobchain", "test-run",
                                                 "rows.idx")))

    def test_running_again_submits_what_was_prepared(self):
        # run is state-aware: repeating it does whatever remains.
        self.make_project(pipeline=True)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.run_cli("run", "config.yaml", expect=0)
        self.assertEqual(len(self.submissions()), 3)

    def test_width_can_be_overridden(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--width", "2", expect=0)
        self.assertEqual(len(self.submissions()), 6)  # two rows, three stages

    def test_the_run_name_can_be_overridden(self):
        self.make_project()
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--run-name", "other", expect=0)
        self.assertTrue(os.path.isdir(self.path(".jobchain", "other")))

    def test_configuration_is_captured_both_ways(self):
        self.make_project()
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        home = self.path(".jobchain", "test-run")
        self.assertTrue(os.path.isfile(os.path.join(home, "config.original.yaml")))
        self.assertTrue(os.path.isfile(os.path.join(home, "config.final.yaml")))

    def test_the_captured_configuration_reproduces_the_run(self):
        self.make_project(pipeline=True)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        final = self.path(".jobchain", "test-run", "config.final.yaml")
        self.run_cli("run", final, "--run-name", "copy", "--no-submit", expect=0)
        self.assertTrue(os.path.isfile(self.path(".jobchain", "copy", "rows.idx")))

    def test_json_output_is_parseable(self):
        self.make_project()
        self.install_scheduler(run_inline=False)
        payload = self.run_cli_json("run", "config.yaml", "--no-submit")
        self.assertEqual(payload["rows_created"], 4)

    def test_a_missing_configuration_is_a_configuration_error(self):
        self.run_cli("run", "absent.yaml", expect=5)

    def test_a_missing_parameter_file_is_a_structure_error(self):
        self.make_project()
        os.unlink(self.path("params.psv"))
        self.run_cli("run", "config.yaml", expect=4)


class TestStatus(TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.make_project(pipeline=True, width=1, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()

    def test_status_prints_a_table(self):
        result = self.run_cli("status", expect=0)
        self.assertIn("ROW", result.stdout)
        self.assertIn("STATUS", result.stdout)
        self.assertIn("a1", result.stdout)

    def test_status_shows_a_completion_bar_and_counts(self):
        result = self.run_cli("status", expect=0)
        self.assertIn("DONE", result.stdout)
        self.assertRegex(result.stdout, r"\d+/\d+ \(\d+\.\d%\)")

    def test_status_filters_by_status_prefix(self):
        payload = self.run_cli_json("status", "--status", "failed")
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0]["row_id"], "a2")

    def test_status_can_show_one_row_as_a_table_line(self):
        # status always prints a table; show always prints sections.
        result = self.run_cli("status", "--row", "a2", expect=0)
        self.assertIn("ROW", result.stdout)
        self.assertNotIn("PARAMETERS", result.stdout)

    def test_summary_only_omits_the_table(self):
        result = self.run_cli("status", "--summary-only", expect=0)
        self.assertNotIn("ROW  ", result.stdout)

    def test_metrics_are_added_on_request(self):
        result = self.run_cli("status", "--metrics", expect=0)
        self.assertIn("Finished", result.stdout)
        self.assertIn("Per stage", result.stdout)

    def test_metrics_are_absent_by_default(self):
        payload = self.run_cli_json("status")
        self.assertNotIn("metrics", payload)

    def test_warnings_appear_above_the_table(self):
        self.run_cli("cancel", "--stop", expect=0)
        result = self.run_cli("status", expect=0)
        stopped_at = result.stdout.index("stopped")
        table_at = result.stdout.index("ROW ")
        self.assertLess(stopped_at, table_at)


class TestShow(TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.make_project(pipeline=True, width=1, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()

    def test_show_prints_sections(self):
        result = self.run_cli("show", "--row", "a1", expect=0)
        self.assertIn("PARAMETERS", result.stdout)
        self.assertIn("STAGES", result.stdout)
        self.assertIn("PATHS", result.stdout)

    def test_a_failure_leads_with_the_failure(self):
        result = self.run_cli("show", "--row", "a2", expect=0)
        self.assertIn("FAILURE", result.stdout)
        self.assertLess(result.stdout.index("FAILURE"),
                        result.stdout.index("PARAMETERS"))

    def test_a_healthy_row_omits_the_failure_section(self):
        result = self.run_cli("show", "--row", "a1", expect=0)
        self.assertNotIn("FAILURE", result.stdout)

    def test_the_stage_table_shows_resources_as_requested(self):
        result = self.run_cli("show", "--row", "a1", expect=0)
        self.assertIn("walltime", result.stdout)
        self.assertIn("ncpus", result.stdout)
        self.assertIn("01:00:00", result.stdout)

    def test_handoff_values_are_shown(self):
        result = self.run_cli("show", "--row", "a1", expect=0)
        self.assertIn("HANDOFF", result.stdout)
        self.assertIn("mesh", result.stdout)

    def test_paths_report_directories_not_files(self):
        # A stage may produce thousands of files; the useful question is
        # which trees hold output.
        result = self.run_cli("show", "--row", "a1", "--paths", expect=0)
        self.assertIn("work", result.stdout)
        self.assertIn("files,", result.stdout)
        self.assertNotIn("mesh.txt", result.stdout)

    def test_a_row_can_be_named_by_a_unique_column(self):
        result = self.run_cli("show", "--row", "rid=a2", expect=0)
        self.assertIn("FAILURE", result.stdout)

    def test_invalid_rows_can_be_listed(self):
        self.write("params.psv", "rid|count|label\na1|5|ok\na2|999|bad\n")
        self.run_cli("run", "config.yaml", "--force", "--yes", expect=0)
        result = self.run_cli("show", "--invalid", expect=0)
        self.assertIn("999", result.stdout)
        self.assertIn("LINE", result.stdout)

    def test_show_needs_a_row(self):
        self.run_cli("show", expect=1)

    def test_history_shows_previous_generations(self):
        self.run_cli("rerun", "--row", "a2", expect=0)
        result = self.run_cli("show", "--row", "a2", "--history", expect=0)
        self.assertIn("HISTORY", result.stdout)
        self.assertIn("generation 1", result.stdout)

    def test_json_output_carries_the_stages(self):
        payload = self.run_cli_json("show", "--row", "a2")
        self.assertEqual(payload["row_id"], "a2")
        self.assertEqual(len(payload["stages"]), 3)


class TestNoHints(TempProject):
    """Messages state what happened and stop."""

    def test_errors_carry_no_suggested_commands(self):
        self.make_project(params="rid|count|label\na1|999|bad\n")
        result = self.run_cli("run", "config.yaml", "--check", "--strict",
                              expect=3)
        combined = result.stdout + result.stderr
        self.assertNotIn("hint:", combined)
        self.assertNotIn("jobchain rerun", combined)

    def test_show_has_no_next_section(self):
        self.make_project(pipeline=True, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("show", "--row", "a2", expect=0)
        self.assertNotIn("NEXT", result.stdout)


class TestLogsAndExport(TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.make_project(pipeline=True, width=1, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()

    def test_the_run_log_records_what_happened(self):
        result = self.run_cli("logs", expect=0)
        self.assertIn("row", result.stdout)
        self.assertTrue(os.path.isfile(self.path(".jobchain", "test-run",
                                                 "jobchain.log")))

    def test_logs_can_be_filtered_by_level(self):
        result = self.run_cli("logs", "--level", "warning", expect=0)
        for line in result.stdout.splitlines():
            if line.strip() and "no matching" not in line:
                self.assertNotIn("INFO", line)

    def test_export_preserves_the_original_columns(self):
        result = self.run_cli("export", expect=0)
        header = result.stdout.splitlines()[0]
        self.assertTrue(header.startswith("rid|count|label|"))
        self.assertIn("status", header)

    def test_export_writes_a_file(self):
        self.run_cli("export", "-o", "out.psv", expect=0)
        text = self.read(self.path("out.psv"))
        self.assertEqual(len(text.strip().splitlines()), 4)  # header + 3 rows

    def test_export_can_be_filtered(self):
        self.run_cli("export", "--status", "failed", "-o", "bad.psv", expect=0)
        self.assertEqual(len(self.read(self.path("bad.psv")).strip().splitlines()),
                         2)

    def test_export_records_the_work_directory(self):
        result = self.run_cli("export", expect=0)
        self.assertIn("work/000001", result.stdout)


class TestDryRun(TempProject):
    def test_a_dry_run_submits_nothing(self):
        self.make_project(pipeline=True)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", "--dry-run", expect=0)
        self.assertEqual(self.submissions(), [])

    def test_a_dry_run_rerun_changes_nothing(self):
        self.make_project(pipeline=True, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("rerun", "--row", "a2", "--dry-run", expect=0)
        self.assertEqual(self.store_for().resolve_row("a2").generation, 1)


if __name__ == "__main__":
    unittest.main()


class TestRemainingPaths(TempProject):
    """Options and branches not exercised by the main flows."""

    def setUp(self) -> None:
        super().setUp()
        self.make_project(pipeline=True, width=1, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()

    def test_show_prints_the_scheduler_output_for_a_stage(self):
        home = self.path(".jobchain", "test-run", "logs", "000002")
        os.makedirs(home, exist_ok=True)
        self.write(os.path.join(home, "solve.log"), "solver: out of memory\n")
        result = self.run_cli("show", "--row", "a2", "--output", expect=0)
        self.assertIn("out of memory", result.stdout)

    def test_show_reports_when_there_is_no_scheduler_output(self):
        self.run_cli("show", "--row", "a1", "--output", expect=6)

    def test_show_full_prints_every_section(self):
        result = self.run_cli("show", "--row", "a1", "--full", expect=0)
        self.assertIn("PARAMETERS", result.stdout)
        self.assertIn("PATHS", result.stdout)

    def test_show_stages_prints_only_the_stage_table(self):
        result = self.run_cli("show", "--row", "a1", "--stages", expect=0)
        self.assertIn("STAGES", result.stdout)
        self.assertNotIn("PARAMETERS", result.stdout)

    def test_invalid_rows_as_json(self):
        self.write("params.psv", "rid|count|label\na1|5|ok\na2|999|bad\n")
        self.run_cli("run", "config.yaml", "--force", "--yes", expect=0)
        payload = self.run_cli_json("show", "--invalid")
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["row_id"], "a2")

    def test_cancel_reports_when_nothing_is_active(self):
        result = self.run_cli("cancel", "--row", "a1", expect=0)
        self.assertIn("skipped", result.stdout + "no active")

    def test_cancel_as_json(self):
        payload = self.run_cli_json("cancel", "--row", "a1")
        self.assertIn("cancelled", payload)

    def test_cancel_needs_a_selection(self):
        self.run_cli("cancel", expect=1)

    def test_rerun_reports_when_nothing_matched(self):
        result = self.run_cli("rerun", "--status", "nosuchstatus", expect=0)
        self.assertIn("no rows matched", result.stdout)

    def test_rerun_rejects_a_malformed_assignment(self):
        self.run_cli("rerun", "--row", "a2", "--set", "justakey", expect=1)

    def test_rerun_rejects_an_unknown_column(self):
        self.run_cli("rerun", "--row", "a2", "--set", "nosuch=1", expect=1)

    def test_rerun_rejects_an_unknown_stage(self):
        self.run_cli("rerun", "--row", "a2", "--from", "nosuchstage", expect=1)

    def test_rerun_as_json(self):
        payload = self.run_cli_json("rerun", "--row", "a2")
        self.assertEqual(payload["rows"], ["000002"])

    def test_doctor_as_json(self):
        payload = self.run_cli_json("doctor", expect=None)
        self.assertIn("findings", payload)
        self.assertIn("environment", payload)

    def test_doctor_checks_the_filesystem(self):
        result = self.run_cli("doctor", "--check-fs", expect=0)
        self.assertIn("mkdir", result.stdout)

    def test_logs_can_be_filtered_by_stage(self):
        result = self.run_cli("logs", "--stage", "solve", expect=0)
        self.assertIsInstance(result.stdout, str)

    def test_logs_as_json(self):
        payload = self.run_cli_json("logs")
        self.assertIn("entries", payload)

    def test_export_as_json(self):
        payload = self.run_cli_json("export")
        self.assertEqual(len(payload), 3)

    def test_status_by_stage(self):
        payload = self.run_cli_json("status", "--stage", "archive")
        self.assertTrue(payload["rows"])

    def test_an_unknown_row_is_a_state_error(self):
        self.run_cli("show", "--row", "nosuchrow", expect=6)

    def test_a_line_reference_selects_a_row(self):
        result = self.run_cli("show", "--row", "line:3", expect=0)
        self.assertIn("a2", result.stdout)
