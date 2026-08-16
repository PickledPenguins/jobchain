"""Stage classes for the slurm-scheduler example.

Identical shape to two-stage-basic, but this project's config.yaml sets
scheduler: slurm, so the generated scripts carry #SBATCH directives and
submission goes through sbatch/--dependency instead of qsub/-W depend.
"""

from jobchain import JobStage


class Prep(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
cp "{row['input_file']}" "{ctx.work_dir}/copied.txt"
rc=$?
{ctx.emit('copied_file', ctx.work_dir + '/copied.txt')}
{ctx.epilogue()}
exit $rc
""")


class Finish(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
wc -c < "$JC_OUT_copied_file" > "{ctx.work_dir}/size.txt"
rc=$?
{ctx.epilogue()}
exit $rc
""")
