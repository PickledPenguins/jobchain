"""Tests for several runs coexisting under one .jobchain directory.

A run is identified by its name. Two runs may share a parameter file, a
schema, and a pipeline; they may not share a name, and nothing about one may
reach into another.
"""

from __future__ import annotations

import os
import unittest

from tests.helpers import TempProject, require_node_binary


def setUpModule() -> None:
    require_node_binary()


class MultiRunCase(TempProject):
    def make_runs(self, *names: str, pipeline: bool = False,
                  submit: bool = True) -> None:
        """Prepare several runs over the same parameter file."""
        self.make_project(pipeline=pipeline, width=1)
        self.install_scheduler(run_inline=submit)
        for name in names:
            arguments = ["run", "config.yaml", "--run-name", name]
            if not submit:
                arguments.append("--no-submit")
            self.run_cli(*arguments, expect=0)


class TestIsolation(MultiRunCase):
    def test_each_run_gets_its_own_directory(self):
        self.make_runs("alpha", "beta", submit=False)
        self.assertTrue(os.path.isdir(self.path(".jobchain", "alpha")))
        self.assertTrue(os.path.isdir(self.path(".jobchain", "beta")))

    def test_each_run_has_its_own_log(self):
        self.make_runs("alpha", "beta", submit=False)
        for name in ("alpha", "beta"):
            self.assertTrue(os.path.isfile(
                self.path(".jobchain", name, "jobchain.log")))

    def test_claiming_never_crosses_runs(self):
        # Each run claims from its own index, so progress in one leaves the
        # other untouched.
        self.make_runs("alpha", "beta", submit=False)
        alpha = self.store_for("alpha")
        beta = self.store_for("beta")
        for _ in range(4):
            alpha.claim()
        self.assertIsNone(alpha.claim())
        self.assertIsNotNone(beta.claim())

    def test_reusing_the_name_of_a_prepared_run_submits_it(self):
        # run is state-aware: a run prepared but never submitted is not a
        # collision, it is work still to do.
        self.make_runs("alpha", submit=False)
        self.run_cli("run", "config.yaml", "--run-name", "alpha", expect=0)
        self.assertTrue(self.submissions())

    def test_reusing_the_name_of_a_started_run_is_refused(self):
        self.make_project(width=1)
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", "--run-name", "alpha", expect=0)
        result = self.run_cli("run", "config.yaml", "--run-name", "alpha",
                              expect=9)
        self.assertIn("already been started", result.stderr + result.stdout)

    def test_runs_may_share_a_parameter_file(self):
        self.make_runs("alpha", "beta", submit=True)
        self.wait_for_jobs()
        self.assertEqual(set(self.statuses("alpha").values()), {"DONE"})
        self.assertEqual(set(self.statuses("beta").values()), {"DONE"})

    def test_job_names_carry_the_run_name(self):
        self.make_runs("alpha", pipeline=True, submit=False)
        store = self.store_for("alpha")
        script = store.read_manifest("000001")[0][2]
        self.assertIn("#PBS -N alpha-prep-000001", self.read(script))

    def test_a_stopped_run_does_not_stop_another(self):
        self.make_runs("alpha", "beta", submit=False)
        self.run_cli("cancel", "--run", "alpha", "--stop", expect=0)
        self.assertTrue(self.store_for("alpha").stopped)
        self.assertFalse(self.store_for("beta").stopped)


class TestSelection(MultiRunCase):
    def test_one_run_is_used_automatically(self):
        self.make_runs("only", submit=False)
        result = self.run_cli("status", expect=0)
        self.assertIn("only", result.stdout)

    def test_several_runs_without_a_selection_lists_them_and_stops(self):
        # Guessing which run was meant would be worse than asking.
        self.make_runs("alpha", "beta", submit=False)
        result = self.run_cli("status", expect=1)
        self.assertIn("alpha", result.stdout)
        self.assertIn("beta", result.stdout)
        self.assertIn("NAME", result.stdout)

    def test_the_run_option_selects_one(self):
        self.make_runs("alpha", "beta", submit=False)
        result = self.run_cli("status", "--run", "beta", expect=0)
        self.assertIn("beta", result.stdout)

    def test_the_environment_variable_selects_one(self):
        self.make_runs("alpha", "beta", submit=False)
        os.environ["JOBCHAIN_RUN"] = "alpha"
        result = self.run_cli("status", expect=0)
        self.assertIn("alpha", result.stdout)

    def test_an_unknown_run_names_the_ones_that_exist(self):
        self.make_runs("alpha", "beta", submit=False)
        result = self.run_cli("status", "--run", "gamma", expect=6)
        self.assertIn("alpha", result.stderr)

    def test_commands_that_act_require_a_selection(self):
        # Acting on the wrong run is worse than being asked which one.
        self.make_runs("alpha", "beta", submit=False)
        for arguments in (("show", "--row", "a1"), ("rerun", "--row", "a1"),
                          ("cancel", "--row", "a1"), ("export",),
                          ("logs",), ("doctor",)):
            with self.subTest(command=arguments[0]):
                self.run_cli(*arguments, expect=1)


class TestMonitoring(MultiRunCase):
    def test_all_lists_every_run(self):
        self.make_runs("alpha", "beta", submit=True)
        self.wait_for_jobs()
        result = self.run_cli("status", "--all", expect=0)
        self.assertIn("alpha", result.stdout)
        self.assertIn("beta", result.stdout)
        self.assertIn("DONE", result.stdout)

    def test_all_is_machine_readable(self):
        self.make_runs("alpha", "beta", submit=False)
        payload = self.run_cli_json("status", "--all")
        self.assertEqual({entry["name"] for entry in payload["runs"]},
                         {"alpha", "beta"})

    def test_doctor_can_check_every_run(self):
        self.make_runs("alpha", "beta", submit=False)
        result = self.run_cli("doctor", "--all", expect=0)
        self.assertIn("alpha", result.stdout)
        self.assertIn("beta", result.stdout)

    def test_doctor_across_runs_reports_each_separately(self):
        self.make_runs("alpha", "beta", submit=False)
        payload = self.run_cli_json("doctor", "--all")
        self.assertEqual(len(payload), 2)
        self.assertEqual({entry["run"] for entry in payload}, {"alpha", "beta"})


class TestRunNameTemplates(TempProject):
    def test_a_dated_run_name_expands(self):
        import time
        self.make_project()
        self.install_scheduler(run_inline=False)
        config = self.read(self.path("config.yaml"))
        self.write("config.yaml", config.replace("name: test-run",
                                                 'name: "sweep-{date}"'))
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        expected = time.strftime("sweep-%Y-%m-%d")
        self.assertTrue(os.path.isdir(self.path(".jobchain", expected)))

    def test_a_dated_name_avoids_collisions_between_days(self):
        self.make_project()
        self.install_scheduler(run_inline=False)
        config = self.read(self.path("config.yaml"))
        self.write("config.yaml", config.replace("name: test-run",
                                                 'name: "sweep-{user}"'))
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        runs = os.listdir(self.path(".jobchain"))
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].startswith("sweep-"))


if __name__ == "__main__":
    unittest.main()
