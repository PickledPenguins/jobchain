# single-job-class

The same shape as `single-job-yaml`, but validation is written as a Python
`SchemaBase` subclass (`validators.py`) instead of a YAML `fields:` list.
Both forms build identical `Field`/`Schema` objects; choose a class when a
check is awkward to express declaratively, as `check_row` here does for a
cross-column rule that a declarative `row_checks:` entry could not express
as written (comparing against a literal threshold conditional on another
column already covers `compare`/`required_when`, but combining "only in
fast mode" with a numeric bound in one message reads more clearly as code).

## What it shows

- `validator_class:` pointing at a Python module
- `SchemaBase` with declarative `fields` plus an overridden `check_row`
- A `str`-shaped column read via `path_exists`, `one_of`, and `float`
  validators, all expressed as Python objects rather than YAML mappings

## Run it

```sh
jobchain run config.yaml --check
jobchain run config.yaml
jobchain show --row run-bravo --full
```

Edit `runs.psv` to set `run-alpha`'s `mode` to `accurate` and its
`scale_factor` above `2.0`, or leave it `fast` with a `scale_factor` of
`2.0` or higher, to see `check_row` reject it — a rule no single-field
validator in the YAML reference can express on its own.

