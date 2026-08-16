"""Shared fixtures for the jobchain test suite.

The suite runs against the real compiled helper, because the claim protocol
is where a subtle mistake produces two jobs running the same parameters, and
a Python reimplementation would prove nothing about what runs on a node.

Scheduler clients are stubbed. The stub honours dependencies: a job with a
dependency waits for its predecessor and, for afterok, does not run at all if
the predecessor failed. Without that, a pipeline's stages would race and the
tests would pass or fail by timing rather than by behaviour.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

NODE_BINARY = os.environ.get(
    "JOBCHAIN_NODE", os.path.join(PROJECT_ROOT, "bin", "jobchain-node")
)

SIMPLE_PARAMS = """\
rid|count|label
a1|5|first
a2|10|second
a3|15|third
a4|20|fourth
"""

#: A configuration with no pipeline: one job per row.
SIMPLE_CONFIG = """\
name: {name}
params: params.psv
width: {width}
scheduler: pbs

schema:
  name: simple
  format: {{delimiter: pipe, header: true, id_field: rid}}
  fields:
    - {{name: rid,   type: regex, pattern: "[a-z0-9]+"}}
    - {{name: count, type: int, min: 1, max: 100}}
    - {{name: label, type: str, optional: true}}

pipeline:
  name: single
  stages:
    - {{name: work, command: "true"}}
"""

#: A three-stage pipeline whose classes live in stages.py.
PIPELINE_CONFIG = """\
name: {name}
params: params.psv
width: {width}
scheduler: pbs

schema:
  name: simple
  format: {{delimiter: pipe, header: true, id_field: rid}}
  fields:
    - {{name: rid,   type: regex, pattern: "[a-z0-9]+"}}
    - {{name: count, type: int, min: 1, max: 100}}
    - {{name: label, type: str, optional: true}}

pipeline:
  name: three
  stage_module: stages.py
  defaults: {{queue: normal}}
  stages:
    - {{name: prep,    walltime: "00:10:00", ncpus: 1}}
    - {{name: solve,   depends: afterok}}
    - {{name: archive, depends: afterany}}
"""

STAGES_MODULE = '''\
"""Stage classes used by the test suite."""

from jobchain import Choice, JobStage


class Prep(JobStage):
    """Publishes a value for later stages."""

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
echo prepared > "{ctx.work_dir}/mesh.txt"
rc=$?
{ctx.emit('mesh', ctx.work_dir + '/mesh.txt')}
{ctx.epilogue()}
exit $rc
""")


class Solve(JobStage):
    """Fails for any row whose label is 'boom'."""

    settings = {"precision": Choice(["single", "double"], default="double")}

    def resources(self, row):
        return {"ncpus": row["count"], "walltime": "01:00:00"}

    def write_script(self, row, ctx):
        # The work must not exit the script directly: the epilogue records
        # the status, so a stage that exits early leaves the row stuck.
        work = "false" if row.get("label") == "boom" else "true"
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
echo "solved from $JC_OUT_mesh" > "{ctx.work_dir}/result.txt"
{work}
rc=$?
{ctx.epilogue()}
exit $rc
""")


class Archive(JobStage):
    """Runs whatever happened upstream, and chains."""

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}
mkdir -p "{ctx.work_dir}/archive"
rc=0
{ctx.epilogue()}
exit $rc
""")
'''


def require_node_binary() -> None:
    """Skip a module when the compiled helper has not been built."""
    if not (os.path.isfile(NODE_BINARY) and os.access(NODE_BINARY, os.X_OK)):
        raise unittest.SkipTest(f"{NODE_BINARY} has not been built; run 'make'")


class TempProject(unittest.TestCase):
    """Base class providing an isolated project directory per test."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="jobchain-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin_dir = os.path.join(self.tmp, "stubbin")
        os.makedirs(self.bin_dir, exist_ok=True)
        self._original_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self) -> None:
        os.environ.clear()
        os.environ.update(self._original_environ)

    # -- files -----------------------------------------------------------

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def write(self, name: str, content: str) -> str:
        target = self.path(name)
        os.makedirs(os.path.dirname(target) or self.tmp, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
        return target

    def write_executable(self, name: str, content: str) -> str:
        target = self.write(name, content)
        os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        return target

    def read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    # -- project scaffolding ---------------------------------------------

    def make_project(self, pipeline: bool = False, width: int = 1,
                     name: str = "test-run", params: Optional[str] = None,
                     config: Optional[str] = None) -> str:
        """Write a parameter file, a configuration, and stage classes."""
        self.write("params.psv", params if params is not None else SIMPLE_PARAMS)
        if pipeline:
            self.write("stages.py", STAGES_MODULE)
        if config is not None:
            # A caller supplying a whole configuration has already written the
            # values it wants; formatting it again would treat its YAML braces
            # as placeholders.
            return self.write("config.yaml", config)
        template = PIPELINE_CONFIG if pipeline else SIMPLE_CONFIG
        return self.write("config.yaml", template.format(name=name, width=width))

    # -- scheduler stubs -------------------------------------------------

    def install_scheduler(self, kind: str = "pbs", run_inline: bool = True,
                          fail: bool = False, alive: bool = False) -> str:
        """Install stub scheduler binaries and put them on PATH.

        With run_inline the stub executes submitted scripts, honouring
        dependencies, so a whole pipeline and the chain that follows it
        actually run.
        """
        state = os.path.join(self.bin_dir, "state")
        os.makedirs(state, exist_ok=True)

        if fail:
            body = 'echo "queue is full" >&2\nexit 1\n'
        else:
            body = _STUB.format(state=state, inline="1" if run_inline else "0",
                                kind=kind)
        self._install("qsub" if kind == "pbs" else "sbatch", body)

        if alive:
            self._install("qstat", 'echo "    job_state = R"\nexit 0\n')
            self._install("squeue", "echo RUNNING\nexit 0\n")
        else:
            self._install("qstat", "exit 153\n")
            self._install("squeue", "exit 0\n")
        self._install("sacct", "echo COMPLETED\n")
        self._install("qdel", f'echo "$1" >> {state}/cancelled\nexit 0\n')
        self._install("scancel", f'echo "$1" >> {state}/cancelled\nexit 0\n')

        os.environ["PATH"] = self.bin_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["JOBCHAIN_NODE"] = NODE_BINARY
        return self.bin_dir

    def _install(self, name: str, body: str) -> str:
        path = os.path.join(self.bin_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def submissions(self) -> List[str]:
        """Every submission the stub recorded, in order."""
        log = os.path.join(self.bin_dir, "state", "submissions.log")
        if not os.path.isfile(log):
            return []
        with open(log, "r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def cancelled_jobs(self) -> List[str]:
        log = os.path.join(self.bin_dir, "state", "cancelled")
        if not os.path.isfile(log):
            return []
        with open(log, "r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]

    def wait_for_jobs(self, seconds: float = 60.0) -> None:
        """Wait until the stub has no jobs running and none pending.

        Quiescence is confirmed over several consecutive checks: a chaining
        stage submits its successor as it exits, so a single empty poll can
        fall in the gap between one pipeline finishing and the next starting.
        """
        state = os.path.join(self.bin_dir, "state")
        deadline = time.time() + seconds
        quiet = 0
        while time.time() < deadline:
            running = [n for n in os.listdir(state) if n.startswith("running.")]
            quiet = 0 if running else quiet + 1
            if quiet >= 5:
                return
            time.sleep(0.1)
        self.fail("stub jobs did not finish within the timeout")

    # -- command runner --------------------------------------------------

    def run_cli(self, *arguments: str, expect: Optional[int] = None,
                cwd: Optional[str] = None,
                stdin: str = "") -> subprocess.CompletedProcess:
        """Invoke the front end in this process and optionally assert its code.

        main() returns its exit code rather than calling sys.exit, so running
        in process exercises argument parsing, exit-code mapping, and output
        formatting exactly as the real entry point does, while remaining
        visible to coverage.
        """
        from jobchain.cli import main

        original_cwd = os.getcwd()
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            os.chdir(cwd or self.tmp)
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr), \
                    _replaced_stdin(stdin):
                code = main(list(arguments))
        except SystemExit as signal:
            code = int(signal.code or 0)
        finally:
            os.chdir(original_cwd)
            logger = logging.getLogger("jobchain")
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)

        completed = subprocess.CompletedProcess(
            args=list(arguments), returncode=code,
            stdout=stdout.getvalue(), stderr=stderr.getvalue())
        if expect is not None and completed.returncode != expect:
            self.fail(
                f"expected exit {expect}, got {completed.returncode}\n"
                f"command: jobchain {' '.join(arguments)}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
        return completed

    def run_cli_json(self, *arguments: str, expect: Optional[int] = 0) -> Any:
        """Run a command with --json and parse its output."""
        result = self.run_cli(*arguments, "--json", expect=expect)
        return json.loads(result.stdout)

    def run_node(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run([NODE_BINARY, *arguments], capture_output=True,
                              text=True, check=False, timeout=60)

    # -- state helpers ---------------------------------------------------

    def store_for(self, name: str = "test-run"):
        from jobchain.store import Store
        return Store(self.path(".jobchain", name))

    def statuses(self, name: str = "test-run") -> Dict[str, str]:
        """Map row id to status for one run."""
        return {row.row_id: row.status for row in self.store_for(name).load_rows()}


@contextlib.contextmanager
def _replaced_stdin(text: str):
    """Feed a command a canned answer, for confirmation prompts."""
    original = sys.stdin
    sys.stdin = _Stdin(text)
    try:
        yield
    finally:
        sys.stdin = original


class _Stdin(io.StringIO):
    """A stdin that reports itself as a terminal, so prompts are shown."""

    def isatty(self) -> bool:
        return True


# A stub scheduler that honours dependencies.
#
# Each submission records its job id and, if it depends on another, waits for
# that job to finish. afterok and afternotok also check the predecessor's exit
# status, so a cancelled-by-dependency stage genuinely does not run.
_STUB = r"""
STATE="{state}"
mkdir -p "$STATE"

n=$(cat "$STATE/counter" 2>/dev/null || echo 0)
n=$((n+1))
echo $n > "$STATE/counter"
jobid="$n.stub"

echo "$*" >> "$STATE/submissions.log"

env_arg=""
script=""
dep_type=""
dep_job=""
while [ $# -gt 0 ]; do
    case "$1" in
        -W) case "$2" in
                depend=*) spec="${{2#depend=}}"
                          dep_type="${{spec%%:*}}"
                          dep_job="${{spec#*:}}" ;;
            esac
            shift 2 ;;
        --dependency=*) spec="${{1#--dependency=}}"
                        dep_type="${{spec%%:*}}"
                        dep_job="${{spec#*:}}"
                        shift ;;
        -v) env_arg="$2"; shift 2 ;;
        --export=*) env_arg="${{1#--export=}}"; shift ;;
        -*) shift ;;
        *) script="$1"; shift ;;
    esac
done

if [ "{inline}" = "1" ]; then
    touch "$STATE/running.$jobid"
    (
        if [ -n "$dep_job" ]; then
            while [ -f "$STATE/running.$dep_job" ]; do sleep 0.05; done
            dep_rc=$(cat "$STATE/rc.$dep_job" 2>/dev/null || echo 0)
            case "$dep_type" in
                afterok)    [ "$dep_rc" -ne 0 ] && {{ echo 1 > "$STATE/rc.$jobid"
                                                     echo cancelled > "$STATE/cancelled.$jobid"
                                                     rm -f "$STATE/running.$jobid"
                                                     exit 0; }} ;;
                afternotok) [ "$dep_rc" -eq 0 ] && {{ rm -f "$STATE/running.$jobid"
                                                     exit 0; }} ;;
            esac
        fi
        if [ "{kind}" = "pbs" ]; then
            PBS_JOBID="$jobid"; export PBS_JOBID
        else
            SLURM_JOB_ID="$jobid"; export SLURM_JOB_ID
        fi
        IFS=','
        for kv in $env_arg; do
            case "$kv" in ALL) ;; *) export "$kv" ;; esac
        done
        unset IFS
        sh "$script" > "$STATE/job-$jobid.out" 2>&1
        echo $? > "$STATE/rc.$jobid"
        rm -f "$STATE/running.$jobid"
    ) &
fi

if [ "{kind}" = "pbs" ]; then
    echo "$jobid"
else
    echo "Submitted batch job $jobid"
fi
"""
