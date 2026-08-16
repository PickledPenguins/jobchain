"""Stages used by the pipeline topology example."""

from jobchain import JobStage


class Transform(JobStage):
    """Creates a handoff artifact for downstream stages."""

    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
printf '%s\\n' '{row["value"]}' > "{ctx.work_dir}/transform.txt"
rc=$?
{ctx.emit("transform", ctx.work_dir + "/transform.txt")}
{ctx.epilogue()}
exit $rc
''')


class Recover(JobStage):
    """No-op recovery point that deliberately owns chaining."""

    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
rc=0
{ctx.epilogue()}
exit $rc
''')


class Archive(JobStage):
    """Consumes the transform handoff and records completion."""

    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
test -f "$JC_OUT_transform"
rc=$?
{ctx.epilogue()}
exit $rc
''')
