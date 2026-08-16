"""Stage classes for the two-stage-basic example.

Two JobStage subclasses: Build produces a file and publishes its path
through the handoff; Finish consumes it. This is the minimal shape of a
real (non-command-string) pipeline.
"""

from jobchain import JobStage


class Build(JobStage):
    """Builds a small output file from the row's seed file."""

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}

built="{ctx.work_dir}/built.txt"
cat "{row['seed_file']}" > "$built"
echo "built by task {row['task_id']}" >> "$built"
rc=$?

{ctx.emit('built_file', ctx.work_dir + '/built.txt')}

{ctx.epilogue()}
exit $rc
""")


class Finish(JobStage):
    """Reads the handoff value Build published and writes a summary."""

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}

wc -l < "$JC_OUT_built_file" > "{ctx.work_dir}/line_count.txt"
rc=$?

{ctx.epilogue()}
exit $rc
""")
