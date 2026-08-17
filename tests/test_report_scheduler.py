"""Tests for the reporting views and the scheduler interface.

These cover the derived measures and the submission surface directly, without
going through a whole run, so their edge cases can be exercised cheaply.
"""

from __future__ import annotations

import os
import unittest

from jobchain.core import SchedulerError
from jobchain.report import (
    _format_duration,
    _format_size,
    _parse_timestamp,
    _stage_elapsed,
    _stage_host,
    build_views,
    compute_metrics,
    export_rows,
    filter_views,
    render_invalid,
    render_metrics,
    render_run_list,
    render_show,
    render_summary,
    render_table,
    render_warnings,
    summarize,
    views_to_dicts,
)
from jobchain.scheduler import (
    ALIVE,
    FINISHED,
    UNKNOWN,
    NullScheduler,
    Scheduler,
    build_directives,
    verify_script,
    write_script,
)
from jobchain.store import (
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    QUEUED,
    RUNNING,
    RowState,
    RunState,
    StageState,
    render_env,
)
from tests.helpers import TempProject


def make_stage(name: str = "solve", status: str = DONE, jobid: str = "1.head",
               start: str = "2026-01-01 10:00:00",
               end: str = "2026-01-01 10:05:00",
               host: str = "node07", **resources) -> StageState:
    """Build a stage with a plausible timeline."""
    timeline = [
        f"{start} host={host} pid=1 stage={name} status=RUNNING jobid={jobid}",
        f"{end} host={host} pid=1 stage={name} status={status} jobid={jobid}",
    ]
    return StageState(name=name, status=status, jobid=jobid, depends="afterok",
                      resources=resources or {"walltime": "01:00:00", "ncpus": 4},
                      timeline=timeline)


def make_row(name: str = "000001", status: str = DONE, generation: int = 1,
             stages=None, valid: bool = True, runs=None) -> RowState:
    """Build a row state without touching the filesystem."""
    if runs is None:
        runs = [RunState(generation=generation,
                         stages=stages if stages is not None
                         else [make_stage(status=status)])]
    return RowState(name=name, row_id=name, line_num=2, index=0,
                    params={"rid": name, "count": 5}, generation=generation,
                    runs=runs, valid=valid,
                    invalid_reasons=[] if valid else ["count: too large"],
                    failure_id="" if valid else "1",
                    work_dir="/scratch/work")


class TestStatusDerivation(unittest.TestCase):
    def test_a_row_is_done_only_when_every_stage_is(self):
        row = make_row(stages=[make_stage("prep", DONE),
                               make_stage("solve", DONE)])
        self.assertEqual(row.status, DONE)

    def test_the_first_failure_names_the_status(self):
        row = make_row(stages=[make_stage("prep", DONE),
                               make_stage("solve", FAILED),
                               make_stage("archive", DONE)])
        self.assertTrue(row.status.startswith("failed.solve"))
        self.assertEqual(row.stage_reached, "solve")

    def test_a_cancelled_stage_is_reported_as_such(self):
        row = make_row(stages=[make_stage("prep", DONE),
                               make_stage("solve", CANCELLED)])
        self.assertEqual(row.status, "cancelled.solve")

    def test_running_beats_queued(self):
        row = make_row(stages=[make_stage("prep", RUNNING),
                               make_stage("solve", QUEUED)])
        self.assertEqual(row.status, RUNNING)

    def test_an_unclaimed_row_is_pending(self):
        row = make_row(runs=[])
        self.assertEqual(row.status, PENDING)
        self.assertIsNone(row.jobid)

    def test_an_invalid_row_reports_its_failure_id(self):
        row = make_row(valid=False)
        self.assertEqual(row.status, "failed.validation.1")

    def test_status_comes_from_the_current_generation_only(self):
        # An old failed attempt must not make a succeeded row look failed.
        row = make_row(generation=2, runs=[
            RunState(generation=1, stages=[make_stage("solve", FAILED)]),
            RunState(generation=2, stages=[make_stage("solve", DONE)]),
        ])
        self.assertEqual(row.status, DONE)
        self.assertEqual(row.attempts, 2)


class TestMetrics(unittest.TestCase):
    def test_counts_and_completion(self):
        rows = [make_row("000001", DONE), make_row("000002", FAILED),
                make_row("000003", DONE), make_row("000004", valid=False)]
        metrics = compute_metrics(rows)
        self.assertEqual(metrics.total, 4)
        self.assertEqual(metrics.completed, 2)
        self.assertEqual(metrics.failed, 1)
        self.assertEqual(metrics.invalid, 1)

    def test_only_successful_stages_contribute_to_timing(self):
        # Including failures would describe what went wrong rather than how
        # long the work takes.
        rows = [make_row("000001", DONE), make_row("000002", FAILED)]
        metrics = compute_metrics(rows)
        self.assertEqual(len(metrics.per_stage["solve"]), 1)
        self.assertEqual(metrics.stage_failures["solve"], 1)

    def test_throughput_and_projection(self):
        rows = [make_row("000001", DONE), make_row("000002", PENDING, runs=[])]
        metrics = compute_metrics(rows)
        self.assertIsNotNone(metrics.throughput_per_hour)
        self.assertIsNotNone(metrics.eta_seconds)

    def test_a_projection_needs_evidence(self):
        metrics = compute_metrics([make_row("000001", PENDING, runs=[])])
        self.assertIsNone(metrics.throughput_per_hour)
        self.assertIsNone(metrics.eta_seconds)
        self.assertIsNone(metrics.failure_rate)

    def test_metrics_serialize(self):
        payload = compute_metrics([make_row()]).to_dict()
        self.assertIn("per_stage", payload)
        self.assertEqual(payload["completed"], 1)

    def test_an_empty_run(self):
        metrics = compute_metrics([])
        self.assertEqual(metrics.total, 0)
        self.assertEqual(render_metrics(metrics)[0], "Finished        0 of 0")


class TestRendering(unittest.TestCase):
    def rows(self):
        return [make_row("000001", DONE), make_row("000002", FAILED),
                make_row("000003", PENDING, runs=[]),
                make_row("000004", valid=False)]

    def test_summary_orders_by_lifecycle(self):
        counts = summarize(self.rows())
        self.assertEqual(next(iter(counts)), PENDING)

    def test_the_bar_marks_failures_separately(self):
        lines = render_summary(summarize(self.rows()), 4)
        self.assertIn("#", lines[0])
        self.assertIn("!", lines[0])

    def test_a_summary_of_nothing(self):
        self.assertEqual(render_summary({}, 0), ["no rows"])

    def test_filtering_matches_by_prefix(self):
        # A prefix narrows: 'failed' takes every failure, including
        # validation, and 'failed.solve' takes only that stage.
        views = build_views(self.rows())
        self.assertEqual(len(filter_views(views, ["failed"])), 2)
        self.assertEqual(len(filter_views(views, ["failed.solve"])), 1)
        self.assertEqual(len(filter_views(views, stage="solve")), 3)
        self.assertEqual(len(filter_views(views)), 4)

    def test_filtering_matches_the_summary_category(self):
        # The words shown in the counts are the words that select rows.
        views = build_views(self.rows())
        self.assertEqual(len(filter_views(views, ["invalid"])), 1)
        self.assertEqual(len(filter_views(views, ["done"])), 1)

    def test_the_table_aligns(self):
        lines = render_table(build_views(self.rows()))
        self.assertTrue(lines[0].startswith("ROW"))
        self.assertEqual(len(lines), 5)

    def test_a_table_of_nothing(self):
        self.assertEqual(render_table([]), ["no rows match"])

    def test_warnings_cover_invalid_rows_and_lost_chains(self):
        lines = render_warnings(self.rows(), live_chains=1, target_width=4,
                                stopped=True)
        joined = "\n".join(lines)
        self.assertIn("failed validation", joined)
        self.assertIn("stopped", joined)
        self.assertIn("chain(s) live", joined)

    def test_no_warnings_for_a_healthy_run(self):
        rows = [make_row("000001", DONE)]
        self.assertEqual(render_warnings(rows, 1, 1, False), [])

    def test_invalid_rows_render_as_a_table(self):
        lines = render_invalid(self.rows())
        self.assertIn("LINE", "\n".join(lines))

    def test_nothing_invalid_says_so(self):
        self.assertIn("passed", render_invalid([make_row()])[0])

    def test_the_run_list_aligns(self):
        lines = render_run_list("/root", [
            {"name": "alpha", "rows": 4, "done": 4, "failed": 0, "active": 0,
             "started": "12:00"}])
        self.assertTrue(lines[0].startswith("NAME"))

    def test_views_serialize(self):
        payload = views_to_dicts(build_views([make_row()]))
        self.assertEqual(payload[0]["status"], DONE)
        self.assertEqual(payload[0]["elapsed_s"], 300.0)


class TestShowSections(TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.store_for("demo")
        os.makedirs(self.store.rows_dir, exist_ok=True)

    def test_a_failure_leads_the_report(self):
        row = make_row(stages=[make_stage("prep", DONE),
                               make_stage("solve", FAILED)])
        row.runs[0].stages[1].error = "exit status 2"
        text = "\n".join(render_show(row, self.store))
        self.assertLess(text.index("FAILURE"), text.index("PARAMETERS"))

    def test_a_healthy_row_is_short(self):
        text = "\n".join(render_show(make_row(), self.store))
        self.assertNotIn("FAILURE", text)
        self.assertIn("PARAMETERS", text)

    def test_an_invalid_row_reports_its_reasons(self):
        text = "\n".join(render_show(make_row(valid=False), self.store))
        self.assertIn("VALIDATION", text)
        self.assertIn("never submitted", text)

    def test_handoff_appears_when_values_exist(self):
        row = make_row()
        row.runs[0].handoff = {"mesh": "/data/m.h5"}
        self.assertIn("HANDOFF", "\n".join(render_show(row, self.store)))

    def test_sections_can_be_selected(self):
        text = "\n".join(render_show(make_row(), self.store, ["paths"]))
        self.assertIn("PATHS", text)
        self.assertNotIn("STAGES", text)

    def test_the_stage_table_shows_requested_resources(self):
        text = "\n".join(render_show(make_row(), self.store))
        self.assertIn("walltime", text)
        self.assertIn("01:00:00", text)


class TestFormatting(unittest.TestCase):
    def test_durations_choose_units(self):
        self.assertEqual(_format_duration(30), "30s")
        self.assertEqual(_format_duration(90), "1.5m")
        self.assertEqual(_format_duration(5400), "1.5h")
        self.assertEqual(_format_duration(172800), "2.0d")
        self.assertEqual(_format_duration(None), "-")

    def test_sizes_choose_units(self):
        self.assertEqual(_format_size(512), "512 B")
        self.assertIn("KB", _format_size(2048))
        self.assertIn("GB", _format_size(3 * 1024 ** 3))

    def test_unparseable_timestamps_are_ignored(self):
        self.assertIsNone(_parse_timestamp("not a timestamp"))
        self.assertIsNone(_stage_elapsed(["garbage"]))

    def test_elapsed_needs_both_ends(self):
        self.assertIsNone(_stage_elapsed(["2026-01-01 10:00:00 status=RUNNING"]))

    def test_the_host_is_recovered(self):
        self.assertEqual(_stage_host(make_stage(host="node12").timeline),
                         "node12")
        self.assertEqual(_stage_host([]), "")


class TestEnvRendering(unittest.TestCase):
    def test_values_are_quoted_for_the_shell(self):
        rendered = render_env({"path": "/a/my file.dat", "note": "it's fine"})
        self.assertIn("JC_path='/a/my file.dat'", rendered)
        self.assertIn("""JC_note='it'\\''s fine'""", rendered)

    def test_none_and_booleans_are_predictable(self):
        rendered = render_env({"a": None, "b": True, "c": False})
        self.assertIn("JC_a=''", rendered)
        self.assertIn("JC_b='1'", rendered)
        self.assertIn("JC_c='0'", rendered)


class TestScheduler(TempProject):
    def test_only_known_schedulers_are_accepted(self):
        with self.assertRaises(SchedulerError):
            Scheduler("torque")

    def test_directive_prefixes(self):
        self.assertEqual(Scheduler("pbs").directive_prefix, "#PBS")
        self.assertEqual(Scheduler("slurm").directive_prefix, "#SBATCH")

    def test_pbs_directives(self):
        lines = build_directives(
            {"walltime": "16:00:00", "ncpus": 32, "mem": "32gb", "ngpus": 2,
             "queue": "normal", "account": "p1"},
            Scheduler("pbs"), "run1", "solve", "000123", "/logs")
        joined = "\n".join(lines)
        self.assertIn("-l select=1:ncpus=32:mem=32gb:ngpus=2", joined)
        self.assertIn("-l walltime=16:00:00", joined)
        self.assertIn("-N run1-solve-000123", joined)

    def test_slurm_directives(self):
        lines = build_directives(
            {"walltime": "16:00:00", "ncpus": 32, "queue": "normal"},
            Scheduler("slurm"), "run1", "solve", "000123", "/logs")
        joined = "\n".join(lines)
        self.assertIn("--cpus-per-task=32", joined)
        self.assertIn("--time=16:00:00", joined)
        self.assertIn("--partition=normal", joined)

    def test_unset_resources_emit_no_directive(self):
        lines = build_directives({}, Scheduler("slurm"), "r", "s", "1", "/logs")
        self.assertFalse(any("--time" in line for line in lines))

    def test_extra_directives_pass_through(self):
        lines = build_directives(
            {"extra_directives": ["-l place=scatter", "#PBS -W depend=x"]},
            Scheduler("pbs"), "r", "s", "1", "/logs")
        self.assertIn("#PBS -l place=scatter", lines)
        self.assertIn("#PBS -W depend=x", lines)

    def test_stage_environment_is_exported(self):
        lines = build_directives({"env": {"OMP_NUM_THREADS": "8"}},
                                 Scheduler("pbs"), "r", "s", "1", "/logs")
        self.assertIn("export OMP_NUM_THREADS='8'", lines)

    def test_job_id_parsing(self):
        self.assertEqual(Scheduler("pbs").parse_job_id("banner\n123.head\n"),
                         "123.head")
        self.assertEqual(Scheduler("slurm").parse_job_id("Submitted batch job 45"),
                         "45")
        self.assertIsNone(Scheduler("pbs").parse_job_id("  "))

    def test_a_missing_client_is_reported(self):
        os.environ["PATH"] = self.bin_dir
        with self.assertRaises(SchedulerError):
            Scheduler("pbs").require_available()

    def test_pbs_reports_a_forgotten_job_as_finished(self):
        self.install_scheduler("pbs", run_inline=False)
        self.assertEqual(Scheduler("pbs").job_state("1.stub"), FINISHED)

    def test_pbs_reports_a_running_job_as_alive(self):
        self.install_scheduler("pbs", run_inline=False, alive=True)
        self.assertEqual(Scheduler("pbs").job_state("1.stub"), ALIVE)

    def test_slurm_falls_back_to_accounting(self):
        self.install_scheduler("slurm", run_inline=False)
        self.assertEqual(Scheduler("slurm").job_state("1"), FINISHED)

    def test_an_unavailable_client_yields_unknown(self):
        os.environ["PATH"] = self.bin_dir
        self.assertEqual(Scheduler("pbs").job_state("1.stub"), UNKNOWN)

    def test_an_empty_job_id_yields_unknown(self):
        self.assertEqual(Scheduler("pbs").job_state(""), UNKNOWN)

    def test_cancelling_without_a_client_reports_it(self):
        os.environ["PATH"] = self.bin_dir
        ok, message = Scheduler("pbs").cancel("1.stub")
        self.assertFalse(ok)
        self.assertIn("not available", message)

    def test_the_null_scheduler_submits_nothing(self):
        scheduler = NullScheduler("slurm")
        submission = scheduler.submit("/nonexistent.sh", {})
        self.assertTrue(submission.success)
        self.assertEqual(scheduler.job_state("1"), UNKNOWN)
        self.assertEqual(scheduler.cancel("1"), (True, ""))


class TestScriptVerification(TempProject):
    def test_a_valid_script_passes(self):
        path = write_script(self.path("ok.sh"), "#!/bin/sh\ntrue\n")
        self.assertIsNone(verify_script(path))
        self.assertTrue(os.access(path, os.X_OK))

    def test_an_empty_script_is_rejected(self):
        path = write_script(self.path("empty.sh"), "")
        self.assertIn("empty", verify_script(path))

    def test_a_script_that_is_not_shell_is_rejected(self):
        path = write_script(self.path("bad.sh"), "#!/bin/sh\nif then fi(\n")
        self.assertIn("not valid shell", verify_script(path))

    def test_a_missing_script_is_reported(self):
        self.assertIn("not written", verify_script(self.path("absent.sh")))


class TestExport(TempProject):
    def test_export_preserves_columns_and_appends_state(self):
        from jobchain.schema import Field, Int, Schema, Str
        schema = Schema(name="s", delimiter="|",
                        fields=[Field("rid", [Str()]), Field("count", [Int()])])
        lines = export_rows(schema, [make_row()])
        self.assertTrue(lines[0].startswith("rid|count|status"))
        self.assertIn("DONE", lines[1])

    def test_an_invalid_row_carries_its_reason(self):
        from jobchain.schema import Field, Int, Schema, Str
        schema = Schema(name="s", delimiter="|",
                        fields=[Field("rid", [Str()]), Field("count", [Int()])])
        lines = export_rows(schema, [make_row(valid=False)])
        self.assertIn("too large", lines[1])


if __name__ == "__main__":
    unittest.main()
