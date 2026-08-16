# Mutation testing

Mutation testing is a separate quality category from ordinary unit-test coverage.

- **Coverage** asks whether code was executed.
- **Mutation testing** asks whether the tests fail when behavior is deliberately
  made incorrect.

`run.py` is dependency-free and uses a small, reviewed set of semantic mutants
against the highest-risk Python behavior: state roll-up, terminal-state logic,
run continuation/force behavior, and scheduler result/error handling.

Run it with:

```sh
./mutation_tests/run.py
```

The command exits non-zero if a mutant survives or if the mutation runner has
an infrastructure error. A new mutant should normally be added when a bug-prone
branch is introduced, and any surviving mutant should result in a stronger test
rather than being silently excluded.

Current baseline: **9/9 mutants killed (100% mutation score)**.
