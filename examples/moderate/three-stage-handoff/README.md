# three-stage-handoff

Three real `JobStage` classes, chained `afterok` then `afterany`, covering
both flavors of handoff and a stage `Choice` setting.

## What it shows

- `ctx.emit_shell_expr(key, shell_expr)` — Prep captures a checksum only
  known once `cksum` actually runs, and must use this rather than `emit()`
  (see `../../simple/two-stage-basic` for `emit()` with a literal)
- A `Choice` setting (`precision`) declared on `Solve` and set from the
  pipeline YAML — note this configures the stage itself, not a per-row
  choice; jobchain does not support per-row branching (see the README's
  "Out of scope" table)
- `pipeline.defaults` (`queue: normal`) merged under each stage's own keys
- Two handoff keys across three stages: `checksum` (Prep→Solve) and
  `result_path` (Solve→Archive)
- `depends: afterok` then `depends: afterany` in one pipeline

## Run it

```sh
jobchain run config.yaml --check
jobchain run config.yaml
jobchain show --row sweep-a --full
```

`show` lists both `checksum` and `result_path` under `HANDOFF`, and the
row's work directory contains `archive/result-double.txt`.
