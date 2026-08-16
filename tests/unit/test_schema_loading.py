import os
import textwrap
import unittest
from jobchain import schema as S
from tests.helpers import TempProject


class TestSchemaLoading(TempProject):
    def test_inline_and_file_sources(self):
        doc = {"name":"x", "fields":[{"name":"id","type":"int"}]}
        s = S.load_schema_source(doc, "/tmp")
        self.assertEqual(s.name, "x")
        with self.assertRaises(S.SchemaError):
            S.load_schema_source(3, "/tmp")

    def test_missing_and_invalid_schema_files(self):
        with self.assertRaises(S.SchemaError):
            S.load_schema("/does/not/exist.yaml")
        bad = self.write("bad.yaml", "- not a mapping\n")
        with self.assertRaises(S.SchemaError): S.load_schema(bad)
        malformed = self.write("bad2.yaml", "name: [\n")
        with self.assertRaises(S.SchemaError): S.load_schema(malformed)

    def test_delimiter_aliases_and_invalid(self):
        self.assertIsNone(S._resolve_delimiter(None))
        self.assertEqual(S._resolve_delimiter("tab"), "\t")
        self.assertEqual(S._resolve_delimiter("comma"), ",")
        self.assertEqual(S._resolve_delimiter("|"), "|")
        with self.assertRaises(S.SchemaError): S._resolve_delimiter("too-long")

    def test_unknown_top_level_and_format_keys(self):
        with self.assertRaises(S.SchemaError):
            S._build_schema({"wat": 1}, "x.yaml")
        with self.assertRaises(S.SchemaError):
            S._build_schema({"format":{"wat":1}}, "x.yaml")

    def test_field_forms_and_invalid_forms(self):
        base = {"fields":[{"name":"x", "checks":[{"type":"int"}]}]}
        self.assertIsInstance(S._build_schema(base, "x.yaml").fields[0].validators[0], S.Int)
        shorthand = {"fields":[{"name":"x", "type":"int", "min":1}]}
        self.assertIsInstance(S._build_schema(shorthand, "x.yaml").fields[0].validators[0], S.Int)
        with self.assertRaises(S.SchemaError): S._build_field({}, 0, "x.yaml")
        with self.assertRaises(S.SchemaError): S._build_field({"type":"int"}, 0, "x.yaml")
        with self.assertRaises(S.SchemaError): S._build_field({"name":"x", "type":"int", "checks":[]}, 0, "x.yaml")
        with self.assertRaises(S.SchemaError): S._build_field({"name":"x", "checks":{}}, 0, "x.yaml")

    def test_composite_and_unknown_checks(self):
        self.assertIsInstance(S._build_field_check({"type":"all_of", "of":[{"type":"int"}]}, "x", "x"), S.AllOf)
        self.assertIsInstance(S._build_field_check({"type":"any_of", "of":[{"type":"int"}]}, "x", "x"), S.AnyOf)
        for spec in ({"type":"all_of"}, {"type":"any_of"}, {"type":"wat"}, {}, 7):
            with self.assertRaises(S.SchemaError): S._build_field_check(spec, "x", "x")

    def test_row_and_file_checks(self):
        self.assertIsInstance(S._build_row_check({"type":"required_when","when_field":"a","equals":"x","require_field":"b"}, "x"), S.RequiredWhen)
        self.assertIsInstance(S._build_row_check({"type":"compare","left":"a","op":"<","right":"b"}, "x"), S.Comparison)
        self.assertIsInstance(S._build_file_check({"type":"unique","fields":["a"]}, "x"), S.Unique)
        self.assertIsInstance(S._build_file_check({"type":"row_count","min":1}, "x"), S.RowCount)
        for fn, value in ((S._build_row_check, 3), (S._build_file_check, 3)):
            with self.assertRaises(S.SchemaError): fn(value, "x")


class TestPythonSchemaLoading(TempProject):
    def test_python_schema_success_and_failures(self):
        path = self.write("s.py", 'from jobchain.schema import Schema, Field, Int\nSCHEMA=Schema(name="s", fields=[Field("x", [Int()])])\n')
        schema = S.load_schema(path)
        self.assertEqual(os.path.abspath(path), schema.source_path)
        bad = self.write("bad.py", "raise RuntimeError('boom')\n")
        with self.assertRaises(S.SchemaError): S.load_schema(bad)
        no_schema = self.write("none.py", "X=1\n")
        with self.assertRaises(S.SchemaError): S.load_schema(no_schema)

    def test_schema_base_reference_and_python_validator(self):
        module = self.write("custom.py", textwrap.dedent('''
            from jobchain.schema import SchemaBase, Field, Int, Validator, CheckResult
            class MySchema(SchemaBase):
                fields=[Field("x", [Int()])]
            class Other(SchemaBase):
                fields=[]
        '''))
        # Explicit class resolves the otherwise ambiguous module.
        doc = {"validator_class": module + ":MySchema", "fields": []}
        s = S._build_schema(doc, self.path("s.yaml"))
        self.assertEqual(len(s.fields), 1)
        with self.assertRaises(S.SchemaError):
            S._build_schema({"validator_class": module}, self.path("s.yaml"))

    def test_python_object_reference_errors(self):
        with self.assertRaises(S.SchemaError): S._load_python_object("bad", self.path("s.yaml"))
        with self.assertRaises(S.SchemaError): S._load_python_object("missing.py:X", self.path("s.yaml"))
        mod = self.write("x.py", "X=1\n")
        with self.assertRaises(S.SchemaError): S._load_python_object(mod + ":NOPE", self.path("s.yaml"))

    def test_apply_base_dir_recurses(self):
        v = S.PathExists()
        nested = S.Optional_(S.AllOf(v))
        schema = S.Schema(name="s", fields=[S.Field("p", [nested])])
        S.apply_base_dir(schema, "/tmp/base")
        self.assertEqual(v.base_dir, "/tmp/base")
