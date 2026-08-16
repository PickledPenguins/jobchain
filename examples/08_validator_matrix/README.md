# Validator matrix example

A compact, user-facing example covering every declarative field validator and
all declarative row/file validator families.

It demonstrates `int`, `float`, `str`, `bool`, `one_of`, `exact`, `regex`,
`path_exists`, `output_path`, `all_of`, `any_of`, `required_when`, `compare`,
`unique`, and `row_count` in one valid parameter set.

Run:

```sh
jobchain run config.yaml --check
jobchain run config.yaml --no-submit
```
