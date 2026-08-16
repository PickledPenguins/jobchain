"""Exhaustive coverage of remaining Store/RowState branches."""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jobchain.store import (
    DONE, FAILED, PENDING, CANCELLED, RowState, RunState, StageState, Store,
    _write_text,
)
from jobchain.core import StateError


def stage(name="s",status=DONE,jobid="1"):
    return StageState(name=name,status=status,jobid=jobid,depends="afterok",resources={},timeline=[])


def row(rid="001",runs=None):
    if runs is None:
        runs=[RunState(generation=1,stages=[stage()])]
    return RowState(name=rid,row_id=rid,line_num=2,index=0,params={"rid":rid},generation=1,runs=runs,valid=True,invalid_reasons=[],failure_id="",work_dir="")


class TestStoreRemaining(unittest.TestCase):
    def test_jobid_none_and_unmatched_stage(self):
        r=row(runs=[]); self.assertIsNone(r.jobid)
        r.runs=[RunState(generation=1,stages=[])]; self.assertIsNone(r.jobid)

    def test_node_binary_lazy_lookup(self):
        store=Store("/tmp/jobchain-store-test")
        with patch("jobchain.store.find_node_binary",return_value="/node") as find:
            self.assertEqual(store.node_binary,"/node"); self.assertEqual(store.node_binary,"/node")
        find.assert_called_once()

    def test_handoff_seed_deletes_existing_when_empty(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d); os.makedirs(store.row_dir("r"),exist_ok=True)
            path=os.path.join(store.row_dir("r"),"handoff.seed")
            open(path,"w").close()
            store.seed_handoff("r",{})
            self.assertFalse(os.path.exists(path))

    def test_load_row_skips_non_directory_run_entries(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d); rd=store.row_dir("r"); os.makedirs(rd,exist_ok=True)
            with open(os.path.join(rd,"meta.json"),"w") as h:
                h.write('{"name":"r","row_id":"r","line_num":1,"index":0,"params":{}}')
            with open(os.path.join(rd,"gen"),"w") as h: h.write("1")
            with open(os.path.join(rd,"run-1"),"w") as h: h.write("not a directory")
            self.assertEqual(store.load_row("r").runs,[])

    def test_resolve_unique_duplicate_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d)
            rows=[row("001"),row("002")]
            rows[0].params["x"]="same"; rows[1].params["x"]="same"
            with self.assertRaises(StateError): store.resolve_row("x=same",["x"])
            with self.assertRaises(StateError): store.resolve_row("x=none",["x"])

    def test_resolve_padded_numeric_name(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d); r=row("000001")
            with patch.object(store,"load_rows",return_value=[r]):
                self.assertIs(store.resolve_row("1",[]),r)

    def test_resolve_row_id_fallback_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d); r=row("abc"); r.row_id="external"
            with patch.object(store,"load_rows",return_value=[r]):
                self.assertIs(store.resolve_row("external",[]),r)
                with self.assertRaises(StateError): store.resolve_row("missing",[])

    def test_event_failure_only_warns(self):
        store=Store("/tmp/x")
        result=SimpleNamespace(returncode=1,stderr="bad",stdout="")
        with patch.object(store,"_run_node",return_value=result),patch("jobchain.store.get_logger") as logger:
            store.event("x")
        logger.return_value.warning.assert_called_once()

    def test_write_text_without_parent_directory(self):
        with tempfile.TemporaryDirectory() as d:
            old=os.getcwd(); os.chdir(d)
            try:
                _write_text("file.txt","hello")
                with open("file.txt") as h: self.assertEqual(h.read(),"hello")
            finally: os.chdir(old)

    def test_row_state_terminal_and_stage_reached_paths(self):
        r=row(runs=[RunState(generation=1,stages=[stage("a",PENDING),stage("b",PENDING)])])
        self.assertEqual(r.stage_reached,"a")
        r.runs=[RunState(generation=1,stages=[stage("a",CANCELLED),stage("b",PENDING)])]
        self.assertEqual(r.stage_reached,"a")
        self.assertTrue(r.is_terminal)

    def test_find_node_binary_failure_is_propagated(self):
        with patch("jobchain.store.find_node_binary",side_effect=StateError("missing")):
            with self.assertRaises(StateError): Store("/tmp/x").node_binary

class TestStoreFinalBranches(unittest.TestCase):
    def test_unique_column_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d); a=row("001"); b=row("002"); a.params["x"]="same"; b.params["x"]="same"
            with patch.object(store,"load_rows",return_value=[a,b]):
                with self.assertRaises(StateError): store.resolve_row("x=same",["x"])
