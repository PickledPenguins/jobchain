"""Explicitly requested load tests.

These are skipped by default. Set JOBCHAIN_RUN_LOAD=1 to run them.
"""

from __future__ import annotations

import os
import shutil
import unittest

from tests.helpers import TempProject

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(os.environ.get("JOBCHAIN_RUN_LOAD") == "1",
                     "load tests require JOBCHAIN_RUN_LOAD=1")
class TestLoadExamples(TempProject):
    def _run(self, directory: str) -> None:
        source = os.path.join(ROOT, "examples", directory)
        target = self.path(directory)
        shutil.copytree(source, target)
        self.run_cli("run", os.path.join(target, "config.yaml"), "--no-submit",
                     expect=0, cwd=target)

    def test_100_rows_generate(self):
        self._run("10_load_100")

    def test_1000_rows_generate(self):
        self._run("11_load_1000")
