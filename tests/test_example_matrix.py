"""Generated boundary and cross-product tests for the user-facing examples."""

from __future__ import annotations

import os
import unittest

from jobchain.schema import Bool, Float, Int, OneOf, Regex, Str
from tests.helpers import TempProject


class TestValidatorBoundaries(unittest.TestCase):
    """Exercise representative valid/invalid boundaries behind the examples."""

    def test_integer_boundaries_and_python_underscore_rejection(self):
        validator = Int(min=1, max=128)
        self.assertEqual(validator.validate("1").value, 1)
        self.assertEqual(validator.validate("128").value, 128)
        self.assertFalse(validator.validate("0").ok)
        self.assertFalse(validator.validate("129").ok)
        self.assertFalse(validator.validate("1_0").ok)
        self.assertTrue(validator.validate(" 1 ").ok)

    def test_float_boundaries_and_nonfinite_defaults(self):
        validator = Float(min=0.0, max=1.0)
        self.assertEqual(validator.validate("0").value, 0.0)
        self.assertEqual(validator.validate("1.0").value, 1.0)
        self.assertFalse(validator.validate("-0.1").ok)
        self.assertFalse(validator.validate("1.1").ok)
        self.assertFalse(validator.validate("nan").ok)
        self.assertFalse(validator.validate("inf").ok)
        permissive = Float(allow_nonfinite=True)
        self.assertTrue(permissive.validate("nan").ok)
        self.assertTrue(permissive.validate("-inf").ok)

    def test_boolean_spellings_normalize(self):
        validator = Bool()
        for value in ("true", "TRUE", "yes", "on", "1"):
            self.assertTrue(validator.validate(value).ok)
        for value in ("false", "FALSE", "no", "off", "0"):
            self.assertTrue(validator.validate(value).ok)
        self.assertFalse(validator.validate("maybe").ok)

    def test_string_and_regex_boundaries(self):
        string = Str(min_length=3, max_length=5, charset="A-Za-z0-9_-")
        self.assertTrue(string.validate("abc").ok)
        self.assertTrue(string.validate("abc12").ok)
        self.assertFalse(string.validate("ab").ok)
        self.assertFalse(string.validate("abcdef").ok)
        self.assertFalse(string.validate("abc!").ok)
        regex = Regex(r"[a-z]+", ignore_case=True)
        self.assertTrue(regex.validate("ABC").ok)
        self.assertFalse(regex.validate("abc123").ok)

    def test_one_of_canonicalizes_case_insensitive_values(self):
        validator = OneOf(["cpu", "gpu"], case_sensitive=False)
        self.assertEqual(validator.validate("GPU").value, "gpu")
        self.assertFalse(validator.validate("fpga").ok)

    def test_exact_and_combinators(self):
        from jobchain.schema import AllOf, AnyOf, Exact

        self.assertTrue(Exact("READY").validate("READY").ok)
        self.assertFalse(Exact("READY").validate("ready").ok)
        combined = AllOf(Str(min_length=3), Regex(r"[a-z]+"))
        self.assertTrue(combined.validate("alpha").ok)
        self.assertFalse(combined.validate("ab").ok)
        alternatives = AnyOf(Exact("auto"), Regex(r"[0-9]+"))
        self.assertTrue(alternatives.validate("auto").ok)
        self.assertTrue(alternatives.validate("123").ok)
        self.assertFalse(alternatives.validate("manual").ok)


class TestExampleCrossChecks(TempProject):
    """Ensure the new examples remain valid user-facing CLI fixtures."""

    def test_validator_matrix_checks_and_generates(self):
        source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "examples", "08_validator_matrix")
        target = self.path("validator-matrix")
        self.copytree(source, target)
        config = os.path.join(target, "config.yaml")
        self.run_cli("run", config, "--check", expect=0, cwd=target)
        self.run_cli("run", config, "--no-submit", expect=0, cwd=target)

    def test_pipeline_matrix_generates_every_stage(self):
        source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "examples", "09_pipeline_matrix")
        target = self.path("pipeline-matrix")
        self.copytree(source, target)
        config = os.path.join(target, "config.yaml")
        self.run_cli("run", config, "--no-submit", expect=0, cwd=target)

    def test_pipeline_matrix_supports_slurm_script_generation(self):
        source = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                              "examples", "09_pipeline_matrix")
        target = self.path("pipeline-slurm")
        self.copytree(source, target)
        config = os.path.join(target, "config.yaml")
        text = self.read(config).replace("scheduler: pbs", "scheduler: slurm")
        self.write("pipeline-slurm/config.yaml", text)
        self.run_cli("run", config, "--no-submit", expect=0, cwd=target)

    def copytree(self, source: str, target: str) -> None:
        import shutil
        shutil.copytree(source, target)
