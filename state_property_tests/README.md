# State & Property Testing

This is a dedicated testing category for jobchain's persisted state-model invariants.

The suite uses deterministic generated cases (with a fixed seed) so failures are reproducible without adding a third-party property-testing dependency.

Run:

    python3 state_property_tests/run.py

The category focuses on state transitions, terminal-state behavior, dependency/status rollups, attempt/generation invariants, and malformed-state handling.
