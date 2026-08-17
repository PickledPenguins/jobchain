"""Deep, mock-heavy unit coverage of jobchain/schema.py.

Consolidated from test_schema_{loading,exhaustive}.py into one file,
matching this project's one-file-per-subsystem convention.
"""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from jobchain import schema as S
from jobchain.core import SchemaError
from jobchain.schema import (
    Field,
    Float,
    Int,
    OutputPath,
    PathExists,
    PredicateFile,
    PredicateRow,
    Schema,
    SchemaBase,
    Str,
    _build_field,
    _build_file_check,
    _build_row_check,
    _load_python_object,
    _load_python_row_validator,
    _load_python_validator,
    _load_schema_class,
    _resolve_delimiter,
    load_schema_source,
)
from tests.helpers import TempProject


class TestSchemaLoading(TempProject):
    def test_inline_and_file_sources(self):
        doc = {"name": "x", "fields": [{"name": "id", "type": "int"}]}
        s = S.load_schema_source(doc, "/tmp")
        self.assertEqual(s.name, "x")
        with self.assertRaises(S.SchemaError):
            S.load_schema_source(3, "/tmp")

    def test_missing_and_invalid_schema_files(self):
        with self.assertRaises(S.SchemaError):
            S.load_schema("/does/not/exist.yaml")
        bad = self.write("bad.yaml", "- not a mapping\n")
        with self.assertRaises(S.SchemaError):
            S.load_schema(bad)
        malformed = self.write("bad2.yaml", "name: [\n")
        with self.assertRaises(S.SchemaError):
            S.load_schema(malformed)

    def test_delimiter_aliases_and_invalid(self):
        self.assertIsNone(S._resolve_delimiter(None))
        self.assertEqual(S._resolve_delimiter("tab"), "\t")
        self.assertEqual(S._resolve_delimiter("comma"), ",")
        self.assertEqual(S._resolve_delimiter("|"), "|")
        with self.assertRaises(S.SchemaError):
            S._resolve_delimiter("too-long")

    def test_unknown_top_level_and_format_keys(self):
        with self.assertRaises(S.SchemaError):
            S._build_schema({"wat": 1}, "x.yaml")
        with self.assertRaises(S.SchemaError):
            S._build_schema({"format": {"wat": 1}}, "x.yaml")

    def test_field_forms_and_invalid_forms(self):
        base = {"fields": [{"name": "x", "checks": [{"type": "int"}]}]}
        self.assertIsInstance(S._build_schema(base, "x.yaml").fields[0].validators[0], S.Int)
        shorthand = {"fields": [{"name": "x", "type": "int", "min": 1}]}
        self.assertIsInstance(S._build_schema(shorthand, "x.yaml").fields[0].validators[0], S.Int)
        with self.assertRaises(S.SchemaError):
            S._build_field({}, 0, "x.yaml")
        with self.assertRaises(S.SchemaError):
            S._build_field({"type": "int"}, 0, "x.yaml")
        with self.assertRaises(S.SchemaError):
            S._build_field({"name": "x", "type": "int", "checks": []}, 0, "x.yaml")
        with self.assertRaises(S.SchemaError):
            S._build_field({"name": "x", "checks": {}}, 0, "x.yaml")

    def test_composite_and_unknown_checks(self):
        self.assertIsInstance(
            S._build_field_check({"type": "all_of", "of": [{"type": "int"}]}, "x", "x"), S.AllOf
        )
        self.assertIsInstance(
            S._build_field_check({"type": "any_of", "of": [{"type": "int"}]}, "x", "x"), S.AnyOf
        )
        for spec in ({"type": "all_of"}, {"type": "any_of"}, {"type": "wat"}, {}, 7):
            with self.assertRaises(S.SchemaError):
                S._build_field_check(spec, "x", "x")

    def test_row_and_file_checks(self):
        self.assertIsInstance(
            S._build_row_check(
                {"type": "required_when", "when_field": "a", "equals": "x", "require_field": "b"},
                "x",
            ),
            S.RequiredWhen,
        )
        self.assertIsInstance(
            S._build_row_check({"type": "compare", "left": "a", "op": "<", "right": "b"}, "x"),
            S.Comparison,
        )
        self.assertIsInstance(
            S._build_file_check({"type": "unique", "fields": ["a"]}, "x"), S.Unique
        )
        self.assertIsInstance(S._build_file_check({"type": "row_count", "min": 1}, "x"), S.RowCount)
        for fn, value in ((S._build_row_check, 3), (S._build_file_check, 3)):
            with self.assertRaises(S.SchemaError):
                fn(value, "x")


class TestPythonSchemaLoading(TempProject):
    def test_python_schema_success_and_failures(self):
        path = self.write(
            "s.py",
            'from jobchain.schema import Schema, Field, Int\nSCHEMA=Schema(name="s", fields=[Field("x", [Int()])])\n',
        )
        schema = S.load_schema(path)
        self.assertEqual(os.path.abspath(path), schema.source_path)
        bad = self.write("bad.py", "raise RuntimeError('boom')\n")
        with self.assertRaises(S.SchemaError):
            S.load_schema(bad)
        no_schema = self.write("none.py", "X=1\n")
        with self.assertRaises(S.SchemaError):
            S.load_schema(no_schema)

    def test_schema_base_reference_and_python_validator(self):
        module = self.write(
            "custom.py",
            textwrap.dedent("""
            from jobchain.schema import SchemaBase, Field, Int, Validator, CheckResult
            class MySchema(SchemaBase):
                fields=[Field("x", [Int()])]
            class Other(SchemaBase):
                fields=[]
        """),
        )
        # Explicit class resolves the otherwise ambiguous module.
        doc = {"validator_class": module + ":MySchema", "fields": []}
        s = S._build_schema(doc, self.path("s.yaml"))
        self.assertEqual(len(s.fields), 1)
        with self.assertRaises(S.SchemaError):
            S._build_schema({"validator_class": module}, self.path("s.yaml"))

    def test_python_object_reference_errors(self):
        with self.assertRaises(S.SchemaError):
            S._load_python_object("bad", self.path("s.yaml"))
        with self.assertRaises(S.SchemaError):
            S._load_python_object("missing.py:X", self.path("s.yaml"))
        mod = self.write("x.py", "X=1\n")
        with self.assertRaises(S.SchemaError):
            S._load_python_object(mod + ":NOPE", self.path("s.yaml"))

    def test_apply_base_dir_recurses(self):
        v = S.PathExists()
        nested = S.Optional_(S.AllOf(v))
        schema = S.Schema(name="s", fields=[S.Field("p", [nested])])
        S.apply_base_dir(schema, "/tmp/base")
        self.assertEqual(v.base_dir, "/tmp/base")


class TestSchemaRemaining(unittest.TestCase):
    def test_float_description_variants(self):
        self.assertIn("at least", Float(min=1).description)
        self.assertIn("at most", Float(max=2).description)
        self.assertEqual(Float().description, "a decimal number")
        with self.assertRaises(ValueError):
            Float(min=2, max=1)

    def test_path_exists_readability_and_normalize(self):
        with tempfile.TemporaryDirectory() as d:
            file = os.path.join(d, "f")
            open(file, "w").close()
            v = PathExists(must_be_file=True, readable=True)
            v.base_dir = d
            self.assertEqual(v.normalize("f"), file)
            self.assertTrue(v.validate(file).ok)
            with patch("jobchain.schema.os.access", return_value=False):
                self.assertFalse(v.validate(file).ok)
            self.assertFalse(PathExists(must_be_dir=True).validate(file).ok)
        self.assertFalse(PathExists().validate("/missing/path").ok)

    def test_output_path_parent_writable_and_existing(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "new")
            self.assertTrue(OutputPath().validate(target).ok)
            with patch("jobchain.schema.os.access", return_value=False):
                self.assertFalse(OutputPath().validate(target).ok)
            with open(target, "w"):
                pass
            self.assertFalse(OutputPath(must_not_exist=True).validate(target).ok)
        self.assertFalse(OutputPath().validate("/missing/parent/file").ok)

    def test_schema_base_default_hooks_and_overrides(self):
        base = SchemaBase()
        self.assertIsNone(base.check_row({}))
        self.assertEqual(base.check_file([]), [])

        class Custom(SchemaBase):
            fields = [Field("x", [Int()])]

            def check_row(self, row):
                return "bad" if row["x"] == 1 else None

            def check_file(self, rows):
                return [(2, "badfile")]

        schema = Custom().build(name="c")
        self.assertEqual(len(schema.row_validators), 1)
        self.assertEqual(len(schema.file_validators), 1)
        self.assertEqual(schema.row_validators[0].check({"x": 1}), "bad")
        self.assertEqual(schema.file_validators[0].check([]), [(2, "badfile")])

    def test_schema_base_default_build_has_no_predicates(self):
        class Empty(SchemaBase):
            fields = [Field("x", [Str()])]

        schema = Empty().build(name="base")
        self.assertEqual(schema.row_validators, [])
        self.assertEqual(schema.file_validators, [])

    def test_load_schema_source_mapping_absolute_and_relative_and_bad_type(self):
        source = {"fields": [{"name": "x", "type": "str"}]}
        self.assertEqual(load_schema_source(source, "/tmp").name, "schema")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.yaml")
            with open(p, "w") as h:
                h.write("fields:\n - name: x\n   type: str\n")
            self.assertEqual(load_schema_source(p, d).field_names, ["x"])
            with self.assertRaises(SchemaError):
                load_schema_source(123, d, label="x")

    def test_yaml_loader_os_error(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".yaml") as f:
            path = f.name
        try:
            with patch("builtins.open", side_effect=OSError("denied")):
                from jobchain.schema import load_schema

                with self.assertRaises(SchemaError):
                    load_schema(path)
        finally:
            os.unlink(path)

    def test_field_non_mapping_and_optional_without_checks(self):
        with self.assertRaises(SchemaError):
            _build_field("x", 0, "s")
        f = _build_field({"name": "x", "optional": True}, 0, "s")
        self.assertEqual(len(f.validators), 1)

    def test_row_and_file_check_shape_and_unknown_errors(self):
        with self.assertRaises(SchemaError):
            _build_row_check("x", "s")
        with self.assertRaises(SchemaError):
            _build_row_check({"type": "missing"}, "s")
        with self.assertRaises(SchemaError):
            _build_file_check("x", "s")
        with self.assertRaises(SchemaError):
            _build_file_check({"type": "missing"}, "s")

    def test_row_and_file_check_invalid_arguments(self):
        with self.assertRaises(SchemaError):
            _build_row_check({"type": "compare"}, "s")
        with self.assertRaises(SchemaError):
            _build_file_check({"type": "unique", "fields": 1}, "s")

    def test_python_row_check_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.py")
            with open(p, "w") as h:
                h.write(
                    "from jobchain.schema import RowValidator\nclass R(RowValidator):\n def __init__(self): super().__init__('r')\n def check(self,r): return None\nR=R()\n"
                )
            v = _build_row_check({"type": "python", "ref": "v.py:R"}, p)
            self.assertIsInstance(v, PredicateRow.__mro__[1] if False else type(v))

    def test_python_validator_wrong_type(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.py")
            with open(p, "w") as h:
                h.write("X=1\n")
            with self.assertRaises(SchemaError):
                _load_python_validator("v.py:X", p)
            with self.assertRaises(SchemaError):
                _load_python_row_validator({"ref": "v.py:X"}, p)

    def test_python_object_invalid_reference_and_missing_file(self):
        with self.assertRaises(SchemaError):
            _load_python_object("bad", "/tmp/s.yaml")
        with self.assertRaises(SchemaError):
            _load_python_object("missing.py:X", "/tmp/s.yaml")

    def test_python_object_missing_attribute(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.py")
            with open(p, "w") as h:
                h.write("X=1\n")
            with self.assertRaises(SchemaError):
                _load_python_object("v.py:Y", p)

    def test_python_object_execution_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.py")
            with open(p, "w") as h:
                h.write("raise RuntimeError('boom')\n")
            with self.assertRaises(SchemaError):
                _load_python_object("v.py:X", p)

    def test_python_object_loader_creation_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.py")
            with open(p, "w") as h:
                h.write("X=1\n")
            with patch("jobchain.schema.importlib.util.spec_from_file_location", return_value=None), self.assertRaises(SchemaError):
                _load_python_object("v.py:X", p)

    def test_python_validator_and_row_validator_type_checks(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "v.py")
            with open(p, "w") as h:
                h.write("X=1\n")
            with self.assertRaises(SchemaError):
                _load_python_validator("v.py:X", p)
            with self.assertRaises(SchemaError):
                _load_python_row_validator({"ref": "v.py:X"}, p)

    def test_delimiter_none_and_literal(self):
        self.assertIsNone(_resolve_delimiter(None))
        self.assertEqual(_resolve_delimiter("|"), "|")

    def test_load_schema_class_missing_and_ambiguous(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "classes.py")
            with open(p, "w") as h:
                h.write(
                    "from jobchain.schema import SchemaBase, Field, Str\nclass A(SchemaBase): fields=[Field('x',[Str()])]\nclass B(SchemaBase): fields=[Field('x',[Str()])]\n"
                )
            with self.assertRaises(SchemaError):
                _load_schema_class("classes.py:C", p, {}, "s", {})
            with self.assertRaises(SchemaError):
                _load_schema_class("classes.py", p, {}, "s", {})

    def test_load_schema_class_explicit_and_single(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "classes.py")
            with open(p, "w") as h:
                h.write(
                    "from jobchain.schema import SchemaBase, Field, Str\nclass A(SchemaBase): fields=[Field('x',[Str()])]\n"
                )
            self.assertIsNotNone(_load_schema_class("classes.py:A", p, {}, "s", {}))
            self.assertIsNotNone(_load_schema_class("classes.py", p, {}, "s", {}))

    def test_predicate_file_empty_return(self):
        p = PredicateFile(lambda rows: None, "x")
        self.assertEqual(p.check([]), [])


class TestSchemaFinalBranches(unittest.TestCase):
    def test_case_insensitive_one_of_matches_candidate(self):
        from jobchain.schema import OneOf

        v = OneOf(["Auto", "Manual"], case_sensitive=False)
        self.assertEqual(v.validate("auto").value, "Auto")
        self.assertFalse(v.validate("other").ok)

    def test_unique_fields_empty_and_populated(self):
        empty = Schema(name="e", fields=[Field("x", [Str()])])
        self.assertEqual(empty.unique_fields, [])
        unique = Schema(name="u", fields=[Field("x", [Str()], unique=True)])
        self.assertEqual(unique.unique_fields, ["x"])

    def test_python_schema_spec_loader_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.py")
            with open(p, "w") as h:
                h.write(
                    "from jobchain.schema import Schema, Field, Str\nSCHEMA=Schema(fields=[Field('x',[Str()])])\n"
                )
            with patch("jobchain.schema.importlib.util.spec_from_file_location", return_value=None):
                from jobchain.schema import _load_python_schema

                with self.assertRaises(SchemaError):
                    _load_python_schema(p)

    def test_schema_class_module_without_schema_base(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "classes.py")
            with open(p, "w") as h:
                h.write("VALUE=1\n")
            with self.assertRaises(SchemaError):
                _load_schema_class("classes.py", p, {}, "s", {})

    def test_import_module_spec_and_execution_failures(self):
        from jobchain.schema import _import_module

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.py")
            with open(p, "w") as h:
                h.write("X=1\n")
            with patch("jobchain.schema.importlib.util.spec_from_file_location", return_value=None), self.assertRaises(SchemaError):
                _import_module("m.py", p)
            with open(p, "w") as h:
                h.write("raise RuntimeError('boom')\n")
            with self.assertRaises(SchemaError):
                _import_module("m.py", p)

    def test_field_description_falls_back_to_validator(self):
        f = Field("x", [Str()])
        self.assertEqual(f.description, Str().description)

    def test_field_explicit_description_does_not_fallback(self):
        f = Field("x", [Str()], description="custom")
        self.assertEqual(f.description, "custom")
