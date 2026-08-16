#!/usr/bin/env python3
"""Fault-injection tests for filesystem, scheduler, and helper failures."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jobchain.core import NodeHelperError, SchedulerError, StateError
from jobchain.scheduler import Scheduler, PBS, _capture, write_script
from jobchain.store import Store, _write_text


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="jobchain-fault-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)


class TestAtomicWriteFailures(TempCase):
    def test_write_text_removes_temporary_file_when_rename_fails(self):
        target = self.path("state")
        with patch("jobchain.store.os.replace", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                _write_text(target, "new")
        self.assertFalse(os.path.exists(target))
        self.assertEqual([n for n in os.listdir(self.tmp) if ".tmp." in n], [])

    def test_write_text_preserves_old_value_when_replacement_fails(self):
        target = self.path("state")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("old")
        with patch("jobchain.store.os.replace", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                _write_text(target, "new")
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old")

    def test_store_config_write_failure_does_not_leave_partial_json(self):
        home = self.path("run")
        store = Store(home)
        os.makedirs(home)
        with open(store.config_path, "w", encoding="utf-8") as handle:
            json.dump({"name": "old"}, handle)
        with patch("jobchain.store.os.replace", side_effect=OSError("I/O failure")):
            with self.assertRaises(OSError):
                store.update_config(name="new")
        self.assertEqual(store.load_config()["name"], "old")
        self.assertEqual([n for n in os.listdir(home) if ".tmp." in n], [])

    def test_script_write_failure_cleans_temporary_script(self):
        target = self.path("bin/job.sh")
        with patch("jobchain.scheduler.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_script(target, "#!/bin/sh\necho ok\n")
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + f".tmp.{os.getpid()}"))


class TestHelperFaults(TempCase):
    def test_helper_execution_failure_becomes_node_helper_error(self):
        store = Store(self.path("run"), node_binary="/missing/jobchain-node")
        with patch("jobchain.store.subprocess.run", side_effect=OSError("exec failed")):
            with self.assertRaises(NodeHelperError):
                store.mark(self.path("run-1"), "main", status="RUNNING")

    def test_claim_rejects_malformed_success_output(self):
        store = Store(self.path("run"), node_binary="jobchain-node")
        completed = subprocess.CompletedProcess([], 0, "unexpected\n", "")
        with patch.object(store, "_run_node", return_value=completed):
            with self.assertRaises(NodeHelperError):
                store.claim()

    def test_claim_reports_helper_failure_diagnostic(self):
        store = Store(self.path("run"), node_binary="jobchain-node")
        completed = subprocess.CompletedProcess([], 17, "", "permission denied")
        with patch.object(store, "_run_node", return_value=completed):
            with self.assertRaisesRegex(NodeHelperError, "permission denied"):
                store.claim()

    def test_mark_reports_helper_failure_diagnostic(self):
        store = Store(self.path("run"), node_binary="jobchain-node")
        completed = subprocess.CompletedProcess([], 2, "", "bad run directory")
        with patch.object(store, "_run_node", return_value=completed):
            with self.assertRaisesRegex(NodeHelperError, "bad run directory"):
                store.mark(self.path("run-1"), "main", status="DONE")


class TestSchedulerFaults(TempCase):
    def test_submit_timeout_is_not_silently_treated_as_success(self):
        scheduler = Scheduler(PBS)
        with patch("jobchain.scheduler.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("qsub", 60)):
            with self.assertRaises(SchedulerError):
                scheduler.submit("job.sh", {})

    def test_scheduler_query_timeout_degrades_to_unknown(self):
        with patch("jobchain.scheduler.shutil.which", return_value="/bin/qstat"), \
             patch("jobchain.scheduler.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("qstat", 60)):
            self.assertIsNone(_capture(["qstat", "-f", "1"]))

    def test_scheduler_nonzero_submission_is_explicit_failure(self):
        completed = subprocess.CompletedProcess(["qsub"], 1, "", "queue unavailable")
        with patch("jobchain.scheduler.subprocess.run", return_value=completed):
            result = Scheduler(PBS).submit("job.sh", {})
        self.assertFalse(result.success)
        self.assertIn("queue unavailable", result.output)


class TestCorruptStateFaults(TempCase):
    def test_corrupt_config_becomes_state_error(self):
        store = Store(self.path("run"))
        os.makedirs(store.home)
        with open(store.config_path, "w", encoding="utf-8") as handle:
            handle.write("{not-json")
        with self.assertRaises(StateError):
            store.load_config()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
