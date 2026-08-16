"""Tests for pipelines: stage resolution, settings, resources, and freezing."""

from __future__ import annotations

import textwrap
import unittest

from jobchain.core import PipelineError
from jobchain.pipeline import (
    Bool,
    Choice,
    Integer,
    JobStage,
    Text,
    load_pipeline_source,
    single_job_pipeline,
)
from jobchain.scheduler import RowContext
from tests.helpers import TempProject

STAGES = '''\
from jobchain import Choice, Integer, JobStage


class Prep(JobStage):
    def write_script(self, row, ctx):
        return ctx.write("#!/bin/sh\\ntrue\\n")


class Solve(JobStage):
    settings = {"mesh": Choice(["coarse", "fine"], default="fine"),
                "retries": Integer(default=0, min=0, max=5)}
    WALLTIME = {"coarse": "01:00:00", "fine": "16:00:00"}

    def resources(self, row):
        return {"walltime": self.WALLTIME[self.config["mesh"]],
                "ncpus": row["threads"]}

    def write_script(self, row, ctx):
        return ctx.write("#!/bin/sh\\ntrue\\n")


class Archive(JobStage):
    def output_dir(self, row, ctx):
        return os.path.join(ctx.work_dir, "scripts")

    def write_script(self, row, ctx):
        return ctx.write("#!/bin/sh\\ntrue\\n")


class Broken(JobStage):
    def write_script(self, row, ctx):
        raise RuntimeError("this stage always fails to render")


class Caching(JobStage):
    def write_script(self, row, ctx):
        self.cached = row          # refused: instances are frozen
        return ctx.write("#!/bin/sh\\ntrue\\n")


class NotAStage:
    pass
'''


class RunStub:
    """Stands in for a RunContext where only work_dir is needed."""

    def __init__(self, directory: str = "/tmp/work"):
        self.directory = directory

    def work_dir(self, row, row_name, row_index=0, generation=1):
        return self.directory


class PipelineCase(TempProject):
    def setUp(self) -> None:
        super().setUp()
        self.write("stages.py", "import os\n" + STAGES)

    def build(self, document, construct: bool = True):
        pipeline = load_pipeline_source(document, self.tmp)
        if construct:
            pipeline.construct(RunStub())
        return pipeline


class TestResolution(PipelineCase):
    def test_a_stage_name_resolves_to_its_class(self):
        pipeline = self.build({"name": "p", "stage_module": "stages.py",
                               "stages": [{"name": "prep"}]})
        self.assertEqual(type(pipeline.stages[0]).__name__, "Prep")

    def test_underscored_names_become_camel_case(self):
        self.write("more.py", "from jobchain import JobStage\n"
                              "class MeshRefine(JobStage):\n"
                              "    def write_script(self, row, ctx):\n"
                              "        return ctx.write('#!/bin/sh\\n')\n")
        pipeline = self.build({"name": "p", "stage_module": "more.py",
                               "stages": [{"name": "mesh_refine"}]})
        self.assertEqual(type(pipeline.stages[0]).__name__, "MeshRefine")

    def test_uses_names_a_class_explicitly(self):
        # Two stages sharing one class, configured differently, is the case
        # that name-derived resolution alone could not express.
        pipeline = self.build({"name": "p", "stage_module": "stages.py",
                               "stages": [
                                   {"name": "solve_coarse", "uses": "Solve",
                                    "mesh": "coarse"},
                                   {"name": "solve_fine", "uses": "Solve",
                                    "mesh": "fine", "depends": "afterany"}]})
        self.assertEqual([type(s).__name__ for s in pipeline.stages],
                         ["Solve", "Solve"])
        row = {"threads": 8}
        self.assertEqual(
            pipeline.stage("solve_coarse").effective_resources(row)["walltime"],
            "01:00:00")
        self.assertEqual(
            pipeline.stage("solve_fine").effective_resources(row)["walltime"],
            "16:00:00")

    def test_a_missing_class_is_an_error_naming_what_exists(self):
        with self.assertRaises(PipelineError) as caught:
            self.build({"name": "p", "stage_module": "stages.py",
                        "stages": [{"name": "reduce"}]})
        message = str(caught.exception)
        self.assertIn("Reduce", message)
        self.assertIn("Prep", message)

    def test_a_stage_with_a_command_needs_no_class(self):
        pipeline = self.build({"name": "p",
                               "stages": [{"name": "anything", "command": "true"}]})
        self.assertIsInstance(pipeline.stages[0], JobStage)

    def test_a_non_jobstage_class_is_rejected(self):
        with self.assertRaises(PipelineError):
            self.build({"name": "p", "stage_module": "stages.py",
                        "stages": [{"name": "x", "uses": "NotAStage"}]})

    def test_duplicate_stage_names_are_rejected(self):
        with self.assertRaises(PipelineError):
            self.build({"name": "p", "stages": [{"name": "a", "command": "t"},
                                                {"name": "a", "command": "t"}]})

    def test_an_empty_pipeline_is_rejected(self):
        with self.assertRaises(PipelineError):
            self.build({"name": "p", "stages": []})

    def test_unknown_pipeline_key_is_rejected(self):
        with self.assertRaises(PipelineError):
            self.build({"name": "p", "stagez": [], "stages": [{"name": "a",
                                                              "command": "t"}]})

    def test_a_pipeline_may_come_from_a_file(self):
        self.write("pipeline.yaml", textwrap.dedent("""
            name: fromfile
            stages:
              - {name: only, command: "true"}
        """))
        pipeline = self.build("pipeline.yaml")
        self.assertEqual(pipeline.name, "fromfile")

    def test_a_missing_pipeline_file_is_reported(self):
        with self.assertRaises(PipelineError):
            self.build("absent.yaml")

    def test_the_implicit_single_job_pipeline(self):
        pipeline = single_job_pipeline("echo hello")
        self.assertEqual(pipeline.stage_names, ["job"])
        self.assertTrue(pipeline.specs[0].chains_next)


class TestChaining(PipelineCase):
    def test_the_last_stage_chains_by_default(self):
        pipeline = self.build({"name": "p", "stages": [
            {"name": "a", "command": "t"},
            {"name": "b", "command": "t", "depends": "afterany"}]},
            construct=False)
        self.assertEqual(pipeline.chaining_stage, "b")

    def test_an_explicit_non_afterany_chaining_stage_is_rejected(self):
        # Otherwise an upstream failure cancels it and the chain stops with
        # no error anywhere.
        with self.assertRaises(PipelineError) as caught:
            self.build({"name": "p", "stages": [
                {"name": "a", "command": "t"},
                {"name": "b", "command": "t", "depends": "afterok",
                 "chains_next": True},
                {"name": "c", "command": "t", "depends": "afterany"}]},
                construct=False)
        self.assertIn("afterany", str(caught.exception))

    def test_a_defaulted_dependency_is_promoted_on_the_chaining_stage(self):
        # A pipeline that says nothing about chaining must still survive a
        # failed stage, so the default is promoted rather than rejected.
        pipeline = self.build({"name": "p", "stages": [
            {"name": "a", "command": "t"},
            {"name": "b", "command": "t"}]}, construct=False)
        self.assertEqual(pipeline.chaining_stage, "b")
        self.assertEqual(pipeline.spec("b").depends, "afterany")
        self.assertEqual(pipeline.spec("a").depends, "afterok")

    def test_chaining_may_be_moved_to_an_afterany_stage(self):
        pipeline = self.build({"name": "p", "stages": [
            {"name": "a", "command": "t", "depends": "afterany",
             "chains_next": True},
            {"name": "b", "command": "t", "depends": "afterany"}]},
            construct=False)
        self.assertEqual(pipeline.chaining_stage, "a")

    def test_only_one_stage_may_chain(self):
        with self.assertRaises(PipelineError):
            self.build({"name": "p", "stages": [
                {"name": "a", "command": "t", "chains_next": True},
                {"name": "b", "command": "t", "depends": "afterany",
                 "chains_next": True}]}, construct=False)

    def test_the_first_stage_may_chain_without_afterany(self):
        # The first stage has no dependency, so the rule does not apply.
        pipeline = self.build({"name": "p", "stages": [
            {"name": "a", "command": "t", "chains_next": True},
            {"name": "b", "command": "t", "depends": "afterany"}]},
            construct=False)
        self.assertEqual(pipeline.chaining_stage, "a")

    def test_bad_dependency_types_are_rejected(self):
        with self.assertRaises(PipelineError):
            self.build({"name": "p", "stages": [{"name": "a", "command": "t",
                                                 "depends": "whenever"}]},
                       construct=False)


class TestSettings(PipelineCase):
    def build_solve(self, **stage):
        entry = {"name": "solve", "uses": "Solve"}
        entry.update(stage)
        return self.build({"name": "p", "stage_module": "stages.py",
                           "stages": [entry]}).stage("solve")

    def test_declared_settings_get_their_defaults(self):
        stage = self.build_solve()
        self.assertEqual(stage.config["mesh"], "fine")
        self.assertEqual(stage.config["retries"], 0)

    def test_a_declared_setting_can_be_given(self):
        self.assertEqual(self.build_solve(mesh="coarse").config["mesh"], "coarse")

    def test_a_bad_choice_is_rejected_at_load(self):
        with self.assertRaises(PipelineError) as caught:
            self.build_solve(mesh="corase")
        self.assertIn("coarse", str(caught.exception))

    def test_an_out_of_range_integer_is_rejected(self):
        with self.assertRaises(PipelineError):
            self.build_solve(retries=99)

    def test_a_non_integer_is_rejected(self):
        with self.assertRaises(PipelineError):
            self.build_solve(retries="many")

    def test_an_undeclared_key_is_rejected_naming_what_is_valid(self):
        with self.assertRaises(PipelineError) as caught:
            self.build_solve(wallclock="1:00")
        message = str(caught.exception)
        self.assertIn("wallclock", message)
        self.assertIn("walltime", message)

    def test_a_required_setting_must_be_supplied(self):
        self.write("req.py", "from jobchain import JobStage, Text\n"
                             "class Needy(JobStage):\n"
                             "    settings = {'token': Text(required=True)}\n"
                             "    def write_script(self, row, ctx):\n"
                             "        return ctx.write('#!/bin/sh\\n')\n")
        with self.assertRaises(PipelineError):
            self.build({"name": "p", "stage_module": "req.py",
                        "stages": [{"name": "needy"}]})

    def test_setting_types_describe_themselves(self):
        self.assertIn("coarse", Choice(["coarse", "fine"]).describe())
        self.assertIn("true", Bool().describe())
        self.assertIn("integer", Integer(min=1, max=2).describe())
        self.assertIn("text", Text().describe())

    def test_bool_rejects_non_boolean_values(self):
        with self.assertRaises(PipelineError):
            Bool().check("flag", "yes")


class TestResources(PipelineCase):
    def pipeline(self):
        return self.build({
            "name": "p", "stage_module": "stages.py",
            "defaults": {"queue": "normal", "account": "proj1"},
            "stages": [
                {"name": "prep", "walltime": "00:30:00", "ncpus": 2, "mem": "8gb"},
                {"name": "solve", "uses": "Solve", "depends": "afterok",
                 "mem": "32gb", "ncpus": 4},
                {"name": "archive", "depends": "afterany"}]})

    def test_defaults_reach_every_stage(self):
        resources = self.pipeline().stage("prep").effective_resources({})
        self.assertEqual(resources["queue"], "normal")
        self.assertEqual(resources["account"], "proj1")

    def test_a_stage_block_overrides_the_defaults(self):
        resources = self.pipeline().stage("prep").effective_resources({})
        self.assertEqual(resources["walltime"], "00:30:00")
        self.assertEqual(resources["ncpus"], 2)

    def test_the_class_overrides_the_stage_block(self):
        # Both specifying the same key is expected: YAML holds the default,
        # the class returns only what varies per row.
        resources = self.pipeline().stage("solve").effective_resources({"threads": 32})
        self.assertEqual(resources["ncpus"], 32)       # class over yaml's 4
        self.assertEqual(resources["mem"], "32gb")     # yaml, untouched
        self.assertEqual(resources["queue"], "normal")  # pipeline defaults

    def test_stages_do_not_share_resources(self):
        pipeline = self.pipeline()
        prep = pipeline.stage("prep").effective_resources({})
        archive = pipeline.stage("archive").effective_resources({})
        self.assertEqual(prep["walltime"], "00:30:00")
        self.assertIsNone(archive["walltime"])

    def test_an_unknown_resource_key_is_rejected(self):
        self.write("bad.py", "from jobchain import JobStage\n"
                             "class Bad(JobStage):\n"
                             "    def resources(self, row):\n"
                             "        return {'cpus': 4}\n"
                             "    def write_script(self, row, ctx):\n"
                             "        return ctx.write('#!/bin/sh\\n')\n")
        pipeline = self.build({"name": "p", "stage_module": "bad.py",
                               "stages": [{"name": "bad"}]})
        with self.assertRaises(PipelineError) as caught:
            pipeline.stage("bad").effective_resources({})
        self.assertIn("cpus", str(caught.exception))


class TestFreezing(PipelineCase):
    def test_instances_are_frozen_after_construction(self):
        # One instance serves every row and every worker thread, so caching
        # into self would make generation order-dependent.
        pipeline = self.build({"name": "p", "stage_module": "stages.py",
                               "stages": [{"name": "prep"}]})
        with self.assertRaises(PipelineError) as caught:
            pipeline.stages[0].anything = 1
        self.assertIn("frozen", str(caught.exception))

    def test_a_stage_that_caches_fails_when_it_writes(self):
        pipeline = self.build({"name": "p", "stage_module": "stages.py",
                               "stages": [{"name": "caching", "uses": "Caching"}]})
        with self.assertRaises(PipelineError):
            pipeline.stages[0].write_script({}, None)

    def test_class_attributes_remain_usable(self):
        pipeline = self.build({"name": "p", "stage_module": "stages.py",
                               "stages": [{"name": "solve", "uses": "Solve"}]})
        self.assertIn("fine", type(pipeline.stages[0]).WALLTIME)


class TestRowContextEmit(unittest.TestCase):
    """RowContext.emit publishes a literal value; emit_shell_expr lets the
    shell expand a run-time expression first. Regression coverage for a
    defect where emit() single-quoted its value unconditionally, so a
    caller passing a shell variable reference (a natural-looking but
    incorrect usage) published the variable's name instead of its
    contents.
    """

    def setUp(self) -> None:
        self.ctx = RowContext(
            run=RunStub(), row_name="r1", row_index=0, stage="prep",
            generation=1, work_dir="/tmp/work", chains_next=False)

    def test_emit_shell_quotes_a_literal_value(self):
        line = self.ctx.emit("mesh_file", "/tmp/work/mesh.dat")
        self.assertEqual(
            line,
            '"$JC_NODE" emit --run "$JC_RUN" mesh_file=\'/tmp/work/mesh.dat\'')

    def test_emit_escapes_embedded_single_quotes(self):
        # A literal value containing a single quote must round-trip through
        # the shell unchanged, not truncate or break the emit call.
        line = self.ctx.emit("label", "it's fine")
        self.assertNotIn("label='it's fine'", line)
        self.assertEqual(line,
            "\"$JC_NODE\" emit --run \"$JC_RUN\" label='it'\\''s fine'")

    def test_emit_does_not_expand_a_shell_variable(self):
        # This is the misuse the defect made easy to fall into: passing a
        # shell variable reference to emit() publishes it literally.
        line = self.ctx.emit("mesh_file", "$mesh")
        self.assertIn("mesh_file='$mesh'", line)

    def test_emit_shell_expr_lets_the_shell_expand_a_variable(self):
        line = self.ctx.emit_shell_expr("mesh_file", "$mesh")
        self.assertEqual(
            line, '"$JC_NODE" emit --run "$JC_RUN" mesh_file="$mesh"')

    def test_emit_shell_expr_supports_command_substitution(self):
        line = self.ctx.emit_shell_expr("count", '$(wc -l < "$out")')
        self.assertEqual(
            line,
            '"$JC_NODE" emit --run "$JC_RUN" count="$(wc -l < "$out")"')


if __name__ == "__main__":
    unittest.main()
