# Validation example

Demonstrates `regex`, `one_of`, `int`, `float`, `path_exists`, `output_path`,
optional/default values, conditional requirements, comparisons, and a file
uniqueness check.

```sh
jobchain run config.yaml --check
```

To experiment with failures, edit `params.psv` and introduce a duplicate
output path, an invalid thread count, or a missing input file.
