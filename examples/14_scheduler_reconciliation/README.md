# Scheduler reconciliation

This example demonstrates how `jobchain doctor` detects state that no longer
matches the scheduler or filesystem. It is also the fixture used by the
reconciliation regression tests.

Normal execution:

```bash
jobchain run config.yaml
jobchain doctor
```

The automated tests use a deterministic scheduler stub and deliberately create
faults such as vanished jobs, missing stage scripts, missing job IDs, and a
missing parameter file. Production users should treat `doctor` as a diagnostic
and recovery command rather than manually editing `.jobchain` state.

Test this example with:

```bash
python -m unittest tests.test_reconciliation_matrix
```
