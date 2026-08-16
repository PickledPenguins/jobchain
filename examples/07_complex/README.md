# Complex example

This is the combined feature example. It exercises:

- conditional and comparison validation;
- path and output-path validation;
- uniqueness checks;
- CPU/GPU resource selection;
- pipeline defaults and per-stage overrides;
- `afterok` and `afterany` dependencies;
- handoff between stages;
- generation-aware work directories; and
- strict validation.

Start with validation:

```sh
jobchain run config.yaml --check
```

Then generate scripts:

```sh
jobchain run config.yaml --no-submit
```
