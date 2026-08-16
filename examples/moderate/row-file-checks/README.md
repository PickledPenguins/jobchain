# row-file-checks

A schema exercising both row-level and file-level checks together. The
stage itself (`mkdir -p`) is incidental; the point is `--check`'s report.

## Rows and what each one violates

| Row | Violates | Which check |
|---|---|---|
| `gpu-ok` | nothing | — |
| `gpu-missing` | `ngpus` blank while `mode: gpu` | `required_when` |
| `cpu-ok` | nothing | — |
| `bad-compare` | `ngpus (4) > threads (2)` | `compare` |
| `dup-output` | `output_dir` repeats `cpu-ok`'s | `unique` (file-level) |

Five data rows also sit just above `row_count`'s `min: 3`, so removing two
of them would additionally trip that check — worth trying.

## What it shows

- `required_when` — a column becomes mandatory only conditional on another
  column's value (an `optional: true` field made mandatory in one case)
- `compare` — a numeric relationship between two columns, with an operator
  (`<=` here; `<`, `>`, `>=`, `==`, `!=` are also available)
- `unique` (file-level) — a value that must not repeat across the whole
  parameter file, not just within one row
- `row_count` (file-level) — bounds on the number of data rows
- A YAML gotcha worth knowing: unquoted `yes`/`no` in YAML parse as
  booleans, not strings — not hit here, but see
  `../depends-variations/config.yaml` for where it was

## Run it

```sh
jobchain run config.yaml --check
```

The report lists four failures, one per violating row (`dup-output` is
reported once, as a file-level failure, not attached to either of the two
rows sharing the value).
