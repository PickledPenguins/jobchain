"""Stage classes for the depends-variations example.

Four stages exercising every `depends` value in one pipeline:
  prep      (first stage, no depends)
  solve     depends: afterok      -- runs only if prep succeeded
  diagnose  depends: afternotok   -- runs only if solve FAILED
  archive   depends: afterany     -- always runs, chains next

`solve` deliberately fails for the row whose `should_fail` column is
`yes`, so a single run demonstrates both branches: for `good`, `diagnose`
is cancelled and `archive` still runs; for `bad`, `diagnose` runs and
`archive` still runs.
"""

from jobchain import JobStage


class Prep(JobStage):
    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
cp "{row['input_file']}" "{ctx.work_dir}/input.txt"
rc=$?
{ctx.epilogue()}
exit $rc
""")


class Solve(JobStage):
    """Fails on purpose for rows marked should_fail=yes."""

    def write_script(self, row, ctx):
        work = (
            "exit 1" if row["should_fail"] == "yes"
            else f'echo solved > "{ctx.work_dir}/solved.txt"'
        )
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
{work}
rc=$?
{ctx.epilogue()}
exit $rc
""")


class Diagnose(JobStage):
    """Only reached when Solve failed (depends: afternotok)."""

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
echo "solve failed for {row['run_id']}, diagnosing" > "{ctx.work_dir}/diagnosis.txt"
rc=$?
{ctx.epilogue()}
exit $rc
""")


class Archive(JobStage):
    """Always runs (depends: afterany) and chains the next row."""

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
echo "archived {row['run_id']}" > "{ctx.work_dir}/archived.txt"
rc=$?
{ctx.epilogue()}
exit $rc
""")
