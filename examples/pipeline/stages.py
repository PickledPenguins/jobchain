"""Stage classes for the example pipeline.

Each class generates one submit script per row. Classes run on the submit
host at generation time and never on a compute node.
"""

from jobchain import Choice, JobStage


class Prep(JobStage):
    """Prepare a mesh, and publish its path for later stages."""

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}

mesh="{ctx.work_dir}/mesh.dat"
echo "mesh for {row['run_id']} size={row['mesh_size']}" > "$mesh"
rc=$?

{ctx.emit('mesh_file', ctx.work_dir + '/mesh.dat')}

{ctx.epilogue()}
exit $rc
""")


class Solve(JobStage):
    """Solve, with resources scaled to the row's mesh size."""

    settings = {"precision": Choice(["single", "double"], default="double")}

    WALLTIME = {"small": "01:00:00", "medium": "04:00:00", "large": "16:00:00"}

    def resources(self, row):
        return {
            "walltime": self.WALLTIME[row["mesh_size"]],
            "ncpus": row["threads"],
            "ngpus": 2 if row["mode"] == "gpu" else 0,
        }

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}

echo "solving with $JC_OUT_mesh_file at {row['tolerance']} " \\
     "precision={self.config['precision']}" > "{ctx.work_dir}/result.dat"
rc=$?

{ctx.emit('result', ctx.work_dir + '/result.dat')}

{ctx.epilogue()}
exit $rc
""")


class Archive(JobStage):
    """Archive whatever the solve produced. Runs even if the solve failed."""

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}

mkdir -p "{ctx.work_dir}/archive"
cp "{ctx.work_dir}"/*.dat "{ctx.work_dir}/archive/" 2>/dev/null
rc=0

{ctx.epilogue()}
exit $rc
""")
