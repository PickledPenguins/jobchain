# Concurrency & Race Testing

This category targets correctness when independent jobchain processes act on the
same run at the same time. It is intentionally separate from ordinary unit
coverage and the node-helper contention tests in `tests/test_node.py`.

The tests cover:

- the Python `Store.claim()` wrapper under process contention;
- setup-lock ownership, proving that a held lock has exactly one owner;
- stop/claim coordination and the post-stop quiescence guarantee;
- generation isolation after concurrent activity.

The tests use real processes and the real compiled `jobchain-node` helper. A
fixed start gate makes the contention reproducible while avoiding timing-based
sleep assertions.

Run with:

```sh
make concurrency
python3 concurrency_tests/run.py
```
