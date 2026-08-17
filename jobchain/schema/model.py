"""The Field/Schema model, and the base class for Python-defined schemas."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple

from ..core import SchemaError
from .validators import (
    FileValidator,
    PredicateFile,
    PredicateRow,
    RowValidator,
    Unique,
    Validator,
    _anchor,
)


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
