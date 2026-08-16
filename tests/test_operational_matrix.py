"""Stateful operational examples and option-combination regression tests."""

from __future__ import annotations

import os
import unittest

from tests.helpers import TempProject

BOOM_PARAMS = """\
rid|count|label
a1|5|first
a2|10|boom
a3|15|third
a4|20|fourth
"""


class TestPreparationAndResume(TempProject):
    def test_no_submit_then_run_submits_prepared_work(self):
        self.make_project(pipeline=True, width=2)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.assertFalse(self.submissions())
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses().values()), {"DONE"})

    def test_regenerate_rebuilds_a_prepared_row_without_changing_parameters(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        before = self.store_for().resolve_row("a1").params.copy()
        self.run_cli("rerun", "--row", "a1", "--regenerate", expect=0)
        row = self.store_for().resolve_row("a1")
        self.assertEqual(row.generation, 2)
        self.assertEqual(row.params, before)
        self.assertTrue(self.store_for().read_manifest("000001"))


class TestPartialRerunMatrix(TempProject):
    def setUp(self):
        super().setUp()
        self.make_project(pipeline=True, width=1, params=BOOM_PARAMS)
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()

    def test_stage_rerun_keeps_generation(self):
        row = self.store_for().resolve_row("a2")
        generation = row.generation
        before = len(self.submissions())
        self.run_cli("rerun", "--row", "a2", "--stage", "archive", expect=0)
        self.wait_for_jobs()
        self.assertEqual(self.store_for().resolve_row("a2").generation, generation)
        self.assertEqual(len(self.submissions()), before + 1)

    def test_stages_rerun_submits_each_selected_stage(self):
        row = self.store_for().resolve_row("a2")
        generation = row.generation
        before = len(self.submissions())
        self.run_cli("rerun", "--row", "a2", "--stages", "solve,archive", expect=0)
        self.wait_for_jobs()
        self.assertEqual(self.store_for().resolve_row("a2").generation, generation)
        self.assertEqual(len(self.submissions()), before + 2)

    def test_from_rerun_submits_the_suffix(self):
        row = self.store_for().resolve_row("a2")
        generation = row.generation
        before = len(self.submissions())
        self.run_cli("rerun", "--row", "a2", "--from", "solve", expect=0)
        self.wait_for_jobs()
        self.assertEqual(self.store_for().resolve_row("a2").generation, generation)
        self.assertEqual(len(self.submissions()), before + 2)

    def test_dry_run_does_not_change_generation_or_submission_count(self):
        row = self.store_for().resolve_row("a2")
        before_generation = row.generation
        before_submissions = len(self.submissions())
        self.run_cli("rerun", "--row", "a2", "--dry-run", expect=0)
        self.assertEqual(self.store_for().resolve_row("a2").generation, before_generation)
        self.assertEqual(len(self.submissions()), before_submissions)


class TestCancellationMatrix(TempProject):
    def test_cancel_one_stage_leaves_other_stage_unchanged(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        self.run_cli("cancel", "--row", "a1", "--stage", "solve", expect=0)
        row = self.store_for().resolve_row("a1")
        statuses = {stage.name: stage.status for stage in row.current.stages}
        self.assertEqual(statuses["solve"], "CANCELLED")
        self.assertNotEqual(statuses["prep"], "CANCELLED")

    def test_cancel_dry_run_changes_nothing(self):
        self.make_project(pipeline=True, width=1)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        row = self.store_for().resolve_row("a1")
        before = [stage.status for stage in row.current.stages]
        self.run_cli("cancel", "--row", "a1", "--dry-run", expect=0)
        after = [stage.status for stage in self.store_for().resolve_row("a1").current.stages]
        self.assertEqual(before, after)


class TestOperationalExample(TempProject):
    def test_documented_example_is_valid_and_executable(self):
        root = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "examples", "13_operations")
        self.write("config.yaml", self.read(os.path.join(root, "config.yaml")))
        self.write("params.psv", self.read(os.path.join(root, "params.psv")))
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses("operations").values()), {"DONE"})


if __name__ == "__main__":
    unittest.main()
