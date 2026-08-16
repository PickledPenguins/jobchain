# two-stage-basic

The minimal shape of a real pipeline: two `JobStage` subclasses in
`stages.py`, referenced through `stage_module:`. `Build` writes a file and
publishes its path through the handoff; `Finish` reads it back. Uses the
compiled C node helper (the default) — see `../shell-helper-node` for the
same pipeline run with the portable shell helper.

## What it shows

- `stage_module:` with two `JobStage` subclasses
- `ctx.emit(key, value)` publishing a path already known when the script is
  generated (`ctx.work_dir + "/built.txt"`) — see `../moderate/three-stage-handoff`
  for `ctx.emit_shell_expr()`, the counterpart for a value only known once
  the script is running
- `$JC_OUT_<key>` reading a handoff value back in a later stage
- `depends: afterany` on the chaining stage
- The compiled node helper (`bin/jobchain-node`), used implicitly

## Run it

```sh
jobchain run config.yaml --check
jobchain run config.yaml
jobchain show --row task-a --full
```

`show` prints a `HANDOFF` section with `built_file` set to the built file's
real path, and the row's work directory contains `built.txt` and
`line_count.txt`.
