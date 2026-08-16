"""Stages for the combined feature example."""

from jobchain import JobStage


class Prep(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
cat "{row["input_file"]}" > "{ctx.work_dir}/prepared.dat"
rc=$?
{ctx.emit("prepared", ctx.work_dir + "/prepared.dat")}
{ctx.epilogue()}
exit $rc
''')


class Solve(JobStage):
    def resources(self, row):
        return {
            "ncpus": row["threads"],
            "ngpus": row["gpu_count"],
            "walltime": "01:00:00",
            "mem": "8gb" if row["mode"].lower() == "gpu" else "4gb",
        }

    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
cp "$JC_OUT_prepared" "{ctx.work_dir}/result.dat"
printf 'tolerance={row["tolerance"]}\\n' >> "{ctx.work_dir}/result.dat"
rc=$?
{ctx.emit("result", ctx.work_dir + "/result.dat")}
{ctx.epilogue()}
exit $rc
''')


class Archive(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
mkdir -p "{row["output_dir"]}"
cp "{ctx.work_dir}/result.dat" "{row["output_dir"]}/result.dat"
rc=$?
{ctx.epilogue()}
exit $rc
''')
