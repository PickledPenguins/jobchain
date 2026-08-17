"""Format definition: validators, the Field/Schema model, and the loader.

A schema describes one parameter-file format completely: how to split lines
into fields, what each column must contain, what relationships must hold
within a row and across the file, and what job resources each row implies.
Changing formats therefore means pointing at a different schema file, never
editing this tool.

This package mirrors the module's own documented two-part structure plus a
third, genuinely distinct concern split out from it:

* ``validators`` -- the Field/Row/File validator class hierarchies and their
  keyword registries. Validators come in three tiers, applied in order by
  the scan: field validators inspect one value in one row (and may
  normalize it); row validators inspect a whole record once its fields have
  converted; file validators inspect every record together, for constraints
  such as uniqueness that cannot be seen one row at a time.
* ``model`` -- Field, Schema, and SchemaBase: what a valid file looks like,
  independent of where that description came from.
* ``loaders`` -- turning YAML (or an inline mapping) into a Schema.
* ``dynamic`` -- loading schemas, validators, and row checks from
  user-written Python files by path reference.

Everything reachable as ``jobchain.schema.X`` before the split still is.
"""

from __future__ import annotations

import importlib.util
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Callable, ClassVar, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core import SchemaError, reject_unknown_keys
from .dynamic import (
    _load_python_object,
    _load_python_row_validator,
    _load_python_schema,
    _load_python_validator,
    _load_schema_class,
    import_module_from_path,
)
from .loaders import (
    _DELIMITER_ALIASES,
    _as_check_list,
    _as_optional_str,
    _build_field,
    _build_field_check,
    _build_file_check,
    _build_row_check,
    _build_schema,
    _load_yaml_schema,
    _resolve_delimiter,
    load_schema,
    load_schema_source,
)
from .model import Field, Schema, SchemaBase, apply_base_dir
from .validators import (
    _FALSE_WORDS,
    _FLOAT_RE,
    _INT_RE,
    _TRUE_WORDS,
    FIELD_VALIDATORS,
    FILE_VALIDATORS,
    ROW_VALIDATORS,
    AllOf,
    AnyOf,
    Bool,
    CheckResult,
    Comparison,
    Exact,
    FileValidator,
    Float,
    Int,
    OneOf,
    Optional_,
    OutputPath,
    PathExists,
    PredicateFile,
    PredicateRow,
    Regex,
    RequiredWhen,
    RowCount,
    RowValidator,
    Str,
    Unique,
    Validator,
    _anchor,
    resolve_path,
)

#: The complete pre-split public surface of jobchain.schema, private helpers
#: included: tests reach jobchain.schema.<name> directly (both as attribute
#: calls and as mock.patch targets against os/importlib) rather than through
#: a submodule. Listed explicitly so every name's re-export is intentional,
#: not an artifact ruff's "unused import" check would otherwise flag.
__all__ = [
    "ABC", "AllOf", "Any", "AnyOf", "Bool", "Callable", "CheckResult",
    "ClassVar", "Comparison", "Dict", "Exact", "FIELD_VALIDATORS",
    "FILE_VALIDATORS", "Field", "FileValidator", "Float", "Int", "Iterable",
    "List", "OneOf", "Optional", "Optional_", "OutputPath", "PathExists",
    "PredicateFile", "PredicateRow", "ROW_VALIDATORS", "Regex",
    "RequiredWhen", "RowCount", "RowValidator", "Schema", "SchemaBase",
    "SchemaError", "Sequence", "Str", "Tuple", "Unique", "Validator",
    "_DELIMITER_ALIASES", "_FALSE_WORDS", "_FLOAT_RE", "_INT_RE",
    "_TRUE_WORDS", "_anchor", "_as_check_list", "_as_optional_str",
    "_build_field", "_build_field_check", "_build_file_check",
    "_build_row_check", "_build_schema", "_load_python_object",
    "_load_python_row_validator", "_load_python_schema",
    "_load_python_validator", "_load_schema_class", "_load_yaml_schema",
    "_resolve_delimiter", "abstractmethod", "apply_base_dir", "dataclass",
    "dc_field", "importlib", "import_module_from_path", "load_schema",
    "load_schema_source", "os", "re", "reject_unknown_keys", "resolve_path",
]
