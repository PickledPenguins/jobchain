# depends-variations

Four stages exercising all three `depends` values in one pipeline:
`prep` → `solve` (`afterok`) → `diagnose` (`afternotok`) → `archive`
(`afterany`, the chaining stage). `solve` fails on purpose for the row
whose `should_fail` column is `yes`, so a single run shows both branches.

## What it shows

| Row | `solve` | `diagnose` (`afternotok`) | `archive` (`afterany`) |
|---|---|---|---|
| `good` | succeeds | dependency never satisfied; the scheduler never runs it | runs |
| `bad` | fails | dependency satisfied; runs | runs |

- `depends: afterok` — the default, shown explicitly
- `depends: afternotok` — a diagnostic stage that only runs on failure
- `depends: afterany` — always runs; required on the chaining (last) stage
- A pipeline where the same run produces two different stage-skip
  outcomes depending on row data

## Run it

```sh
jobchain run config.yaml --check
jobchain run config.yaml
jobchain status
jobchain show --row good --full
jobchain show --row bad --full
```

`bad`'s work directory has `diagnosis.txt`; both rows' work directories
have `archived.txt`, since `archive` (`afterany`) always runs.

## What jobchain cannot see directly, and why `doctor` matters here

A real scheduler never dispatches a job whose dependency condition was not
met — `good`'s `diagnose` (which needed `solve` to fail) is simply never
run, with no notification to jobchain. Its status stays at whatever it was
when submitted (`QUEUED`) until something reconciles it. This is exactly
the "cancelled while queued" row the README's dependency table describes as
**never satisfied** from jobchain's point of view, and it is why `doctor`
exists rather than being optional:

```sh
jobchain doctor            # reports the drift as a finding
jobchain doctor --repair   # marks the row failed at that stage
```

`--repair` cannot distinguish a dependency-cancelled job from one that
genuinely vanished, so it marks both `failed` rather than resolving them
differently — a limitation the README states plainly. For this example
that is the correct, informative outcome: it demonstrates why a production
pipeline runs `doctor --all --repair` on a schedule rather than trusting
`status` alone to reflect scheduler-side cancellations.
