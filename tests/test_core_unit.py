"""Unit coverage of jobchain/core.py and the __main__ entry point.

Consolidated from test_core_unit.py and test_main_module.py.
"""
import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from jobchain import core
import runpy


class TestCoreExceptions(unittest.TestCase):
    def test_exit_constants_and_names(self):
        self.assertEqual(core.EXIT_OK, 0)
        self.assertEqual(core.EXIT_USAGE, 1)
        self.assertEqual(core.EXIT_INTERNAL, 2)
        self.assertEqual(core.EXIT_DATA, 3)
        self.assertEqual(core.EXIT_STRUCTURE, 4)
        self.assertEqual(core.EXIT_CONFIG, 5)
        self.assertEqual(core.EXIT_STATE, 6)
        self.assertEqual(core.EXIT_SCHEDULER, 7)
        self.assertEqual(core.EXIT_NODE_HELPER, 8)
        self.assertEqual(core.EXIT_CONFLICT, 9)
        self.assertEqual(core.EXIT_SCHEMA, core.EXIT_CONFIG)

    def test_exception_messages_and_exit_codes(self):
        cases = [
            (core.JobChainError, core.EXIT_INTERNAL),
            (core.UsageError, core.EXIT_USAGE),
            (core.ConfigError, core.EXIT_CONFIG),
            (core.SchemaError, core.EXIT_CONFIG),
            (core.PipelineError, core.EXIT_CONFIG),
            (core.DataError, core.EXIT_DATA),
            (core.StructureError, core.EXIT_STRUCTURE),
            (core.StateError, core.EXIT_STATE),
            (core.SchedulerError, core.EXIT_SCHEDULER),
            (core.NodeHelperError, core.EXIT_NODE_HELPER),
            (core.ConflictError, core.EXIT_CONFLICT),
        ]
        for cls, code in cases:
            with self.subTest(cls=cls.__name__):
                exc = cls("problem")
                self.assertEqual(str(exc), "problem")
                self.assertEqual(exc.exit_code, code)


class TestLogging(unittest.TestCase):
    def tearDown(self):
        logger = logging.getLogger(core.LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def test_trace_constant_and_shared_logger(self):
        self.assertEqual(core.TRACE, 5)
        self.assertEqual(logging.getLevelName(core.TRACE), "TRACE")
        self.assertIs(core.get_logger(), logging.getLogger(core.LOGGER_NAME))

    def test_trace_emits_at_trace_level(self):
        logger = core.get_logger()
        logger.handlers = [logging.StreamHandler(io.StringIO())]
        logger.setLevel(core.TRACE)
        core.trace("value=%s", 7)
        self.assertEqual(logger.handlers[0].stream.getvalue(), "value=7\n")
        logger.handlers[0].close()
        logger.handlers.clear()

    def test_configure_logging_defaults_without_tty(self):
        with patch.object(core.sys.stderr, "isatty", return_value=False):
            logger = core.configure_logging(use_color=None)
        self.assertFalse(logger.propagate)
        self.assertEqual(logger.level, logging.INFO)
        self.assertEqual(len(logger.handlers), 1)
        self.assertIsInstance(logger.handlers[0], logging.StreamHandler)

    def test_configure_logging_verbosity_levels(self):
        for verbosity, expected in ((0, logging.INFO), (1, logging.DEBUG), (2, core.TRACE)):
            with self.subTest(verbosity=verbosity):
                logger = core.configure_logging(verbosity=verbosity, use_color=False)
                self.assertEqual(logger.level, expected)

    def test_explicit_terminal_level_and_invalid_level(self):
        logger = core.configure_logging(terminal_level="warning", use_color=False)
        self.assertEqual(logger.level, logging.WARNING)
        logger = core.configure_logging(terminal_level="not-a-level", use_color=False)
        self.assertEqual(logger.level, logging.INFO)

    def test_explicit_verbosity_overrides_terminal_level(self):
        logger = core.configure_logging(verbosity=1, terminal_level="error", use_color=False)
        self.assertEqual(logger.level, logging.DEBUG)

    def test_file_logging_creates_directory_and_closes_previous_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "first", "run.log")
            second = os.path.join(tmp, "second", "run.log")
            logger = core.configure_logging(log_file=first, use_color=False)
            old = logger.handlers[-1]
            logger = core.configure_logging(log_file=second, use_color=False)
            self.assertTrue(old.stream is None or old.stream.closed)
            logger.info("hello")
            logger.handlers[-1].flush()
            with open(second, encoding="utf-8") as fh:
                self.assertIn("hello", fh.read())

    def test_color_selection_and_formatter(self):
        formatter = core._Formatter(False)
        for level in (logging.INFO, logging.WARNING, logging.ERROR, logging.DEBUG):
            record = logging.LogRecord("x", level, __file__, 1, "hello %s", ("x",), None)
            text = formatter.format(record)
            if level == logging.INFO:
                self.assertEqual(text, "hello x")
            else:
                self.assertTrue(text.startswith(logging.getLevelName(level).lower() + ": "))
        colored = core._Formatter(True)
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "bad", (), None)
        self.assertIn("bad", colored.format(record))
        self.assertIn("\033[", colored.format(record))

    def test_log_startup_summary_sorts_settings(self):
        logger = core.get_logger()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        core.log_startup_summary("start", {"z": 2, "a": 1})
        output = stream.getvalue()
        self.assertLess(output.index("a"), output.index("z"))
        self.assertIn("start", output)
        handler.close()
        logger.handlers.clear()


class TestMainModule(unittest.TestCase):
    def test_module_entrypoint_calls_cli_main_and_exits_with_its_code(self):
        with patch("jobchain.cli.main", return_value=7) as main:
            with self.assertRaises(SystemExit) as caught:
                runpy.run_module("jobchain.__main__", run_name="__main__")
        self.assertEqual(caught.exception.code,7)
        main.assert_called_once()
        import jobchain.__main__ as module
        self.assertTrue(hasattr(module, "main"))


