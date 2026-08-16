# single-job-yaml

Demonstrates the smallest useful configuration: no stage classes, no stage
module, field validation declared entirely in YAML. Each row becomes one job
running a shell command built from templated row values.

## What it shows

- YAML `fields:` validation (`regex`, `path_exists`, `int` with bounds)
- A pipeline with a single `command:` stage — no Python at all
- `{row.<column>}` and `{run.home}` template expansion in a command
- `JC_<COLUMN>` environment variables available to a running job

## Run it

```sh
jobchain run config.yaml --check          # validate only
jobchain run config.yaml                  # validate, generate, submit
jobchain status --watch
jobchain show --row job-two --full
```

Output lands under `.jobchain/single-job-yaml/output/<run_id>/`.
