from jobchain import Integer, JobStage, Text


class Format(JobStage):
    settings = {"prefix": Text(required=True), "multiplier": Integer(min=1, max=100, required=True)}

    def write_script(self, row, ctx):
        prefix = self.config["prefix"]
        multiplier = int(self.config["multiplier"])
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
printf '%s:%s\\n' '{prefix}' "$((JC_value * {multiplier}))" > "{ctx.work_dir}/{prefix}.txt"
rc=$?
{ctx.epilogue()}
exit $rc
''')
