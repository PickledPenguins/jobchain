# Dynamic resources example

Demonstrates row-dependent CPU, GPU, memory, and walltime requests together
with pipeline defaults.

```sh
jobchain run config.yaml --check
jobchain run config.yaml --no-submit
```

Inspect the generated scripts to see the resource differences for each row.
