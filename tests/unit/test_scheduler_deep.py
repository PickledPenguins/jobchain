import os
import subprocess
import unittest
from unittest.mock import patch

from jobchain.scheduler import (
    ALIVE, FINISHED, UNKNOWN, PBS, SLURM, NullScheduler, Scheduler,
    build_directives, describe_environment, write_script, verify_script,
)
from jobchain.core import SchedulerError, StateError


class TestSchedulerConstruction(unittest.TestCase):
    def test_default_is_pbs(self):
        s = Scheduler()
        self.assertEqual(s.kind, PBS)
        self.assertEqual(s.submit_binary, "qsub")

    def test_kind_is_case_insensitive(self):
        self.assertEqual(Scheduler("SLURM").kind, SLURM)

    def test_none_kind_uses_pbs(self):
        self.assertEqual(Scheduler(None).kind, PBS)

    def test_available_reflects_submit_binary(self):
        with patch("jobchain.scheduler.shutil.which", return_value="/bin/qsub"):
            self.assertTrue(Scheduler(PBS).available)
        with patch("jobchain.scheduler.shutil.which", return_value=None):
            self.assertFalse(Scheduler(PBS).available)

    def test_require_available_success(self):
        with patch.object(Scheduler, "available", new_callable=unittest.mock.PropertyMock, return_value=True):
            Scheduler(PBS).require_available()


class TestSubmission(unittest.TestCase):
    def _completed(self, stdout="123.server\n", stderr="", returncode=0):
        return subprocess.CompletedProcess(["qsub"], returncode, stdout, stderr)

    def test_pbs_submission_without_dependency(self):
        with patch("jobchain.scheduler.subprocess.run", return_value=self._completed()) as run:
            result = Scheduler(PBS).submit("job.sh", {"B": "2", "A": "1"})
        self.assertTrue(result.success)
        self.assertEqual(result.job_id, "123.server")
        self.assertEqual(run.call_args.args[0], ["qsub", "-v", "A=1,B=2", "job.sh"])

    def test_pbs_submission_with_dependency(self):
        with patch("jobchain.scheduler.subprocess.run", return_value=self._completed()) as run:
            Scheduler(PBS).submit("job.sh", {}, "77.server", "afterany")
        self.assertEqual(run.call_args.args[0], ["qsub", "-W", "depend=afterany:77.server", "-v", "", "job.sh"])

    def test_slurm_submission_with_dependency_and_environment(self):
        completed = subprocess.CompletedProcess(["sbatch"], 0, "Submitted batch job 88\n", "")
        with patch("jobchain.scheduler.subprocess.run", return_value=completed) as run:
            result = Scheduler(SLURM).submit("job.sh", {"Z": "9", "A": "1"}, "77", "afterok")
        self.assertEqual(result.job_id, "88")
        self.assertEqual(run.call_args.args[0], ["sbatch", "--dependency=afterok:77", "--export=ALL,A=1,Z=9", "job.sh"])

    def test_submission_failure_is_returned(self):
        completed = self._completed("", "bad request", 1)
        with patch("jobchain.scheduler.subprocess.run", return_value=completed):
            result = Scheduler(PBS).submit("job.sh", {})
        self.assertFalse(result.success)
        self.assertIsNone(result.job_id)
        self.assertIn("bad request", result.output)

    def test_submission_oserror_becomes_scheduler_error(self):
        with patch("jobchain.scheduler.subprocess.run", side_effect=OSError("no qsub")):
            with self.assertRaises(SchedulerError):
                Scheduler(PBS).submit("job.sh", {})

    def test_submission_empty_success_output_has_no_job_id(self):
        with patch("jobchain.scheduler.subprocess.run", return_value=self._completed("")):
            result = Scheduler(PBS).submit("job.sh", {})
        self.assertTrue(result.success)
        self.assertIsNone(result.job_id)

    def test_pipeline_stops_after_first_failure(self):
        scheduler = Scheduler(PBS)
        responses = [
            self._completed("1.server\n"),
            self._completed("", "rejected", 1),
            self._completed("3.server\n"),
        ]
        with patch("jobchain.scheduler.subprocess.run", side_effect=responses) as run:
            result = scheduler.submit_pipeline([
                ("a", "-", "a.sh"),
                ("b", "afterok", "b.sh"),
                ("c", "afterok", "c.sh"),
            ], {})
        self.assertEqual([name for name, _ in result], ["a", "b"])
        self.assertEqual(run.call_count, 2)


class TestStatusParsing(unittest.TestCase):
    def completed(self, stdout="", rc=0):
        return subprocess.CompletedProcess([], rc, stdout, "")

    def test_all_pbs_alive_states(self):
        for state in ["Q", "R", "H", "W", "T", "S", "B", "M"]:
            with self.subTest(state=state), patch("jobchain.scheduler._capture", return_value=self.completed(f"job_state = {state}\n")):
                self.assertEqual(Scheduler(PBS).job_state("1"), ALIVE)

    def test_unknown_pbs_state_is_finished(self):
        with patch("jobchain.scheduler._capture", return_value=self.completed("job_state = F\n")):
            self.assertEqual(Scheduler(PBS).job_state("1"), FINISHED)

    def test_pbs_success_without_state_is_finished(self):
        with patch("jobchain.scheduler._capture", return_value=self.completed("Job_Name = x\n")):
            self.assertEqual(Scheduler(PBS).job_state("1"), FINISHED)

    def test_pbs_query_error_is_finished(self):
        with patch("jobchain.scheduler._capture", return_value=self.completed(rc=1)):
            self.assertEqual(Scheduler(PBS).job_state("1"), FINISHED)

    def test_all_slurm_squeue_alive_states(self):
        for state in ["PENDING", "RUNNING", "SUSPENDED", "COMPLETING", "CONFIGURING", "RESIZING", "REQUEUED", "SIGNALING"]:
            with self.subTest(state=state), patch("jobchain.scheduler._capture", return_value=self.completed(state + "\n")):
                self.assertEqual(Scheduler(SLURM).job_state("1"), ALIVE)

    def test_slurm_squeue_finished_state(self):
        with patch("jobchain.scheduler._capture", return_value=self.completed("COMPLETED\n")):
            self.assertEqual(Scheduler(SLURM).job_state("1"), FINISHED)

    def test_slurm_squeue_failure_falls_back_to_sacct(self):
        responses = [self.completed(rc=1), self.completed("RUNNING\n")]
        with patch("jobchain.scheduler._capture", side_effect=responses):
            self.assertEqual(Scheduler(SLURM).job_state("1"), ALIVE)

    def test_slurm_sacct_empty_is_finished(self):
        responses = [self.completed(rc=1), self.completed("")]
        with patch("jobchain.scheduler._capture", side_effect=responses):
            self.assertEqual(Scheduler(SLURM).job_state("1"), FINISHED)

    def test_slurm_sacct_failure_is_finished(self):
        responses = [self.completed(rc=1), self.completed(rc=1)]
        with patch("jobchain.scheduler._capture", side_effect=responses):
            self.assertEqual(Scheduler(SLURM).job_state("1"), FINISHED)

    def test_slurm_sacct_unknown_state_is_finished(self):
        responses = [self.completed(rc=1), self.completed("CANCELLED by user\n")]
        with patch("jobchain.scheduler._capture", side_effect=responses):
            self.assertEqual(Scheduler(SLURM).job_state("1"), FINISHED)


class TestCancellation(unittest.TestCase):
    def test_pbs_cancel_success(self):
        with patch("jobchain.scheduler._capture", return_value=subprocess.CompletedProcess([], 0, "ok\n", "")) as capture:
            self.assertEqual(Scheduler(PBS).cancel("4.server"), (True, "ok"))
            self.assertEqual(capture.call_args.args[0], ["qdel", "4.server"])

    def test_slurm_cancel_failure_includes_output(self):
        with patch("jobchain.scheduler._capture", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")):
            self.assertEqual(Scheduler(SLURM).cancel("4"), (False, "permission denied"))

    def test_cancel_unavailable(self):
        with patch("jobchain.scheduler._capture", return_value=None):
            ok, msg = Scheduler(SLURM).cancel("4")
        self.assertFalse(ok)
        self.assertIn("scancel", msg)


class TestNullScheduler(unittest.TestCase):
    def test_counter_is_per_scheduler(self):
        a, b = NullScheduler(), NullScheduler()
        self.assertEqual(a.submit("a", {}).job_id, "dry-1")
        self.assertEqual(a.submit("b", {}).job_id, "dry-2")
        self.assertEqual(b.submit("c", {}).job_id, "dry-1")


class TestDirectiveMatrix(unittest.TestCase):
    def test_pbs_nodes_defaults_to_one(self):
        lines = build_directives({}, Scheduler(PBS), "r", "s", "row", "/log")
        self.assertIn("#PBS -l select=1", lines)

    def test_zero_and_empty_resources_are_omitted(self):
        resources = {"nodes": 0, "ncpus": 0, "mem": "", "ngpus": None,
                     "walltime": 0, "queue": "", "account": None}
        pbs = "\n".join(build_directives(resources, Scheduler(PBS), "r", "s", "x", "/log"))
        slurm = "\n".join(build_directives(resources, Scheduler(SLURM), "r", "s", "x", "/log"))
        self.assertNotIn("ncpus", pbs)
        self.assertNotIn("--nodes", slurm)

    def test_slurm_all_resources(self):
        lines = build_directives({"nodes": 2, "ncpus": 4, "mem": "8G", "ngpus": 1,
                                  "walltime": "1:00:00", "queue": "gpu", "account": "acct"},
                                 Scheduler(SLURM), "r", "s", "x", "/log")
        text = "\n".join(lines)
        for expected in ["--nodes=2", "--cpus-per-task=4", "--mem=8G", "--gpus-per-node=1",
                          "--time=1:00:00", "--partition=gpu", "--account=acct"]:
            self.assertIn(expected, text)

    def test_extra_directive_hash_is_not_prefixed(self):
        lines = build_directives({"extra_directives": ["#SBATCH --exclusive"]},
                                 Scheduler(SLURM), "r", "s", "x", "/log")
        self.assertIn("#SBATCH --exclusive", lines)

    def test_environment_is_sorted(self):
        lines = build_directives({"env": {"Z": "2", "A": "1"}}, Scheduler(PBS), "r", "s", "x", "/log")
        self.assertLess(lines.index("export A='1'"), lines.index("export Z='2'"))


class TestCaptureAndEnvironment(unittest.TestCase):
    def test_capture_missing_binary_returns_none(self):
        with patch("jobchain.scheduler.shutil.which", return_value=None):
            self.assertIsNone(__import__("jobchain.scheduler", fromlist=["_capture"])._capture(["missing"]))

    def test_capture_oserror_returns_none(self):
        with patch("jobchain.scheduler.shutil.which", return_value="/bin/tool"), \
             patch("jobchain.scheduler.subprocess.run", side_effect=OSError("bad")):
            from jobchain.scheduler import _capture
            self.assertIsNone(_capture(["tool"]))

    def test_capture_timeout_returns_none(self):
        with patch("jobchain.scheduler.shutil.which", return_value="/bin/tool"), \
             patch("jobchain.scheduler.subprocess.run", side_effect=subprocess.TimeoutExpired("tool", 60)):
            from jobchain.scheduler import _capture
            self.assertIsNone(_capture(["tool"]))

    def test_environment_reports_all_scheduler_clients(self):
        with patch("jobchain.scheduler.shutil.which", side_effect=lambda x: "/bin/" + x):
            facts = describe_environment()
        self.assertEqual(facts["qsub"], "/bin/qsub")
        self.assertIn("PBS_O_WORKDIR", facts)
        self.assertIn("SLURM_JOB_ID", facts)


class TestContextAndScripts(unittest.TestCase):
    def test_work_dir_requires_row_name(self):
        run = __import__("jobchain.scheduler", fromlist=["RunContext"]).RunContext(
            "r", "/home", Scheduler(PBS), "node", "{row.name}", "/log")
        with self.assertRaises(StateError):
            run.work_dir({}, "")

    def test_row_context_paths(self):
        from jobchain.scheduler import RunContext, RowContext
        run = RunContext("r", "/home", Scheduler(PBS), "node", "/work", "/log")
        row = RowContext(run, "001", 0, "stage", 3, "/work/001", True, "/tmp/x.sh")
        self.assertEqual(row.row_dir, "/home/rows/001")
        self.assertEqual(row.run_dir, "/home/rows/001/run-3")
        self.assertEqual(row.handoff, "/home/rows/001/run-3/handoff")
        self.assertEqual(row.handoff_seed, "/home/rows/001/handoff.seed")
        self.assertEqual(row.log_dir, "/log/001")

    def test_pbs_preamble_and_epilogue_contain_pbs_job_id_and_chain(self):
        from jobchain.scheduler import RunContext, RowContext
        run = RunContext("r", "/home", Scheduler(PBS), "node", "/work", "/log")
        row = RowContext(run, "001", 0, "stage", 1, "/work", True)
        self.assertIn("PBS_JOBID", row.preamble())
        self.assertIn('JC_CHAIN:-0', row.epilogue())
        self.assertIn('submit --home', row.epilogue())

    def test_slurm_preamble_uses_slurm_job_id(self):
        from jobchain.scheduler import RunContext, RowContext
        run = RunContext("r", "/home", Scheduler(SLURM), "node", "/work", "/log")
        row = RowContext(run, "001", 0, "stage", 1, "/work", False)
        self.assertIn("SLURM_JOB_ID", row.preamble())
        self.assertNotIn("submit --home", row.epilogue())

    def test_expand_uses_row_and_generation(self):
        from jobchain.scheduler import RunContext, RowContext
        run = RunContext("r", "/home", Scheduler(PBS), "node", "/work", "/log")
        row = RowContext(run, "001", 2, "stage", 4, "/work", False)
        self.assertEqual(row.expand("{row.name}-{row.index}", {"name": "n"}), "001-2")

    def test_emit_quotes_value(self):
        from jobchain.scheduler import RunContext, RowContext
        run = RunContext("r", "/home", Scheduler(PBS), "node", "/work", "/log")
        row = RowContext(run, "001", 0, "s", 1, "/work", False)
        self.assertIn("X='hello'", row.emit("X", "hello"))


if __name__ == "__main__":
    unittest.main()
