"""Validator classes: field, row, and file tiers.

Validators come in three tiers, applied in order by the scan:

* Field validators inspect one value in one row, and may normalize it.
* Row validators inspect a whole record once its fields have converted.
* File validators inspect every record together, for constraints such as
  uniqueness that cannot be seen one row at a time.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, Iterable, List, Optional, Sequence, Tuple

_INT_RE = re.compile(r"[+-]?[0-9]+")
_FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")

_TRUE_WORDS = {"true", "t", "yes", "y", "on", "1"}
_FALSE_WORDS = {"false", "f", "no", "n", "off", "0"}


def resolve_path(raw: str, base_dir: Optional[str] = None) -> str:
    """Expand and absolutize a path value.

    User and variable references are expanded first, then a relative path is
    resolved against base_dir when one is supplied. Anchoring to the
    parameter file's directory rather than the current one is what makes a
    run reproducible from any working directory, and it means the absolute
    path that was checked is the path handed to the job.
    """
    expanded = os.path.expandvars(os.path.expanduser(raw.strip()))
    if not expanded:
        return expanded
    if os.path.isabs(expanded) or base_dir is None:
        return expanded
    return os.path.normpath(os.path.join(base_dir, expanded))


@dataclass
class CheckResult:
    """Outcome of running one validator against one raw string value."""

    ok: bool
    value: Any = None       # converted value, meaningful only when ok is True
    reason: str = ""        # explanation, meaningful only when ok is False


class Validator(ABC):
    """Base class for all field validators.

    Subclasses implement ``_check``, which receives the raw field string and
    returns a CheckResult. Callers use ``validate``, which applies the shared
    normalization hook before delegating, so cross-cutting behavior can be
    added without touching subclasses.
    """

    def __init__(self, description: str):
        if not description:
            raise ValueError("Validator requires a non-empty description")
        self.description = description

    @abstractmethod
    def _check(self, raw: str) -> CheckResult:
        ...

    def normalize(self, raw: str) -> str:
        """Canonicalize a raw value before checking it.

        The default strips surrounding whitespace, which is always safe
        because the normalizer has already decided what belongs to a field.
        Subclasses override this to expand paths or fold case.
        """
        return raw.strip()

    def validate(self, raw: str) -> CheckResult:
        return self._check(self.normalize(raw))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.description!r})"


class Int(Validator):
    """Validates an optionally-signed integer, with optional inclusive bounds.

    Underscore digit separators and surrounding sign padding are rejected
    rather than silently accepted, so that a value only validates if it reads
    as an integer to a human as well as to Python.
    """

    def __init__(self, min: Optional[int] = None, max: Optional[int] = None,
                 description: Optional[str] = None):
        if min is not None and max is not None and min > max:
            raise ValueError(f"Int bounds inverted: min={min} > max={max}")
        self.min = min
        self.max = max
        super().__init__(description or self._default_description())

    def _default_description(self) -> str:
        if self.min is not None and self.max is not None:
            return f"an integer between {self.min} and {self.max}"
        if self.min is not None:
            return f"an integer of at least {self.min}"
        if self.max is not None:
            return f"an integer of at most {self.max}"
        return "an integer"

    def _check(self, raw: str) -> CheckResult:
        if not _INT_RE.fullmatch(raw):
            return CheckResult(ok=False, reason=f"'{raw}' is not a valid integer")
        value = int(raw)
        if self.min is not None and value < self.min:
            return CheckResult(ok=False, reason=f"{value} is less than minimum {self.min}")
        if self.max is not None and value > self.max:
            return CheckResult(ok=False, reason=f"{value} is greater than maximum {self.max}")
        return CheckResult(ok=True, value=value)


class Float(Validator):
    """Validates a decimal number, with optional inclusive bounds.

    Infinities and NaN are rejected: they parse successfully in Python but are
    never a meaningful job parameter, and a NaN propagating into a submitted
    job is far harder to diagnose than a validation failure.
    """

    def __init__(self, min: Optional[float] = None, max: Optional[float] = None,
                 allow_nonfinite: bool = False, description: Optional[str] = None):
        if min is not None and max is not None and min > max:
            raise ValueError(f"Float bounds inverted: min={min} > max={max}")
        self.min = min
        self.max = max
        self.allow_nonfinite = allow_nonfinite
        super().__init__(description or self._default_description())

    def _default_description(self) -> str:
        if self.min is not None and self.max is not None:
            return f"a decimal between {self.min} and {self.max}"
        if self.min is not None:
            return f"a decimal of at least {self.min}"
        if self.max is not None:
            return f"a decimal of at most {self.max}"
        return "a decimal number"

    def _check(self, raw: str) -> CheckResult:
        if not _FLOAT_RE.fullmatch(raw):
            if self.allow_nonfinite and raw.lower().lstrip("+-") in ("nan", "inf", "infinity"):
                return CheckResult(ok=True, value=float(raw))
            return CheckResult(ok=False, reason=f"'{raw}' is not a valid decimal number")
        value = float(raw)
        if self.min is not None and value < self.min:
            return CheckResult(ok=False, reason=f"{value} is less than minimum {self.min}")
        if self.max is not None and value > self.max:
            return CheckResult(ok=False, reason=f"{value} is greater than maximum {self.max}")
        return CheckResult(ok=True, value=value)


class Str(Validator):
    """Validates a string by length and, optionally, an allowed character set."""

    def __init__(self, min_length: int = 0, max_length: Optional[int] = None,
                 charset: Optional[str] = None, description: Optional[str] = None):
        self.min_length = min_length
        self.max_length = max_length
        self.charset = charset
        self._charset_re = re.compile(f"[{charset}]*") if charset else None
        super().__init__(description or self._default_description())

    def _default_description(self) -> str:
        parts = ["text"]
        if self.min_length and self.max_length:
            parts.append(f"of {self.min_length} to {self.max_length} characters")
        elif self.min_length:
            parts.append(f"of at least {self.min_length} characters")
        elif self.max_length:
            parts.append(f"of at most {self.max_length} characters")
        if self.charset:
            parts.append(f"using only [{self.charset}]")
        return " ".join(parts)

    def _check(self, raw: str) -> CheckResult:
        if len(raw) < self.min_length:
            return CheckResult(ok=False,
                               reason=f"'{raw}' is shorter than {self.min_length} characters")
        if self.max_length is not None and len(raw) > self.max_length:
            return CheckResult(ok=False,
                               reason=f"'{raw}' is longer than {self.max_length} characters")
        if self._charset_re is not None and not self._charset_re.fullmatch(raw):
            return CheckResult(ok=False,
                               reason=f"'{raw}' contains characters outside [{self.charset}]")
        return CheckResult(ok=True, value=raw)


class Bool(Validator):
    """Validates a boolean written in any of the common textual spellings."""

    def __init__(self, description: Optional[str] = None):
        super().__init__(description or "a boolean (true/false, yes/no, 1/0)")

    def normalize(self, raw: str) -> str:
        return raw.strip().lower()

    def _check(self, raw: str) -> CheckResult:
        if raw in _TRUE_WORDS:
            return CheckResult(ok=True, value=True)
        if raw in _FALSE_WORDS:
            return CheckResult(ok=True, value=False)
        return CheckResult(ok=False, reason=f"'{raw}' is not a recognized boolean")


class OneOf(Validator):
    """Validates that a value matches one of a fixed set of strings.

    A case-insensitive match returns the member as spelled in the schema, not
    as spelled in the file, so downstream jobs always receive one canonical
    form regardless of how the parameter file was typed.
    """

    def __init__(self, values: Iterable[str], description: Optional[str] = None,
                 case_sensitive: bool = True):
        self.values = list(values)
        if not self.values:
            raise ValueError("OneOf requires at least one permitted value")
        self.case_sensitive = case_sensitive
        super().__init__(description or f"one of: {', '.join(self.values)}")

    def _check(self, raw: str) -> CheckResult:
        if self.case_sensitive:
            if raw in self.values:
                return CheckResult(ok=True, value=raw)
        else:
            for candidate in self.values:
                if candidate.lower() == raw.lower():
                    return CheckResult(ok=True, value=candidate)
        return CheckResult(ok=False,
                           reason=f"'{raw}' is not one of: {', '.join(self.values)}")


class Exact(Validator):
    """Validates that a value equals a fixed literal, compared as a string."""

    def __init__(self, value: Any, description: Optional[str] = None):
        self.expected = value
        super().__init__(description or f"exactly {value}")

    def _check(self, raw: str) -> CheckResult:
        if raw != str(self.expected):
            return CheckResult(ok=False, reason=f"'{raw}' does not equal {self.expected}")
        return CheckResult(ok=True, value=raw)


class Regex(Validator):
    """Validates that a value matches a regular expression over its full length."""

    def __init__(self, pattern: str, ignore_case: bool = False,
                 description: Optional[str] = None):
        self.pattern = pattern
        try:
            self._compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from exc
        super().__init__(description or f"matching pattern {pattern}")

    def _check(self, raw: str) -> CheckResult:
        if not self._compiled.fullmatch(raw):
            return CheckResult(ok=False,
                               reason=f"'{raw}' does not match pattern {self.pattern}")
        return CheckResult(ok=True, value=raw)


class PathExists(Validator):
    """Validates that a value names a filesystem object that exists now.

    The check runs wherever validation runs, which is normally the submit
    host. A compute node may mount storage differently, so a pass here is
    strong evidence rather than a guarantee; scans can disable path checks
    entirely when the two namespaces are known to differ.
    """

    def __init__(self, must_be_file: bool = False, must_be_dir: bool = False,
                 readable: bool = False, description: Optional[str] = None):
        self.must_be_file = must_be_file
        self.must_be_dir = must_be_dir
        self.readable = readable
        #: Directory that relative paths resolve against. Set by the scan to
        #: the parameter file's own directory, so a file validates the same
        #: way no matter which directory a command was invoked from.
        self.base_dir: Optional[str] = None
        super().__init__(description or self._default_description())

    def _default_description(self) -> str:
        if self.must_be_file:
            return "a path to an existing file"
        if self.must_be_dir:
            return "a path to an existing directory"
        return "a path that exists"

    def normalize(self, raw: str) -> str:
        # Expanding here means the job receives the resolved path, so the
        # value that was validated is the value that gets used.
        return resolve_path(raw, self.base_dir)

    def _check(self, raw: str) -> CheckResult:
        if not os.path.exists(raw):
            return CheckResult(ok=False, reason=f"path '{raw}' does not exist")
        if self.must_be_file and not os.path.isfile(raw):
            return CheckResult(ok=False, reason=f"path '{raw}' exists but is not a file")
        if self.must_be_dir and not os.path.isdir(raw):
            return CheckResult(ok=False, reason=f"path '{raw}' exists but is not a directory")
        if self.readable and not os.access(raw, os.R_OK):
            return CheckResult(ok=False, reason=f"path '{raw}' exists but is not readable")
        return CheckResult(ok=True, value=raw)


class OutputPath(Validator):
    """Validates a path that a job will create.

    The path itself is expected not to exist yet, so the check is on the
    parent directory: it must exist and be writable. This catches the common
    failure where a whole chain runs and every job fails at its final write.
    """

    def __init__(self, must_not_exist: bool = False, description: Optional[str] = None):
        self.must_not_exist = must_not_exist
        #: See PathExists.base_dir.
        self.base_dir: Optional[str] = None
        super().__init__(description or "a writable output path")

    def normalize(self, raw: str) -> str:
        return resolve_path(raw, self.base_dir)

    def _check(self, raw: str) -> CheckResult:
        parent = os.path.dirname(os.path.abspath(raw)) or "."
        if not os.path.isdir(parent):
            return CheckResult(ok=False, reason=f"parent directory '{parent}' does not exist")
        if not os.access(parent, os.W_OK):
            return CheckResult(ok=False, reason=f"parent directory '{parent}' is not writable")
        if self.must_not_exist and os.path.exists(raw):
            return CheckResult(ok=False, reason=f"path '{raw}' already exists")
        return CheckResult(ok=True, value=raw)


class Optional_(Validator):
    """Wraps another validator so that an empty field is permitted.

    An empty value yields ``default`` without consulting the inner validator;
    anything else is delegated. This is how a column that is genuinely
    optional is expressed, as distinct from one that is merely often blank.
    """

    def __init__(self, inner: Validator, default: Any = None,
                 description: Optional[str] = None):
        self.inner = inner
        self.default = default
        super().__init__(description or f"{inner.description}, or empty")

    def normalize(self, raw: str) -> str:
        return raw.strip()

    def _check(self, raw: str) -> CheckResult:
        if raw == "":
            return CheckResult(ok=True, value=self.default)
        return self.inner.validate(raw)


class AllOf(Validator):
    """Combinator: the value must pass every child validator.

    Equivalent to listing the same validators directly on a field, and
    provided for cases where an explicit AND must be nested inside an AnyOf.
    """

    def __init__(self, *validators: Validator, description: Optional[str] = None):
        if not validators:
            raise ValueError("AllOf requires at least one validator")
        self.validators = validators
        super().__init__(description or " and ".join(v.description for v in validators))

    def normalize(self, raw: str) -> str:
        return raw

    def _check(self, raw: str) -> CheckResult:
        value: Any = raw
        for validator in self.validators:
            result = validator.validate(raw)
            if not result.ok:
                return result
            value = result.value
        return CheckResult(ok=True, value=value)


class AnyOf(Validator):
    """Combinator: the value must pass at least one child validator.

    The failure message lists what was expected rather than concatenating
    every child's complaint, which stays readable as alternatives are added.
    """

    def __init__(self, *validators: Validator, description: Optional[str] = None):
        if not validators:
            raise ValueError("AnyOf requires at least one validator")
        self.validators = validators
        super().__init__(description or " or ".join(v.description for v in validators))

    def normalize(self, raw: str) -> str:
        return raw

    def _check(self, raw: str) -> CheckResult:
        for validator in self.validators:
            result = validator.validate(raw)
            if result.ok:
                return result
        return CheckResult(ok=False, reason=f"'{raw}' is not {self.description}")


# ---------------------------------------------------------------------------
# Row-level validation
# ---------------------------------------------------------------------------


class RowValidator(ABC):
    """Base class for checks spanning several columns of one record.

    Row validators run only after every field in the record converted
    successfully, so implementations receive typed values and never have to
    re-parse text.
    """

    def __init__(self, description: str):
        self.description = description

    @abstractmethod
    def check(self, record: Dict[str, Any]) -> Optional[str]:
        """Return None if the record is acceptable, or a failure reason."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.description!r})"


class RequiredWhen(RowValidator):
    """Requires one column to be present when another holds a given value.

    This expresses the common shape where a mode selector turns other
    parameters from optional into mandatory.
    """

    def __init__(self, when_field: str, equals: Any, require_field: str,
                 description: Optional[str] = None):
        self.when_field = when_field
        self.equals = equals
        self.require_field = require_field
        super().__init__(
            description
            or f"{require_field} is required when {when_field} is {equals}"
        )

    def check(self, record: Dict[str, Any]) -> Optional[str]:
        if record.get(self.when_field) != self.equals:
            return None
        value = record.get(self.require_field)
        if value is None or value == "":
            return (f"{self.require_field} must be set when "
                    f"{self.when_field} is {self.equals}")
        return None


class Comparison(RowValidator):
    """Compares two numeric columns of the same record."""

    _OPS: ClassVar[Dict[str, Tuple[Callable[[Any, Any], bool], str]]] = {
        "<": (lambda a, b: a < b, "less than"),
        "<=": (lambda a, b: a <= b, "less than or equal to"),
        ">": (lambda a, b: a > b, "greater than"),
        ">=": (lambda a, b: a >= b, "greater than or equal to"),
        "==": (lambda a, b: a == b, "equal to"),
        "!=": (lambda a, b: a != b, "different from"),
    }

    def __init__(self, left: str, op: str, right: str,
                 description: Optional[str] = None):
        if op not in self._OPS:
            raise ValueError(f"unsupported comparison operator {op!r}")
        self.left = left
        self.right = right
        self.op = op
        super().__init__(description or f"{left} must be {self._OPS[op][1]} {right}")

    def check(self, record: Dict[str, Any]) -> Optional[str]:
        left_value, right_value = record.get(self.left), record.get(self.right)
        if left_value is None or right_value is None:
            return None  # an absent side is the other validators' problem
        func, phrase = self._OPS[self.op]
        try:
            if func(left_value, right_value):
                return None
        except TypeError:
            return (f"cannot compare {self.left}={left_value!r} with "
                    f"{self.right}={right_value!r}")
        return (f"{self.left}={left_value} must be {phrase} "
                f"{self.right}={right_value}")


class PredicateRow(RowValidator):
    """Wraps an arbitrary callable, for schema escape-hatch use."""

    def __init__(self, func: Callable[[Dict[str, Any]], Optional[str]],
                 description: str):
        self.func = func
        super().__init__(description)

    def check(self, record: Dict[str, Any]) -> Optional[str]:
        return self.func(record)


# ---------------------------------------------------------------------------
# File-level validation
# ---------------------------------------------------------------------------


class FileValidator(ABC):
    """Base class for checks that need every record at once.

    Uniqueness is the motivating case: a duplicated row identifier cannot be
    detected from a single row, but silently breaks the mapping between rows
    and their state directories.
    """

    def __init__(self, description: str):
        self.description = description

    @abstractmethod
    def check(self, records: Sequence[Tuple[int, Dict[str, Any]]]) -> List[Tuple[int, str]]:
        """Return a list of (line number, reason) pairs for offending records."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.description!r})"


class Unique(FileValidator):
    """Requires the value of one column, or a tuple of columns, to be unique."""

    def __init__(self, fields: Sequence[str], description: Optional[str] = None):
        self.fields = list(fields)
        if not self.fields:
            raise ValueError("Unique requires at least one field name")
        joined = ", ".join(self.fields)
        super().__init__(description or f"{joined} must be unique across all rows")

    def check(self, records: Sequence[Tuple[int, Dict[str, Any]]]) -> List[Tuple[int, str]]:
        seen: Dict[Tuple[Any, ...], int] = {}
        failures: List[Tuple[int, str]] = []
        for line_num, record in records:
            key = tuple(record.get(name) for name in self.fields)
            if key in seen:
                shown = ", ".join(f"{n}={v!r}" for n, v in zip(self.fields, key))
                failures.append(
                    (line_num, f"duplicate value ({shown}); first seen on line {seen[key]}")
                )
            else:
                seen[key] = line_num
        return failures


class RowCount(FileValidator):
    """Requires the number of records to fall within inclusive bounds."""

    def __init__(self, min: Optional[int] = None, max: Optional[int] = None,
                 description: Optional[str] = None):
        self.min = min
        self.max = max
        super().__init__(description or f"between {min} and {max} rows")

    def check(self, records: Sequence[Tuple[int, Dict[str, Any]]]) -> List[Tuple[int, str]]:
        count = len(records)
        if self.min is not None and count < self.min:
            return [(0, f"file has {count} rows, fewer than the required {self.min}")]
        if self.max is not None and count > self.max:
            return [(0, f"file has {count} rows, more than the permitted {self.max}")]
        return []


class PredicateFile(FileValidator):
    """Wraps an arbitrary callable as a file-level check.

    Used for the check_file hook of a schema written as a Python class,
    which cannot be expressed as one of the declarative file checks.
    """

    def __init__(self, func: Callable[[Sequence[Tuple[int, Dict[str, Any]]]],
                                      List[Tuple[int, str]]],
                 description: str):
        super().__init__(description)
        self.func = func
        self.fields: List[str] = []

    def check(self, records: Sequence[Tuple[int, Dict[str, Any]]]
              ) -> List[Tuple[int, str]]:
        return list(self.func(records) or [])


# Registries mapping schema keywords to classes. They are typed loosely
# because the loader constructs them with keyword arguments taken from the
# schema document, which no static signature can describe; bad arguments are
# caught at load time and reported as schema errors.
#: Field-level checks available to a schema.
FIELD_VALIDATORS: Dict[str, Any] = {
    "int": Int,
    "float": Float,
    "str": Str,
    "bool": Bool,
    "one_of": OneOf,
    "exact": Exact,
    "regex": Regex,
    "path_exists": PathExists,
    "output_path": OutputPath,
    "all_of": AllOf,
    "any_of": AnyOf,
}

#: Row-level checks available to a schema.
ROW_VALIDATORS: Dict[str, Any] = {
    "required_when": RequiredWhen,
    "compare": Comparison,
}

#: File-level checks available to a schema.
FILE_VALIDATORS: Dict[str, Any] = {
    "unique": Unique,
    "row_count": RowCount,
}


def _anchor(validator: Validator, base_dir: str) -> None:
    """Set base_dir on a validator and on anything it wraps."""
    if hasattr(validator, "base_dir"):
        validator.base_dir = base_dir
    inner = getattr(validator, "inner", None)
    if inner is not None:
        _anchor(inner, base_dir)
    for child in getattr(validator, "validators", ()):
        _anchor(child, base_dir)
