"""Stage classes for the three-stage-handoff example.

Prep -> Solve -> Archive, prep->solve afterok, solve->archive afterany.
Prep publishes a value only knowable once the shell runs (a checksum),
using emit_shell_expr; Solve republishes a derived value with emit (a
Python literal, known at generation time) for Archive to pick up. Solve
also declares a Choice setting, resolved from the pipeline YAML.
"""

from jobchain import Choice, JobStage


class Prep(JobStage):
    """Computes a checksum of the input file and publishes it.

    The checksum is not knowable until `cksum` actually runs, so it must
    be captured with emit_shell_expr rather than emit: passing a shell
    variable to emit() would publish the variable's name, not its value.
    """

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}

sum=$(cksum < "{row['input_file']}" | cut -d' ' -f1)
rc=$?

{ctx.emit_shell_expr('checksum', '$sum')}

{ctx.epilogue()}
exit $rc
""")


class Solve(JobStage):
    """Writes a result file whose name depends on the precision setting."""

    settings = {"precision": Choice(["single", "double"], default="double")}

    def write_script(self, row, ctx):
        result_path = f"{ctx.work_dir}/result-{self.config['precision']}.txt"
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}

echo "checksum=$JC_OUT_checksum precision={self.config['precision']}" \\
    > "{result_path}"
rc=$?

{ctx.emit('result_path', result_path)}

{ctx.epilogue()}
exit $rc
""")


class Archive(JobStage):
    """Copies Solve's result into an archive/ subdirectory. Chains next."""

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}

mkdir -p "{ctx.work_dir}/archive"
cp "$JC_OUT_result_path" "{ctx.work_dir}/archive/"
rc=$?

{ctx.epilogue()}
exit $rc
""")
