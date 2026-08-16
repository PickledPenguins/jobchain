"""Executable examples that double as smoke, integration, E2E, and regression tests.

The example files are intentionally kept user-facing. These tests copy the
same files into isolated temporary projects and execute them through the real
CLI. Scheduler-dependent examples use the deterministic stub scheduler from
``tests.helpers``; no real scheduler is required for the normal test suite.
"""

from __future__ import annotations

import os
import shutil
import unittest

import yaml

from tests.helpers import TempProject, require_node_binary

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")


with open(os.path.join(EXAMPLES, "manifest.yaml"), encoding="utf-8") as _handle:
    MANIFEST = yaml.safe_load(_handle)


class ExampleCase(TempProject):
    """Base class for executing a repository example in an isolated directory."""

    def copy_example(self, name: str) -> str:
        entry = next(item for item in MANIFEST["examples"] if item["name"] == name)
        source = os.path.join(EXAMPLES, entry["path"])
        target = self.path("examples", entry["path"])
        if not os.path.isdir(target):
            shutil.copytree(source, target)
        return target

    def example_config(self, name: str) -> str:
        directory = self.copy_example(name)
        return os.path.join(directory, "config.yaml")

    def store_for_example(self, name: str):
        entry = next(item for item in MANIFEST["examples"] if item["name"] == name)
        config = os.path.join(EXAMPLES, entry["path"], "config.yaml")
        with open(config, encoding="utf-8") as handle:
            run_name = yaml.safe_load(handle)["name"]
        from jobchain.store import Store
        return Store(self.path("examples", entry["path"], ".jobchain", run_name))

    def run_example(self, name: str, *args: str, expect: int = 0):
        config = self.example_config(name)
        return self.run_cli("run", config, *args, expect=expect,
                            cwd=os.path.dirname(config))


class TestExampleManifest(unittest.TestCase):
    """The manifest itself is part of the executable example contract."""

    def test_manifest_has_unique_names_and_paths(self):
        entries = MANIFEST["examples"]
        self.assertGreaterEqual(len(entries), 7)
        names = [entry["name"] for entry in entries]
        paths = [entry["path"] for entry in entries]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(paths), len(set(paths)))
        for path in paths:
            root = os.path.join(EXAMPLES, path)
            config = os.path.join(root, "config.yaml")
            params = [
                os.path.join(root, name)
                for name in os.listdir(root)
                if name.startswith("params.") and os.path.isfile(os.path.join(root, name))
            ] if os.path.isdir(root) else []
            if os.path.isfile(config):
                self.assertTrue(params, f"{path} has no parameter file")
            else:
                # Matrix examples intentionally contain one independently
                # executable fixture per child directory.
                children = [os.path.join(root, name) for name in os.listdir(root)
                            if os.path.isdir(os.path.join(root, name))]
                self.assertTrue(children)
                for child in children:
                    self.assertTrue(os.path.isfile(os.path.join(child, "config.yaml")))
                    self.assertTrue(
                        any(name.startswith("params.") and os.path.isfile(os.path.join(child, name))
                            for name in os.listdir(child)))

    def test_every_example_declares_a_test_classification(self):
        for entry in MANIFEST["examples"]:
            with self.subTest(example=entry["name"]):
                tags = set(entry["tags"])
                self.assertIn("regression", tags)
                # Negative examples deliberately fail and therefore are not
                # smoke tests. Every other short example must be runnable as
                # a smoke test.
                if entry.get("runtime") != "long" and "negative" not in tags:
                    self.assertIn("smoke", tags)


class TestExampleSmoke(ExampleCase):
    """Short examples that validate and generate without a scheduler."""

    def test_basic(self):
        self.run_example("basic", "--check")
        self.run_example("basic", "--no-submit")

    def test_validation(self):
        self.run_example("validation", "--check")

    def test_formats(self):
        self.run_example("formats", "--check")
        self.run_example("formats", "--no-submit")


class TestExamplePipelines(ExampleCase):
    """End-to-end execution of the examples that require a scheduler."""

    @classmethod
    def setUpClass(cls):
        require_node_binary()

    def test_pipeline_completes_all_rows_and_stages(self):
        self.copy_example("pipeline")
        self.install_scheduler()
        self.run_example("pipeline")
        self.wait_for_jobs()
        self.assertEqual(set(row.status for row in self.store_for_example("pipeline").load_rows()), {"DONE"})
        self.assertEqual(len(self.submissions()), 9)

    def test_pipeline_generated_scripts_contain_row_resources_and_handoff(self):
        self.copy_example("pipeline")
        self.install_scheduler(run_inline=False)
        config = self.path("examples", "03_pipeline", "config.yaml")
        self.run_cli("run", config, expect=0,
                     cwd=os.path.dirname(config))
        store = self.store_for_example("pipeline")
        row = store.resolve_row("small_case")
        solve = row.current.stage("solve")
        self.assertEqual(solve.resources["ncpus"], 2)
        self.assertEqual(solve.resources["queue"], "normal")
        scripts = [script for _, _, script in store.read_manifest(row.name)]
        solve_script = next(path for path in scripts if path.endswith("solve.sh"))
        self.assertIn("JC_OUT_mesh_file", self.read(solve_script))

    def test_dynamic_resources_are_resolved_per_row(self):
        self.copy_example("dynamic-resources")
        self.install_scheduler(run_inline=False)
        config = self.path("examples", "04_dynamic_resources", "config.yaml")
        self.run_cli("run", config, "--width", "4", expect=0,
                     cwd=os.path.dirname(config))
        store = self.store_for_example("dynamic-resources")
        cpu = store.resolve_row("cpu-small").current.stage("solve")
        gpu = store.resolve_row("gpu-large").current.stage("solve")
        self.assertEqual(cpu.resources["ngpus"], 0)
        self.assertEqual(cpu.resources["ncpus"], 2)
        self.assertEqual(gpu.resources["ngpus"], 1)
        self.assertEqual(gpu.resources["ncpus"], 64)
        self.assertEqual(gpu.resources["mem"], "32gb")

    def test_failure_example_can_be_corrected_and_chained(self):
        self.copy_example("failure-recovery")
        self.install_scheduler()
        config = self.path("examples", "05_failure_recovery", "config.yaml")
        self.run_cli("run", config, expect=0, cwd=os.path.dirname(config))
        self.wait_for_jobs()
        statuses = {row.row_id: row.status for row in self.store_for_example("failure-recovery").load_rows()}
        self.assertEqual(statuses["success"], "DONE")
        self.assertTrue(statuses["recoverable"].startswith("failed.solve"))

        self.run_cli("rerun", "--row", "recoverable", "--set", "fail_solve=no",
                     "--chain", expect=0, cwd=os.path.dirname(config))
        self.wait_for_jobs()
        statuses = {row.row_id: row.status for row in self.store_for_example("failure-recovery").load_rows()}
        self.assertEqual(statuses["recoverable"], "DONE")

    def test_complex_example_executes_end_to_end(self):
        self.copy_example("complex")
        self.install_scheduler()
        config = self.path("examples", "07_complex", "config.yaml")
        self.run_cli("run", config, expect=0, cwd=os.path.dirname(config))
        self.wait_for_jobs()
        self.assertEqual(set(row.status for row in self.store_for_example("complex").load_rows()), {"DONE"})
        for case in ("cpu-small", "gpu-small", "cpu-large"):
            row = self.store_for_example("complex").resolve_row(case)
            self.assertTrue(os.path.isfile(os.path.join(row.params["output_dir"], "result.dat")))
