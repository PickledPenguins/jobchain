"""Executable negative examples for invalid configurations and parameter files."""

from __future__ import annotations

import os
import shutil
import unittest

import yaml

from tests.helpers import TempProject

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEGATIVE = os.path.join(ROOT, "examples", "12_negative_matrix")


class TestNegativeExamples(TempProject):
    """Every intentionally invalid fixture must fail cleanly through the CLI."""

    def test_every_negative_fixture_is_rejected_without_traceback(self):
        cases = sorted(
            name for name in os.listdir(NEGATIVE)
            if os.path.isdir(os.path.join(NEGATIVE, name))
        )
        self.assertEqual(len(cases), 12)

        for case in cases:
            with self.subTest(case=case):
                source = os.path.join(NEGATIVE, case)
                target = self.path("negative", case)
                shutil.copytree(source, target)
                result = self.run_cli(
                    "run", os.path.join(target, "config.yaml"), "--check",
                    cwd=target,
                )
                self.assertNotEqual(result.returncode, 0)
                combined = result.stdout + result.stderr
                self.assertTrue(combined.strip())
                self.assertNotIn("Traceback (most recent call last)", combined)

    def test_manifest_classifies_negative_examples_as_regression(self):
        with open(os.path.join(ROOT, "examples", "manifest.yaml"), encoding="utf-8") as handle:
            manifest = yaml.safe_load(handle)
        entry = next(item for item in manifest["examples"] if item["name"] == "negative-matrix")
        self.assertIn("negative", entry["tags"])
        self.assertIn("regression", entry["tags"])
        self.assertNotIn("smoke", entry["tags"])
