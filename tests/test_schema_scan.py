"""Tests for format definition, normalization, and the pre-flight scan.

The normalizer's field-count invariant is the load-bearing property here: a
line that holds N fields before normalization must hold N afterwards, because
an empty field is a value and losing one silently shifts every later column.
"""

from __future__ import annotations

import unittest

from jobchain import schema as V
from jobchain.core import SchemaError, StructureError
from jobchain.parse import (
    explain_row,
    format_report,
    join_fields,
    normalize_file,
    scan,
    split_line,
    write_normalized,
)
from jobchain.schema import Schema, apply_base_dir, load_schema
from tests.helpers import TempProject

SIMPLE_SCHEMA = """\
name: simple
format:
  delimiter: comma
  header: true
  id_field: rid
fields:
  - name: rid
    type: regex
    pattern: "[a-z0-9]+"
  - name: count
    type: int
    min: 1
    max: 100
  - name: label
    optional: true
    type: str
    max_length: 20
"""


class TestSchemaLoading(TempProject):
    def load(self, text: str) -> Schema:
        return load_schema(self.write("s.yaml", text))

    def test_loads_the_simple_schema(self):
        schema = self.load(SIMPLE_SCHEMA)
        self.assertEqual(schema.name, "simple")
        self.assertEqual(schema.field_names, ["rid", "count", "label"])
        self.assertEqual(schema.delimiter, ",")
        self.assertTrue(schema.has_header)
        self.assertEqual(schema.id_field, "rid")

    def test_delimiter_aliases_and_literals(self):
        for spelling, expected in [("comma", ","), ("tab", "\t"), ("pipe", "|"),
                                   ("colon", ":"), ("semicolon", ";"),
                                   ("space", " "), (";", ";")]:
            with self.subTest(spelling=spelling):
                schema = self.load(
                    f"name: d\nformat:\n  delimiter: '{spelling}'\n"
                    f"fields:\n  - name: a\n    type: str\n")
                self.assertEqual(schema.delimiter, expected)

    def test_whitespace_delimiter_becomes_none(self):
        schema = self.load("name: d\nformat:\n  delimiter: whitespace\n"
                           "fields:\n  - name: a\n    type: str\n")
        self.assertIsNone(schema.delimiter)

    def test_multi_character_delimiter_is_rejected(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nformat:\n  delimiter: '::'\n"
                      "fields:\n  - name: a\n    type: str\n")

    def test_unknown_top_level_key_is_reported(self):
        with self.assertRaises(SchemaError) as caught:
            self.load("name: d\nfeilds: []\nfields:\n  - name: a\n    type: str\n")
        self.assertIn("feilds", str(caught.exception))

    def test_a_schema_without_a_name_gets_a_default(self):
        # A schema may be written inline in the run configuration, where a
        # separate name would be noise, so one is not required.
        self.assertEqual(self.load("fields:\n  - name: a\n    type: str\n").name,
                         "schema")

    def test_no_fields_is_rejected(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields: []\n")

    def test_duplicate_field_names_are_rejected(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields:\n  - name: a\n    type: str\n"
                      "  - name: a\n    type: str\n")

    def test_unknown_check_type_is_reported_with_alternatives(self):
        with self.assertRaises(SchemaError) as caught:
            self.load("name: d\nfields:\n  - name: a\n    type: integer\n")
        self.assertIn("integer", str(caught.exception))
        self.assertIn("int", str(caught.exception))

    def test_bad_check_arguments_are_reported(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields:\n  - name: a\n    type: int\n    minimum: 1\n")

    def test_inverted_bounds_surface_as_schema_errors(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields:\n  - name: a\n    type: int\n"
                      "    min: 10\n    max: 1\n")

    def test_checks_list_form(self):
        schema = self.load("name: d\nfields:\n  - name: a\n    checks:\n"
                           "      - type: regex\n        pattern: '[0-9]+'\n"
                           "      - type: int\n        max: 9\n")
        self.assertEqual(len(schema.fields[0].validators), 2)

    def test_type_and_checks_together_are_rejected(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields:\n  - name: a\n    type: int\n"
                      "    checks:\n      - type: str\n")

    def test_optional_wraps_the_declared_checks(self):
        schema = self.load("name: d\nfields:\n  - name: a\n    optional: true\n"
                           "    default: 7\n    type: int\n")
        validator = schema.fields[0].validators[0]
        self.assertIsInstance(validator, V.Optional_)
        self.assertEqual(validator.validate("").value, 7)

    def test_nested_combinators(self):
        schema = self.load("name: d\nfields:\n  - name: a\n    checks:\n"
                           "      - type: any_of\n        of:\n"
                           "          - type: int\n"
                           "          - type: one_of\n            values: [auto]\n")
        validator = schema.fields[0].validators[0]
        self.assertTrue(validator.validate("auto").ok)
        self.assertTrue(validator.validate("5").ok)
        self.assertFalse(validator.validate("x").ok)

    def test_string_shorthand_for_a_check(self):
        schema = self.load("name: d\nfields:\n  - name: a\n    checks: [bool]\n")
        self.assertTrue(schema.fields[0].validators[0].validate("yes").ok)

    def test_row_check_referencing_an_unknown_field_is_rejected(self):
        with self.assertRaises(SchemaError) as caught:
            self.load("name: d\nfields:\n  - name: a\n    type: int\n"
                      "row_checks:\n  - type: compare\n    left: a\n"
                      "    op: '<'\n    right: nope\n")
        self.assertIn("nope", str(caught.exception))

    def test_file_check_referencing_an_unknown_field_is_rejected(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields:\n  - name: a\n    type: int\n"
                      "file_checks:\n  - type: unique\n    fields: [missing]\n")

    def test_id_field_must_be_declared(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nformat:\n  id_field: nope\n"
                      "fields:\n  - name: a\n    type: int\n")

    def test_resource_column_must_be_declared_and_overridable(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields:\n  - name: a\n    type: int\n"
                      "job:\n  resource_columns:\n    ncpus: missing\n")
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields:\n  - name: a\n    type: int\n"
                      "job:\n  resource_columns:\n    job_name: a\n")

    def test_quoting_requires_a_single_character_delimiter(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nformat:\n  delimiter: whitespace\n  quoting: true\n"
                      "fields:\n  - name: a\n    type: str\n")

    def test_missing_file_is_reported(self):
        with self.assertRaises(SchemaError):
            load_schema(self.path("absent.yaml"))

    def test_invalid_yaml_is_reported(self):
        with self.assertRaises(SchemaError):
            self.load("name: [unclosed\n")

    def test_non_mapping_document_is_rejected(self):
        with self.assertRaises(SchemaError):
            self.load("- just\n- a\n- list\n")

    def test_python_schema_file(self):
        self.write("myschema.py", (
            "from jobchain.schema import Field, Schema\n"
            "from jobchain.schema import Int\n"
            "SCHEMA = Schema(name='py', fields=[Field('n', [Int(min=0)])])\n"
        ))
        schema = load_schema(self.path("myschema.py"))
        self.assertEqual(schema.name, "py")
        self.assertTrue(schema.source_path.endswith("myschema.py"))

    def test_python_schema_without_a_schema_object_is_rejected(self):
        self.write("bad.py", "VALUE = 1\n")
        with self.assertRaises(SchemaError):
            load_schema(self.path("bad.py"))

    def test_python_escape_hatch_on_a_field(self):
        self.write("checks.py", (
            "from jobchain.schema import CheckResult, Validator\n"
            "class EvenOnly(Validator):\n"
            "    def __init__(self):\n"
            "        super().__init__('an even integer')\n"
            "    def _check(self, raw):\n"
            "        if raw.isdigit() and int(raw) % 2 == 0:\n"
            "            return CheckResult(ok=True, value=int(raw))\n"
            "        return CheckResult(ok=False, reason=f'{raw} is not even')\n"
            "EVEN = EvenOnly()\n"
        ))
        schema = self.load("name: d\nfields:\n  - name: a\n"
                           "    python: 'checks.py:EVEN'\n")
        validator = schema.fields[0].validators[0]
        self.assertTrue(validator.validate("4").ok)
        self.assertFalse(validator.validate("5").ok)

    def test_python_reference_must_name_an_attribute(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields:\n  - name: a\n    python: 'checks.py'\n")

    def test_python_reference_to_a_missing_file_is_reported(self):
        with self.assertRaises(SchemaError):
            self.load("name: d\nfields:\n  - name: a\n    python: 'nope.py:X'\n")



class TestNormalization(TempProject):
    def schema(self, delimiter="comma", header=True, quoting=False, fields=3) -> Schema:
        columns = "\n".join(f"  - name: c{i}\n    type: str\n" for i in range(fields))
        return load_schema(self.write("s.yaml",
            f"name: n\nformat:\n  delimiter: {delimiter}\n"
            f"  header: {'true' if header else 'false'}\n"
            f"  quoting: {'true' if quoting else 'false'}\n"
            f"fields:\n{columns}"))

    def test_field_count_is_preserved_when_fields_are_empty(self):
        # The central invariant. Collapsing "a,,b" to "a,b" would shift every
        # later column and validate the row against the wrong schema.
        schema = self.schema()
        path = self.write("p.csv", "c0,c1,c2\na,,b\n")
        result = normalize_file(path, schema)
        self.assertEqual(result.rows[0][1], ["a", "", "b"])

    def test_leading_and_trailing_empty_fields_survive(self):
        schema = self.schema()
        path = self.write("p.csv", "c0,c1,c2\n,b,\n")
        self.assertEqual(normalize_file(path, schema).rows[0][1], ["", "b", ""])

    def test_whitespace_around_fields_is_trimmed(self):
        schema = self.schema()
        path = self.write("p.csv", "c0,c1,c2\n  a , b ,c  \n")
        result = normalize_file(path, schema)
        self.assertEqual(result.rows[0][1], ["a", "b", "c"])
        self.assertEqual(result.changed_count, 1)
        self.assertIn("trimmed", result.changes[0].reasons[0])

    def test_whitespace_inside_a_field_is_preserved(self):
        # Stripping internal whitespace would corrupt any path or label with
        # a space in it.
        schema = self.schema()
        path = self.write("p.csv", "c0,c1,c2\na, my file.dat ,c\n")
        self.assertEqual(normalize_file(path, schema).rows[0][1][1], "my file.dat")

    def test_crlf_and_bare_cr_are_normalized(self):
        schema = self.schema()
        for ending in ["\r\n", "\r", "\n"]:
            with self.subTest(ending=ending):
                path = self.write("p.csv", f"c0,c1,c2{ending}a,b,c{ending}")
                self.assertEqual(normalize_file(path, schema).rows[0][1],
                                 ["a", "b", "c"])

    def test_byte_order_mark_is_stripped(self):
        schema = self.schema()
        path = self.write("p.csv", "\ufeffc0,c1,c2\na,b,c\n")
        result = normalize_file(path, schema)
        self.assertEqual(result.header, ["c0", "c1", "c2"])

    def test_blank_and_comment_lines_are_counted_not_kept(self):
        schema = self.schema()
        path = self.write("p.csv", "# note\nc0,c1,c2\n\na,b,c\n# trailing\n")
        result = normalize_file(path, schema)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.skipped_blank, 1)
        self.assertEqual(result.skipped_comment, 2)

    def test_original_line_numbers_are_preserved(self):
        # Reports must point at the file the user wrote, not at a compacted
        # version of it.
        schema = self.schema()
        path = self.write("p.csv", "c0,c1,c2\n\n# skip\na,b,c\n")
        self.assertEqual(normalize_file(path, schema).rows[0][0], 4)

    def test_runs_of_delimiters_are_never_collapsed(self):
        # There is no option to collapse them: an empty field is a value, and
        # removing one would shift every later column.
        schema = self.schema()
        path = self.write("p.csv", "c0,c1,c2\na,,b\n")
        self.assertEqual(normalize_file(path, schema).rows[0][1], ["a", "", "b"])

    def test_whitespace_delimiter_splits_on_runs(self):
        schema = self.schema(delimiter="whitespace")
        path = self.write("p.txt", "c0 c1 c2\na    b\tc\n")
        self.assertEqual(normalize_file(path, schema).rows[0][1], ["a", "b", "c"])

    def test_quoted_fields_keep_embedded_delimiters(self):
        schema = self.schema(quoting=True)
        path = self.write("p.csv", 'c0,c1,c2\na,"b,still b",c\n')
        self.assertEqual(normalize_file(path, schema).rows[0][1],
                         ["a", "b,still b", "c"])

    def test_headerless_files(self):
        schema = self.schema(header=False)
        path = self.write("p.csv", "a,b,c\nd,e,f\n")
        self.assertEqual(len(normalize_file(path, schema).rows), 2)

    def test_missing_header_is_reported(self):
        schema = self.schema()
        path = self.write("p.csv", "# only comments\n")
        with self.assertRaises(StructureError):
            normalize_file(path, schema)

    def test_missing_file_is_reported(self):
        with self.assertRaises(StructureError):
            normalize_file(self.path("absent.csv"), self.schema())

    def test_non_utf8_input_is_reported_clearly(self):
        path = self.path("bad.csv")
        with open(path, "wb") as handle:
            handle.write(b"c0,c1,c2\na,\xff\xfe,c\n")
        with self.assertRaises(StructureError) as caught:
            normalize_file(path, self.schema())
        self.assertIn("UTF-8", str(caught.exception))

    def test_round_trip_through_write_normalized(self):
        schema = self.schema()
        path = self.write("p.csv", "c0,c1,c2\n a , b ,c\n")
        result = normalize_file(path, schema)
        written = write_normalized(self.path("clean.csv"), result, schema)
        self.assertEqual(self.read(written), "c0,c1,c2\na,b,c\n")

    def test_split_and_join_are_inverse_for_plain_values(self):
        schema = self.schema()
        for line in ["a,b,c", "a,,c", ",,", "a,b,"]:
            with self.subTest(line=line):
                self.assertEqual(join_fields(split_line(line, schema), schema), line)


class TestScan(TempProject):
    def build(self, params: str, schema_text: str = SIMPLE_SCHEMA):
        schema = load_schema(self.write("s.yaml", schema_text))
        path = self.write("p.csv", params)
        apply_base_dir(schema, self.tmp)
        return scan(normalize_file(path, schema), schema, path), schema

    def test_a_clean_file_passes(self):
        report, _ = self.build("rid,count,label\na1,5,x\na2,6,y\n")
        self.assertTrue(report.ok)
        self.assertEqual(len(report.valid_rows), 2)

    def test_wrong_field_count_is_a_structural_failure(self):
        report, _ = self.build("rid,count,label\na1,5\n")
        row = report.rows[0]
        self.assertIsNotNone(row.structural_failure)
        self.assertIn("expected 3", row.structural_failure)
        # Field checks are skipped, so one root cause yields one message.
        self.assertEqual(row.field_failures, [])

    def test_field_failures_name_the_column_and_the_expectation(self):
        report, _ = self.build("rid,count,label\na1,0,x\n")
        failure = report.rows[0].field_failures[0]
        self.assertEqual(failure.field_name, "count")
        self.assertEqual(failure.raw_value, "0")
        self.assertIn("minimum", failure.reason)

    def test_every_failing_field_is_collected(self):
        # The point of a pre-flight scan is a complete list, not the first
        # problem encountered.
        report, _ = self.build("rid,count,label\nA1,0,thisismuchtoolongforthefield\n")
        self.assertEqual(len(report.rows[0].field_failures), 3)

    def test_only_the_first_failing_check_per_field_is_reported(self):
        report, _ = self.build("rid,count,label\na1,abc,x\n")
        self.assertEqual(len(report.rows[0].field_failures), 1)

    def test_row_checks_run_only_when_fields_converted(self):
        schema_text = (SIMPLE_SCHEMA
                       + "row_checks:\n  - type: compare\n    left: count\n"
                         "    op: '<'\n    right: count\n")
        report, _ = self.build("rid,count,label\na1,abc,x\n", schema_text)
        self.assertEqual(report.rows[0].row_failures, [])
        self.assertEqual(len(report.rows[0].field_failures), 1)

    def test_row_check_failure_is_recorded(self):
        schema_text = (SIMPLE_SCHEMA
                       + "row_checks:\n  - type: compare\n    left: count\n"
                         "    op: '<'\n    right: count\n")
        report, _ = self.build("rid,count,label\na1,5,x\n", schema_text)
        self.assertTrue(report.rows[0].row_failures)
        self.assertFalse(report.ok)

    def test_duplicates_are_attributed_to_the_later_row(self):
        report, _ = self.build("rid,count,label\na1,5,x\na1,6,y\n")
        self.assertTrue(report.rows[0].ok)
        self.assertFalse(report.rows[1].ok)
        self.assertIn("duplicate", report.rows[1].file_failures[0])

    def test_invalid_rows_do_not_participate_in_file_checks(self):
        # A row that already failed must not also generate a duplicate
        # complaint, which would be noise from a single root cause.
        report, _ = self.build("rid,count,label\na1,5,x\na1,999,y\n")
        self.assertEqual(report.rows[1].file_failures, [])
        self.assertTrue(report.rows[1].field_failures)

    def test_file_level_failure_is_reported_separately(self):
        schema_text = (
            "name: counted\n"
            "format:\n  delimiter: comma\n  header: true\n"
            "fields:\n  - name: rid\n    type: str\n"
            "  - name: count\n    type: int\n"
            "  - name: label\n    optional: true\n    type: str\n"
            "file_checks:\n  - type: row_count\n    min: 5\n"
        )
        report, _ = self.build("rid,count,label\na1,5,x\n", schema_text)
        self.assertTrue(report.file_level_failures)
        self.assertFalse(report.ok)

    def test_optional_field_default_reaches_the_record(self):
        report, _ = self.build("rid,count,label\na1,5,\n")
        self.assertTrue(report.ok)
        self.assertIsNone(report.rows[0].record["label"])

    def test_report_serializes_to_plain_data(self):
        report, _ = self.build("rid,count,label\na1,0,x\n")
        payload = report.to_dict()
        self.assertEqual(payload["invalid"], 1)
        self.assertEqual(payload["failures"][0]["line"], 2)

    def test_format_report_summarizes_and_truncates(self):
        rows = "".join(f"a{i},0,x\n" for i in range(40))
        report, _ = self.build("rid,count,label\n" + rows)
        lines = format_report(report, limit=5)
        self.assertIn("40 invalid", lines[0])
        self.assertTrue(any("and 35 more" in line for line in lines))

    def test_explain_shows_passing_and_failing_checks(self):
        report, schema = self.build("rid,count,label\na1,0,x\n")
        lines = "\n".join(explain_row(report.rows[0], schema))
        self.assertIn("PASS", lines)
        self.assertIn("FAIL", lines)
        self.assertIn("count", lines)

    def test_explain_reports_a_structural_failure_without_field_detail(self):
        report, schema = self.build("rid,count,label\na1,5\n")
        lines = "\n".join(explain_row(report.rows[0], schema))
        self.assertIn("STRUCTURE", lines)
        self.assertNotIn("PASS", lines)


if __name__ == "__main__":
    unittest.main()
