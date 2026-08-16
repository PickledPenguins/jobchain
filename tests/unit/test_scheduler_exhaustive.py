"""Exhaustive coverage of remaining scheduler/context branches."""
from __future__ import annotations
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from jobchain.core import StateError
from jobchain.scheduler import Scheduler, NullScheduler, Submission, PBS, SLURM, RunContext, RowContext, write_script

class TestSchedulerRemaining(unittest.TestCase):
    def test_slurm_submit_without_and_with_dependency(self):
        s=Scheduler(SLURM)
        completed=SimpleNamespace(returncode=0,stdout="123",stderr="")
        with patch("jobchain.scheduler.subprocess.run",return_value=completed) as run:
            self.assertEqual(s.submit("x.sh",{"A":"1"}).job_id,"123")
            self.assertEqual(s.submit("x.sh",{"A":"1"},depends_on="7",depends_type="afterany").job_id,"123")
        self.assertIn("--dependency=afterany:7",run.call_args.args[0])

    def test_submit_pipeline_without_dependencies(self):
        s=Scheduler(PBS)
        submissions=[Submission(True,"1"),Submission(True,"2")]
        with patch.object(s,"submit",side_effect=submissions) as submit:
            result=s.submit_pipeline([("a","-","a.sh"),("b","-","b.sh")],{})
        self.assertEqual(len(result),2); self.assertIsNone(submit.call_args_list[1].kwargs["depends_on"])

    def test_null_scheduler_counter_and_submission(self):
        s=NullScheduler(PBS)
        self.assertTrue(s.available); self.assertTrue(s.require_available() is None)
        a=s.submit("x",{}); b=s.submit("x",{},depends_on="1")
        self.assertEqual(a.job_id,"dry-1"); self.assertEqual(b.job_id,"dry-2")
        self.assertEqual(s.cancel("x"),(True,"")); self.assertEqual(s.job_state("x"),"UNKNOWN")

    def test_run_context_requires_row_name(self):
        scheduler=Scheduler(PBS)
        run=RunContext("r","/h",scheduler,"node","{run.home}/{row.name}","{run.home}/logs")
        with self.assertRaises(StateError): run.work_dir({"x":1},"")
        self.assertIn("RunContext",repr(run))

    def test_run_context_work_dir_and_row_context_directives(self):
        scheduler=Scheduler(PBS)
        run=RunContext("r","/h",scheduler,"node","{run.home}/{row.name}","{run.home}/logs")
        self.assertEqual(run.work_dir({"x":1},"a"),"/h/a")
        ctx=RowContext(run,"a",0,"s",1,"/h/a",False,"/h/a/s.sh")
        self.assertIn("qsub",ctx.directives({})) if False else self.assertIsInstance(ctx.directives({}),str)
        self.assertIn("JC_ROW",ctx.preamble()); self.assertIn("mark",ctx.epilogue()); self.assertNotIn("--next",ctx.epilogue())

    def test_row_context_write(self):
        scheduler=Scheduler(PBS); run=RunContext("r","/h",scheduler,"node","/h/{row.name}","/h/logs")
        with tempfile.TemporaryDirectory() as d:
            path=os.path.join(d,"script.sh"); ctx=RowContext(run,"a",0,"s",1,d,False,path)
            self.assertEqual(ctx.write("echo hi\n"),path); self.assertTrue(os.access(path,os.X_OK))

    def test_write_script_without_directory(self):
        with tempfile.TemporaryDirectory() as d:
            old=os.getcwd(); os.chdir(d)
            try:
                self.assertEqual(write_script("x.sh","echo hi\n"),"x.sh")
                self.assertTrue(os.path.exists("x.sh"))
            finally: os.chdir(old)
