# Failure and recovery example

The `recoverable` row intentionally fails in `Solve`. `Archive` uses
`afterany`, so it still runs. The row can then be corrected and rerun with
`--set`.

```sh
jobchain run config.yaml
jobchain rerun --row recoverable --set fail_solve=no --chain
```

The work directory includes `{row.generation}` so a new generation does not
overwrite the previous attempt.
