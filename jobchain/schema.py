"""Format definition: validators, the Field/Schema model, and the loader.

A schema describes one parameter-file format completely: how to split lines
into fields, what each column must contain, what relationships must hold
within a row and across the file, and what job resources each row implies.
Changing formats therefore means pointing at a different schema file, never
editing this tool.

The file has two halves. The first defines the validator classes; the second
builds Field and Schema objects out of them, from YAML or from Python. They
live together because they are one concept: what a valid file looks like.

Validators come in three tiers, applied in order by the scan:

* Field validators inspect one value in one row, and may normalize it.
* Row validators inspect a whole record once its fields have converted.
* File validators inspect every record together, for constraints such as
  uniqueness that cannot be seen one row at a time.
"""

from __future__ import annotations

import importlib.util
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Callable, ClassVar, Dict, Iterable, List, Optional, Sequence, Tuple

from .core import SchemaError

# =========================================================================
# Part 1: validators
# =========================================================================

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

# =========================================================================
# Part 2: the schema model and its loaders
# =========================================================================

_DELIMITER_ALIASES = {
    "comma": ",",
    "tab": "\t",
    "pipe": "|",
    "colon": ":",
    "semicolon": ";",
    "space": " ",
    "whitespace": None,   # None means "split on any run of whitespace"
}


@dataclass
class Field:
    """One column of the parameter file.

    ``name`` is how the column is referenced everywhere else: in row and file
    validators, in path templates, and as the ``JC_<name>`` environment
    variable handed to the job.

    ``unique`` does two things: validation fails the file if the column's
    values are not distinct, and the column becomes usable to name a row on
    the command line as ``--row name=value``.
    """

    name: str
    validators: List[Validator] = dc_field(default_factory=list)
    description: str = ""
    required: bool = True
    unique: bool = False

    def __post_init__(self) -> None:
        if not self.description:
            # Fall back to the validators' own descriptions so that every
            # field can explain itself in reports even when the schema
            # author did not write prose for it.
            self.description = " and ".join(
                v.description for v in self.validators
            ) or "any value"


@dataclass
class Schema:
    """A complete parameter-file format definition."""

    name: str
    fields: List[Field]
    delimiter: Optional[str] = ","
    has_header: bool = False
    comment_char: Optional[str] = "#"
    quoting: bool = False          # honour quoted fields via the csv module
    id_field: Optional[str] = None  # column shown as the row's identifier
    row_validators: List[RowValidator] = dc_field(default_factory=list)
    file_validators: List[FileValidator] = dc_field(default_factory=list)
    version: Optional[str] = None
    description: str = ""
    source_path: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.fields:
            raise SchemaError("schema must define at least one field")
        names = [f.name for f in self.fields]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise SchemaError(f"duplicate field names in schema: {sorted(duplicates)}")
        if self.id_field and self.id_field not in names:
            raise SchemaError(
                f"id_field '{self.id_field}' is not a declared field; "
                f"known fields are {names}"
            )
        if self.id_field:
            # The identifying column must be unique, so declaring it as the
            # id_field is enough; the field need not also say so.
            for column in self.fields:
                if column.name == self.id_field:
                    column.unique = True
        self._add_uniqueness_checks()
        if self.quoting and self.delimiter is None:
            raise SchemaError("quoting requires an explicit single-character delimiter")
        self._check_references(names)

    def _check_references(self, names: List[str]) -> None:
        """Fail early if a validator or resource mapping names a missing column."""
        for validator in self.row_validators:
            for attribute in ("when_field", "require_field", "left", "right"):
                referenced = getattr(validator, attribute, None)
                if referenced is not None and referenced not in names:
                    raise SchemaError(
                        f"row validator {validator!r} references unknown field "
                        f"'{referenced}'"
                    )
        for file_validator in self.file_validators:
            for referenced in getattr(file_validator, "fields", []):
                if referenced not in names:
                    raise SchemaError(
                        f"file validator {file_validator!r} references unknown "
                        f"field '{referenced}'"
                    )

    def _add_uniqueness_checks(self) -> None:
        """Turn every field marked unique into a file-level check.

        Uniqueness cannot be seen one row at a time, so it is enforced by the
        same file tier that handles explicit unique checks. Declaring it on
        the field is a shorthand, not a second mechanism.
        """
        declared = {tuple(v.fields) for v in self.file_validators
                    if isinstance(v, Unique)}
        for column in self.fields:
            if column.unique and (column.name,) not in declared:
                self.file_validators.append(Unique([column.name]))

    @property
    def field_names(self) -> List[str]:
        return [f.name for f in self.fields]

    @property
    def unique_fields(self) -> List[str]:
        """Columns usable to name a row on the command line."""
        return [f.name for f in self.fields if f.unique]


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------



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


class SchemaBase:
    """Base class for schemas expressed in Python rather than YAML.

    A subclass declares ``fields`` in column order, exactly as the YAML list
    does, and may override ``check_row`` and ``check_file`` for rules that are
    awkward to express declaratively.
    """

    #: Column definitions, in file order. A subclass replaces this list
    #: wholesale rather than mutating it.
    fields: ClassVar[List[Field]] = []

    def check_row(self, row: Dict[str, Any]) -> Optional[str]:
        """Return None if the record is acceptable, or a failure reason."""
        return None

    def check_file(self, rows: Sequence[Tuple[int, Dict[str, Any]]]
                   ) -> List[Tuple[int, str]]:
        """Return (line number, reason) pairs for offending records."""
        return []

    def build(self, **format_options: Any) -> Schema:
        """Assemble a Schema from this class's declarations."""
        row_checks: List[RowValidator] = []
        file_checks: List[FileValidator] = []
        if type(self).check_row is not SchemaBase.check_row:
            row_checks.append(PredicateRow(self.check_row,
                                           f"{type(self).__name__} row rules"))
        if type(self).check_file is not SchemaBase.check_file:
            file_checks.append(PredicateFile(self.check_file,
                                             f"{type(self).__name__} file rules"))
        return Schema(fields=list(self.fields), row_validators=row_checks,
                      file_validators=file_checks, **format_options)


def load_schema_source(source: Any, base_dir: str, label: str = "schema") -> Schema:
    """Build a Schema from a mapping or a path.

    The run configuration may hold a schema inline or name a file. Both reach
    the same objects, so nothing downstream needs to know which was used.
    """
    if isinstance(source, dict):
        return _build_schema(source, os.path.join(base_dir, "<inline>"))
    if isinstance(source, str):
        path = source if os.path.isabs(source) else os.path.join(base_dir, source)
        return load_schema(path)
    raise SchemaError(f"{label} must be a mapping or a path, got {type(source).__name__}")


def load_schema(path: str) -> Schema:
    """Load a schema from a YAML file, or from a Python module.

    A path ending in ``.py`` is imported and must expose a module-level
    ``SCHEMA`` object, which allows arbitrarily complex formats to be
    expressed when the declarative form is not enough.
    """
    if not os.path.isfile(path):
        raise SchemaError(f"schema file not found: {path}")
    if path.endswith(".py"):
        return _load_python_schema(path)
    return _load_yaml_schema(path)


def _load_python_schema(path: str) -> Schema:
    """Import a Python schema module and return its SCHEMA object."""
    spec = importlib.util.spec_from_file_location("jobchain_user_schema", path)
    if spec is None or spec.loader is None:
        raise SchemaError(f"could not load Python schema: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise SchemaError(f"error executing Python schema {path}: {exc}") from exc
    schema = getattr(module, "SCHEMA", None)
    if not isinstance(schema, Schema):
        raise SchemaError(
            f"Python schema {path} must define a module-level SCHEMA of type Schema"
        )
    schema.source_path = os.path.abspath(path)
    return schema


def _load_yaml_schema(path: str) -> Schema:
    """Parse a YAML schema document into a Schema object."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on site packages
        raise SchemaError(
            "PyYAML is required to read YAML schemas; install it, or use a "
            "Python schema file (.py) instead"
        ) from exc

    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise SchemaError(f"invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise SchemaError(f"could not read schema {path}: {exc}") from exc

    if not isinstance(document, dict):
        raise SchemaError(f"schema {path} must be a YAML mapping at the top level")

    return _build_schema(document, path)


def _build_schema(document: Dict[str, Any], path: str) -> Schema:
    """Build a Schema from an already-parsed document."""

    _reject_unknown(document, {
        "name", "version", "description", "format", "fields",
        "row_checks", "file_checks", "validator_class",
    }, "top level")

    name = document.get("name") or "schema"

    fmt = document.get("format") or {}
    _reject_unknown(fmt, {"delimiter", "header", "comment", "quoting", "id_field"}, "format")

    reference = document.get("validator_class")
    if reference:
        return _load_schema_class(reference, path, fmt, name, document)

    fields = [_build_field(entry, index, path)
              for index, entry in enumerate(document.get("fields") or [])]

    schema = Schema(
        name=str(name),
        version=_as_optional_str(document.get("version")),
        description=str(document.get("description") or ""),
        fields=fields,
        delimiter=_resolve_delimiter(fmt.get("delimiter", ",")),
        has_header=bool(fmt.get("header", False)),
        comment_char=_as_optional_str(fmt.get("comment", "#")),
        quoting=bool(fmt.get("quoting", False)),
        id_field=_as_optional_str(fmt.get("id_field")),
        row_validators=[_build_row_check(e, path) for e in document.get("row_checks") or []],
        file_validators=[_build_file_check(e, path) for e in document.get("file_checks") or []],
        source_path=os.path.abspath(path),
    )
    return schema


def _load_schema_class(reference: str, schema_path: str, fmt: Dict[str, Any],
                       name: str, document: Dict[str, Any]) -> Schema:
    """Build a Schema from a SchemaBase subclass in a module.

    The module must define exactly one SchemaBase subclass, or the reference
    may name one explicitly as "file.py:ClassName".
    """
    module_path, _, wanted = reference.partition(":")
    module = _import_module(module_path, schema_path)
    candidates = [obj for obj in vars(module).values()
                  if isinstance(obj, type) and issubclass(obj, SchemaBase)
                  and obj is not SchemaBase]
    if wanted:
        candidates = [c for c in candidates if c.__name__ == wanted]
        if not candidates:
            raise SchemaError(f"{module_path} has no SchemaBase subclass '{wanted}'")
    if not candidates:
        raise SchemaError(f"{module_path} defines no SchemaBase subclass")
    if len(candidates) > 1:
        raise SchemaError(
            f"{module_path} defines several SchemaBase subclasses "
            f"({', '.join(sorted(c.__name__ for c in candidates))}); name one "
            f"as 'file.py:ClassName'"
        )
    schema = candidates[0]().build(
        name=str(name),
        version=_as_optional_str(document.get("version")),
        description=str(document.get("description") or ""),
        delimiter=_resolve_delimiter(fmt.get("delimiter", ",")),
        has_header=bool(fmt.get("header", False)),
        comment_char=_as_optional_str(fmt.get("comment", "#")),
        quoting=bool(fmt.get("quoting", False)),
        id_field=_as_optional_str(fmt.get("id_field")),
    )
    schema.source_path = os.path.abspath(module_path)
    return schema


def _import_module(module_path: str, relative_to: str):
    """Import a module by path, resolved relative to a configuration file."""
    if not os.path.isabs(module_path):
        module_path = os.path.join(os.path.dirname(os.path.abspath(relative_to)),
                                   module_path)
    if not os.path.isfile(module_path):
        raise SchemaError(f"module not found: {module_path}")
    spec = importlib.util.spec_from_file_location("jobchain_user_module", module_path)
    if spec is None or spec.loader is None:
        raise SchemaError(f"could not load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise SchemaError(f"error executing {module_path}: {exc}") from exc
    return module


def _reject_unknown(mapping: Dict[str, Any], allowed: set, where: str) -> None:
    """Fail on unrecognized keys so that typos surface immediately."""
    unknown = set(mapping) - allowed
    if unknown:
        raise SchemaError(
            f"unknown key(s) {sorted(unknown)} in {where}; "
            f"recognized keys are {sorted(allowed)}"
        )


def _as_optional_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _resolve_delimiter(value: Any) -> Optional[str]:
    """Turn a schema delimiter spelling into the literal separator."""
    if value is None:
        return None
    text = str(value)
    if text in _DELIMITER_ALIASES:
        return _DELIMITER_ALIASES[text]
    if len(text) != 1:
        raise SchemaError(
            f"delimiter must be a single character or a known alias "
            f"({', '.join(sorted(_DELIMITER_ALIASES))}), got {text!r}"
        )
    return text


def _build_field(entry: Any, index: int, path: str) -> Field:
    """Build one Field from a YAML column entry."""
    if not isinstance(entry, dict):
        raise SchemaError(f"field #{index} in {path} must be a mapping")
    if "type" not in entry:
        # Without the shorthand, only the structural keys are meaningful, so
        # an unrecognized key is a typo and should be reported as one. With
        # the shorthand, sibling keys are the check's own arguments and are
        # validated when the validator is constructed instead.
        _reject_unknown(entry, {"name", "description", "checks", "optional",
                                "default", "python", "unique"}, f"field #{index}")
    name = entry.get("name")
    if not name:
        raise SchemaError(f"field #{index} in {path} is missing a 'name'")

    checks: List[Validator] = []
    if entry.get("python"):
        checks.append(_load_python_validator(entry["python"], path))
    for spec in _as_check_list(entry, index):
        checks.append(_build_field_check(spec, name, path))

    if entry.get("optional"):
        if not checks:
            checks = [Str()]
        inner = checks[0] if len(checks) == 1 else AllOf(*checks)
        checks = [Optional_(inner, default=entry.get("default"))]

    return Field(
        name=str(name),
        validators=checks,
        description=str(entry.get("description") or ""),
        required=not entry.get("optional", False),
        unique=bool(entry.get("unique", False)),
    )


def _as_check_list(entry: Dict[str, Any], index: int) -> List[Any]:
    """Normalize the two accepted spellings of a field's checks.

    A field may use the shorthand ``type: int`` with sibling arguments, or
    the explicit ``checks:`` list. Both produce the same validator list.
    """
    if "checks" in entry and entry.get("type"):
        raise SchemaError(
            f"field #{index} sets both 'type' and 'checks'; use one or the other"
        )
    if "checks" in entry:
        checks = entry["checks"]
        if not isinstance(checks, list):
            raise SchemaError(f"field #{index} 'checks' must be a list")
        return checks
    if entry.get("type"):
        shorthand = {k: v for k, v in entry.items()
                     if k not in ("name", "description", "optional", "default",
                                  "python", "unique")}
        return [shorthand]
    return []


def _build_field_check(spec: Any, field_name: str, path: str) -> Validator:
    """Build one field validator from a YAML check entry."""
    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, dict):
        raise SchemaError(f"check on field '{field_name}' must be a mapping or a type name")
    kind = spec.get("type")
    if not kind:
        raise SchemaError(f"check on field '{field_name}' is missing 'type'")
    if kind in ("all_of", "any_of"):
        children = [_build_field_check(child, field_name, path)
                    for child in spec.get("of") or []]
        if not children:
            raise SchemaError(f"'{kind}' on field '{field_name}' requires an 'of' list")
        cls = AllOf if kind == "all_of" else AnyOf
        return cls(*children, description=spec.get("description"))
    if kind not in FIELD_VALIDATORS:
        raise SchemaError(
            f"unknown check type '{kind}' on field '{field_name}'; "
            f"known types are {sorted(FIELD_VALIDATORS)}"
        )
    kwargs = {k: v for k, v in spec.items() if k not in ("type", "of")}
    try:
        return FIELD_VALIDATORS[kind](**kwargs)
    except TypeError as exc:
        raise SchemaError(
            f"bad arguments for check '{kind}' on field '{field_name}': {exc}"
        ) from exc
    except ValueError as exc:
        raise SchemaError(
            f"invalid check '{kind}' on field '{field_name}': {exc}"
        ) from exc


def _build_row_check(spec: Any, path: str) -> RowValidator:
    """Build one row validator from a YAML row_checks entry."""
    if not isinstance(spec, dict):
        raise SchemaError(f"row check in {path} must be a mapping")
    kind = spec.get("type")
    if kind == "python":
        return _load_python_row_validator(spec, path)
    if kind not in ROW_VALIDATORS:
        raise SchemaError(
            f"unknown row check '{kind}'; known types are "
            f"{[*sorted(ROW_VALIDATORS), 'python']}"
        )
    kwargs = {k: v for k, v in spec.items() if k != "type"}
    try:
        return ROW_VALIDATORS[kind](**kwargs)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"invalid row check '{kind}': {exc}") from exc


def _build_file_check(spec: Any, path: str) -> FileValidator:
    """Build one file validator from a YAML file_checks entry."""
    if not isinstance(spec, dict):
        raise SchemaError(f"file check in {path} must be a mapping")
    kind = spec.get("type")
    if kind not in FILE_VALIDATORS:
        raise SchemaError(
            f"unknown file check '{kind}'; known types are {sorted(FILE_VALIDATORS)}"
        )
    kwargs = {k: v for k, v in spec.items() if k != "type"}
    try:
        return FILE_VALIDATORS[kind](**kwargs)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"invalid file check '{kind}': {exc}") from exc



def _load_python_validator(spec: Any, path: str) -> Validator:
    """Load a Validator instance from a "module.py:name" reference."""
    obj = _load_python_object(spec, path)
    if not isinstance(obj, Validator):
        raise SchemaError(f"python check {spec!r} did not yield a Validator")
    return obj


def _load_python_row_validator(spec: Dict[str, Any], path: str) -> RowValidator:
    """Load a RowValidator instance from a "module.py:name" reference."""
    obj = _load_python_object(spec.get("ref"), path)
    if not isinstance(obj, RowValidator):
        raise SchemaError(f"python row check {spec.get('ref')!r} did not yield a RowValidator")
    return obj


def _load_python_object(reference: Any, schema_path: str) -> Any:
    """Resolve a "path/to/module.py:attribute" reference to a live object.

    References are resolved relative to the schema file, so a schema and its
    custom validators travel together.
    """
    if not isinstance(reference, str) or ":" not in reference:
        raise SchemaError(
            f"python reference must be 'file.py:name', got {reference!r}"
        )
    module_path, _, attribute = reference.rpartition(":")
    if not os.path.isabs(module_path):
        module_path = os.path.join(os.path.dirname(os.path.abspath(schema_path)), module_path)
    if not os.path.isfile(module_path):
        raise SchemaError(f"python reference file not found: {module_path}")
    spec = importlib.util.spec_from_file_location("jobchain_user_checks", module_path)
    if spec is None or spec.loader is None:
        raise SchemaError(f"could not load python reference: {module_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise SchemaError(f"error executing {module_path}: {exc}") from exc
    if not hasattr(module, attribute):
        raise SchemaError(f"{module_path} has no attribute '{attribute}'")
    return getattr(module, attribute)


def apply_base_dir(schema: Schema, base_dir: str) -> None:
    """Anchor every path-valued check in a schema to a base directory.

    Path validators are discovered by walking the schema's field validators,
    including inside the Optional, AllOf, and AnyOf wrappers, so a path check
    behaves identically however it was composed.

    This is applied once, after loading, using the parameter file's own
    directory. Without it a relative path in the file would resolve against
    whichever directory a command happened to be run from, so the same file
    could validate on one invocation and fail on the next.
    """
    for column in schema.fields:
        for validator in column.validators:
            _anchor(validator, base_dir)


def _anchor(validator: Validator, base_dir: str) -> None:
    """Set base_dir on a validator and on anything it wraps."""
    if hasattr(validator, "base_dir"):
        validator.base_dir = base_dir
    inner = getattr(validator, "inner", None)
    if inner is not None:
        _anchor(inner, base_dir)
    for child in getattr(validator, "validators", ()):
        _anchor(child, base_dir)
