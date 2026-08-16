"""Resource-scaling stage used by the dynamic-resource example."""

from jobchain import JobStage


class Solve(JobStage):
    """Map parameter values to scheduler resources."""

    WALLTIME = {"small": "00:10:00", "medium": "01:00:00", "large": "08:00:00"}
    MEMORY = {"small": "2gb", "medium": "8gb", "large": "32gb"}

    def resources(self, row):
        return {
            "ncpus": row["threads"],
            "ngpus": 1 if row["mode"] == "gpu" else 0,
            "walltime": self.WALLTIME[row["size"]],
            "mem": self.MEMORY[row["size"]],
        }

    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
printf '%s\\n' 'mode={row["mode"]} size={row["size"]} threads={row["threads"]}' > "{ctx.work_dir}/result.txt"
rc=$?
{ctx.epilogue()}
exit $rc
''')
