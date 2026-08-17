"""Security-focused tests: malformed and adversarial row data, injection
through templated commands, resource limits, and how jobchain fails when
the filesystem or configuration is not what it expects.

Threat model: jobchain runs with the invoking operator's own privileges on
a shared HPC filesystem, so this is not about a sandboxed multi-tenant
boundary. What matters here is (a) row data in a parameter file, which may
come from an upstream pipeline, a shared file another user can edit, or
simple operator error, should not let arbitrary shell commands run beyond
what the pipeline's own `command:` or stage code already intends, and (b)
malformed input, missing files, or a hostile filesystem should fail
cleanly rather than crash, corrupt state, or silently produce wrong
results. Tests that document an existing, unfixed gap say so explicitly
in a comment, rather than asserting a safe outcome that is not actually
true.
"""

from __future__ import annotations

import os
import subprocess
import unittest

from tests.helpers import NODE_BINARY, TempProject, require_node_binary


class TestCommandTemplateInjection(TempProject):
    """`command:` stages interpolate {row.<column>} into shell text as a
    `$JC_<column>` variable reference, not the value itself (jobchain/
    config.py expand_template, shell=True, called from scheduler.py
    RowContext.expand). The actual value reaches the script only through
    the row's env file, written by store.render_env with proper shell
    quoting and sourced by ctx.preamble() before the command body runs.
    Because the shell parses command structure before it expands a
    variable, nothing embedded in the value -- unbalanced quotes, `;`,
    `&&`, backticks -- can introduce new shell syntax merely by being
    substituted into a template, regardless of whether the template
    itself quotes the placeholder.
    """

    def _project(self, command: str, value: str) -> None:
        self.write("params.psv", f"id|payload\nrow1|{value}\n")
        self.write("config.yaml", f"""\
name: test-run
params: params.psv
width: 1
schema:
  name: s
  format: {{delimiter: pipe, header: true, id_field: id}}
  fields:
    - {{name: id, type: str}}
    - {{name: payload, type: str}}
pipeline:
  stages:
    - name: only
      command: '{command}'
""")

    def test_an_unquoted_placeholder_must_not_let_row_data_run_as_shell_code(self):
        # Row data must never become executable shell syntax merely because
        # a template author omitted quoting.
        marker = self.path("injected.marker")
        self._project("touch {run_marker} && true # {row.payload}"
                       .replace("{run_marker}", marker),
                       f"safe && touch {marker}INJECTED #")
        require_node_binary()
        os.environ["JOBCHAIN_NODE"] = NODE_BINARY
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        script = self.path(".jobchain", "test-run", "work", "000001",
                            "01-only.sh")
        content = self.read(script)
        # The generated script must not contain the row value as live shell
        # syntax: it is referenced as $JC_payload, with the actual value
        # only in the separately-sourced, quoted env file.
        self.assertNotIn(f"touch {marker}INJECTED #", content)

    def test_double_quoting_the_placeholder_contains_the_value(self):
        # The defense is straightforward once known: quote every {row.*}
        # reference. A value that would be dangerous unquoted becomes
        # inert literal text once the template wraps it in double quotes,
        # PROVIDED the value itself contains no double quote (see the
        # next test for what happens when it does).
        out = self.path("out.txt")
        self._project(f'echo "value: {{row.payload}}" > {out}',
                       "hello; touch /tmp/should-not-exist-security-test")
        require_node_binary()
        os.environ["JOBCHAIN_NODE"] = NODE_BINARY
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertFalse(
            os.path.isfile("/tmp/should-not-exist-security-test"))
        self.assertIn("hello; touch", self.read(out))

    def test_an_embedded_double_quote_must_not_break_out_of_double_quoting(self):
        # Even a quoted row placeholder must remain inert when the value
        # itself contains shell metacharacters, including a double quote.
        out = self.path("out.txt")
        marker = self.path("broke-out.marker")
        self._project(f'echo "value: {{row.payload}}" > {out}',
                       f'" && touch {marker} && echo "')
        require_node_binary()
        os.environ["JOBCHAIN_NODE"] = NODE_BINARY
        self.install_scheduler()
        self.run_cli("run", "config.yaml", expect=0)
        self.wait_for_jobs()
        self.assertFalse(
            os.path.isfile(marker),
            "row data escaped from a double-quoted placeholder and executed "
            "as shell syntax")


class TestMalformedParameterFiles(TempProject):
    """Malformed, oversized, or unusual parameter data should fail
    validation cleanly (or, where validation has no opinion, produce a
    script that is well-formed even if the value is unusual) rather than
    crash jobchain or corrupt run state.
    """

    def _project(self) -> None:
        self.write("config.yaml", """\
name: test-run
params: params.psv
width: 1
schema:
  name: s
  format: {delimiter: pipe, header: true, id_field: id}
  fields:
    - {name: id, type: str}
    - {name: a, type: str}
    - {name: b, type: str}
pipeline:
  stages:
    - name: only
      command: 'echo ok'
""")

    def test_a_row_with_too_few_fields_is_rejected_not_crashed_on(self):
        self._project()
        self.write("params.psv", "id|a|b\nrow1|onlyone\n")
        result = self.run_cli("run", "config.yaml", "--check", expect=3)
        self.assertIn("expected 3 field(s), found 2",
                       result.stdout + result.stderr)

    def test_a_row_with_too_many_fields_is_rejected_not_crashed_on(self):
        self._project()
        self.write("params.psv", "id|a|b\nrow1|one|two|three|four\n")
        result = self.run_cli("run", "config.yaml", "--check", expect=3)
        self.assertIn("expected 3 field(s), found 5",
                       result.stdout + result.stderr)

    def test_an_embedded_null_byte_does_not_crash_validation_or_generation(self):
        self._project()
        with open(self.path("params.psv"), "wb") as handle:
            handle.write(b"id|a|b\nrow1|hel\x00lo|world\n")
        require_node_binary()
        os.environ["JOBCHAIN_NODE"] = NODE_BINARY
        # No specific exit code asserted beyond "did not crash": a NUL
        # byte in a str field has no field-level validator to reject it,
        # so the practically important property is that scanning and
        # script generation complete rather than raising an unhandled
        # exception (a stack trace on stderr with no jobchain error
        # prefix, or a non-standard exit code, would indicate a crash).
        result = self.run_cli("run", "config.yaml", "--no-submit")
        self.assertIn(result.returncode, (0, 3, 4))
        self.assertNotIn("Traceback (most recent call last)", result.stderr)

    def test_a_100kb_field_value_does_not_crash_or_truncate_the_row_env(self):
        # The compiled node helper reads several small, fixed-size
        # buffers via fgets (memory-safe: it truncates rather than
        # overflowing), but the row's env file -- where a long field
        # value like this one lives -- is sourced directly by the shell,
        # which has no such limit. This pins down that a large value
        # survives intact all the way through script generation.
        self._project()
        long_value = "A" * 100_000
        self.write("params.psv", f"id|a|b\nrow1|{long_value}|short\n")
        require_node_binary()
        os.environ["JOBCHAIN_NODE"] = NODE_BINARY
        self.run_cli("run", "config.yaml", "--no-submit", expect=0)
        env_path = self.path(".jobchain", "test-run", "rows", "000001", "env")
        content = self.read(env_path)
        self.assertIn(long_value, content)

    def test_a_completely_empty_parameter_file_is_a_clean_error(self):
        self._project()
        self.write("params.psv", "")
        result = self.run_cli("run", "config.yaml", "--check")
        self.assertNotIn("Traceback (most recent call last)", result.stderr)

    def test_a_parameter_file_with_only_a_header_produces_zero_rows(self):
        self._project()
        self.write("params.psv", "id|a|b\n")
        result = self.run_cli("run", "config.yaml", "--check", expect=0)
        self.assertIn("0 row", result.stdout + result.stderr)


class TestMissingAndHostileFilesystemState(TempProject):
    """Missing files, bad paths, and directories that are not what
    jobchain expects should produce a clear error rather than a crash or
    silent misbehavior.
    """

    def test_a_nonexistent_parameter_file_is_a_clean_error(self):
        self.write("config.yaml", """\
name: test-run
params: does-not-exist.psv
width: 1
schema:
  name: s
  format: {delimiter: pipe, header: true, id_field: id}
  fields:
    - {name: id, type: str}
pipeline:
  stages:
    - name: only
      command: 'echo ok'
""")
        result = self.run_cli("run", "config.yaml", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stdout + result.stderr)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)

    def test_a_config_referencing_a_nonexistent_stage_module_is_a_clean_error(self):
        self.write("params.psv", "id\nrow1\n")
        self.write("config.yaml", """\
name: test-run
params: params.psv
width: 1
schema:
  name: s
  format: {delimiter: pipe, header: true, id_field: id}
  fields:
    - {name: id, type: str}
pipeline:
  stage_module: does_not_exist.py
  stages:
    - {name: only}
""")
        result = self.run_cli("run", "config.yaml", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)

    def test_malformed_yaml_in_the_config_is_a_clean_error(self):
        self.write("params.psv", "id\nrow1\n")
        self.write("config.yaml", "name: test-run\nparams: [this is not\n"
                    "  valid: yaml: at: all\n")
        result = self.run_cli("run", "config.yaml", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)

    def test_a_config_that_is_a_directory_not_a_file_is_a_clean_error(self):
        os.makedirs(self.path("config.yaml"))
        result = self.run_cli("run", "config.yaml", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)

    def test_status_on_a_run_that_was_never_created_is_a_clean_error(self):
        result = self.run_cli("status", "--run", "never-existed")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)


class TestCLIArgumentMisuse(TempProject):
    """Malformed or hostile command-line invocations should be rejected
    with a usage error, not crash or silently do something unintended.
    """

    def test_an_unknown_command_is_a_clean_usage_error(self):
        result = self.run_cli("this-is-not-a-real-command")
        self.assertNotEqual(result.returncode, 0)

    def test_a_negative_width_is_rejected(self):
        self.write("params.psv", "id\nrow1\n")
        self.write("config.yaml", """\
name: test-run
params: params.psv
width: -1
schema:
  name: s
  format: {delimiter: pipe, header: true, id_field: id}
  fields:
    - {name: id, type: str}
pipeline:
  stages:
    - name: only
      command: 'echo ok'
""")
        result = self.run_cli("run", "config.yaml", "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)

    def test_run_with_no_config_argument_is_a_usage_error_not_a_crash(self):
        result = self.run_cli("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)


class TestNodeHelperMisuse(TempProject):
    """Direct, malformed invocations of the compute-node helper -- as if
    a job's environment were corrupted, or the helper were invoked by
    hand with the wrong arguments -- should fail cleanly. This matters
    because the helper runs on a compute node with no interactive
    operator present to notice a hang or a crash.
    """

    def setUp(self) -> None:
        super().setUp()
        require_node_binary()

    def _run_node(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([NODE_BINARY, *args], capture_output=True,
                              text=True, timeout=10)

    def test_claim_against_a_nonexistent_home_directory_is_a_clean_error(self):
        result = self._run_node("claim", "--home",
                                self.path("does-not-exist"))
        self.assertNotEqual(result.returncode, 0)

    def test_emit_with_a_key_value_pair_missing_a_value_is_rejected(self):
        os.makedirs(self.path("run-dir"))
        result = self._run_node("emit", "--run", self.path("run-dir"),
                                "keywithnovalue")
        self.assertNotEqual(result.returncode, 0)

    def test_mark_with_an_unrecognized_status_is_accepted_and_displayed_verbatim(self):
        # Documents actual behavior: mark --status does not validate its
        # argument against the known status vocabulary
        # (RUNNING/DONE/FAILED/CANCELLED/...). An arbitrary string is
        # written to the row's status file as-is, and `show` later
        # displays it verbatim in the STAGES table with no sanitization.
        # This is reachable from a corrupted job script, a bug in a
        # JobStage subclass that calls the wrong status constant, or a
        # hand-run mark invocation with a typo -- not from row data
        # directly, since callers construct --status from a fixed set of
        # constants rather than from a field value, but it is a gap worth
        # having coverage for: a future caller building --status
        # dynamically (a custom stage class, a wrapper script) gets no
        # help from jobchain-node catching the mistake.
        os.makedirs(self.path("run-dir"))
        result = self._run_node("mark", "--run", self.path("run-dir"),
                                "--stage", "only", "--status",
                                "NOT_A_REAL_STATUS")
        self.assertEqual(result.returncode, 0)
        status_path = self.path("run-dir", "status.only")
        self.assertEqual(self.read(status_path), "NOT_A_REAL_STATUS")

    def test_no_subcommand_at_all_is_a_usage_error(self):
        result = self._run_node()
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
