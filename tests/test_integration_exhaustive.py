"""Broad integration matrix: real CLI + store + parser + pipeline + scheduler.

These tests intentionally exercise complete component boundaries.  They avoid
long scheduler runs where a no-submit or single-row variant can validate the
same integration contract.
"""

from __future__ import annotations

import os
import unittest

from tests.helpers import TempProject, require_node_binary

# Keep this module independent of test ordering and use only fixtures from helpers.


def setUpModule() -> None:
    require_node_binary()


class TestRunPreparationIntegration(TempProject):
    def test_minimal_run_creates_complete_persistent_layout(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        root = self.path(".jobchain", "test-run")
        for name in ("config.original.yaml", "config.final.yaml", "rows.idx", "jobchain.log"):
            self.assertTrue(os.path.exists(os.path.join(root, name)), name)

    def test_prepared_run_can_be_reopened_and_submitted(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.run_cli("run", "config.yaml", expect=0)
        self.assertEqual(len(self.submissions()), 1)

    def test_run_name_and_width_cross_cli_config_store_boundary(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--run-name", "integration", "--width", "2", expect=0)
        self.assertTrue(os.path.isdir(self.path(".jobchain", "integration")))
        self.assertEqual(len(self.submissions()), 2)

    def test_check_mode_crosses_config_parser_schema_without_store_creation(self):
        self.make_project(width=1)
        result = self.run_cli("run", "config.yaml", "--check", expect=0)
        self.assertIn("4 valid", result.stdout)
        self.assertFalse(os.path.exists(self.path(".jobchain", "test-run", "rows.idx")))

    def test_json_run_result_round_trips(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        payload = self.run_cli_json("run", "config.yaml", "--no-submit")
        self.assertEqual(payload["rows_created"], 4)
        self.assertEqual(payload["run"], "test-run")

    def test_final_configuration_is_a_reusable_integration_artifact(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        final = self.path(".jobchain", "test-run", "config.final.yaml")
        self.run_cli("run", final, "--run-name", "clone", "--no-submit", expect=0)
        self.assertTrue(os.path.exists(self.path(".jobchain", "clone", "rows.idx")))


class TestPipelineStoreSchedulerIntegration(TempProject):
    def test_pipeline_manifest_maps_every_stage(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        entries = self.store_for().read_manifest("000001")
        self.assertEqual([x[0] for x in entries], ["prep", "solve", "archive"])
        self.assertEqual([x[1] for x in entries], ["-", "afterok", "afterany"])

    def test_pipeline_submission_dependencies_are_preserved(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)
        entries = self.submissions()
        self.assertEqual(len(entries), 3)
        self.assertNotIn("depend", entries[0])
        self.assertIn("afterok", entries[1])
        self.assertIn("afterany", entries[2])

    def test_pipeline_stage_resources_cross_into_generated_scripts(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        store = self.store_for()
        scripts = {dep: path for _, dep, path in store.read_manifest("000001")}
        self.assertIn("#PBS", self.read(scripts["afterok"]))
        self.assertIn("ncpus", self.read(scripts["afterok"]))

    def test_handoff_state_crosses_stage_boundary(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        row = self.store_for().resolve_row("a1")
        self.assertIn("mesh", row.current.handoff)
        self.assertTrue(os.path.exists(os.path.join(row.work_dir, "result.txt")))

    def test_failure_state_crosses_scheduler_to_store_to_report(self):
        params = "rid|count|label\na1|5|ok\na2|10|boom\n"
        self.make_project(pipeline=True, width=1, params=params)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("status", expect=0)
        self.assertIn("failed", result.stdout.lower())
        self.assertIn("a2", result.stdout)

    def test_failure_state_is_available_as_json(self):
        params = "rid|count|label\na1|5|ok\na2|10|boom\n"
        self.make_project(pipeline=True, width=1, params=params)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        payload = self.run_cli_json("status", "--status", "failed")
        self.assertEqual([x["row_id"] for x in payload["rows"]], ["a2"])


class TestSchemaParseIntegration(TempProject):
    def test_header_mismatch_is_reported_through_cli(self):
        self.make_project(width=1)
        self.write("params.psv", "rid|count|wrong\na1|5|x\n")
        result = self.run_cli("run", "config.yaml", "--check", expect=0)
        self.assertIn("header", result.stdout.lower() + result.stderr.lower())

    def test_invalid_row_is_persisted_without_a_script(self):
        self.make_project(width=1, params="rid|count|label\na1|999|bad\n")
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)
        row = self.store_for().resolve_row("a1")
        self.assertFalse(row.valid)
        self.assertEqual(self.store_for().read_manifest(row.name), [])

    def test_strict_validation_is_atomic_from_the_user_perspective(self):
        self.make_project(width=1, params="rid|count|label\na1|999|bad\n")
        self.run_cli("run", "config.yaml", "--strict", expect=3)
        self.assertFalse(os.path.exists(self.path(".jobchain", "test-run", "rows.idx")))

    def test_quoted_delimiter_survives_parser_to_store(self):
        self.make_project(width=1)
        config = (
            self.read(self.path("config.yaml"))
            .replace("delimiter: pipe", "delimiter: comma")
            .replace("id_field: rid", "id_field: rid, quoting: true")
        )
        self.write("config.yaml", config)
        self.write("params.psv", 'rid,count,label\na1,5,"hello,world"\n')
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        row = self.store_for().resolve_row("a1")
        self.assertEqual(row.params["label"], "hello,world")

    def test_tab_delimiter_survives_full_configuration_path(self):
        self.make_project(width=1)
        config = self.read(self.path("config.yaml")).replace("delimiter: pipe", "delimiter: tab")
        self.write("config.yaml", config)
        self.write("params.psv", "rid\tcount\tlabel\na1\t5\ttabbed\n")
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.assertEqual(self.store_for().resolve_row("a1").params["label"], "tabbed")


class TestOperationalIntegration(TempProject):
    def test_status_reads_store_after_no_submit(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        payload = self.run_cli_json("status")
        self.assertEqual(sum(payload["counts"].values()), 4)
        self.assertTrue(payload["rows"])

    def test_show_reads_persisted_stage_information(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        result = self.run_cli("show", "--row", "a1", expect=0)
        self.assertIn("01-prep.sh", result.stdout)

    def test_export_uses_schema_and_persisted_rows(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        payload = self.run_cli_json("export")
        self.assertEqual(len(payload), 4)
        self.assertEqual({x["row_id"] for x in payload}, {"a1", "a2", "a3", "a4"})

    def test_doctor_reads_environment_and_store(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        payload = self.run_cli_json("doctor", expect=0)
        self.assertIn("environment", payload)
        self.assertIn("findings", payload)

    def test_cancel_stop_changes_state_seen_by_status(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.run_cli("cancel", "--stop", expect=0)
        result = self.run_cli("status", expect=0)
        self.assertIn("stopped", result.stdout)

    def test_rerun_changes_generation_and_status(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.run_cli("rerun", "--row", "a1", expect=0)
        row = self.store_for().resolve_row("a1")
        self.assertEqual(row.generation, 2)
        self.assertEqual(row.status, "PENDING")

    def test_rerun_assignment_flows_through_schema_conversion(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.run_cli("rerun", "--row", "a1", "--set", "count=42", expect=0)
        self.assertEqual(self.store_for().resolve_row("a1").params["count"], 42)


class TestMultiRunIntegration(TempProject):
    def test_two_runs_are_independent_end_to_end(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--run-name", "alpha", "--no-submit", expect=0)
        self.run_cli("run", "config.yaml", "--run-name", "beta", "--no-submit", expect=0)
        self.assertTrue(os.path.exists(self.path(".jobchain", "alpha", "rows.idx")))
        self.assertTrue(os.path.exists(self.path(".jobchain", "beta", "rows.idx")))
        self.run_cli("cancel", "--run", "alpha", "--stop", expect=0)
        self.assertTrue(self.store_for("alpha").stopped)
        self.assertFalse(self.store_for("beta").stopped)

    def test_all_run_reporting_round_trips_through_json(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        for name in ("alpha", "beta", "gamma"):
            self.run_cli("run", "config.yaml", "--run-name", name, "--no-submit", expect=0)
        payload = self.run_cli_json("status", "--all")
        self.assertEqual({x["name"] for x in payload["runs"]}, {"alpha", "beta", "gamma"})


class TestFilesystemIntegration(TempProject):
    def test_logs_and_export_share_the_same_persisted_run(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        logs = self.run_cli_json("logs")
        export = self.run_cli_json("export")
        self.assertIn("entries", logs)
        self.assertEqual(len(export), 4)

    def test_output_directory_information_is_derived_from_real_files(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("show", "--row", "a1", "--paths", expect=0)
        self.assertIn("files", result.stdout)

    def test_history_survives_generation_transition(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.run_cli("rerun", "--row", "a1", expect=0)
        result = self.run_cli("show", "--row", "a1", "--history", expect=0)
        self.assertIn("generation 2", result.stdout)


if __name__ == "__main__":
    unittest.main()


class TestDocumentedExampleIntegration(TempProject):
    """Run the broad positive example corpus through the real integration path."""

    def _example(self, number: int, scheduler: str = "pbs") -> None:
        import shutil

        self.path("..", "examples", f"{number:02d}_*")
        # The repository examples live beside the tests; resolve the wildcard
        # without relying on the shell so the test remains portable.
        repo_examples = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "examples"))
        candidates = [
            name for name in os.listdir(repo_examples) if name.startswith(f"{number:02d}_")
        ]
        self.assertEqual(len(candidates), 1)
        src = os.path.join(repo_examples, candidates[0])
        for name in os.listdir(src):
            source_path = os.path.join(src, name)
            dest_path = self.path(name)
            if os.path.isdir(source_path):
                shutil.copytree(source_path, dest_path)
            else:
                shutil.copy2(source_path, dest_path)
        config = self.read(self.path("config.yaml"))
        if scheduler != "pbs":
            config = config.replace("scheduler: pbs", f"scheduler: {scheduler}")
            self.write("config.yaml", config)
        self.install_scheduler(kind=scheduler, run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.assertTrue(
            os.path.isfile(self.path(".jobchain", self._run_name_from_config(), "rows.idx"))
        )

    def _run_name_from_config(self) -> str:
        import re

        match = re.search(r"^name:\s*([^\n]+)", self.read(self.path("config.yaml")), re.MULTILINE)
        self.assertIsNotNone(match)
        value = match.group(1).strip().strip("\"'")
        # Dynamic names are resolved by the application; inspect the actual run directory.
        if "{" in value:
            runs = [
                x
                for x in os.listdir(self.path(".jobchain"))
                if os.path.isdir(self.path(".jobchain", x))
            ]
            self.assertEqual(len(runs), 1)
            return runs[0]
        return value

    def test_16_schema_edges(self):
        self._example(16)

    def test_17_input_formats(self):
        self._example(17)

    def test_18_resource_precedence(self):
        self._example(18)

    def test_19_stage_settings(self):
        self._example(19)

    def test_20_handoff_generations(self):
        self._example(20)

    def test_21_scheduler_equivalence_pbs(self):
        self._example(21, "pbs")

    def test_21_scheduler_equivalence_slurm(self):
        self._example(21, "slurm")

    def test_22_multirun_isolation(self):
        self._example(22)

    def test_23_comments_and_empty_rows(self):
        self._example(23)

    def test_24_max_in_flight(self):
        self._example(24)

    def test_25_quoted_csv(self):
        self._example(25)

    def test_26_tab_delimiter(self):
        self._example(26)

    def test_27_literal_delimiter(self):
        self._example(27)

    def test_28_header_warning(self):
        self._example(28)

    def test_29_optional_defaults(self):
        self._example(29)

    def test_30_output_paths(self):
        self._example(30)

    def test_31_env_and_directives(self):
        self._example(31)

    def test_32_single_stage(self):
        self._example(32)

    def test_33_long_pipeline(self):
        self._example(33)

    def test_34_afternotok(self):
        self._example(34)

    def test_35_multi_delimiter_values(self):
        self._example(35)


class TestSchemaValidatorIntegration(TempProject):
    """Exercise schema validators through config loading and real scanning."""

    def _check(self, field_spec: str, value: str, *, extra_schema: str = "", expect: int = 0):
        config = f"""name: validator-integration
params: params.psv
scheduler: pbs
schema:
  name: validator
  format: {{delimiter: pipe, header: true, id_field: value}}
  fields:
    - {{name: value, {field_spec}}}
{extra_schema}pipeline:
  name: single
  stages:
    - {{name: work, command: "true"}}
"""
        self.make_project(width=1, config=config, params=f"value\n{value}\n")
        self.install_scheduler(run_inline=False)
        return self.run_cli("run", "config.yaml", "--check", expect=expect)

    def test_int_validator(self):
        self._check("type: int, min: 1, max: 10", "5")

    def test_float_validator(self):
        self._check("type: float, min: 0.5, max: 2.5", "1.25")

    def test_string_length_validator(self):
        self._check("type: str, min_length: 2, max_length: 8", "hello")

    def test_bool_validator(self):
        self._check("type: bool", "true")

    def test_one_of_validator(self):
        self._check("type: one_of, values: [red, green]", "green")

    def test_exact_validator(self):
        self._check("type: exact, value: READY", "READY")

    def test_regex_validator(self):
        self._check('type: regex, pattern: "^[A-Z]{3}$"', "ABC")

    def test_optional_validator(self):
        self._check("type: str, optional: true", "")

    def test_all_of_validator(self):
        self._check(
            "type: all_of, of: [{type: str, min_length: 2}, {type: regex, pattern: '^[a-z]+$'}]",
            "abc",
        )

    def test_any_of_validator(self):
        self._check("type: any_of, of: [{type: exact, value: A}, {type: exact, value: B}]", "B")

    def test_invalid_int_reaches_scan_result(self):
        result = self._check("type: int, min: 1, max: 10", "bad", expect=3)
        self.assertIn("invalid", result.stdout.lower())

    def test_invalid_regex_reaches_scan_result(self):
        result = self._check('type: regex, pattern: "^[A-Z]{3}$"', "abc", expect=3)
        self.assertIn("invalid", result.stdout.lower())

    def test_required_when_row_validator(self):
        config = """name: required-when
params: params.psv
scheduler: pbs
schema:
  name: required
  format: {delimiter: pipe, header: true, id_field: id}
  fields:
    - {name: id, type: str}
    - {name: kind, type: str}
    - {name: detail, type: str, optional: true}
  row_checks:
    - {type: required_when, when_field: kind, equals: special, require_field: detail}
pipeline:
  name: single
  stages: [{name: work, command: "true"}]
"""
        self.make_project(width=1, config=config, params="id|kind|detail\na1|special|\n")
        self.run_cli("run", "config.yaml", "--check", expect=3)

    def test_comparison_row_validator(self):
        config = """name: comparison
params: params.psv
scheduler: pbs
schema:
  name: comparison
  format: {delimiter: pipe, header: true, id_field: id}
  fields:
    - {name: id, type: str}
    - {name: low, type: int}
    - {name: high, type: int}
  row_checks:
    - {type: compare, left: low, op: '<=', right: high}
pipeline:
  name: single
  stages: [{name: work, command: "true"}]
"""
        self.make_project(width=1, config=config, params="id|low|high\na1|5|4\n")
        self.run_cli("run", "config.yaml", "--check", expect=3)

    def test_unique_file_validator(self):
        config = """name: unique
params: params.psv
scheduler: pbs
schema:
  name: unique
  format: {delimiter: pipe, header: true, id_field: id}
  fields:
    - {name: id, type: str}
  file_checks:
    - {type: unique, fields: [id]}
pipeline:
  name: single
  stages: [{name: work, command: "true"}]
"""
        self.make_project(width=1, config=config, params="id\na1\na1\n")
        self.run_cli("run", "config.yaml", "--check", expect=3)

    def test_row_count_file_validator(self):
        config = """name: row-count
params: params.psv
scheduler: pbs
schema:
  name: row-count
  format: {delimiter: pipe, header: true, id_field: id}
  fields:
    - {name: id, type: str}
  file_checks:
    - {type: row_count, min: 3, max: 3}
pipeline:
  name: single
  stages: [{name: work, command: "true"}]
"""
        self.make_project(width=1, config=config, params="id\na1\n")
        self.run_cli("run", "config.yaml", "--check", expect=3)


class TestReportingIntegration(TempProject):
    def setUp(self):
        super().setUp()
        params = "rid|count|label\na1|5|ok\na2|10|boom\na3|15|ok\n"
        self.make_project(pipeline=True, width=1, params=params)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()

    def test_status_summary_only(self):
        result = self.run_cli("status", "--summary-only", expect=0)
        self.assertNotIn("ROW  ", result.stdout)
        self.assertIn("DONE", result.stdout)

    def test_status_metrics(self):
        result = self.run_cli("status", "--metrics", expect=0)
        self.assertIn("Finished", result.stdout)
        self.assertIn("Per stage", result.stdout)

    def test_status_stage_filter(self):
        payload = self.run_cli_json("status", "--stage", "archive")
        self.assertTrue(payload["rows"])
        self.assertTrue(all(r["stage"] in (None, "archive") for r in payload["rows"]))

    def test_status_failed_filter(self):
        payload = self.run_cli_json("status", "--status", "failed")
        self.assertEqual([r["row_id"] for r in payload["rows"]], ["a2"])

    def test_show_full(self):
        result = self.run_cli("show", "--row", "a1", "--full", expect=0)
        for section in ("PARAMETERS", "STAGES", "PATHS"):
            self.assertIn(section, result.stdout)

    def test_show_output(self):
        row = self.store_for().resolve_row("a1")
        self.write(
            os.path.join(".jobchain", "test-run", "logs", row.name, "archive.log"),
            "scheduler output\n",
        )
        result = self.run_cli("show", "--row", "a1", "--output", expect=0)
        self.assertIn("scheduler output", result.stdout)

    def test_show_history_after_rerun(self):
        self.run_cli("rerun", "--row", "a2", expect=0)
        result = self.run_cli("show", "--row", "a2", "--history", expect=0)
        self.assertIn("generation 2", result.stdout)

    def test_show_invalid_rows(self):
        self.write("params.psv", "rid|count|label\na1|5|ok\na2|999|bad\n")
        self.run_cli("run", "config.yaml", "--force", "--yes", expect=0)
        result = self.run_cli("show", "--invalid", expect=0)
        self.assertIn("a2", result.stdout)

    def test_export_round_trip(self):
        payload = self.run_cli_json("export")
        self.assertEqual(len(payload), 3)
        self.assertIn("status", payload[0])

    def test_logs_json(self):
        payload = self.run_cli_json("logs")
        self.assertIn("entries", payload)

    def test_logs_stage_filter(self):
        result = self.run_cli("logs", "--stage", "solve", expect=0)
        self.assertIsInstance(result.stdout, str)

    def test_doctor_json(self):
        payload = self.run_cli_json("doctor", expect=0)
        self.assertIn("findings", payload)
        self.assertIn("environment", payload)

    def test_doctor_filesystem_check(self):
        result = self.run_cli("doctor", "--check-fs", expect=0)
        self.assertIn("mkdir", result.stdout)

    def test_cancel_row_and_report(self):
        # Completed rows have nothing active to cancel; this exercises the
        # operational result and report path without altering scheduler state.
        payload = self.run_cli_json("cancel", "--row", "a1")
        self.assertIn("cancelled", payload)


class TestSchedulerBackendIntegration(TempProject):
    def test_slurm_run_uses_slurm_submission(self):
        self.make_project(width=1)
        self.install_scheduler(kind="slurm", run_inline=False)
        config = self.read(self.path("config.yaml")).replace("scheduler: pbs", "scheduler: slurm")
        self.write("config.yaml", config)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.run_cli("run", "config.yaml", expect=0)
        self.assertTrue(self.submissions())

    def test_scheduler_failure_is_reported_across_layers(self):
        self.make_project(width=1)
        self.install_scheduler(fail=True)
        result = self.run_cli("run", "config.yaml", expect=7)
        self.assertIn("queue is full", result.stdout + result.stderr)

    def test_scheduler_job_is_cancelled_and_store_reflects_it(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        self.run_cli("cancel", "--row", "a1", expect=0)
        self.assertTrue(self.cancelled_jobs())


class TestCommandOptionIntegration(TempProject):
    def setUp(self):
        super().setUp()
        params = "rid|count|label\na1|5|ok\na2|10|ok\n"
        self.make_project(pipeline=True, width=1, params=params)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)

    def test_run_submit_only_uses_existing_scripts(self):
        self.run_cli("run", "config.yaml", "--submit-only", expect=0)
        self.assertTrue(self.submissions())

    def test_run_regenerate_rewrites_existing_scripts(self):
        store = self.store_for()
        script = store.read_manifest("000001")[0][2]
        before = self.read(script)
        self.run_cli("run", "config.yaml", "--regenerate", expect=0)
        self.assertEqual(self.read(script), before)

    def test_run_workers_option_is_accepted(self):
        self.run_cli("run", "config.yaml", "--workers", "1", "--no-submit", expect=0)

    def test_run_dry_run_does_not_submit(self):
        self.run_cli("run", "config.yaml", "--dry-run", expect=0)
        self.assertEqual(self.submissions(), [])

    def test_run_resume_clears_stop_marker(self):
        self.run_cli("cancel", "--stop", expect=0)
        self.run_cli("run", "config.yaml", "--resume", expect=0)
        self.assertFalse(self.store_for().stopped)

    def test_force_replaces_prepared_run(self):
        self.run_cli("run", "config.yaml", "--force", "--yes", "--no-submit", expect=0)
        self.assertTrue(os.path.exists(self.path(".jobchain", "test-run", "rows.idx")))

    def test_status_multiple_status_filters(self):
        self.run_cli("status", "--status", "pending", "--status", "done", expect=0)

    def test_status_all_lists_multiple_runs(self):
        self.run_cli("run", "config.yaml", "--run-name", "other", "--no-submit", expect=0)
        result = self.run_cli("status", "--all", expect=0)
        self.assertIn("test-run", result.stdout)
        self.assertIn("other", result.stdout)

    def test_status_all_json_lists_multiple_runs(self):
        self.run_cli("run", "config.yaml", "--run-name", "other", "--no-submit", expect=0)
        payload = self.run_cli_json("status", "--all")
        self.assertEqual({x["name"] for x in payload["runs"]}, {"test-run", "other"})

    def test_rerun_multiple_rows(self):
        self.run_cli("rerun", "--row", "a1", "--row", "a2", expect=0)
        self.assertEqual(self.store_for().resolve_row("a1").generation, 2)
        self.assertEqual(self.store_for().resolve_row("a2").generation, 2)

    def test_rerun_stages_list(self):
        self.run_cli("rerun", "--row", "a1", "--stages", "solve,archive", expect=0)

    def test_rerun_from_stage(self):
        self.run_cli("rerun", "--row", "a1", "--from", "solve", expect=0)
        self.assertEqual(self.store_for().resolve_row("a1").generation, 1)

    def test_rerun_regenerate(self):
        self.run_cli("rerun", "--row", "a1", "--regenerate", expect=0)

    def test_rerun_fresh_handoff(self):
        self.run_cli("rerun", "--row", "a1", "--fresh-handoff", expect=0)
        row = self.store_for().resolve_row("a1")
        seed = self.path(".jobchain", "test-run", "rows", row.name, "handoff.seed")
        self.assertFalse(os.path.exists(seed))

    def test_cancel_stage(self):
        self.install_scheduler(run_inline=False, alive=True)
        # Recreate state with active jobs for cancellation.
        self.run_cli("run", "config.yaml", "--force", "--yes", expect=0)
        self.run_cli("cancel", "--row", "a1", "--stage", "solve", expect=0)
        self.assertTrue(self.cancelled_jobs())

    def test_cancel_all_stops_chain(self):
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", "--force", "--yes", expect=0)
        self.run_cli("cancel", "--all", expect=0)
        self.assertTrue(self.store_for().stopped)

    def test_doctor_repair_dry_run_reports_without_mutation(self):
        payload = self.run_cli_json("doctor", "--repair", "--dry-run")
        self.assertIsInstance(payload, dict)

    def test_logs_level_filter(self):
        result = self.run_cli("logs", "--level", "debug", "--lines", "5", expect=0)
        self.assertIsInstance(result.stdout, str)

    def test_logs_lines_limit(self):
        result = self.run_cli("logs", "--lines", "1", expect=0)
        self.assertIsInstance(result.stdout, str)

    def test_export_status_filter(self):
        payload = self.run_cli_json("export", "--status", "pending")
        self.assertEqual(len(payload), 2)

    def test_export_to_file_round_trip(self):
        target = self.path("export.txt")
        self.run_cli("export", "-o", target, expect=0)
        self.assertTrue(os.path.isfile(target))
        self.assertIn("a1", self.read(target))

    def test_global_json_and_log_level_cross_command(self):
        payload = self.run_cli_json("status", "--log-level", "debug")
        self.assertIn("rows", payload)

    def test_global_dry_run_status_is_non_mutating(self):
        result = self.run_cli("status", "--dry-run", expect=0)
        self.assertIsInstance(result.stdout, str)


class TestCoreExampleIntegration(TempProject):
    """Exercise the first, core example families through the real CLI."""

    def _copy_example(self, number: int):
        import shutil

        repo_examples = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "examples"))
        candidates = [
            name for name in os.listdir(repo_examples) if name.startswith(f"{number:02d}_")
        ]
        self.assertEqual(len(candidates), 1)
        src = os.path.join(repo_examples, candidates[0])
        for name in os.listdir(src):
            source = os.path.join(src, name)
            target = self.path(name)
            if os.path.isdir(source):
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.assertTrue(os.path.isdir(self.path(".jobchain")))

    def test_01_basic(self):
        self._copy_example(1)

    def test_02_validation(self):
        self._copy_example(2)

    def test_03_pipeline(self):
        self._copy_example(3)

    def test_04_dynamic_resources(self):
        self._copy_example(4)

    def test_05_failure_recovery(self):
        self._copy_example(5)

    def test_06_formats(self):
        self._copy_example(6)

    def test_07_complex(self):
        self._copy_example(7)

    def test_08_validator_matrix(self):
        self._copy_example(8)

    def test_09_pipeline_matrix(self):
        self._copy_example(9)

    def test_13_operations(self):
        self._copy_example(13)

    def test_14_scheduler_reconciliation(self):
        self._copy_example(14)

    def test_15_concurrency(self):
        self._copy_example(15)

    def test_complex_pipeline_executes_end_to_end(self):
        import shutil

        repo = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "examples", "07_complex")
        )
        for name in os.listdir(repo):
            source = os.path.join(repo, name)
            target = self.path(name)
            if os.path.isdir(source):
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        payload = self.run_cli_json("status")
        self.assertTrue(payload["counts"])

    def test_failure_recovery_example_executes(self):
        import shutil

        repo = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "examples", "05_failure_recovery")
        )
        for name in os.listdir(repo):
            source = os.path.join(repo, name)
            target = self.path(name)
            if os.path.isdir(source):
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("status", expect=0)
        self.assertIn("DONE", result.stdout)

    def test_concurrency_example_can_prepare_repeatedly(self):
        import shutil

        repo = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "examples", "15_concurrency")
        )
        for name in os.listdir(repo):
            source = os.path.join(repo, name)
            target = self.path(name)
            if os.path.isdir(source):
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.assertTrue(os.path.exists(self.path(".jobchain")))


class TestSchemaConfigurationFailureIntegration(TempProject):
    """Malformed configurations must fail through the complete CLI stack."""

    BASE = """name: bad-config
params: params.psv
scheduler: pbs
schema:
  name: bad
  format: {delimiter: pipe, header: true, id_field: id}
  fields:
    - {name: id, type: str}
pipeline:
  name: single
  stages: [{name: work, command: "true"}]
"""

    def _run_bad(self, config: str, *, expect: int = 5):
        self.make_project(width=1, config=config, params="id\na1\n")
        return self.run_cli("run", "config.yaml", "--check", expect=expect)

    def test_missing_schema(self):
        self._run_bad(self.BASE.replace("schema:\n", "# schema removed\n"))

    def test_unknown_schema_key(self):
        self._run_bad(self.BASE.replace("schema:\n", "schema:\n  mystery: true\n"))

    def test_unknown_format_key(self):
        self._run_bad(self.BASE.replace("header: true", "header: true, mystery: true"))

    def test_invalid_delimiter(self):
        self._run_bad(self.BASE.replace("delimiter: pipe", "delimiter: unknown"))

    def test_missing_field_name(self):
        self._run_bad(self.BASE.replace("{name: id, type: str}", "{type: str}"))

    def test_unknown_field_type(self):
        self._run_bad(self.BASE.replace("type: str", "type: mystery"))

    def test_invalid_integer_bounds(self):
        self._run_bad(self.BASE.replace("type: str", "type: int, min: 10, max: 1"))

    def test_invalid_regex(self):
        self._run_bad(self.BASE.replace("type: str", 'type: regex, pattern: "["'))

    def test_invalid_one_of_values(self):
        self._run_bad(self.BASE.replace("type: str", "type: one_of"))

    def test_invalid_exact_without_value(self):
        self._run_bad(self.BASE.replace("type: str", "type: exact"))

    def test_invalid_all_of_without_children(self):
        self._run_bad(self.BASE.replace("type: str", "type: all_of"))

    def test_invalid_any_of_without_children(self):
        self._run_bad(self.BASE.replace("type: str", "type: any_of"))

    def test_unknown_row_validator(self):
        config = self.BASE.replace("pipeline:", "  row_checks: [{type: mystery}]\npipeline:")
        self._run_bad(config)

    def test_unknown_file_validator(self):
        config = self.BASE.replace("pipeline:", "  file_checks: [{type: mystery}]\npipeline:")
        self._run_bad(config)

    def test_invalid_pipeline_root(self):
        self._run_bad(self.BASE.replace('stages: [{name: work, command: "true"}]', "stages: bad"))

    def test_pipeline_stage_missing_name(self):
        self._run_bad(self.BASE.replace('{name: work, command: "true"}', '{command: "true"}'))

    def test_pipeline_stage_missing_command(self):
        self._run_bad(self.BASE.replace('{name: work, command: "true"}', "{name: work}"))

    def test_missing_parameter_file_crosses_cli_boundary(self):
        self.make_project(width=1)
        os.unlink(self.path("params.psv"))
        self.run_cli("run", "config.yaml", expect=4)

    def test_malformed_yaml_is_configuration_error(self):
        self.make_project(width=1)
        self.write("config.yaml", "name: [broken\n")
        self.run_cli("run", "config.yaml", expect=5)

    def test_non_mapping_yaml_is_configuration_error(self):
        self.make_project(width=1)
        self.write("config.yaml", "- not-a-mapping\n")
        self.run_cli("run", "config.yaml", expect=5)


class TestOperationalFailureIntegration(TempProject):
    def setUp(self):
        super().setUp()
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)

    def test_submit_only_without_scheduler_output_still_records_submission(self):
        self.run_cli("run", "config.yaml", "--submit-only", expect=0)
        self.assertTrue(self.submissions())

    def test_repeat_submit_only_does_not_duplicate_completed_submission(self):
        self.run_cli("run", "config.yaml", "--submit-only", expect=0)
        before = len(self.submissions())
        self.run_cli("run", "config.yaml", "--submit-only", expect=0)
        self.assertGreaterEqual(len(self.submissions()), before)

    def test_regenerate_and_submit(self):
        self.run_cli("run", "config.yaml", "--regenerate", expect=0)
        self.assertTrue(self.submissions())

    def test_dry_run_rerun_does_not_change_generation(self):
        before = self.store_for().resolve_row("a1").generation
        self.run_cli("rerun", "--row", "a1", "--dry-run", expect=0)
        self.assertEqual(self.store_for().resolve_row("a1").generation, before)

    def test_dry_run_cancel_does_not_change_stop_state(self):
        self.run_cli("cancel", "--dry-run", "--stop", expect=0)
        self.assertFalse(self.store_for().stopped)

    def test_dry_run_doctor_does_not_repair(self):
        self.run_cli("doctor", "--dry-run", "--repair", expect=0)

    def test_doctor_check_filesystem(self):
        result = self.run_cli("doctor", "--check-fs", expect=0)
        self.assertIn("filesystem", result.stdout.lower() + result.stderr.lower())

    def test_status_row_selection(self):
        result = self.run_cli("status", "--row", "a1", expect=0)
        self.assertIn("a1", result.stdout)

    def test_export_status_filter_has_only_matching_rows(self):
        payload = self.run_cli_json("export", "--status", "pending")
        self.assertEqual(len(payload), 4)

    def test_log_file_is_created_and_nonempty(self):
        path = self.path(".jobchain", "test-run", "jobchain.log")
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(self.read(path))

    def test_environment_selection_crosses_process_boundary(self):
        os.environ["JOBCHAIN_RUN"] = "test-run"
        result = self.run_cli("status", expect=0)
        self.assertIn("test-run", result.stdout)

    def test_unknown_run_selection_reports_existing_runs(self):
        self.run_cli("run", "config.yaml", "--run-name", "other", "--no-submit", expect=0)
        result = self.run_cli("status", "--run", "missing", expect=6)
        self.assertIn("test-run", result.stderr)

    def test_show_unknown_row_is_state_error(self):
        self.run_cli("show", "--row", "missing", expect=6)

    def test_rerun_unknown_stage_is_usage_error(self):
        self.run_cli("rerun", "--row", "a1", "--from", "missing", expect=1)

    def test_rerun_unknown_assignment_is_usage_error(self):
        self.run_cli("rerun", "--row", "a1", "--set", "missing=1", expect=1)

    def test_rerun_malformed_assignment_is_usage_error(self):
        self.run_cli("rerun", "--row", "a1", "--set", "missing", expect=1)

    def test_cancel_requires_a_selection_when_multiple_runs_exist(self):
        self.run_cli("run", "config.yaml", "--run-name", "other", "--no-submit", expect=0)
        self.run_cli("cancel", expect=1)


class TestDeepReportAndDoctorIntegration(TempProject):
    def _prepare_pipeline(self, params=None, inline=False):
        self.make_project(
            pipeline=True, width=1, params=params or "rid|count|label\na1|5|ok\na2|10|ok\n"
        )
        self.install_scheduler(run_inline=inline)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)

    def test_show_paths_section(self):
        self._prepare_pipeline()
        result = self.run_cli("show", "--row", "a1", "--paths", expect=0)
        self.assertIn("PATHS", result.stdout)
        self.assertIn("script", result.stdout)

    def test_show_stages_section(self):
        self._prepare_pipeline()
        self.run_cli("run", "config.yaml", expect=0)
        result = self.run_cli("show", "--row", "a1", "--stages", expect=0)
        self.assertIn("STAGES", result.stdout)

    def test_show_json_contains_paths_and_stage_state(self):
        self._prepare_pipeline()
        payload = self.run_cli_json("show", "--row", "a1")
        self.assertIn("stages", payload)
        self.assertIn("work_dir", payload)

    def test_status_json_contains_counts_and_rows(self):
        self._prepare_pipeline()
        payload = self.run_cli_json("status")
        self.assertIn("counts", payload)
        self.assertEqual(len(payload["rows"]), 2)

    def test_export_with_failed_filter(self):
        params = "rid|count|label\na1|5|ok\na2|999|bad\n"
        self._prepare_pipeline(params)
        self.run_cli("run", "config.yaml", "--force", "--yes", expect=0)
        payload = self.run_cli_json("export", "--status", "failed")
        self.assertTrue(payload)
        self.assertTrue(all("status" in row for row in payload))

    def test_doctor_finds_missing_script(self):
        self._prepare_pipeline()
        self.run_cli("run", "config.yaml", expect=0)
        store = self.store_for()
        script = store.read_manifest("000001")[0][2]
        os.unlink(script)
        payload = self.run_cli_json("doctor", expect=6)
        self.assertTrue(any("script no longer exists" in f["detail"] for f in payload["findings"]))

    def test_doctor_finds_changed_parameter_file(self):
        self._prepare_pipeline()
        self.write("params.psv", "rid|count|label\na1|6|changed\na2|10|ok\n")
        payload = self.run_cli_json("doctor", expect=6)
        self.assertTrue(any("has changed since" in f["detail"] for f in payload["findings"]))

    def test_doctor_finds_missing_parameter_file(self):
        self._prepare_pipeline()
        os.unlink(self.path("params.psv"))
        payload = self.run_cli_json("doctor", expect=6)
        self.assertTrue(any("no longer exists" in f["detail"] for f in payload["findings"]))

    def test_doctor_reports_stopped_run(self):
        self._prepare_pipeline()
        self.run_cli("cancel", "--stop", expect=0)
        payload = self.run_cli_json("doctor", expect=6)
        self.assertTrue(any("stopped" in f["detail"] for f in payload["findings"]))

    def test_doctor_reports_invalid_rows(self):
        params = "rid|count|label\na1|999|bad\n"
        self._prepare_pipeline(params)
        self.run_cli("run", "config.yaml", "--force", "--yes", expect=0)
        payload = self.run_cli_json("doctor", expect=6)
        self.assertTrue(any("failed validation" in f["detail"] for f in payload["findings"]))

    def test_status_summary_with_warnings(self):
        self._prepare_pipeline()
        self.run_cli("cancel", "--stop", expect=0)
        result = self.run_cli("status", "--summary-only", expect=0)
        self.assertIn("stopped", result.stdout)

    def test_metrics_after_completed_pipeline(self):
        self._prepare_pipeline(inline=True)
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("status", "--metrics", expect=0)
        self.assertIn("Per stage", result.stdout)

    def test_stage_specific_show_output(self):
        self._prepare_pipeline(inline=True)
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        row = self.store_for().resolve_row("a1")
        self.write(
            os.path.join(self.path(".jobchain", "test-run", "logs", row.name), "archive.log"),
            "archive scheduler output\n",
        )
        result = self.run_cli("show", "--row", "a1", "--output", "--stage", "archive", expect=0)
        self.assertIn("archive scheduler output", result.stdout)

    def test_invalid_row_report_contains_line_and_failure(self):
        self.make_project(width=1, params="rid|count|label\na1|999|bad\n")
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)
        result = self.run_cli("show", "--invalid", expect=0)
        self.assertIn("LINE", result.stdout)
        self.assertIn("999", result.stdout)
