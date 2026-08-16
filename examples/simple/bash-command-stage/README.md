# bash-command-stage

A two-stage pipeline with no Python stage classes and no `stage_module` at
all — every stage is a `command:` string. Demonstrates that handoff values
work the same way for a bare command as for a `JobStage` class: the first
stage calls `$JC_NODE emit` directly (the same primitive `ctx.emit()`
wraps), and the second reads it back as `$JC_OUT_<key>`.

## What it shows

- A multi-stage pipeline with `stage_module` omitted entirely
- `depends: afterany` on the last (chaining) stage, required because a
  chaining stage cannot depend `afterok` — see "Chaining" in the README
- Handoff via `$JC_NODE emit` / `$JC_OUT_<key>`, without a stage class
- `walltime` set per stage in YAML with no class involved

## Run it

```sh
jobchain run config.yaml --check
jobchain run config.yaml
jobchain status
jobchain show --row convert-x --full
```

`verified.txt` only appears if the `verify` stage's `[ -s ... ]` check
passed, which it can only do if the handoff value from `convert` reached it.
