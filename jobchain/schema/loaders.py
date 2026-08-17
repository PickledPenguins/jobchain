"""Building a Schema from YAML or an inline mapping."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..core import SchemaError, reject_unknown_keys
from .dynamic import (
    _load_python_row_validator,
    _load_python_schema,
    _load_python_validator,
    _load_schema_class,
)
from .model import Field, Schema
from .validators import (
    FIELD_VALIDATORS,
    FILE_VALIDATORS,
    ROW_VALIDATORS,
    AllOf,
    AnyOf,
    FileValidator,
    Optional_,
    RowValidator,
    Str,
    Validator,
)

_DELIMITER_ALIASES = {
    "comma": ",",
    "tab": "\t",
    "pipe": "|",
    "colon": ":",
    "semicolon": ";",
    "space": " ",
    "whitespace": None,   # None means "split on any run of whitespace"
}


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

    reject_unknown_keys(document, {
        "name", "version", "description", "format", "fields",
        "row_checks", "file_checks", "validator_class",
    }, "top level", SchemaError)

    name = document.get("name") or "schema"

    fmt = document.get("format") or {}
    reject_unknown_keys(fmt, {"delimiter", "header", "comment", "quoting", "id_field"},
                        "format", SchemaError)

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
        reject_unknown_keys(entry, {"name", "description", "checks", "optional",
                                    "default", "python", "unique"}, f"field #{index}",
                            SchemaError)
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
