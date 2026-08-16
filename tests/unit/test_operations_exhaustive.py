"""Exhaustive coverage of remaining operations state/repair branches."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jobchain.core import DataError, ConflictError
from jobchain.operations import (
    PreparedRun, RunResult, RerunPlan, DoctorResult, execute_rerun,
    _prepare_fresh, _submit_chains, _submit_row, _record_submissions,
    _identifier_for, cancel, doctor,
)
from jobchain.scheduler import ALIVE, FINISHED
from jobchain.store import CANCELLED, DONE, FAILED, PENDING, QUEUED, RUNNING, RunState, StageState, RowState


def make_row(name="r", stages=None, valid=True, runs_marker=False):
    if runs_marker:
        runs=[]
    else:
        stages=stages if stages is not None else [StageState(name="s",status=DONE,jobid="1",resources={},timeline=[])]
        runs=[RunState(generation=1,stages=stages)]
    return RowState(name=name,row_id=name,line_num=2,index=0,params={"a":1},generation=1,runs=runs,valid=valid,invalid_reasons=[],failure_id="",work_dir="")


class TestOperationsRemaining(unittest.TestCase):
    def test_prepare_fresh_strict_validation_raises(self):
        config=SimpleNamespace(name="r",params_path="p",strict=True,effective_workers=1,scheduler="pbs",width=1)
        schema=SimpleNamespace(name="s",field_names=[],id_field=None)
        pipeline=SimpleNamespace(name="p",specs=[],chaining_stage=None)
        store=MagicMock(name="store",home="/h",stopped=False)
        prepared=SimpleNamespace(config=config,schema=schema,pipeline=pipeline,store=store)
        report=SimpleNamespace(ok=False,invalid_rows=[SimpleNamespace(line_num=2,reasons=lambda:["bad"])],rows=[1],valid_rows=[])
        with patch("jobchain.operations.normalize_file",return_value=SimpleNamespace(rows=[],changed_count=0,skipped_blank=0,skipped_comment=0)),patch("jobchain.operations.scan",return_value=report),patch("jobchain.operations._digest",return_value="d"),patch("jobchain.operations.log_startup_summary"):
            with self.assertRaises(DataError): _prepare_fresh(prepared)
        store.create.assert_not_called()

    def test_identifier_raw_fallbacks(self):
        schema=SimpleNamespace(id_field=None,field_names=[])
        result=SimpleNamespace(raw_fields=[])
        self.assertEqual(_identifier_for(schema,result,{},"fallback"),"fallback")
        schema.id_field="rid"; schema.field_names=["rid"]
        self.assertEqual(_identifier_for(schema,result,{},"fallback"),"fallback")
        result.raw_fields=["   "]
        self.assertEqual(_identifier_for(schema,result,{},"fallback"),"fallback")
        result.raw_fields=[" raw "]
        self.assertEqual(_identifier_for(schema,result,{},"fallback"),"raw")

    def test_submit_row_without_manifest(self):
        store=MagicMock(); store.read_manifest.return_value=[]
        prepared=SimpleNamespace(store=store,scheduler=MagicMock(),pipeline=MagicMock())
        self.assertEqual(_submit_row(prepared,"r","run"),([],"no manifest: the row has no generated scripts"))

    def test_record_submissions_success_and_middle_failure(self):
        store=MagicMock(); scheduler=MagicMock()
        good=SimpleNamespace(success=True,job_id="1")
        bad=SimpleNamespace(success=False,error="queue full",output="queue full")
        jobs,reason=_record_submissions(store,"run",[("a",good),("b",bad)],scheduler)
        self.assertEqual(jobs,[("a","1")]); self.assertEqual(reason,"queue full"); scheduler.cancel.assert_called_once_with("1")

    def test_submit_chains_ceiling_reduces_width_and_exhausts(self):
        store=MagicMock(); store.stopped=False; store.load_rows.return_value=[SimpleNamespace(current=1,is_terminal=False)]
        store.claim.return_value=None
        config=SimpleNamespace(max_in_flight=1)
        prepared=SimpleNamespace(store=store,config=config)
        result=RunResult(store=store)
        with patch("jobchain.operations._submit_row") as submit:
            _submit_chains(prepared,2,result)
        self.assertFalse(result.exhausted); submit.assert_not_called()

    def test_submit_chains_failure_is_recorded(self):
        store=MagicMock(); store.stopped=False; store.load_rows.return_value=[]; store.claim.return_value=("r","run")
        prepared=SimpleNamespace(store=store,config=SimpleNamespace(max_in_flight=0))
        result=RunResult(store=store)
        with patch("jobchain.operations._submit_row",return_value=([],"bad")):
            _submit_chains(prepared,1,result)
        self.assertEqual(result.failures,[ ("r","bad") ])

    def test_execute_rerun_assignments_regenerate_and_submit_failure(self):
        row=make_row()
        store=MagicMock(); store.load_row.return_value=row; store.bump_generation.return_value=2
        prepared=SimpleNamespace(store=store,config=SimpleNamespace(on_complete=None),schema=MagicMock(),pipeline=MagicMock())
        plan=RerunPlan(rows=[row],new_generation=True,stages=["s"])
        result=SimpleNamespace(rows=[],regenerated=0,submitted=[],failures=[],skipped=[])
        with patch("jobchain.operations._apply_changes"),patch("jobchain.operations._regenerate_row",return_value=1),patch("jobchain.operations._submit_selected",return_value=([],"bad")):
            out=execute_rerun(prepared,plan,assignments={"a":"2"},regenerate=True,chain=True)
        self.assertEqual(out.regenerated,1); self.assertEqual(out.failures,[ ("r","bad") ])

    def test_execute_rerun_dry_run_does_not_change_or_submit(self):
        row=make_row(); store=MagicMock(); prepared=SimpleNamespace(store=store)
        plan=RerunPlan(rows=[row],new_generation=True)
        with patch("jobchain.operations._apply_changes") as change,patch("jobchain.operations._submit_selected") as submit:
            out=execute_rerun(prepared,plan,dry_run=True)
        self.assertEqual(out.rows,["r"]); change.assert_not_called(); submit.assert_not_called()

    def test_cancel_skips_unclaimed_and_dry_runs_active_jobs(self):
        unclaimed=make_row("u",runs_marker=True)
        active=make_row("a",stages=[StageState(name="s",status=QUEUED,jobid="9",resources={},timeline=[])])
        prepared=SimpleNamespace(store=MagicMock(),scheduler=MagicMock())
        result=cancel(prepared,[unclaimed,active],stage=None,stop=False,dry_run=True)
        self.assertEqual(result.skipped,[ ("u","never claimed") ]); self.assertEqual(result.cancelled,[ ("a",["9"])])
        prepared.scheduler.cancel.assert_not_called()

    def test_doctor_alive_and_finished_and_chain_shortfall(self):
        active=make_row("a",stages=[StageState(name="s",status=RUNNING,jobid="1",resources={},timeline=[])])
        pending=make_row("p",runs_marker=True)
        store=MagicMock(name="store"); store.name="r"; store.stopped=False; store.load_config.return_value={"width":2}; store.load_rows.return_value=[active,pending]; store.run_dir.return_value="/run"
        scheduler=MagicMock(); scheduler.job_state.return_value=ALIVE
        prepared=SimpleNamespace(store=store,scheduler=scheduler,config=SimpleNamespace(params_path="p"))
        with patch("jobchain.operations._check_params_digest"),patch("jobchain.operations._submit_chains") as submit:
            result=doctor(prepared,repair=False,dry_run=False)
        self.assertEqual(result.live_chains,1); self.assertTrue(any("short" in f.detail for f in result.findings)); submit.assert_not_called()

    def test_doctor_finished_job_and_repair(self):
        active=make_row("a",stages=[StageState(name="s",status=RUNNING,jobid="1",resources={},timeline=[])])
        store=MagicMock(name="store"); store.name="r"; store.stopped=False; store.load_config.return_value={"width":1}; store.load_rows.return_value=[active]; store.run_dir.return_value="/run"
        scheduler=MagicMock(); scheduler.job_state.return_value=FINISHED
        prepared=SimpleNamespace(store=store,scheduler=scheduler,config=SimpleNamespace(params_path="p"))
        with patch("jobchain.operations._check_params_digest"):
            result=doctor(prepared,repair=True,dry_run=False)
        self.assertTrue(result.findings[0].repaired); store.mark.assert_called()

    def test_doctor_invalid_and_stopped_findings(self):
        invalid=make_row("bad",valid=False)
        store=MagicMock(name="store"); store.name="r"; store.stopped=False; store.load_config.return_value={"width":1}; store.load_rows.return_value=[invalid]; store.stopped=True
        prepared=SimpleNamespace(store=store,scheduler=MagicMock(),config=SimpleNamespace(params_path="p"))
        with patch("jobchain.operations._check_params_digest"):
            result=doctor(prepared)
        text="\n".join(f.detail for f in result.findings); self.assertIn("failed validation",text); self.assertIn("stopped",text)

    def test_doctor_shortfall_relaunches_when_repair_allowed(self):
        active=make_row("a",stages=[StageState(name="s",status=RUNNING,jobid="1",resources={},timeline=[])])
        pending=make_row("p",runs_marker=True)
        store=MagicMock(name="store"); store.name="r"; store.stopped=False; store.load_config.return_value={"width":2}; store.load_rows.return_value=[active,pending]
        scheduler=MagicMock(); scheduler.job_state.return_value=ALIVE
        prepared=SimpleNamespace(store=store,scheduler=scheduler,config=SimpleNamespace(params_path="p"))
        launched=RunResult(store=store); launched.submitted=[("p",[("s","2")])]
        with patch("jobchain.operations._check_params_digest"),patch("jobchain.operations._submit_chains",side_effect=lambda p,w,r:setattr(r,"submitted",launched.submitted)):
            result=doctor(prepared,repair=True,dry_run=False)
        self.assertEqual(result.relaunched,launched.submitted)

    def test_run_hook_nonzero_and_exception_are_swallowed(self):
        from jobchain.operations import _run_hook
        store=SimpleNamespace(name="r",home="/h")
        payload={"rows":{"done":1,"failed":2},"completion":"c"}
        bad=SimpleNamespace(returncode=1,stderr="bad")
        with patch("jobchain.operations.subprocess.run",return_value=bad),patch("jobchain.operations.get_logger") as logger:
            _run_hook(store,"echo {run.name}",payload)
        logger.return_value.warning.assert_called_once()
        with patch("jobchain.operations.subprocess.run",side_effect=RuntimeError("x")),patch("jobchain.operations.get_logger") as logger:
            _run_hook(store,"echo",payload)
        self.assertEqual(logger.return_value.warning.call_count,1)

    def test_write_json_file_without_directory(self):
        from jobchain.operations import _write_json_file
        with tempfile.TemporaryDirectory() as d:
            old=os.getcwd(); os.chdir(d)
            try:
                _write_json_file("x.json",{"a":1})
                with open("x.json") as h: self.assertIn('"a": 1',h.read())
            finally: os.chdir(old)

class TestOperationsFinalBranches(unittest.TestCase):
    def test_cancel_stage_filter_skips_other_stages(self):
        row=make_row("r",stages=[StageState(name="a",status=QUEUED,jobid="1",resources={},timeline=[]),StageState(name="b",status=QUEUED,jobid="2",resources={},timeline=[])])
        prepared=SimpleNamespace(store=MagicMock(stopped=False),scheduler=MagicMock())
        result=cancel(prepared,[row],stage="b",stop=False,dry_run=True)
        self.assertEqual(result.cancelled,[("r",["2"])])

    def test_doctor_terminal_chaining_stage_is_not_reported_as_ended_chain(self):
        run=RunState(generation=1,stages=[StageState(name="chain",status=DONE,jobid="1",resources={},timeline=[]),StageState(name="other",status=PENDING,jobid=None,resources={},timeline=[])])
        r=make_row("r",runs_marker=True); r.runs=[run]
        store=MagicMock(); store.name="r"; store.stopped=False; store.load_config.return_value={"width":1,"chaining_stage":"chain"}; store.load_rows.return_value=[r]
        prepared=SimpleNamespace(store=store,scheduler=MagicMock(),config=SimpleNamespace(params_path="p"))
        with patch("jobchain.operations._check_params_digest"):
            result=doctor(prepared)
        self.assertFalse(any("chain ended here" in f.detail for f in result.findings))
