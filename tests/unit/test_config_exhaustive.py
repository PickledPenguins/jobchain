"""Exhaustive coverage of remaining RunConfig branches."""
import os
import tempfile
import unittest
from unittest.mock import patch
from jobchain.config import RunConfig, describe_settings, expand_template, render_final_config, _resolve
from jobchain.core import ConfigError

class TestConfigRemaining(unittest.TestCase):
    def test_empty_name_is_rejected(self):
        with self.assertRaises(ConfigError): RunConfig(name="",params="p",schema_source={})

    def test_direct_config_base_dir_and_home(self):
        cfg=RunConfig(name="r",params="p",schema_source={},source_path="")
        self.assertEqual(cfg.base_dir,os.getcwd()); self.assertTrue(cfg.home().endswith(os.path.join(".jobchain","r")))

    def test_absolute_resolve_is_normalized(self):
        self.assertEqual(_resolve("/tmp/../tmp/x","/base"),"/tmp/x")

    def test_render_final_config_includes_pipeline_when_present(self):
        cfg=RunConfig(name="r",params="p",schema_source={},pipeline_source={},provenance={})
        text=render_final_config(cfg,{"fields":[]},{"stages":[]})
        self.assertIn("pipeline:",text)

    def test_describe_settings(self):
        cfg=RunConfig(name="r",params="p",schema_source={},source_path="")
        values=dict(describe_settings(cfg)); self.assertEqual(values["config"],"(built directly)"); self.assertEqual(values["max attempts"],"unlimited")
