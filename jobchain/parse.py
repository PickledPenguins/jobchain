"""Reading a parameter file: normalization, then the pre-flight scan.

Normalization repairs presentation without changing meaning. The governing
rule is that the number of fields on a line is an invariant: a line that
holds seven fields before normalization holds seven afterwards. That rule
exists because an empty field is a value, so collapsing runs of delimiters
would shift every later column and silently validate a row against the wrong
schema.

The scan then runs in three tiers, in order, and always completes: it
collects every failure rather than stopping at the first, because the point
of a pre-flight scan is to hand back a complete list of what needs fixing.

  1. Structural: does each line split into the expected number of fields?
  2. Field:      does each value satisfy its column's validators?
  3. File:       do the cross-row constraints hold over the whole set?

A row that fails the structural tier is not field-checked, and a row that
fails the field tier does not contribute to the file tier, because both
would produce cascading noise from a single root cause.

The two halves live together because nothing consumes one without the other:
normalization exists to produce rows for the scan.
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .core import StructureError, get_logger, trace
from .schema import Schema

_BOM = "\ufeff"

# =========================================================================
# Part 1: normalization
# =========================================================================

_BOM = "\ufeff"


@dataclass
class LineChange:
    """One line that normalization altered, kept for the change report."""

    line_num: int
    before: str
    after: str
    reasons: List[str] = dc_field(default_factory=list)


@dataclass
class NormalizeResult:
    """Outcome of normalizing a whole file."""

    header: Optional[List[str]] = None
    rows: List[Tuple[int, List[str]]] = dc_field(default_factory=list)
    changes: List[LineChange] = dc_field(default_factory=list)
    skipped_blank: int = 0
    skipped_comment: int = 0
    total_lines: int = 0

    @property
    def changed_count(self) -> int:
        return len(self.changes)


def split_line(line: str, schema: Schema) -> List[str]:
    """Split one line into raw fields according to the schema's format.

    Quoted formats are delegated to the csv module so that a delimiter inside
    a quoted value does not split the field. Unquoted formats split literally,
    which keeps empty fields intact.
    """
    if schema.quoting:
        reader = csv.reader(io.StringIO(line), delimiter=schema.delimiter or ",")
        try:
            fields = next(reader, [])
        except csv.Error as exc:
            raise StructureError(f"could not parse quoted line: {exc}") from exc
        return list(fields)
    if schema.delimiter is None:
        # Whitespace-separated: runs of whitespace are one separator by
        # definition, so there are no empty fields to preserve.
        return line.split()
    return line.split(schema.delimiter)


def join_fields(fields: Sequence[str], schema: Schema) -> str:
    """Render fields back into a line using the schema's delimiter."""
    if schema.quoting:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=schema.delimiter or ",",
                            lineterminator="")
        writer.writerow(list(fields))
        return buffer.getvalue()
    return (schema.delimiter if schema.delimiter is not None else " ").join(fields)


def normalize_file(path: str, schema: Schema) -> NormalizeResult:
    """Read and normalize a parameter file, returning rows and a change report.

    Blank lines and comment lines are dropped but counted. Original line
    numbers are preserved on every surviving row so that reports point at the
    file the user actually wrote.
    """
    logger = get_logger()
    try:
        with open(path, "r", encoding="utf-8", errors="strict", newline="") as handle:
            raw_text = handle.read()
    except FileNotFoundError as exc:
        raise StructureError(f"parameter file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise StructureError(
            f"parameter file {path} is not valid UTF-8: {exc}"
        ) from exc
    except OSError as exc:
        raise StructureError(f"could not read parameter file {path}: {exc}") from exc

    result = NormalizeResult()
    had_bom = raw_text.startswith(_BOM)
    if had_bom:
        raw_text = raw_text[len(_BOM):]
        logger.debug("stripped a UTF-8 byte-order mark from %s", path)

    # splitlines handles LF, CRLF, and bare CR uniformly, which is the whole
    # point: files edited on more than one platform normalize identically.
    lines = raw_text.splitlines()
    result.total_lines = len(lines)
    expected_count = len(schema.fields)

    for index, original in enumerate(lines, start=1):
        stripped = original.strip()
        if not stripped:
            result.skipped_blank += 1
            continue
        if schema.comment_char and stripped.startswith(schema.comment_char):
            result.skipped_comment += 1
            continue

        fields, reasons = _normalize_line(original, schema)

        if result.header is None and schema.has_header:
            result.header = fields
            _check_header(fields, schema, index)
            continue

        rebuilt = join_fields(fields, schema)
        if rebuilt != original:
            if had_bom and index == 1:
                reasons.append("removed byte-order mark")
            result.changes.append(
                LineChange(line_num=index, before=original, after=rebuilt, reasons=reasons)
            )
        trace("line %d normalized to %d fields", index, len(fields))
        result.rows.append((index, fields))

    if schema.has_header and result.header is None:
        raise StructureError(
            f"{path} contains no header row"
        )

    logger.debug(
        "normalized %s: %d rows, %d changed, %d blank, %d comment, expecting %d fields",
        path, len(result.rows), result.changed_count, result.skipped_blank,
        result.skipped_comment, expected_count,
    )
    return result


def _normalize_line(line: str, schema: Schema) -> Tuple[List[str], List[str]]:
    """Normalize one line, returning its fields and the reasons it changed.

    The field-count invariant is asserted here: the number of fields after
    normalization must equal the number before. An empty field is a value,
    so losing one would shift every later column and validate the row
    against the wrong schema.
    """
    before = split_line(line, schema)
    reasons: List[str] = []

    after = [value.strip() for value in before]
    if after != before:
        reasons.append("trimmed whitespace around fields")

    if len(after) != len(before):  # pragma: no cover - guards a coding error
        raise StructureError(
            "internal invariant violated: normalization changed the field count"
        )

    return after, reasons


def _check_header(header: Sequence[str], schema: Schema, line_num: int) -> None:
    """Warn when the file's header disagrees with the schema's field order.

    This is a warning rather than an error because the schema, not the
    header, defines column order; but a mismatch is almost always a sign that
    the wrong schema was selected, so it must be visible.
    """
    logger = get_logger()
    expected = schema.field_names
    if list(header) == expected:
        return
    if len(header) != len(expected):
        logger.warning(
            "header on line %d has %d column(s) but the schema declares %d: "
            "%s vs %s",
            line_num, len(header), len(expected), list(header), expected,
        )
        return
    differing = [f"{got!r} (schema says {want!r})"
                 for got, want in zip(header, expected) if got != want]
    logger.warning(
        "header on line %d does not match the schema field names: %s",
        line_num, "; ".join(differing),
    )


def write_normalized(path: str, result: NormalizeResult, schema: Schema) -> str:
    """Write the normalized rows to a file and return the path written.

    The normalized copy is what the scan and the run consume. The original
    file is never modified, so a run can always be reproduced from the input
    the user actually wrote.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        if result.header is not None:
            handle.write(join_fields(result.header, schema) + "\n")
        handle.writelines(join_fields(fields, schema) + "\n" for _, fields in result.rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    trace("wrote normalized file %s", path)
    return path


def format_change_report(result: NormalizeResult, limit: int = 20) -> List[str]:
    """Render the normalization change report as displayable lines."""
    lines: List[str] = []
    if not result.changes:
        lines.append("No lines required normalization.")
        return lines
    lines.append(f"{result.changed_count} line(s) normalized:")
    for change in result.changes[:limit]:
        lines.append(f"  line {change.line_num}: {'; '.join(change.reasons) or 'reformatted'}")
        lines.append(f"    before: {change.before!r}")
        lines.append(f"    after:  {change.after!r}")
    if result.changed_count > limit:
        lines.append(f"  ... and {result.changed_count - limit} more")
    return lines

# =========================================================================
# Part 2: the scan
# =========================================================================

@dataclass
class FieldFailure:
    """One field on one row that failed its validators."""

    field_name: str
    field_index: int
    raw_value: str
    description: str    # what the column expected
    reason: str         # what was wrong with this value


@dataclass
class RowResult:
    """Outcome of validating one row."""

    line_num: int
    index: int                                   # position among data rows
    raw_fields: List[str]
    record: Optional[Dict[str, Any]] = None      # typed values, only when ok
    structural_failure: Optional[str] = None
    field_failures: List[FieldFailure] = dc_field(default_factory=list)
    row_failures: List[str] = dc_field(default_factory=list)
    file_failures: List[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.structural_failure or self.field_failures
                    or self.row_failures or self.file_failures)

    def failure_id(self) -> str:
        """Compact encoding of why this row failed, for its status string.

        Column indices rather than names keep the status short and stable when
        columns are renamed; explain expands it into the full reasons.
        """
        if self.structural_failure:
            return "struct"
        if self.field_failures:
            return "+".join(str(f.field_index) for f in self.field_failures)
        if self.row_failures:
            return "row"
        if self.file_failures:
            return "file"
        return "unknown"

    def reasons(self) -> List[str]:
        """Every reason this row failed, as displayable text."""
        out: List[str] = []
        if self.structural_failure:
            out.append(self.structural_failure)
        out.extend(
            f"{f.field_name}: {f.reason} (expected {f.description})"
            for f in self.field_failures
        )
        out.extend(self.row_failures)
        out.extend(self.file_failures)
        return out


@dataclass
class ScanReport:
    """Aggregate result of a full-file scan."""

    schema_name: str
    params_path: str
    rows: List[RowResult] = dc_field(default_factory=list)
    file_level_failures: List[str] = dc_field(default_factory=list)
    skipped_blank: int = 0
    skipped_comment: int = 0

    @property
    def valid_rows(self) -> List[RowResult]:
        return [r for r in self.rows if r.ok]

    @property
    def invalid_rows(self) -> List[RowResult]:
        return [r for r in self.rows if not r.ok]

    @property
    def ok(self) -> bool:
        return not self.invalid_rows and not self.file_level_failures

    def to_dict(self) -> Dict[str, Any]:
        """Render the report as plain data for JSON output or archival."""
        return {
            "schema": self.schema_name,
            "params": self.params_path,
            "total_rows": len(self.rows),
            "valid": len(self.valid_rows),
            "invalid": len(self.invalid_rows),
            "skipped_blank": self.skipped_blank,
            "skipped_comment": self.skipped_comment,
            "file_level_failures": list(self.file_level_failures),
            "failures": [
                {"line": r.line_num, "index": r.index, "reasons": r.reasons()}
                for r in self.invalid_rows
            ],
        }


def scan(normalized: NormalizeResult, schema: Schema,
         params_path: str = "") -> ScanReport:
    """Validate every row of a normalized file against a schema."""
    logger = get_logger()
    report = ScanReport(
        schema_name=schema.name,
        params_path=params_path,
        skipped_blank=normalized.skipped_blank,
        skipped_comment=normalized.skipped_comment,
    )

    for index, (line_num, fields) in enumerate(normalized.rows):
        report.rows.append(_scan_row(line_num, index, fields, schema))

    _apply_file_validators(report, schema)

    logger.debug(
        "scan complete: %d row(s), %d valid, %d invalid, %d file-level failure(s)",
        len(report.rows), len(report.valid_rows), len(report.invalid_rows),
        len(report.file_level_failures),
    )
    return report


def _scan_row(line_num: int, index: int, fields: Sequence[str],
              schema: Schema) -> RowResult:
    """Run the structural and field tiers over one row."""
    result = RowResult(line_num=line_num, index=index, raw_fields=list(fields))

    if len(fields) != len(schema.fields):
        result.structural_failure = (
            f"expected {len(schema.fields)} field(s), found {len(fields)}"
        )
        return result

    record: Dict[str, Any] = {}
    for position, (column, raw_value) in enumerate(zip(schema.fields, fields)):
        value: Any = raw_value
        failed = False
        for validator in column.validators:
            outcome = validator.validate(raw_value)
            if not outcome.ok:
                result.field_failures.append(FieldFailure(
                    field_name=column.name,
                    field_index=position,
                    raw_value=raw_value,
                    description=column.description,
                    reason=outcome.reason,
                ))
                failed = True
                break  # one failing validator is enough to condemn the field
            value = outcome.value
        if not failed:
            record[column.name] = value

    if result.field_failures:
        return result

    # Row validators see typed values, so they run only once every field
    # converted successfully.
    for row_validator in schema.row_validators:
        reason = row_validator.check(record)
        if reason:
            result.row_failures.append(reason)

    if not result.row_failures:
        result.record = record
    trace("row at line %d: %s", line_num, "ok" if result.ok else "failed")
    return result


def _apply_file_validators(report: ScanReport, schema: Schema) -> None:
    """Run file-level validators over the rows that passed earlier tiers."""
    if not schema.file_validators:
        return
    candidates: List[Tuple[int, Dict[str, Any]]] = [
        (r.line_num, r.record) for r in report.rows if r.ok and r.record is not None
    ]
    by_line = {r.line_num: r for r in report.rows}

    for file_validator in schema.file_validators:
        for line_num, reason in file_validator.check(candidates):
            if line_num == 0:
                # Line 0 means the finding is about the file as a whole
                # rather than any particular row.
                report.file_level_failures.append(reason)
            elif line_num in by_line:
                by_line[line_num].file_failures.append(reason)


def explain_row(result: RowResult, schema: Schema) -> List[str]:
    """Render every validator applied to one row, with each outcome.

    This is the answer to "why was this row rejected", and it deliberately
    shows the checks that passed as well as the ones that failed, so that a
    schema that is checking the wrong thing is as visible as a value that is
    wrong.
    """
    lines = [f"Row at line {result.line_num} (data row {result.index}):"]
    if result.structural_failure:
        lines.append(f"  STRUCTURE  {result.structural_failure}")
        lines.append(f"  raw fields: {result.raw_fields}")
        return lines

    failed_fields = {f.field_name: f for f in result.field_failures}
    for position, (column, raw_value) in enumerate(zip(schema.fields, result.raw_fields)):
        lines.append(f"  [{position}] {column.name} = {raw_value!r}")
        if not column.validators:
            lines.append("        (no checks declared)")
        failure = failed_fields.get(column.name)
        for validator in column.validators:
            outcome = validator.validate(raw_value)
            mark = "PASS" if outcome.ok else "FAIL"
            detail = "" if outcome.ok else f" -- {outcome.reason}"
            lines.append(f"        {mark}  {validator.description}{detail}")
            if not outcome.ok:
                break
        if failure is None and column.validators:
            typed = result.record.get(column.name) if result.record else None
            lines.append(f"        value used: {typed!r}")

    for row_validator in schema.row_validators:
        if result.record is None:
            lines.append(f"  ROW   SKIPPED  {row_validator.description}")
            continue
        reason = row_validator.check(result.record)
        mark = "PASS" if reason is None else "FAIL"
        detail = "" if reason is None else f" -- {reason}"
        lines.append(f"  ROW   {mark}  {row_validator.description}{detail}")

    for reason in result.file_failures:
        lines.append(f"  FILE  FAIL  {reason}")

    return lines


def format_report(report: ScanReport, limit: int = 25) -> List[str]:
    """Render a scan report as displayable lines."""
    lines: List[str] = []
    total = len(report.rows)
    valid = len(report.valid_rows)
    invalid = len(report.invalid_rows)
    lines.append(
        f"Scanned {total} row(s) against schema '{report.schema_name}': "
        f"{valid} valid, {invalid} invalid"
    )
    if report.skipped_blank or report.skipped_comment:
        lines.append(
            f"Skipped {report.skipped_blank} blank and "
            f"{report.skipped_comment} comment line(s)"
        )
    for reason in report.file_level_failures:
        lines.append(f"  FILE: {reason}")
    for row in report.invalid_rows[:limit]:
        lines.append(f"  line {row.line_num}:")
        for reason in row.reasons():
            lines.append(f"    - {reason}")
    if invalid > limit:
        lines.append(f"  ... and {invalid - limit} more invalid row(s)")
    return lines
