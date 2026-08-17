from jobchain import JobStage


class Fail(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
false
rc=$?
{ctx.epilogue()}
exit $rc
''')

class Recover(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f'''#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
printf 'recovered:%s\\n' "$JC_id" > "{ctx.work_dir}/recovered.txt"
rc=$?
{ctx.epilogue()}
exit $rc
''')
