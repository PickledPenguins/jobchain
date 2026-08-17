from jobchain import JobStage


class Produce(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
printf '%s\\n' '{row["value"]}' > "{ctx.work_dir}/payload.txt"
rc=$?
{ctx.emit("payload", ctx.work_dir + "/payload.txt")}
{ctx.epilogue()}
exit $rc
''')

class Consume(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
cat "$JC_OUT_payload" > "{ctx.work_dir}/consumed.txt"
rc=$?
{ctx.epilogue()}
exit $rc
''')
