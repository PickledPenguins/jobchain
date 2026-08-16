"""Deep, mock-heavy unit coverage of jobchain/report.py.

Renamed from test_report_exhaustive.py for this project's
one-file-per-subsystem convention.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from jobchain.report import (
    Metrics, RowView, _category, _directory_summary, _failed_stage,
    _first_line, _format_duration, _format_size, _parse_timestamp,
    _render_stage_table, _render_value, _stage_elapsed, _stage_host,
    _timestamps, build_views, compute_metrics, export_rows, filter_views,
    render_invalid, render_metrics, render_run_list, render_show,
    render_summary, render_table, render_warnings, summarize, views_to_dicts,
)
from jobchain.store import CANCELLED, DONE, FAILED, PENDING, RowState, RunState, StageState


# from test_report_exhaustive.py
def stage(name="solve", status=DONE, jobid="1", resources=None, timeline=None, error=None):
    return StageState(name=name,status=status,jobid=jobid,depends="afterok",resources=resources or {},timeline=timeline or [],error=error)


# from test_report_exhaustive.py
def row(name="r", status=DONE, valid=True, run=None, runs=None, work_dir=""):
    if runs is None:
        runs=[run] if run is not None else [RunState(generation=1,stages=[stage(status=status)])]
    return RowState(name=name,row_id=name,line_num=2,index=0,params={"a":1},generation=runs[-1].generation if runs else 0,runs=runs,valid=valid,invalid_reasons=[] if valid else [],failure_id="",work_dir=work_dir)


class TestReportCategoriesAndMetrics(unittest.TestCase):
    def test_category_variants(self):
        self.assertEqual(_category("failed.validation.1"),"INVALID")
        self.assertEqual(_category("failed.solve"),"failed")
        self.assertEqual(_category("cancelled.solve"),"cancelled")
        self.assertEqual(_category("DONE"),"DONE")

    def test_matches_multiple_words_and_categories(self):
        views=[RowView("a","a",1,"failed.solve","solve",1,1,"1",None,"","",True)]
        self.assertEqual(len(filter_views(views,["done","failed"])),1)
        self.assertEqual(filter_views(views,["xyz"]),[])

    def test_metrics_invalid_cancelled_and_stage_failure(self):
        failed=stage("s",FAILED,timeline=[])
        cancelled=stage("c",CANCELLED,timeline=[])
        rows=[row("done",DONE),row("bad",valid=False),row("fail",FAILED,run=RunState(generation=1,stages=[failed])),row("cancel",CANCELLED,run=RunState(generation=1,stages=[cancelled]))]
        m=compute_metrics(rows)
        self.assertEqual(m.completed,1); self.assertEqual(m.invalid,1); self.assertEqual(m.failed,2)
        self.assertEqual(m.stage_failures["s"],1)

    def test_metrics_timestamp_min_max_updates(self):
        timeline=["2026-01-01 10:05:00 host=n", "2026-01-01 10:00:00 host=n"]
        r=row(run=RunState(generation=1,stages=[stage(timeline=timeline)]))
        m=compute_metrics([r]); self.assertEqual(m.first_event.hour,10); self.assertEqual(m.last_event.minute,5)

    def test_metrics_properties_no_finished_and_no_remaining(self):
        m=Metrics(total=1,invalid=1)
        self.assertIsNone(m.failure_rate); self.assertIsNone(m.eta_seconds)
        m=Metrics(total=0,completed=0,failed=0,first_event=None,last_event=None)
        self.assertIsNone(m.wall_elapsed); self.assertIsNone(m.throughput_per_hour)

    def test_metrics_to_dict_empty_stage_values(self):
        m=Metrics(total=0,per_stage={"s":[]},stage_failures={"s":2})
        d=m.to_dict(); self.assertIsNone(d["per_stage"]["s"]["mean_s"]); self.assertEqual(d["per_stage"]["s"]["failures"],2)


class TestStatusRendering(unittest.TestCase):
    def test_summary_total_zero_with_counts(self):
        self.assertEqual(render_summary({"DONE":0},0),["DONE 0"])

    def test_summary_partial_failed_bar(self):
        lines=render_summary({"DONE":1,"failed":1},4,width=10)
        self.assertIn("!",lines[0]); self.assertIn("2/4",lines[0])

    def test_warnings_each_independent_condition(self):
        invalid=row("i",valid=False)
        pending=row("p",PENDING,runs=[])
        self.assertEqual(len(render_warnings([invalid,pending],0,2,True)),3)
        self.assertEqual(render_warnings([pending],0,0,False),[])

    def test_table_all_placeholder_fields(self):
        v=RowView("a","id",1,"DONE","",1,1,"",None,"","",True)
        lines=render_table([v]); self.assertIn("-",lines[1]); self.assertIn("DONE",lines[1])

    def test_metrics_every_optional_section(self):
        m=Metrics(total=5,completed=2,failed=1,invalid=1,live_chains=1,target_width=2)
        m.first_event=__import__('datetime').datetime(2026,1,1,10); m.last_event=__import__('datetime').datetime(2026,1,1,11)
        m.per_stage={"s":[30.0,60.0],"empty":[]}; m.stage_failures={"s":1}
        lines=render_metrics(m); text="\n".join(lines)
        for x in ("Invalid","Failure rate","Per stage","Wall elapsed","Throughput","Projected left","Chains"):
            self.assertIn(x,text)

    def test_run_list_empty(self):
        self.assertEqual(render_run_list("/r",[]),["NAME  ROWS  DONE  FAILED  ACTIVE  STARTED"])


class TestShowRendering(unittest.TestCase):
    def test_show_invalid_failure_handoff_paths_and_history(self):
        failed=stage("solve",FAILED,jobid="9",timeline=["2026-01-01 10:00:00 host=node", "2026-01-01 10:05:00 host=node status=DONE"],error="first\nsecond")
        old=RunState(generation=1,stages=[stage("old",DONE)])
        current=RunState(generation=2,stages=[failed],handoff={"mesh":"/x"})
        r=row("a",FAILED,valid=False,runs=[old,current],work_dir="/does/not/exist"); r.generation=2; r.invalid_reasons=["bad"]
        store=SimpleNamespace(home="/h",row_dir=lambda n:"/state",read_manifest=lambda n:[])
        with patch("jobchain.report._directory_summary",return_value=(0,0)):
            text="\n".join(render_show(r,store,history=True))
        for x in ("VALIDATION","FAILURE","message","job","HANDOFF","HISTORY","generation 1","generation 2 (current)"):
            self.assertIn(x,text)

    def test_show_selective_sections(self):
        r=row(work_dir="/w"); store=SimpleNamespace(home="/h",row_dir=lambda n:"/state",read_manifest=lambda n:[("a","b","script")])
        with patch("jobchain.report._directory_summary",return_value=(2,2048)):
            text="\n".join(render_show(r,store,sections=["paths"]))
        self.assertIn("PATHS",text); self.assertNotIn("PARAMETERS",text); self.assertIn("2 files",text)

    def test_show_empty_work_dir_and_no_run(self):
        r=RowState(name="a",row_id="a",line_num=1,index=0,params={},generation=0,runs=[],valid=True,invalid_reasons=[],failure_id="",work_dir="")
        store=SimpleNamespace(home="/h",row_dir=lambda n:"/state",read_manifest=lambda n:[])
        text="\n".join(render_show(r,store,sections=["paths"]))
        self.assertIn("PATHS",text); self.assertNotIn("work",text)

    def test_show_stage_table_with_defaults(self):
        stages=[stage("a",DONE,jobid="",resources={}),stage("b",PENDING,jobid=None,resources={"mem":"1G"})]
        text="\n".join(_render_stage_table(stages))
        self.assertIn("-",text); self.assertIn("1G",text)

    def test_invalid_render_unknown_reason(self):
        r=row("a",valid=False); r.invalid_reasons=[]
        self.assertIn("unknown",render_invalid([r])[-1])


class TestExportAndTimeline(unittest.TestCase):
    def test_export_row_with_no_stage_and_invalid_reasons(self):
        schema=SimpleNamespace(field_names=["a"],delimiter="|",quoting=False)
        r=row("a",valid=False,runs=[],work_dir="/w"); r.invalid_reasons=["bad"]
        lines=export_rows(schema,[r]); self.assertIn("|bad",lines[1])

    def test_export_row_with_stage_error(self):
        st=stage("s",FAILED,timeline=["2026-01-01 10:00:00 host=n status=RUNNING","2026-01-01 10:01:00 host=n status=FAILED"],error="bad\nmore")
        schema=SimpleNamespace(field_names=["a"],delimiter="|",quoting=False)
        r=row(run=RunState(generation=1,stages=[st]),work_dir="/w")
        lines=export_rows(schema,[r]); self.assertIn("bad",lines[1]); self.assertIn("60",lines[1])

    def test_view_conversion_none_fields(self):
        v=RowView("a","id",1,"DONE","",1,1,"",None,"","",True)
        d=views_to_dicts([v])[0]; self.assertIsNone(d["stage"]); self.assertIsNone(d["jobid"]); self.assertIsNone(d["host"]); self.assertIsNone(d["error"])

    def test_parse_timestamp_invalid(self):
        self.assertIsNone(_parse_timestamp("not-a-date")); self.assertEqual(len(_timestamps(["not-a-date","2026-01-01 10:00:00 x"])),1)

    def test_stage_elapsed_missing_start_end_and_negative(self):
        self.assertIsNone(_stage_elapsed([])); self.assertIsNone(_stage_elapsed(["2026-01-01 10:00:00 status=RUNNING"]))
        self.assertEqual(_stage_elapsed(["2026-01-01 10:05:00 status=RUNNING","2026-01-01 10:00:00 status=DONE"]),0.0)

    def test_stage_host_unknown_then_valid(self):
        self.assertEqual(_stage_host(["host=unknown","x"]),"")
        self.assertEqual(_stage_host(["host=unknown","host=node01"]),"node01")

    def test_failed_stage_none_and_match(self):
        self.assertIsNone(_failed_stage(None)); self.assertIsNone(_failed_stage(SimpleNamespace(stages=[])))
        self.assertEqual(_failed_stage(SimpleNamespace(stages=[stage("s",FAILED)] )).name,"s")

    def test_directory_summary_missing_and_unreadable_file(self):
        self.assertEqual(_directory_summary("/does/not/exist"),(0,0))
        with tempfile.TemporaryDirectory() as d:
            p=os.path.join(d,"f")
            with open(p,"w") as h: h.write("abc")
            with patch("jobchain.report.os.path.getsize",side_effect=OSError("gone")):
                self.assertEqual(_directory_summary(d),(1,0))

    def test_directory_summary_walk_error(self):
        with tempfile.TemporaryDirectory() as d, patch("jobchain.report.os.walk",side_effect=OSError("denied")):
            self.assertEqual(_directory_summary(d),(0,0))

    def test_directory_summary_depth_limit(self):
        with tempfile.TemporaryDirectory() as d:
            nested=os.path.join(d,"a","b","c"); os.makedirs(nested)
            with open(os.path.join(d,"root"),"w") as h: h.write("x")
            with open(os.path.join(nested,"deep"),"w") as h: h.write("xx")
            count,size=_directory_summary(d,depth=1); self.assertGreaterEqual(count,1); self.assertGreater(size,0)

    def test_format_duration_all_ranges(self):
        self.assertEqual(_format_duration(None),"-")
        self.assertEqual(_format_duration(30),"30s")
        self.assertEqual(_format_duration(90),"1.5m")
        self.assertEqual(_format_duration(7200),"2.0h")
        self.assertEqual(_format_duration(90000),"1.0d")

    def test_format_size_all_units(self):
        for size,token in [(1,"B"),(1024,"KB"),(1024**2,"MB"),(1024**3,"GB"),(1024**4,"TB")]:
            self.assertIn(token,_format_size(size))

    def test_first_line_empty_and_multiline(self):
        self.assertEqual(_first_line(None),""); self.assertEqual(_first_line("  \n"),""); self.assertEqual(_first_line(" first\nsecond "),"first")

    def test_render_value_types(self):
        self.assertEqual(_render_value(None),""); self.assertEqual(_render_value(True),"true"); self.assertEqual(_render_value(False),"false"); self.assertEqual(_render_value(3),"3")


class TestReportFinalBranches(unittest.TestCase):
    def test_show_failed_stage_without_error_or_jobid_and_without_paths(self):
        bad=stage("s",FAILED,jobid="",timeline=[],error=None)
        r=row("r",FAILED,run=RunState(generation=1,stages=[bad]),work_dir="/w")
        store=SimpleNamespace(home="/h",row_dir=lambda n:"/state",read_manifest=lambda n:[])
        text="\n".join(render_show(r,store,sections=["failure"]))
        self.assertIn("FAILURE",text); self.assertNotIn("message",text); self.assertNotIn("job",text); self.assertNotIn("PATHS",text)


