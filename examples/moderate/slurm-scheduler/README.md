# slurm-scheduler

The same two-stage shape as `../../simple/two-stage-basic`, but with
`scheduler: slurm` in the config. Every generated script gets `#SBATCH`
directives instead of `#PBS`, submission uses `sbatch` with
`--dependency=<type>:<jobid>` instead of `qsub -W depend=<type>:<jobid>`,
and status polling uses `squeue`/`sacct` instead of `qstat`.

## What it shows

- `scheduler: slurm` at the top level of the config
- `#SBATCH` directives generated from the same per-stage resource keys
  (`walltime`, `ncpus`, `mem`) used for PBS elsewhere in these examples —
  the resource keys are scheduler-agnostic; only the directive syntax
  changes
- `sbatch --dependency=afterany:<jobid>` for the chaining stage

## Run it

```sh
jobchain run config.yaml --check
jobchain run config.yaml
jobchain show --row slurm-one --full
```

A real cluster needs `sbatch`, `squeue`, `sacct`, and `scancel` on `PATH`;
this example was verified against a stub Slurm scheduler that mimics their
output formats closely enough to exercise jobchain's own polling logic
(`squeue` for a running job, falling back to `sacct` once it leaves the
queue).

## Cross-checked against both node helpers

This example was also run with `JOBCHAIN_NODE` pointed at the shell
helper (`bin/jobchain-node.sh`) instead of the default compiled one, which
exercises Slurm's `--export=ALL,<list>` self-chained submission path
specifically — see `../shell-helper-node` and `../../../BUGFIXES.md` item
4 for a real defect that lived in exactly that code path.
