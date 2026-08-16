# Jobchain examples

These examples are both user documentation and executable test fixtures.
They intentionally use small deterministic workloads so the same
configurations can be run against a real scheduler or the repository's stub
scheduler.

| Example | Level | Main capabilities |
|---|---|---|
| `01_basic` | Simple | rows, schema, command stage, templates, width |
| `02_validation` | Moderate | validators, defaults, row/file checks, paths |
| `03_pipeline` | Moderate | pipeline, dependencies, resources, handoff |
| `04_dynamic_resources` | Moderate | row-dependent CPU/GPU/memory/walltime |
| `05_failure_recovery` | Moderate | failure, afterany, generation, rerun |
| `06_formats` | Moderate | CSV quoting and embedded delimiters |
| `07_complex` | Complex | combined end-to-end feature coverage |
| `08_validator_matrix` | Moderate | validator combinations and boundaries |
| `09_pipeline_matrix` | Complex | mixed stages, dependencies, chaining, resources |
| `10_load_100` | Long | 100-row generation/load scenario |
| `11_load_1000` | Long | 1,000-row generation/load scenario |
| `12_negative_matrix` | Moderate | invalid configurations and negative regression cases |
| `13_operations` | Complex | run, rerun, cancellation, regeneration, operational state |

The automated suite classifies these as smoke, integration, E2E, and
regression scenarios. Long-running and real-scheduler scenarios will be added
separately and will never run as part of the normal short test set.
### Additional coverage examples

| Example | Main capability |
|---|---|
| `16_schema_edges` | optional/default fields, composite checks, paths, row/file checks |
| `17_input_formats` | headerless whitespace input, comments, blank rows |
| `18_resource_precedence` | pipeline/stage/row resource precedence |
| `19_stage_settings` | repeated class with distinct settings |
| `20_handoff_generations` | handoff values and generation-specific paths |
| `21_scheduler_equivalence` | PBS/Slurm rendering comparison |
| `22_multirun_isolation` | reusable configurations and run isolation |
| `23_comments_and_empty_rows` | comments, blank records, row counts |
| `24_max_in_flight` | width versus unfinished-pipeline ceiling |
| `25_quoted_csv` | quoted delimiters and escaped quotes |
| `26_tab_delimiter` | tab-delimited input |
| `27_literal_delimiter` | literal delimiter character |
| `28_header_warning` | header mismatch warning without positional breakage |
| `29_optional_defaults` | optional fields and defaults reaching jobs |
| `30_output_paths` | unique, non-existing output paths |
| `31_env_and_directives` | pipeline environment and extra directives |
| `32_single_stage` | explicit single-stage equivalent |
| `33_long_pipeline` | six-stage dependency chain |
| `34_afternotok` | failure branch using `afternotok` |
| `35_multi_delimiter_values` | semicolon-delimited input |

