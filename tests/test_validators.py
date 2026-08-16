"""Tests for the validator library.

Coverage is deliberately weighted toward values that Python's own
constructors accept but a parameter file should not, because those are the
failures that would otherwise pass validation and surface as a wrong answer
rather than an error.
"""

from __future__ import annotations

import os
import unittest

from jobchain import schema as V
from tests.helpers import TempProject


class TestInt(unittest.TestCase):
    def test_accepts_plain_integers(self):
        for text, expected in [("0", 0), ("7", 7), ("-3", -3), ("+4", 4),
                               ("007", 7), ("  12  ", 12)]:
            with self.subTest(text=text):
                result = V.Int().validate(text)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(result.value, expected)

    def test_rejects_underscore_separators(self):
        # int("1_0") returns 10 in Python, which would silently change the
        # meaning of a parameter file that contains a typo.
        result = V.Int().validate("1_0")
        self.assertFalse(result.ok)
        self.assertIn("not a valid integer", result.reason)

    def test_rejects_non_integers(self):
        for text in ["", "abc", "1.5", "0x10", "1e3", "--5", "5-", "1 2", "٣"]:
            with self.subTest(text=text):
                self.assertFalse(V.Int().validate(text).ok)

    def test_enforces_bounds(self):
        validator = V.Int(min=1, max=10)
        self.assertTrue(validator.validate("1").ok)
        self.assertTrue(validator.validate("10").ok)
        self.assertFalse(validator.validate("0").ok)
        self.assertFalse(validator.validate("11").ok)
        self.assertIn("less than minimum", validator.validate("0").reason)
        self.assertIn("greater than maximum", validator.validate("11").reason)

    def test_rejects_inverted_bounds_at_construction(self):
        with self.assertRaises(ValueError):
            V.Int(min=10, max=1)

    def test_description_reflects_bounds(self):
        self.assertIn("between 1 and 10", V.Int(min=1, max=10).description)
        self.assertIn("at least 1", V.Int(min=1).description)
        self.assertIn("at most 10", V.Int(max=10).description)
        self.assertEqual(V.Int().description, "an integer")


class TestFloat(unittest.TestCase):
    def test_accepts_decimal_spellings(self):
        for text, expected in [("1", 1.0), ("1.5", 1.5), (".5", 0.5),
                               ("1.", 1.0), ("-2.5e-3", -2.5e-3), ("1E3", 1000.0)]:
            with self.subTest(text=text):
                result = V.Float().validate(text)
                self.assertTrue(result.ok, result.reason)
                self.assertAlmostEqual(result.value, expected)

    def test_rejects_nan_and_infinity_by_default(self):
        # These parse successfully in Python but are never a meaningful job
        # parameter, and propagate silently if admitted.
        for text in ["nan", "NaN", "inf", "-inf", "Infinity"]:
            with self.subTest(text=text):
                self.assertFalse(V.Float().validate(text).ok)

    def test_allows_nonfinite_when_requested(self):
        result = V.Float(allow_nonfinite=True).validate("inf")
        self.assertTrue(result.ok)
        self.assertEqual(result.value, float("inf"))

    def test_rejects_underscore_separators(self):
        self.assertFalse(V.Float().validate("1_0.5").ok)

    def test_enforces_bounds(self):
        validator = V.Float(min=0.0, max=1.0)
        self.assertTrue(validator.validate("0.5").ok)
        self.assertFalse(validator.validate("1.5").ok)
        self.assertFalse(validator.validate("-0.1").ok)

    def test_rejects_inverted_bounds_at_construction(self):
        with self.assertRaises(ValueError):
            V.Float(min=1.0, max=0.0)


class TestStr(unittest.TestCase):
    def test_length_bounds(self):
        validator = V.Str(min_length=2, max_length=4)
        self.assertTrue(validator.validate("abc").ok)
        self.assertFalse(validator.validate("a").ok)
        self.assertFalse(validator.validate("abcde").ok)

    def test_charset(self):
        validator = V.Str(charset="a-z0-9_")
        self.assertTrue(validator.validate("run_01").ok)
        self.assertFalse(validator.validate("Run-01").ok)

    def test_empty_is_allowed_without_a_minimum(self):
        self.assertTrue(V.Str().validate("").ok)

    def test_description_mentions_constraints(self):
        self.assertIn("at least 2", V.Str(min_length=2).description)
        self.assertIn("at most 4", V.Str(max_length=4).description)
        self.assertIn("2 to 4", V.Str(min_length=2, max_length=4).description)


class TestBool(unittest.TestCase):
    def test_true_spellings(self):
        for text in ["true", "TRUE", "yes", "Y", "on", "1", " t "]:
            with self.subTest(text=text):
                result = V.Bool().validate(text)
                self.assertTrue(result.ok)
                self.assertIs(result.value, True)

    def test_false_spellings(self):
        for text in ["false", "No", "off", "0", "f"]:
            with self.subTest(text=text):
                result = V.Bool().validate(text)
                self.assertTrue(result.ok)
                self.assertIs(result.value, False)

    def test_rejects_other_words(self):
        for text in ["maybe", "", "2", "yep"]:
            with self.subTest(text=text):
                self.assertFalse(V.Bool().validate(text).ok)


class TestOneOf(unittest.TestCase):
    def test_case_sensitive_match(self):
        validator = V.OneOf(["cpu", "gpu"])
        self.assertTrue(validator.validate("cpu").ok)
        self.assertFalse(validator.validate("CPU").ok)

    def test_case_insensitive_returns_the_canonical_spelling(self):
        # The job must receive one spelling regardless of how the file was
        # typed, or downstream comparisons become unreliable.
        validator = V.OneOf(["cpu", "gpu"], case_sensitive=False)
        result = validator.validate("GPU")
        self.assertTrue(result.ok)
        self.assertEqual(result.value, "gpu")

    def test_rejects_empty_value_list(self):
        with self.assertRaises(ValueError):
            V.OneOf([])

    def test_failure_lists_permitted_values(self):
        self.assertIn("cpu, gpu", V.OneOf(["cpu", "gpu"]).validate("tpu").reason)


class TestRegexAndExact(unittest.TestCase):
    def test_pattern_must_match_the_whole_value(self):
        validator = V.Regex("[a-z]+")
        self.assertTrue(validator.validate("abc").ok)
        self.assertFalse(validator.validate("abc1").ok)
        self.assertFalse(validator.validate("1abc").ok)

    def test_ignore_case(self):
        self.assertTrue(V.Regex("[a-z]+", ignore_case=True).validate("ABC").ok)

    def test_invalid_pattern_fails_at_construction(self):
        with self.assertRaises(ValueError):
            V.Regex("[unclosed")

    def test_exact_compares_as_text(self):
        self.assertTrue(V.Exact(5).validate("5").ok)
        self.assertFalse(V.Exact(5).validate("05").ok)


class TestPathValidators(TempProject):
    def test_path_exists(self):
        target = self.write("data/input.dat", "x")
        validator = V.PathExists(must_be_file=True, readable=True)
        self.assertTrue(validator.validate(target).ok)
        self.assertFalse(validator.validate(target + ".missing").ok)

    def test_directory_and_file_are_distinguished(self):
        self.write("data/input.dat", "x")
        directory = self.path("data")
        self.assertFalse(V.PathExists(must_be_file=True).validate(directory).ok)
        self.assertTrue(V.PathExists(must_be_dir=True).validate(directory).ok)

    def test_relative_paths_resolve_against_the_base_directory(self):
        # Without anchoring, the same file would validate or fail depending
        # on which directory the command was run from.
        self.write("data/input.dat", "x")
        validator = V.PathExists(must_be_file=True)
        validator.base_dir = self.tmp
        result = validator.validate("data/input.dat")
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.value, self.path("data/input.dat"))

    def test_absolute_paths_ignore_the_base_directory(self):
        target = self.write("data/input.dat", "x")
        validator = V.PathExists()
        validator.base_dir = "/nonexistent"
        self.assertTrue(validator.validate(target).ok)

    def test_output_path_requires_a_writable_parent(self):
        validator = V.OutputPath()
        validator.base_dir = self.tmp
        self.assertTrue(validator.validate("result.out").ok)
        self.assertFalse(validator.validate("missing/dir/result.out").ok)

    def test_output_path_can_require_absence(self):
        existing = self.write("already.out", "x")
        self.assertFalse(V.OutputPath(must_not_exist=True).validate(existing).ok)
        self.assertTrue(V.OutputPath(must_not_exist=True).validate(existing + ".new").ok)

    def test_user_and_variable_expansion(self):
        os.environ["JOBCHAIN_TEST_DIR"] = self.tmp
        try:
            self.write("expanded.dat", "x")
            result = V.PathExists().validate("$JOBCHAIN_TEST_DIR/expanded.dat")
            self.assertTrue(result.ok, result.reason)
        finally:
            del os.environ["JOBCHAIN_TEST_DIR"]


class TestOptionalAndCombinators(unittest.TestCase):
    def test_optional_permits_empty_and_yields_the_default(self):
        validator = V.Optional_(V.Int(min=1), default=0)
        result = validator.validate("")
        self.assertTrue(result.ok)
        self.assertEqual(result.value, 0)

    def test_optional_still_validates_non_empty_values(self):
        validator = V.Optional_(V.Int(min=1))
        self.assertFalse(validator.validate("0").ok)
        self.assertTrue(validator.validate("5").ok)

    def test_all_of_requires_every_child(self):
        validator = V.AllOf(V.Regex("[0-9]+"), V.Int(max=50))
        self.assertTrue(validator.validate("42").ok)
        self.assertFalse(validator.validate("99").ok)
        self.assertFalse(validator.validate("abc").ok)

    def test_all_of_carries_the_last_converted_value(self):
        result = V.AllOf(V.Regex("[0-9]+"), V.Int()).validate("42")
        self.assertEqual(result.value, 42)

    def test_any_of_accepts_either_alternative(self):
        validator = V.AnyOf(V.Int(), V.OneOf(["auto"]))
        self.assertTrue(validator.validate("5").ok)
        self.assertTrue(validator.validate("auto").ok)
        self.assertFalse(validator.validate("maybe").ok)

    def test_any_of_failure_describes_the_alternatives(self):
        # The message must stay readable as alternatives accumulate, rather
        # than concatenating every child's complaint.
        validator = V.AnyOf(V.Int(), V.OneOf(["auto"]), V.Bool())
        reason = validator.validate("maybe").reason
        self.assertIn("an integer", reason)
        self.assertNotIn(";", reason)

    def test_combinators_reject_empty_child_lists(self):
        with self.assertRaises(ValueError):
            V.AllOf()
        with self.assertRaises(ValueError):
            V.AnyOf()

    def test_empty_description_falls_back_to_the_generated_one(self):
        # Built-in validators generate a description, so passing an empty one
        # selects the default rather than leaving the validator unable to
        # explain itself in a report.
        self.assertEqual(V.Int(description="").description, "an integer")

    def test_base_class_rejects_a_validator_with_no_description(self):
        class Nameless(V.Validator):
            def _check(self, raw):
                return V.CheckResult(ok=True, value=raw)

        with self.assertRaises(ValueError):
            Nameless("")


class TestRowValidators(unittest.TestCase):
    def test_required_when_triggers_only_on_the_matching_value(self):
        validator = V.RequiredWhen("mode", "gpu", "ngpus")
        self.assertIsNone(validator.check({"mode": "cpu", "ngpus": None}))
        self.assertIsNotNone(validator.check({"mode": "gpu", "ngpus": None}))
        self.assertIsNone(validator.check({"mode": "gpu", "ngpus": 2}))

    def test_required_when_treats_empty_as_absent(self):
        validator = V.RequiredWhen("mode", "gpu", "ngpus")
        self.assertIsNotNone(validator.check({"mode": "gpu", "ngpus": ""}))

    def test_comparison_operators(self):
        cases = [("<", 1, 2, True), ("<", 2, 1, False), ("<=", 2, 2, True),
                 (">", 3, 2, True), (">=", 2, 3, False), ("==", 2, 2, True),
                 ("!=", 2, 3, True)]
        for op, left, right, expected in cases:
            with self.subTest(op=op):
                validator = V.Comparison("a", op, "b")
                passed = validator.check({"a": left, "b": right}) is None
                self.assertEqual(passed, expected)

    def test_comparison_ignores_absent_sides(self):
        validator = V.Comparison("a", "<", "b")
        self.assertIsNone(validator.check({"a": None, "b": 2}))
        self.assertIsNone(validator.check({"b": 2}))

    def test_comparison_reports_incomparable_types(self):
        validator = V.Comparison("a", "<", "b")
        reason = validator.check({"a": "text", "b": 2})
        self.assertIn("cannot compare", reason)

    def test_unknown_operator_is_rejected(self):
        with self.assertRaises(ValueError):
            V.Comparison("a", "~", "b")

    def test_predicate_row_wraps_a_callable(self):
        validator = V.PredicateRow(
            lambda record: None if record.get("a") else "a is required", "a must be set")
        self.assertIsNone(validator.check({"a": 1}))
        self.assertEqual(validator.check({"a": 0}), "a is required")


class TestFileValidators(unittest.TestCase):
    def test_unique_detects_duplicates_and_names_the_first_occurrence(self):
        records = [(1, {"rid": "a"}), (2, {"rid": "b"}), (3, {"rid": "a"})]
        failures = V.Unique(["rid"]).check(records)
        self.assertEqual(len(failures), 1)
        line, reason = failures[0]
        self.assertEqual(line, 3)
        self.assertIn("first seen on line 1", reason)

    def test_unique_over_a_tuple_of_columns(self):
        records = [(1, {"a": "x", "b": "1"}), (2, {"a": "x", "b": "2"}),
                   (3, {"a": "x", "b": "1"})]
        self.assertEqual(len(V.Unique(["a", "b"]).check(records)), 1)
        self.assertEqual(len(V.Unique(["a"]).check(records)), 2)

    def test_unique_requires_field_names(self):
        with self.assertRaises(ValueError):
            V.Unique([])

    def test_row_count_bounds(self):
        records = [(i, {}) for i in range(1, 4)]
        self.assertEqual(V.RowCount(min=1, max=5).check(records), [])
        self.assertEqual(len(V.RowCount(min=5).check(records)), 1)
        self.assertEqual(len(V.RowCount(max=2).check(records)), 1)

    def test_row_count_failures_are_file_level(self):
        # Line 0 marks a finding about the file rather than any one row.
        failures = V.RowCount(min=5).check([(1, {})])
        self.assertEqual(failures[0][0], 0)


class TestRegistries(unittest.TestCase):
    def test_every_registered_name_maps_to_a_class(self):
        for registry, base in [(V.FIELD_VALIDATORS, V.Validator),
                               (V.ROW_VALIDATORS, V.RowValidator),
                               (V.FILE_VALIDATORS, V.FileValidator)]:
            for name, cls in registry.items():
                with self.subTest(name=name):
                    self.assertTrue(issubclass(cls, base))

    def test_repr_includes_the_description(self):
        self.assertIn("an integer", repr(V.Int()))


if __name__ == "__main__":
    unittest.main()


class TestDocumentedEmptyValueBehavior(unittest.TestCase):
    """Pins the empty-value table in the README's option reference.

    An empty field is a value, not an absence, and which checks accept one is
    the least obvious part of writing a schema. These are documented, so they
    are a contract rather than an accident.
    """

    def test_checks_that_reject_an_empty_value(self):
        for name, validator in [("int", V.Int()), ("float", V.Float()),
                                ("bool", V.Bool()), ("one_of", V.OneOf(["a"])),
                                ("path_exists", V.PathExists())]:
            with self.subTest(check=name):
                self.assertFalse(validator.validate("").ok)
                self.assertFalse(validator.validate("   ").ok)

    def test_checks_that_accept_an_empty_value(self):
        # These four surprise people, so the README calls them out by name.
        for name, validator in [("str", V.Str()), ("exact", V.Exact("")),
                                ("regex", V.Regex("[a-z]*")),
                                ("output_path", V.OutputPath())]:
            with self.subTest(check=name):
                self.assertTrue(validator.validate("").ok)

    def test_min_length_is_how_a_string_column_is_made_mandatory(self):
        self.assertFalse(V.Str(min_length=1).validate("").ok)
        self.assertFalse(V.Str(min_length=1).validate("   ").ok)

    def test_optional_short_circuits_the_inner_check(self):
        validator = V.Optional_(V.Int(min=1), default=7)
        self.assertEqual(validator.validate("").value, 7)
        self.assertEqual(validator.validate("   ").value, 7)
        self.assertFalse(validator.validate("0").ok)

    def test_documented_conversions_reaching_the_job(self):
        self.assertEqual(V.Int().validate("007").value, 7)
        self.assertIs(V.Bool().validate("yes").value, True)
        self.assertEqual(
            V.OneOf(["gpu"], case_sensitive=False).validate("GPU").value, "gpu")

    def test_unique_compares_converted_values(self):
        # 007 and 7 collide in an int column but not in a string one.
        as_int = [(1, {"r": 7}), (2, {"r": 7})]
        as_text = [(1, {"r": "007"}), (2, {"r": "7"})]
        self.assertEqual(len(V.Unique(["r"]).check(as_int)), 1)
        self.assertEqual(len(V.Unique(["r"]).check(as_text)), 0)
