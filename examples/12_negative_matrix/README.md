# Negative input matrix

These fixtures are intentionally invalid. They demonstrate common configuration
and parameter-file failures and are executed as regression tests. A successful
validation is a failure for these examples: each case must be rejected without
a traceback.

Run one manually with:

```bash
jobchain run <case>/config.yaml --check
```

The command is expected to return a non-zero exit status.
