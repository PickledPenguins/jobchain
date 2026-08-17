"""Loading schemas, validators, and row checks from user-written Python.

Every function here resolves a "path/to/module.py[:attribute]" reference
relative to the schema file that named it, so a schema and its custom code
travel together regardless of the current working directory.
"""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Dict

from ..core import SchemaError
from .model import Schema, SchemaBase
from .validators import RowValidator, Validator


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


def _load_schema_class(reference: str, schema_path: str, fmt: Dict[str, Any],
                       name: str, document: Dict[str, Any]) -> Schema:
    """Build a Schema from a SchemaBase subclass in a module.

    The module must define exactly one SchemaBase subclass, or the reference
    may name one explicitly as "file.py:ClassName".
    """
    from .loaders import _as_optional_str, _resolve_delimiter

    module_path, _, wanted = reference.partition(":")
    module = import_module_from_path(module_path, schema_path)
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


def import_module_from_path(module_path: str, relative_to: str):
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
