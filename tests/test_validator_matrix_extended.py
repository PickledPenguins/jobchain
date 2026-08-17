"""Exhaustive-ish validator combinations used as executable regression cases."""

from __future__ import annotations

import os
import unittest

from jobchain.schema import (
    Bool,
    Comparison,
    Exact,
    Float,
    Int,
    OneOf,
    OutputPath,
    RequiredWhen,
    RowCount,
    Unique,
)
from tests.helpers import TempProject


class TestPrimitiveValidatorInputs(unittest.TestCase):
    def test_integer_accepted_forms(self):
        validator = Int()
        for raw, expected in [("0", 0), ("+1", 1), ("-1", -1), ("007", 7), (" 42 ", 42)]:
            with self.subTest(raw=raw):
                self.assertEqual(validator.validate(raw).value, expected)
        for raw in ("", "+", "-", "1.0", "1_000", "0x10", "1 2"):
            with self.subTest(raw=raw):
                self.assertFalse(validator.validate(raw).ok)

    def test_float_accepted_forms(self):
        validator = Float()
        for raw in ("0", "-1", "+1.5", ".5", "1.", "1e3", "-2.5E-2"):
            with self.subTest(raw=raw):
                self.assertTrue(validator.validate(raw).ok)
        for raw in ("", ".", "1e", "1_000", "0x1", "nan", "inf", "-inf"):
            with self.subTest(raw=raw):
                self.assertFalse(validator.validate(raw).ok)

    def test_boolean_all_supported_spellings(self):
        validator = Bool()
        for raw in ("true", "TRUE", "t", "yes", "Y", "on", "1"):
            self.assertTrue(validator.validate(raw).value is True, raw)
        for raw in ("false", "FALSE", "f", "no", "N", "off", "0"):
            self.assertTrue(validator.validate(raw).value is False, raw)
        for raw in ("2", "enabled", "none", "null", ""):
            self.assertFalse(validator.validate(raw).ok, raw)

    def test_one_of_case_modes(self):
        sensitive = OneOf(["CPU", "GPU"])
        self.assertTrue(sensitive.validate("CPU").ok)
        self.assertFalse(sensitive.validate("cpu").ok)
        insensitive = OneOf(["CPU", "GPU"], case_sensitive=False)
        self.assertEqual(insensitive.validate("gpu").value, "GPU")

    def test_exact_is_string_based(self):
        self.assertTrue(Exact(10).validate("10").ok)
        self.assertFalse(Exact(10).validate("010").ok)


class TestPathValidators(TempProject):
    def test_existing_file_directory_and_relative_resolution(self):
        self.write("data/input.dat", "x")
        schema_file = self.write("schema.yaml", """\
name: paths
format: {delimiter: comma, header: true}
fields:
  - {name: file, type: path_exists, must_be_file: true}
  - {name: directory, type: path_exists, must_be_dir: true}
""")
        from jobchain.schema import apply_base_dir, load_schema
        schema = load_schema(schema_file)
        apply_base_dir(schema, self.tmp)
        file_validator = schema.fields[0].validators[0]
        dir_validator = schema.fields[1].validators[0]
        self.assertEqual(file_validator.validate("data/input.dat").value,
                         os.path.join(self.tmp, "data/input.dat"))
        self.assertEqual(dir_validator.validate("data").value,
                         os.path.join(self.tmp, "data"))
        self.assertFalse(file_validator.validate("data").ok)
        self.assertFalse(dir_validator.validate("data/input.dat").ok)
        self.assertFalse(file_validator.validate("missing").ok)

    def test_output_path_requires_parent_and_optionally_new_path(self):
        validator = OutputPath(must_not_exist=True)
        validator.base_dir = self.tmp
        self.assertFalse(validator.validate("new/result.dat").ok)
        os.makedirs(self.path("new"), exist_ok=True)
        self.assertTrue(validator.validate("new/result.dat").ok)
        self.write("new/existing.dat", "x")
        self.assertFalse(validator.validate("new/existing.dat").ok)
        self.assertFalse(validator.validate("missing/result.dat").ok)


class TestRowAndFileValidators(unittest.TestCase):
    def test_comparison_supports_every_operator(self):
        expected = {"<": True, "<=": True, ">": False,
                    ">=": False, "==": False, "!=": True}
        for op, result in expected.items():
            with self.subTest(op=op):
                check = Comparison("a", op, "b")
                self.assertEqual(check.check({"a": 1, "b": 2}) is None, result)

    def test_comparison_equal_boundaries(self):
        for op in ("<=", ">=", "=="):
            self.assertIsNone(Comparison("a", op, "b").check({"a": 2, "b": 2}))
        for op in ("<", ">", "!="):
            self.assertIsNotNone(Comparison("a", op, "b").check({"a": 2, "b": 2}))

    def test_required_when_only_requires_for_matching_condition(self):
        check = RequiredWhen("mode", "gpu", "memory")
        self.assertIsNone(check.check({"mode": "cpu", "memory": ""}))
        self.assertIsNone(check.check({"mode": "gpu", "memory": "16gb"}))
        self.assertIsNotNone(check.check({"mode": "gpu", "memory": ""}))

    def test_unique_supports_single_and_composite_keys(self):
        records = [(1, {"a": "x", "b": "1"}),
                   (2, {"a": "x", "b": "2"}),
                   (3, {"a": "x", "b": "1"})]
        self.assertEqual([line for line, _ in Unique(["a"]).check(records)], [2, 3])
        self.assertEqual([line for line, _ in Unique(["a", "b"]).check(records)], [3])

    def test_row_count_boundaries(self):
        records = [(1, {}), (2, {})]
        self.assertEqual(RowCount(min=2, max=2).check(records), [])
        self.assertTrue(RowCount(min=3).check(records))
        self.assertTrue(RowCount(max=1).check(records))
