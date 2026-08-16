# Pipeline example

Demonstrates a three-stage pipeline, `afterok`/`afterany` dependencies,
row-dependent resources, and handoff values.

```sh
jobchain run config.yaml --check
jobchain run config.yaml --no-submit
jobchain run config.yaml
```

The `Solve` stage receives the path emitted by `Prep` as `JC_OUT_mesh_file`.
`Archive` is deliberately configured with `afterany` so it can run even when
an upstream stage fails.
