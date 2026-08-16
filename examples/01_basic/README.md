# Basic example

This is the smallest useful multi-row jobchain example. It demonstrates a
headered pipe-delimited parameter file, scalar validation, a single command
stage, row templates, and limited concurrency.

Validate without changing anything:

```sh
jobchain run config.yaml --check
```

Generate scripts without submitting them:

```sh
jobchain run config.yaml --no-submit
```

The generated scripts are under `.jobchain/example-basic/`.
