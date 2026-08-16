# Bottleneck & Scaling Tests

This category tests architecture-specific overload surfaces rather than generic
"large workload" survival.

## Surfaces

- **Discovery scaling** — sequential `rows.idx` scans with many unavailable rows.
- **Claim hotspot** — many workers competing through the atomic filesystem claim.
- **Reporting scaling** — state loading and report construction as history grows.
- **Scheduler backpressure** — repeated slow fake scheduler submissions through the real C helper.
- **Width profile** — increasing concurrent claimers to expose contention collapse.

These are regression guards, not hardware benchmarks. Thresholds are deliberately
loose so ordinary machine variance does not make the suite flaky. The tests are
expected to expose nonlinear regressions, leaks, and catastrophic throughput
collapse rather than establish universal performance numbers.

Run with:

```sh
python3 bottleneck_tests/run.py
```
