from jobchain import JobStage

class Compute(JobStage):
    def resources(self, row):
        return {
            "ncpus": row["cpus"],
            "mem": f"{row['mem_gb']}gb",
            "walltime": "00:20:00" if row["cpus"] > 8 else "00:10:00",
        }

    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
printf 'compute:%s\\n' "$JC_case" > "{ctx.work_dir}/compute.txt"
rc=$?
{ctx.emit("compute", ctx.work_dir + "/compute.txt")}
{ctx.epilogue()}
exit $rc
''')
