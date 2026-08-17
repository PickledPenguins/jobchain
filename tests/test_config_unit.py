"""Deep, mock-heavy unit coverage of jobchain/config.py.

Consolidated from test_config_{remaining,exhaustive}.py into one file,
matching this project's one-file-per-subsystem convention.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from jobchain.config import (
    ConfigError,
    RunConfig,
    _resolve,
    describe_settings,
    expand_template,
    load_config,
    render_final_config,
)


class TestRunConfigBoundaries(unittest.TestCase):
    def base(self, **overrides):
        values = {
            "name": "run",
            "params": "p",
            "schema_source": {"fields": []},
            "width": 1,
            "workers": 0,
            "scheduler": "pbs",
            "terminal_level": "info",
            "file_level": "debug",
        }
        values.update(overrides)
        return RunConfig(**values)

    def test_invalid_run_name(self):
        with self.assertRaises(ConfigError):
            self.base(name="bad/name")

    def test_negative_workers(self):
        with self.assertRaises(ConfigError):
            self.base(workers=-1)

    def test_invalid_scheduler(self):
        with self.assertRaises(ConfigError):
            self.base(scheduler="local")

    def test_invalid_terminal_and_file_levels(self):
        with self.assertRaises(ConfigError):
            self.base(terminal_level="nope")
        with self.assertRaises(ConfigError):
            self.base(file_level="nope")

    def test_effective_workers_falls_back_when_cpu_count_is_none(self):
        config = self.base(workers=0)
        with patch("jobchain.config.os.cpu_count", return_value=None):
            self.assertEqual(config.effective_workers, 4)

    def test_base_dir_from_source_and_direct(self):
        self.assertTrue(self.base(source_path="/tmp/cfg.yaml").base_dir == "/tmp")
        with patch("jobchain.config.os.getcwd", return_value="/cwd"):
            self.assertEqual(self.base().base_dir, "/cwd")


class TestTemplateBranches(unittest.TestCase):
    def test_date_time_and_username_placeholders(self):
        with patch("jobchain.config.time.strftime", side_effect=["2026-01-02", "030405"]), patch(
            "jobchain.config._username", return_value="alice"
        ):
            value = expand_template("{date}/{time}/{user}", "run", "/tmp/run")
        self.assertEqual(value, "2026-01-02/030405/alice")

    def test_run_and_row_placeholders(self):
        value = expand_template(
            "{run.name}/{run.home}/{row.name}/{row.index}/{row.x}/{row.generation}",
            "r",
            "/tmp/r",
            {"x": "v"},
            "000001",
            3,
            7,
        )
        self.assertEqual(value, "/".join(["r", "/tmp/r", "000001", "3", "v", "7"]))

    def test_missing_row_value_is_empty(self):
        with self.assertRaises(ConfigError):
            expand_template("{row.missing}", "r", "/tmp/r")

    def test_unknown_placeholders_raise(self):
        for template in ("{run.bad}", "{row.bad}", "{bad.key}"):
            with self.subTest(template=template), self.assertRaises(ConfigError):
                expand_template(template, "r", "/tmp/r")


class TestConfigFileErrors(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/missing/jobchain.yaml")

    def test_non_mapping(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "c.yaml")
            with open(path, "w") as handle:
                handle.write("- one\n")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_unknown_logging_and_paths_keys(self):
        with tempfile.TemporaryDirectory() as root:
            for body in (
                "name: r\nparams: p\nschema: {}\nlogging:\n  nope: 1\n",
                "name: r\nparams: p\nschema: {}\npaths:\n  nope: 1\n",
            ):
                path = os.path.join(root, "c.yaml")
                with open(path, "w") as handle:
                    handle.write(body)
                with self.subTest(body=body), self.assertRaises(ConfigError):
                    load_config(path)

    def test_missing_required_key(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "c.yaml")
            with open(path, "w") as handle:
                handle.write("name: r\nparams: p\n")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_render_final_config_without_pipeline(self):
        config = self.base_config()
        text = render_final_config(config, {"fields": []}, None)
        self.assertIn("Effective configuration", text)
        self.assertNotIn("pipeline:", text)

    def base_config(self):
        return RunConfig(
            name="r", params="p", schema_source={"fields": []}, source_path="/tmp/c.yaml"
        )


class TestConfigRemaining(unittest.TestCase):
    def test_empty_name_is_rejected(self):
        with self.assertRaises(ConfigError):
            RunConfig(name="", params="p", schema_source={})

    def test_direct_config_base_dir_and_home(self):
        cfg = RunConfig(name="r", params="p", schema_source={}, source_path="")
        self.assertEqual(cfg.base_dir, os.getcwd())
        self.assertTrue(cfg.home().endswith(os.path.join(".jobchain", "r")))

    def test_absolute_resolve_is_normalized(self):
        self.assertEqual(_resolve("/tmp/../tmp/x", "/base"), "/tmp/x")

    def test_render_final_config_includes_pipeline_when_present(self):
        cfg = RunConfig(name="r", params="p", schema_source={}, pipeline_source={}, provenance={})
        text = render_final_config(cfg, {"fields": []}, {"stages": []})
        self.assertIn("pipeline:", text)

    def test_describe_settings(self):
        cfg = RunConfig(name="r", params="p", schema_source={}, source_path="")
        values = dict(describe_settings(cfg))
        self.assertEqual(values["config"], "(built directly)")
        self.assertEqual(values["max attempts"], "unlimited")
