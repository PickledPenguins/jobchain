# 100-row load example

Short load fixture: 100 independent rows through a single-stage pipeline.
It is intentionally excluded from the normal smoke suite and can be run as a
load/regression test when explicitly requested.

```sh
jobchain run config.yaml --no-submit
```
