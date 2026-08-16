"""Focused unit tests for pipeline branches not covered by the public pipeline suite."""
from __future__ import annotations

import textwrap
import unittest

from jobchain.core import PipelineError
from jobchain.pipeline import (
    Bool, Choice, Integer, JobStage, Setting, Text, describe_pipeline,
    load_pipeline_source,
)
from tests.helpers import TempProject


class _Ctx:
    work_dir = "/tmp/work"

    def __init__(self):
        self.writes = []

    def expand(self, command, row):
        return command.replace("{x}", str(row["x"]))

    def directives(self, resources):
        return "# directives"

    def preamble(self):
        return "# preamble"

    def epilogue(self):
        return "# epilogue"

    def write(self, text):
        self.writes.append(text)
        return "/tmp/work/script.sh"


class TestSettingPrimitives(unittest.TestCase):
    def test_base_setting_accepts_any_value_and_describes_it(self):
        setting = Setting()
        value = object()
        self.assertIs(setting.check("x", value), value)
        self.assertEqual(setting.describe(), "any value")

    def test_choice_accepts_value(self):
        self.assertEqual(Choice([1, 2]).check("x", 2), 2)

    def test_bool_accepts_true_and_false(self):
        setting = Bool()
        self.assertTrue(setting.check("x", True))
        self.assertFalse(setting.check("x", False))

    def test_integer_bounds_are_independent(self):
        self.assertEqual(Integer(min=2).check("x", "2"), 2)
        self.assertEqual(Integer(max=2).check("x", "2"), 2)
        with self.assertRaises(PipelineError):
            Integer(min=2).check("x", 1)
        with self.assertRaises(PipelineError):
            Integer(max=2).check("x", 3)

    def test_integer_without_bounds_describes_as_integer(self):
        self.assertEqual(Integer().describe(), "an integer")

    def test_integer_bad_value_is_wrapped(self):
        with self.assertRaisesRegex(PipelineError, "must be an integer"):
            Integer().check("x", "nope")

    def test_text_converts_values(self):
        self.assertEqual(Text().check("x", 123), "123")


class PipelineDeepCase(TempProject):
    def build(self, document, construct=True):
        from jobchain.pipeline import load_pipeline_source
        pipeline = load_pipeline_source(document, self.tmp)
        if construct:
            pipeline.construct(object())
        return pipeline


class TestPipelineLoadingBranches(PipelineDeepCase):
    def test_non_mapping_source_is_rejected(self):
        with self.assertRaisesRegex(PipelineError, "mapping or a path"):
            self.build(42, construct=False)

    def test_pipeline_file_with_non_mapping_document_is_rejected(self):
        self.write("bad.yaml", "- not a mapping\n")
        with self.assertRaisesRegex(PipelineError, "YAML mapping"):
            self.build("bad.yaml", construct=False)

    def test_non_mapping_defaults_are_rejected(self):
        with self.assertRaisesRegex(PipelineError, "defaults.*mapping"):
            self.build({"stages": [{"name": "a", "command": "true"}],
                        "defaults": "bad"}, construct=False)

    def test_non_mapping_stage_is_rejected(self):
        with self.assertRaisesRegex(PipelineError, "stage #1 must be a mapping"):
            self.build({"stages": ["bad"]}, construct=False)

    def test_missing_stage_name_is_rejected(self):
        with self.assertRaisesRegex(PipelineError, "missing a 'name'"):
            self.build({"stages": [{"command": "true"}]}, construct=False)

    def test_explicit_afterany_is_preserved(self):
        p = self.build({"stages": [
            {"name": "a", "command": "true"},
            {"name": "b", "command": "true", "depends": "AFTERANY"},
        ]}, construct=False)
        self.assertEqual(p.spec("b").depends, "afterany")
        self.assertTrue(p.spec("b").depends_explicit)

    def test_stage_lookup_failure_names_pipeline(self):
        p = self.build({"name": "demo", "stages": [{"name": "a", "command": "true"}]}, construct=False)
        with self.assertRaisesRegex(PipelineError, "no stage named 'missing'.*demo"):
            p.stage("missing")
        with self.assertRaisesRegex(PipelineError, "no stage named 'missing'.*demo"):
            p.spec("missing")

    def test_chaining_stage_property_fallback_is_safe_for_unresolved_specs(self):
        p = self.build({"stages": [{"name": "a", "command": "true"}]}, construct=False)
        p.specs[0].chains_next = False
        self.assertEqual(p.chaining_stage, "a")

    def test_stage_module_import_failure_is_propagated(self):
        with self.assertRaises(Exception):
            self.build({"stage_module": "missing_module.py",
                        "stages": [{"name": "a"}]})

    def test_missing_class_without_command_reports_no_module_classes(self):
        self.write("empty.py", "x = 1\n")
        with self.assertRaisesRegex(PipelineError, "no class 'A'.*no 'command'"):
            self.build({"stage_module": "empty.py", "stages": [{"name": "a"}]})


class TestJobStageDefaults(unittest.TestCase):
    def test_default_helpers(self):
        stage = JobStage("x", {"_position": 3}, object())
        row = {"x": 7}
        self.assertEqual(stage.resources(row), {})
        ctx = _Ctx()
        self.assertEqual(stage.output_dir(row, ctx), ctx.work_dir)
        self.assertEqual(stage.script_name(row), "03-x.sh")
        self.assertEqual(repr(stage), "<JobStage stage 'x'>")

    def test_default_write_script_requires_command(self):
        stage = JobStage("x", {"_position": 1}, object())
        with self.assertRaisesRegex(PipelineError, "no write_script implementation"):
            stage.write_script({}, _Ctx())

    def test_default_write_script_renders_command(self):
        stage = JobStage("x", {"_position": 1, "command": "echo {x}",
                                "walltime": "00:01:00", "env": {"A": "B"}}, object())
        ctx = _Ctx()
        path = stage.write_script({"x": 4}, ctx)
        self.assertEqual(path, "/tmp/work/script.sh")
        self.assertIn("echo 4", ctx.writes[0])
        self.assertIn("exit $rc", ctx.writes[0])

    def test_effective_resources_ignores_none_overrides_and_copies_mutables(self):
        class Stage(JobStage):
            def resources(self, row):
                return {"ncpus": None}
        stage = Stage("x", {"_position": 1, "ncpus": 4,
                            "extra_directives": ["-x"], "env": {"A": "B"}}, object())
        result = stage.effective_resources({})
        self.assertEqual(result["ncpus"], 4)
        self.assertEqual(result["extra_directives"], ["-x"])
        self.assertEqual(result["env"], {"A": "B"})
        self.assertIsNot(result["env"], stage.config["env"])


class TestPipelineDescription(PipelineDeepCase):
    def test_description_includes_dependencies_and_chaining(self):
        p = self.build({"stages": [
            {"name": "a", "command": "true"},
            {"name": "b", "command": "true", "depends": "afterany"},
        ]}, construct=False)
        lines = describe_pipeline(p)
        self.assertEqual(len(lines), 2)
        self.assertIn("a", lines[0])
        self.assertIn("b", lines[1])
        self.assertIn("afterany", lines[1])
        self.assertIn("chains next", lines[1])

    def test_description_handles_first_stage_explicit_chaining(self):
        p = self.build({"stages": [
            {"name": "a", "command": "true", "chains_next": True},
            {"name": "b", "command": "true", "depends": "afterok"},
        ]}, construct=False)
        lines = describe_pipeline(p)
        self.assertIn("chains next", lines[0])
        self.assertNotIn("afterok", lines[0])


if __name__ == "__main__":
    unittest.main()
