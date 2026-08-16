"""Tests for the run configuration: loading, merging, templates, and capture."""

from __future__ import annotations

import unittest
from typing import ClassVar

from jobchain.config import (
    RunConfig,
    expand_run_name,
    expand_template,
    load_config,
    render_final_config,
    template_is_generation_aware,
)
from jobchain.core import ConfigError, UsageError
from tests.helpers import TempProject

MINIMAL = """\
name: demo
params: params.psv
schema:
  name: s
  fields:
    - {name: a, type: str}
"""


class TestLoading(TempProject):
    def load(self, text: str = MINIMAL, **overrides) -> RunConfig:
        path = self.write("config.yaml", text)
        return load_config(path, overrides or None)

    def test_minimal_configuration(self):
        config = self.load()
        self.assertEqual(config.name, "demo")
        self.assertEqual(config.params_path, self.path("params.psv"))
        self.assertIsNone(config.pipeline_source)

    def test_defaults(self):
        config = self.load()
        self.assertEqual(config.width, 1)
        self.assertEqual(config.scheduler, "pbs")
        self.assertFalse(config.strict)
        self.assertEqual(config.max_attempts, 0)
        self.assertEqual(config.terminal_level, "info")
        self.assertEqual(config.file_level, "debug")

    def test_scheduler_is_never_detected(self):
        # Detection would guess, and a wrong guess produces scripts whose
        # directives the other scheduler silently ignores.
        self.assertEqual(self.load().scheduler, "pbs")

    def test_max_attempts_has_no_cap_by_default(self):
        self.assertEqual(self.load().max_attempts, 0)

    def test_missing_required_keys(self):
        for missing in ("name", "params", "schema"):
            text = "\n".join(line for line in MINIMAL.splitlines()
                             if not line.startswith(missing))
            with self.subTest(missing=missing), self.assertRaises(ConfigError):
                self.load(text if missing != "schema" else
                          "name: d\nparams: p.psv\n")

    def test_unknown_top_level_key_is_reported(self):
        with self.assertRaises(ConfigError) as caught:
            self.load(MINIMAL + "widht: 8\n")
        self.assertIn("widht", str(caught.exception))

    def test_unknown_logging_key_is_reported(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "logging:\n  terminal: info\n  colour: true\n")

    def test_invalid_yaml_is_reported(self):
        with self.assertRaises(ConfigError):
            self.load("name: [unclosed\n")

    def test_missing_file_is_reported(self):
        with self.assertRaises(ConfigError):
            load_config(self.path("absent.yaml"))

    def test_bad_width_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "width: 0\n")

    def test_bad_scheduler_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "scheduler: torque\n")

    def test_bad_log_level_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load(MINIMAL + "logging:\n  terminal: chatty\n")

    def test_bad_run_name_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load("name: has/slash\nparams: p\nschema: {name: s, fields: []}\n")


class TestOverrides(TempProject):
    def load(self, **overrides) -> RunConfig:
        path = self.write("config.yaml", MINIMAL + "width: 4\n")
        return load_config(path, overrides or None)

    def test_command_line_wins_over_the_file(self):
        self.assertEqual(self.load(width=16).width, 16)

    def test_absent_overrides_are_ignored(self):
        self.assertEqual(self.load(width=None).width, 4)

    def test_run_name_can_be_overridden(self):
        self.assertEqual(self.load(run_name="other").name, "other")

    def test_unknown_override_is_a_usage_error(self):
        with self.assertRaises(UsageError):
            self.load(nonsense=1)

    def test_provenance_records_where_each_value_came_from(self):
        config = self.load(width=16)
        self.assertEqual(config.provenance["width"], "cli")
        self.assertEqual(config.provenance["scheduler"], "default")


class TestRunNames(unittest.TestCase):
    def test_date_and_time_expand(self):
        expanded = expand_run_name("solver-{date}")
        self.assertTrue(expanded.startswith("solver-"))
        self.assertRegex(expanded, r"solver-\d{4}-\d{2}-\d{2}")

    def test_user_expands(self):
        self.assertNotIn("{user}", expand_run_name("sweep-{user}"))

    def test_a_plain_name_is_unchanged(self):
        self.assertEqual(expand_run_name("solver"), "solver")


class TestTemplates(unittest.TestCase):
    ROW: ClassVar[dict] = {"output_dir": "/scratch/beta", "threads": 32}

    def expand(self, template: str, **kwargs) -> str:
        options = {"row": self.ROW, "row_name": "000123", "row_index": 2,
                   "generation": 1}
        options.update(kwargs)
        return expand_template(template, "myrun", "/home/.jobchain/myrun", **options)

    def test_run_placeholders(self):
        self.assertEqual(self.expand("{run.name}"), "myrun")
        self.assertEqual(self.expand("{run.home}"), "/home/.jobchain/myrun")

    def test_row_placeholders(self):
        self.assertEqual(self.expand("{row.name}"), "000123")
        self.assertEqual(self.expand("{row.index}"), "2")
        self.assertEqual(self.expand("{row.generation}"), "1")

    def test_row_columns(self):
        self.assertEqual(self.expand("{row.output_dir}/out"), "/scratch/beta/out")
        self.assertEqual(self.expand("{row.threads}"), "32")

    def test_generation_varies(self):
        self.assertEqual(self.expand("g{row.generation}", generation=3), "g3")

    def test_unknown_column_is_an_error(self):
        # A path with a typo would otherwise be discovered only when a job
        # failed to write.
        with self.assertRaises(ConfigError) as caught:
            self.expand("{row.nosuch}")
        self.assertIn("nosuch", str(caught.exception))

    def test_unknown_namespace_is_an_error(self):
        with self.assertRaises(ConfigError):
            self.expand("{job.name}")

    def test_unknown_run_key_is_an_error(self):
        with self.assertRaises(ConfigError):
            self.expand("{run.nonsense}")

    def test_text_without_placeholders_is_unchanged(self):
        self.assertEqual(self.expand("/fixed/path"), "/fixed/path")

    def test_generation_awareness_is_detected(self):
        self.assertTrue(template_is_generation_aware("{row.output_dir}/g{row.generation}"))
        self.assertFalse(template_is_generation_aware("{row.output_dir}"))


class TestCapture(TempProject):
    def test_final_configuration_is_complete_and_runnable(self):
        path = self.write("config.yaml", MINIMAL + "width: 4\n")
        config = load_config(path, {"width": 16})
        text = render_final_config(config, {"name": "s", "fields": []}, None)
        self.write("final.yaml", text)

        reloaded = load_config(self.path("final.yaml"))
        self.assertEqual(reloaded.name, "demo")
        self.assertEqual(reloaded.width, 16)

    def test_provenance_appears_as_comments(self):
        path = self.write("config.yaml", MINIMAL + "width: 4\n")
        config = load_config(path, {"width": 16})
        text = render_final_config(config, {"name": "s", "fields": []}, None)
        self.assertIn("# from the cli", text)

    def test_paths_are_absolute_in_the_capture(self):
        path = self.write("config.yaml", MINIMAL)
        config = load_config(path)
        text = render_final_config(config, {"name": "s", "fields": []}, None)
        self.assertIn(self.tmp, text)


if __name__ == "__main__":
    unittest.main()
