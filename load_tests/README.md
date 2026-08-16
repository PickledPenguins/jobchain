# Load testing

Load testing is a separate quality category from unit, mutation, state/property,
concurrency, and fault-injection testing.

The tests use bounded, reproducible workloads and conservative time ceilings.
They are intended to detect catastrophic performance regressions, correctness
loss at larger state sizes, and throughput collapse under process contention.
They are **not** a hardware benchmark or a promise of a particular production
throughput.

Run with:

```sh
make load
```

Current workloads:

- 5,000-row state load/read
- 2,000-row claim workload with 24 processes
- three repeated 500-row/8-process stability samples
