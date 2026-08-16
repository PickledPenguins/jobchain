# Operational examples

These examples exercise stateful jobchain operations rather than only initial
configuration. They are intended to be both user documentation and executable
regression scenarios.

The scenarios cover preparation, resumption, partial reruns, regeneration,
fresh handoff, cancellation, and doctor/reconciliation.

Run the associated tests with:

```bash
python -m unittest tests.test_operational_matrix
```
