"""Tests for failure handling and the less-travelled paths.

The governing rule is that every expected failure produces a message and a
documented exit code, never a traceback.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from jobchain.core import (
    EXIT_NAMES,
    NodeHelperError,
    StateError,
    configure_logging,
    get_logger,
)
from jobchain.store import Store, find_node_binary, row_name
from tests.helpers import NODE_BINARY, PROJECT_ROOT, TempProject, require_node_binary


def setUpModule() -> None:
    require_node_binary()


class TestExitCodes(TempProject):
    """Each class of failure gets its own code, so a wrapper can branch."""

    def test_usage_error(self):
        self.make_project()
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.run_cli("show", expect=1)               # no row given

    def test_data_error(self):
        self.make_project(params="rid|count|label\na1|999|bad\n")
        self.run_cli("run", "config.yaml", "--check", "--strict", expect=3)

    def test_structure_error(self):
        self.make_project()
        os.unlink(self.path("params.psv"))
        self.run_cli("run", "config.yaml", "--check", expect=4)

    def test_configuration_error(self):
        self.write("bad.yaml", "name: x\nparams: p.psv\n")   # no schema
        self.run_cli("run", "bad.yaml", expect=5)

    def test_state_error(self):
        self.run_cli("status", expect=6)

    def test_conflict_error(self):
        self.make_project()
        self.install_scheduler(run_inline=False, alive=True)
        self.run_cli("run", "config.yaml", expect=0)
        self.run_cli("run", "config.yaml", expect=9)

    def test_every_exit_code_has_a_name(self):
        from jobchain import core
        codes = {value for name, value in vars(core).items()
                 if name.startswith("EXIT_") and isinstance(value, int)}
        self.assertEqual(codes, set(EXIT_NAMES))

    def test_no_traceback_reaches_the_terminal_for_bad_input(self):
        self.make_project(params="rid|count|label\na1|999|bad\n")
        result = self.run_cli("run", "config.yaml", "--check", "--strict",
                              expect=3)
        self.assertNotIn("Traceback", result.stderr)


class TestPipelineFailures(TempProject):
    def test_a_stage_class_that_raises_stops_the_run(self):
        self.make_project(pipeline=True)
        self.write("stages.py", self.read(self.path("stages.py")).replace(
            'echo prepared > "{ctx.work_dir}/mesh.txt"',
            '{1/0}'))
        self.install_scheduler(run_inline=False)
        result = self.run_cli("run", "config.yaml", expect=3)
        self.assertIn("could not be generated", result.stderr)
        self.assertEqual(self.submissions(), [])

    def test_every_rendering_failure_is_reported_at_once(self):
        # A class that breaks for one row usually breaks for many; reporting
        # them one at a time wastes the user's time.
        self.make_project(pipeline=True)
        self.write("stages.py", self.read(self.path("stages.py")).replace(
            'echo prepared > "{ctx.work_dir}/mesh.txt"', '{1/0}'))
        self.install_scheduler(run_inline=False)
        result = self.run_cli("run", "config.yaml", expect=3)
        self.assertIn("4 script(s) could not be generated", result.stderr)

    def test_a_script_that_is_not_shell_is_rejected(self):
        self.make_project(pipeline=True)
        self.write("stages.py",
                   "from jobchain import JobStage\n"
                   "class Prep(JobStage):\n"
                   "    def write_script(self, row, ctx):\n"
                   "        return ctx.write('#!/bin/sh\\nif then fi(\\n')\n"
                   "class Solve(JobStage):\n"
                   "    def write_script(self, row, ctx):\n"
                   "        return ctx.write('#!/bin/sh\\ntrue\\n')\n"
                   "class Archive(JobStage):\n"
                   "    def write_script(self, row, ctx):\n"
                   "        return ctx.write('#!/bin/sh\\ntrue\\n')\n")
        self.install_scheduler(run_inline=False)
        result = self.run_cli("run", "config.yaml", expect=3)
        self.assertIn("not valid shell", result.stderr)

    def test_a_rejected_submission_cancels_the_partial_pipeline(self):
        # Leaving a partial pipeline queued would strand its successors
        # behind a dependency that can never be satisfied.
        self.make_project(pipeline=True)
        self.install_scheduler(fail=True)
        result = self.run_cli("run", "config.yaml", expect=7)
        self.assertIn("failed to submit", result.stdout)

    def test_submitting_without_a_scheduler_client_is_a_scheduler_error(self):
        # Preparing a run needs no scheduler; only submitting does.
        self.make_project()
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        os.unlink(os.path.join(self.bin_dir, "qsub"))
        result = self.run_cli("run", "config.yaml", expect=7)
        self.assertIn("qsub", result.stderr)


class TestStoreErrors(TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.make_project()
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        self.store = self.store_for()

    def test_a_row_without_metadata_is_a_state_error(self):
        os.unlink(os.path.join(self.store.row_dir("000001"), "meta.json"))
        with self.assertRaises(StateError):
            self.store.load_row("000001")

    def test_corrupt_metadata_is_a_state_error(self):
        self.write(os.path.join(self.store.row_dir("000001"), "meta.json"),
                   "{not json")
        with self.assertRaises(StateError):
            self.store.load_row("000001")

    def test_a_corrupt_configuration_is_a_state_error(self):
        self.write(self.store.config_path, "{broken")
        with self.assertRaises(StateError):
            self.store.load_config()

    def test_stray_directories_are_not_mistaken_for_attempts(self):
        os.makedirs(os.path.join(self.store.row_dir("000001"), "run-notanumber"))
        os.makedirs(os.path.join(self.store.row_dir("000001"), "scratch"))
        self.assertEqual(self.store.load_row("000001").attempts, 0)

    def test_unresolvable_row_references(self):
        for identifier in ("nosuch", "line:999", "line:abc", "rid=nosuch"):
            with self.subTest(identifier=identifier), self.assertRaises(StateError):
                self.store.resolve_row(identifier, ["rid"])

    def test_rows_resolve_by_name_number_line_and_column(self):
        for identifier in ("000002", "2", "a2", "line:3", "rid=a2"):
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    self.store.resolve_row(identifier, ["rid"]).name, "000002")

    def test_row_names_are_padded_for_stable_ordering(self):
        self.assertEqual(row_name(0), "000001")
        self.assertEqual(sorted([row_name(0), row_name(10), row_name(2)]),
                         [row_name(0), row_name(2), row_name(10)])

    def test_the_setup_lock_refuses_a_second_holder(self):
        self.store.acquire_lock()
        try:
            with self.assertRaises(StateError) as caught:
                self.store.acquire_lock()
            self.assertIn("preparing", str(caught.exception))
        finally:
            self.store.release_lock()


class TestNodeBinaryDiscovery(TempProject):
    def test_an_explicit_path_wins(self):
        self.assertEqual(find_node_binary(NODE_BINARY),
                         os.path.abspath(NODE_BINARY))

    def test_the_environment_variable_is_consulted(self):
        os.environ["JOBCHAIN_NODE"] = NODE_BINARY
        self.assertEqual(find_node_binary(), os.path.abspath(NODE_BINARY))

    def test_a_missing_binary_names_everywhere_tried(self):
        store = Store(self.path("home"), node_binary=self.path("nonexistent"))
        with self.assertRaises(NodeHelperError) as caught:
            store._run_node(["version"])
        self.assertIn("could not execute", str(caught.exception))


class TestLogging(TempProject):
    def tearDown(self) -> None:
        get_logger().handlers.clear()
        super().tearDown()

    def test_the_console_shows_progress_by_default(self):
        import logging
        self.assertEqual(configure_logging(use_color=False).level, logging.INFO)

    def test_verbosity_lowers_the_console_level(self):
        import logging
        self.assertEqual(configure_logging(verbosity=1, use_color=False).level,
                         logging.DEBUG)
        self.assertEqual(configure_logging(verbosity=2, use_color=False).level, 5)

    def test_repeated_configuration_does_not_duplicate_handlers(self):
        for _ in range(3):
            logger = configure_logging(use_color=False)
        self.assertEqual(len(logger.handlers), 1)

    def test_the_file_records_more_than_the_console(self):
        path = self.path("run.log")
        logger = configure_logging(terminal_level="warning", file_level="debug",
                                   log_file=path, use_color=False)
        logger.debug("a debug message")
        for handler in logger.handlers:
            handler.flush()
        self.assertIn("a debug message", self.read(path))

    def test_a_run_writes_its_own_log_file(self):
        self.make_project()
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        log = self.path(".jobchain", "test-run", "jobchain.log")
        self.assertTrue(os.path.isfile(log))
        self.assertIn("row", self.read(log))

    def test_validation_failures_reach_the_log_file(self):
        self.make_project(params="rid|count|label\na1|5|ok\na2|999|bad\n")
        self.install_scheduler(run_inline=False)
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        text = self.read(self.path(".jobchain", "test-run", "jobchain.log"))
        self.assertIn("invalid", text)


class TestDocumentationMatchesTheCode(TempProject):
    """Guards the README against drifting away from the code.

    The command list and the file tree are where documentation goes stale
    silently, because nothing breaks when it does.
    """

    def setUp(self) -> None:
        super().setUp()
        self.readme_path = os.path.join(PROJECT_ROOT, "README.md")
        if not os.path.isfile(self.readme_path):
            self.skipTest("README.md has not been written yet")
        self.text = Path(self.readme_path).read_text(encoding="utf-8")

    def test_every_command_is_documented(self):
        from jobchain.cli import _HANDLERS
        for command in _HANDLERS:
            with self.subTest(command=command):
                self.assertIn(f"`{command}", self.text)

    def test_every_documented_command_exists(self):
        from jobchain.cli import _HANDLERS
        section = self.text[self.text.index("## Command reference"):]
        section = section[:section.index("\n## ")]
        for match in re.findall(r"^\| `([a-z]+)", section, re.MULTILINE):
            with self.subTest(command=match):
                self.assertIn(match, _HANDLERS)

    def test_the_documented_version_matches_the_code(self):
        from jobchain.core import VERSION
        self.assertIn(f"**Version {VERSION}**", self.text)

    def test_every_source_file_appears_in_the_structure_tree(self):
        start = self.text.index("## Project structure")
        tree = self.text[start:self.text.index("\n## ", start)]
        # The README structure tree documents the user-facing test suite,
        # not every internal test harness implementation.  Test infrastructure
        # such as run_suite.py and coverage-gap probes are intentionally kept
        # out of that tree, so only the production package and C source remain
        # exhaustive documentation requirements here.
        for directory, pattern in (("jobchain", "*.py"), ("src", "*")):
            for path in sorted(Path(PROJECT_ROOT, directory).glob(pattern)):
                if "__pycache__" in str(path):
                    continue
                with self.subTest(file=path.name):
                    self.assertIn(path.name, tree)


if __name__ == "__main__":
    unittest.main()
