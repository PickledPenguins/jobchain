"""Stages for the pipeline example.

The scripts intentionally perform simple deterministic filesystem work so the
example can also serve as an end-to-end fixture.
"""

from jobchain import JobStage


class Prep(JobStage):
    """Create the input artifact and publish it to the next stage."""

    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
printf '%s\\n' 'mesh={row["mesh"]}' > "{ctx.work_dir}/mesh.dat"
rc=$?
{ctx.emit("mesh_file", ctx.work_dir + "/mesh.dat")}
{ctx.epilogue()}
exit $rc
''')


class Solve(JobStage):
    """Consume the preparation artifact and publish a result."""

    def resources(self, row):
        return {"ncpus": row["threads"], "walltime": "01:00:00"}

    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
cat "$JC_OUT_mesh_file" > "{ctx.work_dir}/result.dat"
printf '%s\\n' 'solved' >> "{ctx.work_dir}/result.dat"
rc=$?
{ctx.emit("result_file", ctx.work_dir + "/result.dat")}
{ctx.epilogue()}
exit $rc
''')


class Archive(JobStage):
    """Archive the result and finish the chain."""

    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
mkdir -p "{ctx.work_dir}/archive"
cp "{ctx.work_dir}/result.dat" "{ctx.work_dir}/archive/"
rc=$?
{ctx.epilogue()}
exit $rc
''')
