"""Ensure ``python -m jobchain`` has an exercised package entry point."""
import runpy
import unittest
from unittest.mock import patch


class TestMainModule(unittest.TestCase):
    def test_module_entrypoint_calls_cli_main_and_exits_with_its_code(self):
        with patch("jobchain.cli.main", return_value=7) as main:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("jobchain.__main__", run_name="__main__")
        self.assertEqual(caught.exception.code,7)
        main.assert_called_once()
        import jobchain.__main__ as module
        self.assertTrue(hasattr(module, "main"))
