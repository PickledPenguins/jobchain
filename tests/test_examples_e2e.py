"""End-to-end tests that run the actual example projects under
examples/moderate/ through the real CLI and a stub scheduler.

These formalize verification that was previously done by hand while
building each example. Running the example's own files (rather than a
duplicated inline copy) means an edit to an example is exercised by its
test automatically, and a change to jobchain that breaks an example is
caught here rather than only by someone re-running the example by hand.
"""

from __future__ import annotations

import os
import shutil
import unittest

from tests.helpers import NODE_BINARY, TempProject, require_node_binary

EXAMPLES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "moderate")


class ExampleProjectCase(TempProject):
    """Base class that copies one examples/moderate/<name> project into
    the test's own temp directory, so it runs in isolation and never
    touches the checked-in example files.
    """

    EXAMPLE_NAME: str = ""

    def setUp(self) -> None:
        super().setUp()
        require_node_binary()
        source = os.path.join(EXAMPLES_ROOT, self.EXAMPLE_NAME)
        if not os.path.isdir(source):
            self.skipTest(f"example directory not found: {source}")
        for entry in os.listdir(source):
            src = os.path.join(source, entry)
            dst = self.path(entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)


class TestThreeStageHandoffExample(ExampleProjectCase):
    EXAMPLE_NAME = "three-stage-handoff"

    def test_both_rows_complete_with_correct_checksums(self):
        import subprocess
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses("three-stage-handoff").values()),
                          {"DONE"})
        for row_id, data_file in (("sweep-a", "data/small.dat"),
                                   ("sweep-b", "data/large.dat")):
            row = self.store_for("three-stage-handoff").resolve_row(row_id)
            expected = subprocess.run(
                ["sh", "-c", f"cksum < {self.path(data_file)} | cut -d' ' -f1"],
                capture_output=True, text=True, check=True).stdout.strip()
            result_path = os.path.join(row.work_dir, "result-double.txt")
            content = self.read(result_path)
            self.assertIn(f"checksum={expected}", content)
            self.assertIn("precision=double", content)
            archived = os.path.join(row.work_dir, "archive",
                                     "result-double.txt")
            self.assertTrue(os.path.isfile(archived))


class TestDependsVariationsExample(ExampleProjectCase):
    EXAMPLE_NAME = "depends-variations"

    def test_afterok_afternotok_afterany_all_behave_correctly(self):
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        good = self.store_for("depends-variations").resolve_row("good")
        bad = self.store_for("depends-variations").resolve_row("bad")
        # good: solve succeeded, so diagnose (afternotok) never runs, and
        # archive (afterany) always does.
        self.assertTrue(os.path.isfile(
            os.path.join(good.work_dir, "solved.txt")))
        self.assertFalse(os.path.isfile(
            os.path.join(good.work_dir, "diagnosis.txt")))
        self.assertTrue(os.path.isfile(
            os.path.join(good.work_dir, "archived.txt")))
        # bad: solve failed, so diagnose (afternotok) runs, and archive
        # (afterany) still runs regardless.
        self.assertFalse(os.path.isfile(
            os.path.join(bad.work_dir, "solved.txt")))
        self.assertTrue(os.path.isfile(
            os.path.join(bad.work_dir, "diagnosis.txt")))
        self.assertTrue(os.path.isfile(
            os.path.join(bad.work_dir, "archived.txt")))

    def test_doctor_repair_reconciles_the_scheduler_cancelled_stage(self):
        # good's diagnose stage is never dispatched by the scheduler at
        # all (its afternotok dependency was never satisfied), so nothing
        # ever marks it terminal; doctor --repair is what resolves this,
        # exactly as the README's dependency table describes.
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        good_before = self.store_for("depends-variations").resolve_row("good")
        self.assertFalse(good_before.is_terminal)
        self.run_cli("doctor", "--repair", expect=0)
        good_after = self.store_for("depends-variations").resolve_row("good")
        self.assertTrue(good_after.is_terminal)
        # doctor --repair must not have disturbed a row that was already
        # correctly terminal (bad's solve stage fails on purpose, so its
        # overall status is a failure, not DONE, but it must already have
        # been terminal before doctor ran, and stay exactly as it was).
        bad = self.store_for("depends-variations").resolve_row("bad")
        self.assertTrue(bad.is_terminal)
        self.assertEqual(bad.status, "failed.solve.error")


class TestRowFileChecksExample(ExampleProjectCase):
    EXAMPLE_NAME = "row-file-checks"

    def test_check_reports_exactly_the_expected_violations(self):
        result = self.run_cli("run", "config.yaml", "--check", expect=3)
        output = result.stdout + result.stderr
        self.assertIn("ngpus must be set when mode is gpu", output)
        self.assertIn("must be less than or equal to", output)
        self.assertIn("duplicate value", output)
        self.assertIn("2 valid, 3 invalid", output)

    def test_valid_rows_still_run_despite_invalid_siblings(self):
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        statuses = self.statuses("row-file-checks")
        self.assertEqual(statuses.get("gpu-ok"), "DONE")
        self.assertEqual(statuses.get("cpu-ok"), "DONE")
        self.assertTrue(statuses.get("gpu-missing", "").startswith("failed"))
        self.assertTrue(statuses.get("bad-compare", "").startswith("failed"))
        self.assertTrue(statuses.get("dup-output", "").startswith("failed"))


class TestSlurmSchedulerExample(ExampleProjectCase):
    EXAMPLE_NAME = "slurm-scheduler"

    def test_both_rows_complete_via_sbatch(self):
        self.install_scheduler(kind="slurm")
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses("slurm-scheduler").values()),
                          {"DONE"})
        for row_id in ("slurm-one", "slurm-two"):
            row = self.store_for("slurm-scheduler").resolve_row(row_id)
            self.assertTrue(os.path.isfile(
                os.path.join(row.work_dir, "copied.txt")))
            self.assertTrue(os.path.isfile(
                os.path.join(row.work_dir, "size.txt")))


class TestShellHelperNodeExample(ExampleProjectCase):
    EXAMPLE_NAME = "shell-helper-node"

    def test_every_row_keeps_its_own_handoff_value(self):
        # This example's width (1) is deliberately below its row count
        # (3), so most of the run is submitted by the shell helper's own
        # self-chaining rather than Python's initial submission -- the
        # code path the defect in BUGFIXES.md item 4 lived in.
        shell_helper = os.path.join(os.path.dirname(NODE_BINARY),
                                     "jobchain-node.sh")
        if not os.path.isfile(shell_helper):
            self.skipTest("the shell helper is not present")
        self.install_scheduler()
        os.environ["JOBCHAIN_NODE"] = shell_helper
        try:
            self.run_cli("run", "config.yaml", expect=0)
            self.wait_for_jobs()
        finally:
            os.environ["JOBCHAIN_NODE"] = NODE_BINARY
        self.assertEqual(set(self.statuses("shell-helper-node").values()),
                          {"DONE"})
        for row_id in ("task-a", "task-b", "task-c"):
            row = self.store_for("shell-helper-node").resolve_row(row_id)
            self.assertEqual(row.current.handoff["built_file"],
                              os.path.join(row.work_dir, "built.txt"))


class TestExamplePipelineReference(ExampleProjectCase):
    """The README's own canonical example, examples/pipeline. Its config
    file is solver.yaml rather than config.yaml, so it does not fit the
    config.yaml-only helper methods and drives the CLI directly instead.
    """

    EXAMPLE_NAME = os.path.join("..", "pipeline")

    def test_mesh_handoff_carries_the_real_path_not_the_literal_variable(self):
        # Regression coverage at the example level for BUGFIXES.md item 2:
        # Prep's mesh path used to be published as the literal text
        # "$mesh" because ctx.emit() single-quoted its value unconditionally.
        self.install_scheduler()
        self.run_cli("run", "solver.yaml", expect=0)  # 2 rows are invalid
        self.wait_for_jobs()
        for row_id in ("r001", "r002", "r003", "r004"):
            row = self.store_for("solver-example").resolve_row(row_id)
            self.assertTrue(row.is_terminal)
            self.assertEqual(row.status, "DONE")
            result_path = os.path.join(row.work_dir, "result.dat")
            content = self.read(result_path)
            self.assertNotIn("$mesh", content)
            self.assertIn(os.path.join(row.work_dir, "mesh.dat"), content)


if __name__ == "__main__":
    unittest.main()
