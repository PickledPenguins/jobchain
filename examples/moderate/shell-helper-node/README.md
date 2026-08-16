# shell-helper-node

The same pipeline shape as `../../simple/two-stage-basic` — same schema,
same `stages.py` — run with `JOBCHAIN_NODE` pointed at the portable shell
helper (`bin/jobchain-node.sh`) instead of the default compiled binary.
`width: 1` against three rows is deliberate: only the first row's chain is
submitted directly by the Python front end, so rows two and three are
submitted entirely by the shell helper's own self-chaining logic — the
exact code path a real defect lived in (see `../../../BUGFIXES.md`, item
4). This example is what surfaced it: a `show --full` on one row was
silently displaying another row's handoff value.

## What it shows

- `JOBCHAIN_NODE=/path/to/jobchain-node.sh` overriding the default node
  binary
- More rows than `width`, so most of the run goes through the shell
  helper's self-chained submission rather than Python's own — the
  distinction that mattered for the defect this example caught
- Each row's handoff staying correctly scoped to that row, not leaking
  into another row's state

## Run it

```sh
export JOBCHAIN_NODE=/path/to/jobchain-0.5/bin/jobchain-node.sh
jobchain run config.yaml --check
jobchain run config.yaml
jobchain show --row task-a --full
jobchain show --row task-b --full
jobchain show --row task-c --full
unset JOBCHAIN_NODE
```

Each row's `HANDOFF` section should show a `built_file` path inside *that
row's own* work directory — `work/000001/built.txt` for `task-a`,
`work/000002/built.txt` for `task-b`, and so on. If any row shows another
row's path, or is missing the entry, that is the exact symptom this
example was built to catch.
