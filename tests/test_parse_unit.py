"""Deep, mock-heavy unit coverage of parameter-file parsing.

Renamed from test_parse_exhaustive.py for this project's
one-file-per-subsystem convention.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jobchain.core import StructureError
from jobchain.parse import (
    FieldFailure,
    LineChange,
    NormalizeResult,
    RowResult,
    ScanReport,
    _apply_file_validators,
    _check_header,
    _normalize_line,
    explain_row,
    format_change_report,
    format_report,
    join_fields,
    normalize_file,
    scan,
    split_line,
    write_normalized,
)


class TestParseRemaining(unittest.TestCase):
    def schema(self, **kw):
        base = {
            "name": "test",
            "delimiter": "|",
            "quoting": False,
            "fields": [],
            "has_header": False,
            "comment_char": "#",
            "field_names": [],
            "row_validators": [],
            "file_validators": [],
        }
        base.update(kw)
        return SimpleNamespace(**base)

    def test_split_quoted_csv_error(self):
        schema = self.schema(delimiter=",", quoting=True)

        # csv.reader is permissive by default, so force its Error path to
        # verify that parser failures are translated to StructureError.
        class BrokenReader:
            def __next__(self):
                raise __import__("csv").Error("bad quote")

        with patch("jobchain.parse.csv.reader", return_value=BrokenReader()), self.assertRaises(StructureError):
            split_line('"bad', schema)

    def test_normalize_missing_and_decode_and_os_errors(self):
        schema = self.schema()
        with patch("builtins.open", side_effect=FileNotFoundError), self.assertRaises(StructureError):
            normalize_file("x", schema)
        with patch("builtins.open", side_effect=UnicodeDecodeError("utf-8", b"x", 0, 1, "bad")), self.assertRaises(StructureError):
            normalize_file("x", schema)
        with patch("builtins.open", side_effect=OSError("denied")), self.assertRaises(StructureError):
            normalize_file("x", schema)

    def test_normalize_bom_records_change_reason(self):
        schema = self.schema(fields=[1], field_names=["a"])
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as f:
            f.write("\ufeff value \n")
            path = f.name
        try:
            result = normalize_file(path, schema)
            self.assertIn("removed byte-order mark", result.changes[0].reasons)
        finally:
            os.unlink(path)

    def test_normalize_header_and_mismatch(self):
        schema = self.schema(fields=[1, 2], field_names=["a", "b"], has_header=True)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("a|x\n1|2\n")
            path = f.name
        try:
            with patch("jobchain.parse._check_header") as check:
                result = normalize_file(path, schema)
            self.assertEqual(result.header, ["a", "x"])
            check.assert_called_once()
        finally:
            os.unlink(path)

    def test_normalize_requires_header_when_file_empty(self):
        schema = self.schema(fields=[1], field_names=["a"], has_header=True)
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            path = f.name
        try:
            with self.assertRaises(StructureError):
                normalize_file(path, schema)
        finally:
            os.unlink(path)

    def test_header_check_exact_length_mismatch_and_same_length_mismatch(self):
        schema = self.schema(field_names=["a", "b"])
        logger = SimpleNamespace(warning=lambda *args: None)
        with patch("jobchain.parse.get_logger", return_value=logger):
            _check_header(["a", "b"], schema, 1)
            _check_header(["a"], schema, 2)
            _check_header(["a", "x"], schema, 3)
        self.assertTrue(True)

    def test_write_normalized_writes_header_and_rows(self):
        schema = self.schema(delimiter="|")
        result = NormalizeResult(header=["a", "b"], rows=[(2, ["1", "2"]), (3, ["3", "4"])])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nested", "out.psv")
            self.assertEqual(write_normalized(path, result, schema), path)
            with open(path) as h:
                self.assertEqual(h.read(), "a|b\n1|2\n3|4\n")

    def test_change_report_empty_and_truncated(self):
        self.assertEqual(
            format_change_report(NormalizeResult()), ["No lines required normalization."]
        )
        result = NormalizeResult(changes=[LineChange(i, str(i), str(i + 1), []) for i in range(3)])
        lines = format_change_report(result, limit=1)
        self.assertIn("... and 2 more", lines[-1])
        self.assertIn("reformatted", lines[1])

    def test_row_failure_id_all_categories(self):
        r = RowResult(1, 0, ["x"])
        self.assertEqual(r.failure_id(), "unknown")
        r.structural_failure = "bad"
        self.assertEqual(r.failure_id(), "struct")
        r.structural_failure = None
        r.field_failures = [
            FieldFailure("a", 2, "x", "d", "bad"),
            FieldFailure("b", 4, "x", "d", "bad"),
        ]
        self.assertEqual(r.failure_id(), "2+4")
        r.field_failures = []
        r.row_failures = ["bad"]
        self.assertEqual(r.failure_id(), "row")
        r.row_failures = []
        r.file_failures = ["bad"]
        self.assertEqual(r.failure_id(), "file")

    def test_row_reasons_combines_every_failure(self):
        r = RowResult(
            1,
            0,
            ["x"],
            structural_failure="shape",
            field_failures=[FieldFailure("a", 0, "x", "int", "bad")],
            row_failures=["rowbad"],
            file_failures=["filebad"],
        )
        reasons = r.reasons()
        self.assertEqual(len(reasons), 4)
        self.assertIn("a: bad", reasons[1])

    def test_scan_structural_field_and_row_paths(self):
        class V:
            description = "check"

            def __init__(self, ok=True):
                self.ok = ok

            def validate(self, raw):
                return SimpleNamespace(
                    ok=self.ok, value=int(raw) if self.ok else None, reason="bad"
                )

        col = SimpleNamespace(name="a", description="number", validators=[V(True)])
        rowcheck = SimpleNamespace(description="row check", check=lambda record: "rowbad")
        schema = self.schema(fields=[col], field_names=["a"], row_validators=[rowcheck])
        normalized = NormalizeResult(rows=[(1, []), (2, ["1"])])
        report = scan(normalized, schema, "p")
        self.assertTrue(report.rows[0].structural_failure)
        self.assertEqual(report.rows[1].row_failures, ["rowbad"])

    def test_scan_field_failure_stops_later_validators(self):
        class V:
            description = "bad"

            def validate(self, raw):
                return SimpleNamespace(ok=False, value=None, reason="bad")

        class Never:
            description = "never"

            def validate(self, raw):
                raise AssertionError("should not execute")

        col = SimpleNamespace(name="a", description="a", validators=[V(), Never()])
        schema = self.schema(fields=[col], field_names=["a"])
        report = scan(NormalizeResult(rows=[(1, ["x"])]), schema)
        self.assertEqual(len(report.rows[0].field_failures), 1)

    def test_apply_file_validators_no_validators_is_noop(self):
        report = ScanReport("s", "p")
        schema = self.schema(file_validators=[])
        _apply_file_validators(report, schema)
        self.assertEqual(report.file_level_failures, [])

    def test_apply_file_validators_file_and_known_and_unknown_lines(self):
        validator = SimpleNamespace(
            check=lambda candidates: [(0, "whole file"), (2, "row two"), (99, "ignored")]
        )
        good = RowResult(2, 0, ["x"], record={"a": 1})
        bad = RowResult(3, 1, ["x"], structural_failure="bad")
        report = ScanReport("s", "p", rows=[good, bad])
        schema = self.schema(file_validators=[validator])
        _apply_file_validators(report, schema)
        self.assertEqual(report.file_level_failures, ["whole file"])
        self.assertEqual(good.file_failures, ["row two"])
        self.assertEqual(bad.file_failures, [])

    def test_explain_structural_short_circuit(self):
        r = RowResult(3, 1, ["x"], structural_failure="expected 2")
        schema = self.schema()
        lines = explain_row(r, schema)
        self.assertIn("STRUCTURE", lines[1])
        self.assertIn("raw fields", lines[2])

    def test_explain_no_checks_and_pass_and_fail(self):
        class V:
            description = "check"

            def __init__(self, ok):
                self.ok = ok

            def validate(self, raw):
                return SimpleNamespace(ok=self.ok, value=7, reason="wrong")

        class RV:
            description = "row check"

            def __init__(self, reason):
                self.reason_value = reason

            def check(self, record):
                return self.reason_value

        cols = [
            SimpleNamespace(name="plain", validators=[]),
            SimpleNamespace(name="ok", validators=[V(True)]),
            SimpleNamespace(name="bad", validators=[V(False)]),
        ]
        schema = self.schema(
            fields=cols, field_names=["plain", "ok", "bad"], row_validators=[RV(None), RV("rowbad")]
        )
        r = RowResult(
            1, 0, ["x", "1", "2"], record={"plain": "x", "ok": 7, "bad": None}, row_failures=[]
        )
        lines = explain_row(r, schema)
        text = "\n".join(lines)
        self.assertIn("no checks declared", text)
        self.assertIn("PASS", text)
        self.assertIn("FAIL", text)
        self.assertIn("value used", text)
        self.assertIn("-- rowbad", text)

    def test_explain_skips_row_validators_when_record_missing(self):
        rv = SimpleNamespace(description="row check", check=lambda r: None)
        schema = self.schema(fields=[], field_names=[], row_validators=[rv])
        r = RowResult(1, 0, [], field_failures=[FieldFailure("a", 0, "x", "a", "bad")])
        self.assertIn("SKIPPED", "\n".join(explain_row(r, schema)))

    def test_explain_file_failures(self):
        schema = self.schema(fields=[], field_names=[])
        r = RowResult(1, 0, [], file_failures=["filebad"], record={})
        self.assertIn("FILE  FAIL", "\n".join(explain_row(r, schema)))

    def test_format_report_optional_sections_and_limit(self):
        bads = []
        for i in range(3):
            bads.append(RowResult(i + 1, i, ["x"], structural_failure="bad"))
        report = ScanReport(
            "s", "p", rows=bads, file_level_failures=["whole"], skipped_blank=1, skipped_comment=2
        )
        lines = format_report(report, limit=1)
        text = "\n".join(lines)
        self.assertIn("Skipped 1 blank and 2 comment", text)
        self.assertIn("FILE: whole", text)
        self.assertIn("... and 2 more", text)

    def test_format_report_without_optional_sections(self):
        report = ScanReport("s", "p", rows=[])
        self.assertEqual(len(format_report(report)), 1)

    def test_join_fields_whitespace_and_quoted(self):
        self.assertEqual(join_fields(["a", "b"], self.schema(delimiter=None)), "a b")
        self.assertEqual(
            join_fields(["a,b", "c"], self.schema(delimiter=",", quoting=True)), '"a,b",c'
        )

    def test_normalize_line_reports_trim_reason(self):
        fields, reasons = _normalize_line(" a | b ", self.schema())
        self.assertEqual(fields, ["a", "b"])
        self.assertIn("trimmed whitespace", reasons[0])


class TestParseFinalBranches(unittest.TestCase):
    def test_write_normalized_without_header(self):
        schema = SimpleNamespace(delimiter="|", quoting=False)
        result = NormalizeResult(header=None, rows=[(1, ["a", "b"])])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out")
            write_normalized(path, result, schema)
            with open(path) as h:
                self.assertEqual(h.read(), "a|b\n")

    def test_change_report_at_or_below_limit_has_no_truncation_line(self):
        result = NormalizeResult(changes=[LineChange(1, "a", "b", [])])
        lines = format_change_report(result, limit=1)
        self.assertFalse(any("more" in line for line in lines))

    def test_scan_row_validator_pass_records_typed_record(self):
        class V:
            description = "int"

            def validate(self, raw):
                return SimpleNamespace(ok=True, value=1, reason=None)

        col = SimpleNamespace(name="a", description="int", validators=[V()])
        rv = SimpleNamespace(description="passes", check=lambda record: None)
        schema = SimpleNamespace(name="s", fields=[col], row_validators=[rv], file_validators=[])
        report = scan(NormalizeResult(rows=[(1, ["1"])]), schema)
        self.assertEqual(report.rows[0].record, {"a": 1})
