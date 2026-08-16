# Concurrent run operations

This example demonstrates two important concurrency properties of jobchain:

1. Two processes preparing the same run cannot both own the preparation lock.
2. Independent run names use independent run directories and can be prepared concurrently.

The scenario is intentionally scheduler-free (`--no-submit`) so it tests filesystem and run-state coordination without introducing scheduler timing.

## Run manually

```bash
jobchain run config.yaml --no-submit --run-name alpha
```

For concurrency testing, invoke the same command from multiple processes at once.
