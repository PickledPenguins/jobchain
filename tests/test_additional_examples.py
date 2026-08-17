"""Executable user examples covering additional configuration combinations."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from tests.helpers import TempProject

EXAMPLES = [
    "16_schema_edges", "17_input_formats", "18_resource_precedence",
    "19_stage_settings", "20_handoff_generations", "21_scheduler_equivalence",
    "22_multirun_isolation", "23_comments_and_empty_rows", "24_max_in_flight",
    "25_quoted_csv", "26_tab_delimiter", "27_literal_delimiter",
    "28_header_warning", "29_optional_defaults", "30_output_paths",
    "31_env_and_directives", "32_single_stage", "33_long_pipeline",
    "34_afternotok", "35_multi_delimiter_values",
]


class TestAdditionalExamples(TempProject):
    """Run the user-facing examples through the real CLI."""

    def _copy(self, name: str) -> str:
        source = Path(__file__).resolve().parents[1] / "examples" / name
        target = Path(self.tmp) / name
        shutil.copytree(source, target)
        return str(target)

    def test_all_additional_examples_check(self):
        for name in EXAMPLES:
            with self.subTest(example=name):
                target = self._copy(name)
                self.run_cli("run", os.path.join(target, "config.yaml"), "--check", expect=0, cwd=target)

    def test_all_additional_examples_generate(self):
        for name in EXAMPLES:
            with self.subTest(example=name):
                target = self._copy(name)
                self.run_cli("run", os.path.join(target, "config.yaml"), "--no-submit", expect=0, cwd=target)

    def test_scheduler_equivalence_generates_both_scheduler_forms(self):
        target = self._copy("21_scheduler_equivalence")
        config = os.path.join(target, "config.yaml")
        self.run_cli("run", config, "--no-submit", expect=0, cwd=target)
        pbs_scripts = list(Path(target, ".jobchain", "example-scheduler-equivalence").rglob("*.sh"))
        self.assertTrue(pbs_scripts)
        pbs_text = "\n".join(p.read_text() for p in pbs_scripts)
        self.assertIn("# custom-directive", pbs_text)

        source = Path(__file__).resolve().parents[1] / "examples" / "21_scheduler_equivalence"
        slurm = Path(target).with_name("scheduler-slurm")
        shutil.copytree(source, slurm)
        slurm_config = slurm / "config.yaml"
        slurm_config.write_text(slurm_config.read_text().replace("scheduler: pbs", "scheduler: slurm"))
        self.run_cli("run", str(slurm_config), "--no-submit", expect=0, cwd=str(slurm))
        scripts = list((slurm / ".jobchain" / "example-scheduler-equivalence").rglob("*.sh"))
        self.assertTrue(scripts)
        slurm_text = "\n".join(p.read_text() for p in scripts)
        self.assertNotEqual(pbs_text, slurm_text)

    def test_stage_settings_are_rendered_independently(self):
        target = self._copy("19_stage_settings")
        self.run_cli("run", os.path.join(target, "config.yaml"), "--no-submit", expect=0, cwd=target)
        scripts = list(Path(target, ".jobchain", "example-stage-settings").rglob("*.sh"))
        text = "\n".join(p.read_text() for p in scripts)
        self.assertIn("coarse", text)
        self.assertIn("fine", text)
        self.assertIn("$((JC_value * 2))", text)
        self.assertIn("$((JC_value * 10))", text)

    def test_header_mismatch_is_warning_not_failure(self):
        target = self._copy("28_header_warning")
        result = self.run_cli("run", os.path.join(target, "config.yaml"), "--check", expect=0, cwd=target)
        self.assertIn("does not match the schema field names", result.stderr)

    def test_optional_defaults_generate_default_values(self):
        target = self._copy("29_optional_defaults")
        self.run_cli("run", os.path.join(target, "config.yaml"), "--no-submit", expect=0, cwd=target)
        scripts = list(Path(target, ".jobchain", "example-optional-defaults").rglob("*.sh"))
        self.assertTrue(scripts)
        env_files = list(Path(target, ".jobchain", "example-optional-defaults").rglob("env"))
        self.assertEqual(len(env_files), 2)
        env_text = "\n".join(p.read_text() for p in env_files)
        self.assertIn("default-label", env_text)
        self.assertIn("JC_count", env_text)

class TestAdditionalExampleExecution(TempProject):
    """Execute selected examples against the repository scheduler stub."""

    def _copy(self, name: str) -> str:
        source = Path(__file__).resolve().parents[1] / "examples" / name
        target = Path(self.tmp) / name
        shutil.copytree(source, target)
        return str(target)

    def test_handoff_example_executes_all_rows_and_stages(self):
        target = self._copy("20_handoff_generations")
        self.install_scheduler()
        self.run_cli("run", os.path.join(target, "config.yaml"), expect=0, cwd=target)
        self.wait_for_jobs()
        from jobchain.store import Store
        store = Store(os.path.join(target, ".jobchain", "example-handoff-generations"))
        self.assertEqual({row.status for row in store.load_rows()}, {"DONE"})

    def test_long_pipeline_example_executes_every_stage(self):
        target = self._copy("33_long_pipeline")
        self.install_scheduler()
        self.run_cli("run", os.path.join(target, "config.yaml"), expect=0, cwd=target)
        self.wait_for_jobs()
        from jobchain.store import Store
        store = Store(os.path.join(target, ".jobchain", "example-long-pipeline"))
        self.assertEqual({row.status for row in store.load_rows()}, {"DONE"})
        self.assertEqual(len(self.submissions()), 12)

    def test_afternotok_example_reaches_recovery_stage(self):
        target = self._copy("34_afternotok")
        self.install_scheduler()
        self.run_cli("run", os.path.join(target, "config.yaml"), expect=0, cwd=target)
        self.wait_for_jobs()
        from jobchain.store import Store
        homes = list(Path(target, ".jobchain").iterdir())
        self.assertEqual(len(homes), 1)
        store = Store(str(homes[0]))
        for row in store.load_rows():
            self.assertEqual(row.status, "failed.fail.1")
            stage_status = {stage.name: stage.status for stage in row.current.stages}
            self.assertEqual(stage_status["fail"], "FAILED")
            self.assertEqual(stage_status["recover"], "DONE")
            self.assertEqual(stage_status["finalize"], "DONE")
