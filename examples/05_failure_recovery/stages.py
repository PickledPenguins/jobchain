"""Deterministic failure/recovery stages."""

from jobchain import JobStage


class Prep(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.preamble()}
printf '%s\\n' prepared > "{ctx.work_dir}/input.txt"
rc=$?
{ctx.emit("input", ctx.work_dir + "/input.txt")}
{ctx.epilogue()}
exit $rc
''')


class Solve(JobStage):
    def write_script(self, row, ctx):
        command = "false" if row["fail_solve"] == "yes" else "true"
        return ctx.write(f'''#!/bin/sh
{ctx.preamble()}
cat "$JC_OUT_input" > "{ctx.work_dir}/result.txt"
{command}
rc=$?
{ctx.epilogue()}
exit $rc
''')


class Archive(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.preamble()}
mkdir -p "{ctx.work_dir}/archive"
cp "{ctx.work_dir}/result.txt" "{ctx.work_dir}/archive/" 2>/dev/null || true
rc=0
{ctx.epilogue()}
exit $rc
''')
