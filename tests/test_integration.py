"""Integration tests: whole runs, chaining, correction, and reconciliation.

These drive the front end against a stub scheduler that honours dependencies
and executes submitted scripts, so a pipeline and the chain that follows it
genuinely run.
"""

from __future__ import annotations

import json
import os
import time
import unittest

from tests.helpers import (NODE_BINARY, PIPELINE_CONFIG, STAGES_MODULE,
                           TempProject, require_node_binary)

BOOM_PARAMS = """\
rid|count|label
a1|5|first
a2|10|boom
a3|15|third
a4|20|fourth
"""

INVALID_PARAMS = """\
rid|count|label
a1|5|first
a2|999|toobig
a3|15|third
"""


def setUpModule() -> None:
    require_node_binary()


class TestSingleJobRuns(TempProject):
    """A configuration with no pipeline: one job per row."""

    def test_a_run_completes_every_row(self):
        self.make_project(width=2)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses().values()), {"DONE"})

    def test_each_row_runs_exactly_once(self):
        self.make_project(width=2)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        scripts = [line.split()[-1] for line in self.submissions()]
        self.assertEqual(len(scripts), len(set(scripts)))
        self.assertEqual(len(scripts), 4)

    def test_the_parameter_file_is_never_modified(self):
        self.make_project(width=2)
        before = self.read(self.path("params.psv"))
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertEqual(self.read(self.path("params.psv")), before)


class TestPipelineRuns(TempProject):
    def test_every_stage_of_every_row_runs(self):
        self.make_project(pipeline=True, width=2)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses().values()), {"DONE"})
        self.assertEqual(len(self.submissions()), 12)  # 4 rows x 3 stages

    def test_slurm_chaining_submits_with_sbatch_not_qsub(self):
        # Regression coverage for a defect where a self-chained submission
        # (the resubmission a job issues for the next row as it exits, via
        # the compute-node helper rather than the Python front end) always
        # used qsub, even when the run was configured for Slurm, because
        # neither node helper learned which scheduler the run used: the C
        # helper chose qsub vs sbatch at compile time (and no build path
        # ever produced a Slurm binary), and the shell helper checked an
        # environment variable nothing ever set. Both are fixed by
        # RowContext.preamble exporting JC_SCHEDULER into every generated
        # script. This test only proves the failure mode is gone if it
        # actually drives more than one chained submission: width=2 against
        # 4 rows forces a resubmission once the first two chains finish.
        config = PIPELINE_CONFIG.format(name="test-run", width=2) \
            .replace("scheduler: pbs", "scheduler: slurm")
        self.write("stages.py", STAGES_MODULE)
        self.make_project(pipeline=True, width=2, config=config)
        self.install_scheduler(kind="slurm")
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses().values()), {"DONE"})
        submissions = self.submissions()
        # 4 rows x 3 stages: the first submission of each row's first chain
        # comes from the Python front end; every later stage and every
        # later row's first stage is a self-chained submission from the
        # node helper. All of them must have used sbatch.
        self.assertEqual(len(submissions), 12)
        for line in submissions:
            self.assertNotIn("-W depend=", line)
            self.assertNotIn(" -v ", line)
        # At least the dependent stages must show Slurm's dependency flag;
        # a run that silently fell back to qsub's -W depend= would already
        # have failed the assertions above, but this confirms the fix
        # positively rather than only by absence.
        self.assertTrue(any("--dependency=" in line for line in submissions))

    def test_shell_helper_chaining_does_not_corrupt_the_environment(self):
        # Regression coverage for a defect in bin/jobchain-node.sh: its
        # self-chained submission built "-v " + environment as a single
        # concatenated string, then passed that whole string as one quoted
        # shell argument to qsub. A real qsub (and every scheduler stub,
        # including this test's) receives that as ONE argv word, not the
        # two words ("-v", "KEY=VALUE,...") it actually parses -v against,
        # so the entire environment was silently dropped. The submitted
        # job then inherited whatever JC_HOME/JC_ROW/JC_RUN its parent
        # process happened to have exported already -- the previous row's
        # values -- rather than its own, so every row past the first
        # chain claimed by Python wrote its handoff into the *previous*
        # row's handoff file. Only exercised once row count exceeds width,
        # since Python's own initial submission (a real subprocess argv
        # list, not a concatenated string) was never affected.
        shell_helper = os.path.join(os.path.dirname(NODE_BINARY),
                                     "jobchain-node.sh")
        if not os.path.isfile(shell_helper):
            self.skipTest("the shell helper is not present")
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        os.environ["JOBCHAIN_NODE"] = shell_helper
        try:
            self.run_cli("run", "config.yaml", expect=0)
            self.wait_for_jobs()
        finally:
            os.environ["JOBCHAIN_NODE"] = NODE_BINARY
        self.assertEqual(set(self.statuses().values()), {"DONE"})
        # Each row's own handoff file must hold only its own value: a
        # corrupted run leaves an earlier row's handoff overwritten by (or
        # missing in favor of) a later row's, or a later row's handoff
        # missing its own entry entirely because it wrote into the wrong
        # row's file.
        for row_id in ("a1", "a2", "a3", "a4"):
            row = self.store_for().resolve_row(row_id)
            self.assertEqual(row.current.handoff["mesh"],
                              os.path.join(row.work_dir, "mesh.txt"))

    def test_dependencies_are_passed_at_submission(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)
        submissions = self.submissions()
        self.assertNotIn("depend", submissions[0])
        self.assertIn("depend=afterok", submissions[1])
        self.assertIn("depend=afterany", submissions[2])

    def test_scripts_do_not_reference_other_jobs(self):
        # A stage script must stay a standalone artifact, resubmittable by
        # hand months later.
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        store = self.store_for()
        for _, _, script in store.read_manifest("000001"):
            text = self.read(script)
            self.assertNotIn("depend", text)
            self.assertNotIn("BASH_SOURCE", text)

    def test_handoff_reaches_the_next_stage(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        row = self.store_for().resolve_row("a1")
        self.assertIn("mesh", row.current.handoff)
        result = os.path.join(row.work_dir, "result.txt")
        # The handoff must carry the actual published value through to the
        # next stage, not just be present as a key: a regression here (see
        # RowContext.emit) can leave the key set to a bogus value while
        # still satisfying a substring-only assertion.
        self.assertEqual(row.current.handoff["mesh"],
                          os.path.join(row.work_dir, "mesh.txt"))
        self.assertEqual(
            self.read(result).strip(),
            f"solved from {os.path.join(row.work_dir, 'mesh.txt')}")

    def test_emit_shell_expr_carries_a_run_time_value_to_the_next_stage(self):
        # Unlike the fixture's Prep (which emits a path already known in
        # Python), this stage only learns the path after `mktemp` runs, so
        # it must use emit_shell_expr for the shell to expand it. Regression
        # coverage for RowContext.emit_shell_expr end to end, through a real
        # generated script and a real handoff file, not just as a string
        # comparison of the generated line.
        stages = '''\\
from jobchain import JobStage


class Prep(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
generated="{ctx.work_dir}/generated-$$.txt"
echo "made at runtime" > "$generated"
rc=$?
{ctx.emit_shell_expr('generated_file', '$generated')}
{ctx.epilogue()}
exit $rc
""")


class Solve(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
cat "$JC_OUT_generated_file" > "{ctx.work_dir}/copied.txt"
rc=$?
{ctx.epilogue()}
exit $rc
""")


class Archive(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
true
rc=$?
{ctx.epilogue()}
exit $rc
""")
'''
        self.make_project(pipeline=True, width=1)
        self.write("stages.py", stages)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        row = self.store_for().resolve_row("a1")
        # The handoff value is a real, generation-time-unknowable path (it
        # embeds the Prep job's PID via $$), so its mere presence already
        # shows the shell expanded it rather than publishing "$generated"
        # literally; comparing the copied file's content confirms the
        # value that reached Solve was usable, not just non-empty.
        self.assertNotEqual(row.current.handoff["generated_file"], "$generated")
        copied = os.path.join(row.work_dir, "copied.txt")
        self.assertEqual(self.read(copied).strip(), "made at runtime")

    def test_a_failing_stage_does_not_stop_the_chain(self):
        self.make_project(pipeline=True, width=1, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        statuses = self.statuses()
        self.assertTrue(statuses["a2"].startswith("failed.solve"))
        self.assertEqual(statuses["a1"], "DONE")
        self.assertEqual(statuses["a4"], "DONE")

    def test_afterok_cancels_and_afterany_still_runs(self):
        self.make_project(pipeline=True, width=1, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        row = self.store_for().resolve_row("a2")
        stages = {s.name: s.status for s in row.current.stages}
        self.assertEqual(stages["prep"], "DONE")
        self.assertEqual(stages["solve"], "FAILED")
        self.assertEqual(stages["archive"], "DONE")

    def test_resources_are_recorded_as_requested(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)
        row = self.store_for().resolve_row("a1")
        solve = row.current.stage("solve")
        self.assertEqual(solve.resources["ncpus"], 5)     # from the row
        self.assertEqual(solve.resources["queue"], "normal")  # from defaults


class TestValidation(TempProject):
    def test_invalid_rows_are_skipped_but_recorded(self):
        self.make_project(width=1, params=INVALID_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        statuses = self.statuses()
        self.assertTrue(statuses["a2"].startswith("failed.validation"))
        self.assertEqual(statuses["a1"], "DONE")

    def test_an_invalid_row_has_state_but_no_scripts(self):
        # State lets it be corrected into a running run; the absent manifest
        # is what keeps it unclaimable meanwhile.
        self.make_project(width=1, params=INVALID_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        store = self.store_for()
        row = store.resolve_row("a2")
        self.assertFalse(row.valid)
        self.assertEqual(store.read_manifest(row.name), [])

    def test_strict_mode_refuses_to_create_anything(self):
        self.make_project(width=1, params=INVALID_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", "--strict", expect=3)
        self.assertFalse(os.path.exists(self.path(".jobchain", "test-run",
                                                  "rows.idx")))

    def test_check_writes_nothing(self):
        self.make_project(width=1)
        self.run_cli("run", "config.yaml", "--check", expect=0)
        self.assertFalse(os.path.exists(self.path(".jobchain")))

    def test_check_reports_failures_with_exit_three(self):
        self.make_project(width=1, params=INVALID_PARAMS)
        result = self.run_cli("run", "config.yaml", "--check", expect=3)
        self.assertIn("999 is greater than maximum", result.stdout)


class TestCorrection(TempProject):
    def prepare_failure(self):
        self.make_project(pipeline=True, width=1, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()

    def test_a_failed_row_reruns_at_a_new_generation(self):
        self.prepare_failure()
        self.run_cli("rerun", "--row", "a2", expect=0)
        row = self.store_for().resolve_row("a2")
        self.assertEqual(row.generation, 2)
        self.assertEqual(row.status, "PENDING")
        self.assertEqual(row.attempts, 1)   # the failed attempt is still there

    def test_set_corrects_values_and_regenerates(self):
        self.prepare_failure()
        self.run_cli("rerun", "--row", "a2", "--set", "label=fixed", expect=0)
        row = self.store_for().resolve_row("a2")
        self.assertEqual(row.params["label"], "fixed")
        self.assertIn("JC_label='fixed'",
                      self.read(os.path.join(self.store_for().row_dir(row.name),
                                             "env")))

    def test_a_correction_that_does_not_validate_changes_nothing(self):
        self.prepare_failure()
        before = self.store_for().resolve_row("a2").params["count"]
        self.run_cli("rerun", "--row", "a2", "--set", "count=999", expect=3)
        self.assertEqual(self.store_for().resolve_row("a2").params["count"],
                         before)

    def test_a_corrected_row_is_picked_up_by_a_chain(self):
        self.prepare_failure()
        self.run_cli("rerun", "--row", "a2", "--set", "label=fixed",
                     "--chain", expect=0)
        self.wait_for_jobs()
        self.assertEqual(self.statuses()["a2"], "DONE")

    def test_an_invalid_row_can_be_corrected_into_the_run(self):
        self.make_project(width=1, params=INVALID_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("rerun", "--row", "a2", "--set", "count=20", "--chain",
                     expect=0)
        self.wait_for_jobs()
        self.assertEqual(self.statuses()["a2"], "DONE")

    def test_a_row_can_be_named_by_a_unique_column(self):
        self.prepare_failure()
        self.run_cli("rerun", "--row", "rid=a2", expect=0)
        self.assertEqual(self.store_for().resolve_row("a2").generation, 2)

    def test_naming_a_row_by_a_non_unique_column_is_refused(self):
        self.prepare_failure()
        result = self.run_cli("show", "--row", "label=boom", expect=6)
        self.assertIn("not unique", result.stderr)

    def test_rerunning_a_running_row_is_refused(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        result = self.run_cli("rerun", "--row", "a1", expect=0)
        self.assertIn("skipped", result.stdout)

    def test_a_partial_rerun_keeps_the_generation(self):
        self.prepare_failure()
        self.run_cli("rerun", "--row", "a2", "--from", "solve", expect=0)
        self.assertEqual(self.store_for().resolve_row("a2").generation, 1)

    def test_a_partial_rerun_submits_only_the_selected_stages(self):
        self.prepare_failure()
        before = len(self.submissions())
        self.run_cli("rerun", "--row", "a2", "--stage", "archive", expect=0)
        self.assertEqual(len(self.submissions()) - before, 1)

    def test_handoff_is_carried_into_a_new_generation(self):
        # The seed lives beside the row, not inside the next generation's
        # directory: creating that directory is how a row is claimed.
        self.prepare_failure()
        self.run_cli("rerun", "--row", "a2", expect=0)
        store = self.store_for()
        row = store.resolve_row("a2")
        seed = os.path.join(store.row_dir(row.name), "handoff.seed")
        self.assertTrue(os.path.isfile(seed))
        self.assertIn("mesh", self.read(seed))

    def test_fresh_handoff_drops_carried_values(self):
        self.prepare_failure()
        self.run_cli("rerun", "--row", "a2", "--fresh-handoff", expect=0)
        store = self.store_for()
        seed = os.path.join(store.row_dir(store.resolve_row("a2").name),
                            "handoff.seed")
        self.assertFalse(os.path.isfile(seed))

    def test_a_requeued_row_becomes_claimable_again(self):
        self.prepare_failure()
        self.run_cli("rerun", "--row", "a2", expect=0)
        store = self.store_for()
        claimed = store.claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], store.resolve_row("a2").name)


class TestGuards(TempProject):
    def test_starting_an_already_running_run_is_refused(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        result = self.run_cli("run", "config.yaml", expect=9)
        self.assertIn("already been started", result.stderr)

    def test_a_changed_parameter_file_blocks_further_submission(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.write("params.psv", "rid|count|label\nz9|1|changed\n")
        result = self.run_cli("run", "config.yaml", expect=9)
        self.assertIn("has changed", result.stderr)

    def test_force_discards_an_existing_run(self):
        self.make_project(width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("run", "config.yaml", "--force", "--yes", expect=0)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses().values()), {"DONE"})

    def test_rerunning_a_completed_row_with_output_needs_force(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("rerun", "--row", "a1", expect=9)
        self.assertIn("still have output", result.stderr)

    def test_a_completed_row_without_output_needs_only_force(self):
        # Removing the output is the signal that cleanup already happened,
        # so nothing can be destroyed.
        import shutil
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        row = self.store_for().resolve_row("a1")
        shutil.rmtree(row.work_dir, ignore_errors=True)
        self.run_cli("rerun", "--row", "a1", "--force", expect=0)
        self.assertEqual(self.store_for().resolve_row("a1").generation, 2)

    def test_a_completed_row_with_output_is_confirmed_by_typing(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("rerun", "--row", "a1", "--force", stdin="a1\n", expect=0)
        self.assertEqual(self.store_for().resolve_row("a1").generation, 2)

    def test_declining_the_confirmation_changes_nothing(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("rerun", "--row", "a1", "--force", stdin="no\n", expect=1)
        self.assertEqual(self.store_for().resolve_row("a1").generation, 1)


class TestStopping(TempProject):
    def test_stop_prevents_further_claiming(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)
        self.run_cli("cancel", "--stop", expect=0)
        self.assertTrue(self.store_for().stopped)
        result = self.run_cli("run", "config.yaml", "--submit-only", expect=9)
        self.assertIn("stopped", result.stderr)

    def test_resume_clears_the_stop_marker(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)
        self.run_cli("cancel", "--stop", expect=0)
        self.run_cli("run", "config.yaml", "--resume", expect=0)
        self.assertFalse(self.store_for().stopped)

    def test_cancel_all_stops_the_chain_as_well(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        self.run_cli("cancel", "--all", expect=0)
        self.assertTrue(self.store_for().stopped)
        self.assertTrue(self.cancelled_jobs())

    def test_cancel_marks_rows_rerunnable(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        self.run_cli("cancel", "--row", "a1", expect=0)
        row = self.store_for().resolve_row("a1")
        self.assertTrue(row.status.startswith("cancelled."))
        self.run_cli("rerun", "--row", "a1", expect=0)
        self.assertEqual(self.store_for().resolve_row("a1").generation, 2)


class TestReloadWithExternalModules(TempProject):
    """A run using an inline `validator_class:` or `stage_module:` must
    remain fully usable after the first `run`: every other command reloads
    the run from config.final.yaml, which lives in a different directory
    than the original configuration. Regression coverage for a defect
    where an inline schema's `validator_class` was captured as a relative
    path and so could not be found on reload, while `pipeline.stage_module`
    (already absolutized) worked correctly -- an inconsistency that made
    the defect easy to miss.
    """

    SCHEMA_VALIDATOR = '''\
from jobchain import Field, Int, SchemaBase


class V(SchemaBase):
    fields = [Field("rid", []), Field("count", [Int(min=1, max=100)])]
'''

    CONFIG = """\
name: reload-test
params: params.psv
scheduler: pbs

schema:
  name: s
  format: {delimiter: pipe, header: true, id_field: rid}
  validator_class: validators.py

pipeline:
  stage_module: stages.py
  stages:
    - {name: only}
"""

    def _make(self) -> None:
        self.write("params.psv", "rid|count\na1|5\n")
        self.write("validators.py", self.SCHEMA_VALIDATOR)
        self.write("stages.py", "from jobchain import JobStage\n"
                    "class Only(JobStage):\n"
                    "    def write_script(self, row, ctx):\n"
                    "        return ctx.write(f'#!/bin/sh\\n"
                    "{ctx.directives(self.effective_resources(row))}\\n"
                    "{ctx.preamble()}\\ntrue\\nrc=$?\\n{ctx.epilogue()}\\n"
                    "exit $rc\\n')\n")
        self.write("config.yaml", self.CONFIG)

    def test_validator_class_is_absolute_in_the_capture(self):
        self._make()
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        final = self.read(self.store_for("reload-test").home + "/config.final.yaml")
        self.assertIn(self.path("validators.py"), final)
        self.assertNotIn("validator_class: validators.py", final)

    def test_status_succeeds_after_the_first_run(self):
        self._make()
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("status", expect=0)
        self.assertNotIn("module not found", result.stdout)
        self.assertNotIn("module not found", result.stderr)

    def test_show_succeeds_after_the_first_run(self):
        self._make()
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("show", "--row", "a1", expect=0)
        self.assertIn("a1", result.stdout)

    def test_doctor_succeeds_after_the_first_run(self):
        self._make()
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("doctor", expect=0)
        self.assertIn("no problems found", result.stdout)


class TestDoctor(TempProject):
    def test_a_healthy_run_reports_nothing(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        result = self.run_cli("doctor", expect=0)
        self.assertIn("no problems found", result.stdout)

    def test_a_vanished_job_is_detected(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)   # qstat reports finished
        self.run_cli("run", "config.yaml", expect=0)
        result = self.run_cli("doctor", expect=6)
        self.assertIn("no longer known", result.stdout)

    def test_repair_marks_vanished_stages_failed(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)
        self.run_cli("doctor", "--repair", expect=0)
        row = self.store_for().resolve_row("a1")
        self.assertTrue(row.status.startswith("failed."))

    def test_invalid_rows_are_reported_as_a_finding(self):
        self.make_project(width=1, params=INVALID_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        result = self.run_cli("doctor", expect=6)
        self.assertIn("failed validation", result.stdout)

    def test_a_changed_parameter_file_is_reported_only(self):
        self.make_project(width=1)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        with open(self.path("params.psv"), "a", encoding="utf-8") as handle:
            handle.write("z9|1|late\n")
        result = self.run_cli("doctor", expect=6)
        self.assertIn("has changed", result.stdout)

    def test_a_stopped_run_is_reported(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", expect=0)
        self.run_cli("cancel", "--stop", expect=0)
        result = self.run_cli("doctor", expect=6)
        self.assertIn("stopped", result.stdout)


class TestCompletion(TempProject):
    def test_a_finished_run_writes_the_done_marker(self):
        self.make_project(width=2)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("status", expect=0)   # any command notices completion
        store = self.store_for()
        self.assertTrue(os.path.isfile(store.done_path))
        payload = json.loads(self.read(store.done_path))
        self.assertEqual(payload["completion"], 1)
        self.assertEqual(payload["rows"]["done"], 4)

    def test_a_rerun_removes_the_marker(self):
        self.make_project(pipeline=True, width=2)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("status", expect=0)
        store = self.store_for()
        self.assertTrue(os.path.isfile(store.done_path))
        self.run_cli("rerun", "--row", "a1", "--force", stdin="a1\n", expect=0)
        self.assertFalse(os.path.isfile(store.done_path))

    def test_the_completion_counter_rises(self):
        self.make_project(width=2)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("status", expect=0)
        self.run_cli("rerun", "--row", "a1", "--force", "--chain",
                     stdin="a1\n", expect=0)
        self.wait_for_jobs()
        self.run_cli("status", expect=0)
        payload = json.loads(self.read(self.store_for().done_path))
        self.assertEqual(payload["completion"], 2)

    def test_the_hook_runs_on_completion(self):
        marker = self.path("finished.txt")
        config = (PIPELINE_CONFIG.format(name="test-run", width=2)
                  + f'\non_complete: "echo done > {marker}"\n')
        self.write("stages.py", STAGES_MODULE)
        self.make_project(pipeline=True, width=2, config=config)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("status", expect=0)
        self.assertTrue(os.path.isfile(marker))


if __name__ == "__main__":
    unittest.main()


class TestInFlightCeiling(TempProject):
    """max_in_flight caps pipelines submitted but not finished."""

    CONFIG = """\
name: capped
params: params.psv
width: 4
max_in_flight: 2
scheduler: pbs

schema:
  format: {delimiter: pipe, header: true, id_field: rid}
  fields:
    - {name: rid,   type: str}
    - {name: count, type: int, min: 1}
    - {name: label, optional: true, type: str}

pipeline:
  stages:
    - {name: work, command: "true"}
"""

    def test_the_ceiling_limits_what_is_launched(self):
        self.make_project(config=self.CONFIG, name="capped", width=4)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        # Width is 4 but the ceiling is 2, so only two pipelines start.
        self.assertEqual(len(self.submissions()), 2)

    def test_without_a_ceiling_the_width_is_used(self):
        self.make_project(config=self.CONFIG.replace("max_in_flight: 2\n", ""),
                          name="capped", width=4)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        self.assertEqual(len(self.submissions()), 4)

    def test_the_ceiling_counts_only_unfinished_pipelines(self):
        self.make_project(config=self.CONFIG, name="capped", width=4)
        self.install_scheduler()          # runs inline, so rows finish
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        # Everything finished, so nothing counts against the ceiling.
        self.assertEqual(set(self.statuses("capped").values()), {"DONE"})


class TestPruning(TempProject):
    def test_a_recent_run_is_never_pruned(self):
        self.make_project(width=1, name="alpha")
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("status", expect=0)          # notices completion
        result = self.run_cli("status", "--all", "--prune-after", "30", expect=0)
        self.assertIn("no runs finished", result.stdout)
        self.assertTrue(os.path.isdir(self.path(".jobchain", "alpha")))

    def test_pruning_lists_before_removing(self):
        self.make_project(width=1, name="alpha")
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("status", expect=0)
        # Age the completion marker past the cutoff.
        done = self.store_for("alpha").done_path
        old = time.time() - 10 * 86400
        os.utime(done, (old, old))
        result = self.run_cli("status", "--all", "--prune-after", "5", expect=1)
        self.assertIn("alpha", result.stdout)
        self.assertIn("nothing was removed", result.stdout)
        self.assertTrue(os.path.isdir(self.path(".jobchain", "alpha")))

    def test_pruning_removes_when_confirmed(self):
        self.make_project(width=1, name="alpha")
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.run_cli("status", expect=0)
        done = self.store_for("alpha").done_path
        old = time.time() - 10 * 86400
        os.utime(done, (old, old))
        self.run_cli("status", "--all", "--prune-after", "5", "--yes", expect=0)
        self.assertFalse(os.path.isdir(self.path(".jobchain", "alpha")))

    def test_an_unfinished_run_is_never_eligible(self):
        self.make_project(width=1, name="alpha")
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        result = self.run_cli("status", "--all", "--prune-after", "0", "--yes",
                              expect=0)
        self.assertIn("no runs finished", result.stdout)
        self.assertTrue(os.path.isdir(self.path(".jobchain", "alpha")))
