"""Talking to the scheduler, and generating the scripts it runs.

Only what a chain needs is implemented: submit a script with an environment
and an optional dependency, ask whether a job is still alive, and cancel one.

Script generation lives here because a script is mostly scheduler directives,
and the two must agree about which scheduler is in use. One script is written
per stage per row, ahead of time, on the submit host: the chain continues from
inside a running job, where no interpreter is assumed, so a chaining job only
has to submit an already-rendered file.

Job state is reduced to three values. A scheduler that has forgotten a job
reports it as finished, because both schedulers age completed jobs out of
their active queues and an absent job is never one that is still running.

Scheduler-specific behavior (submit-command syntax, directive prefixes, job-
state queries, cancellation) lives entirely on ``SchedulerBackend``
subclasses -- one per scheduler kind -- following the same
abstract-base-plus-registry shape ``schema.validators``'s validators already use.
Everything else (subprocess execution, dependency threading across a
pipeline, dry-run behavior) is scheduler-agnostic and stays on the base
class or as free functions.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple

from .core import PBS, SLURM, SchedulerError, StateError, get_logger, trace
from .store import ManifestEntry, _write_text, shell_quote


class JobState(str, Enum):
    """A scheduler's own live answer to "is this job still running".

    Deliberately not merged with store.RowStatus: this answers a different
    question ("what does the live queue say right now" vs. "what does
    jobchain's own status file say"), and operations.doctor() exists
    specifically to reconcile the two by hand when they disagree -- which
    they legitimately can, e.g. a job that died before writing any status.

    See RowStatus in store/model.py for why __str__/__format__ are overridden.
    """

    ALIVE = "ALIVE"        # queued, held, or running
    FINISHED = "FINISHED"  # completed, failed, cancelled, or forgotten
    UNKNOWN = "UNKNOWN"    # the scheduler could not be consulted

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self.value), spec)


# Back-compat module attributes; see RowStatus in store/model.py for why a str
# mixin makes this a no-op change everywhere else.
ALIVE = JobState.ALIVE
FINISHED = JobState.FINISHED
UNKNOWN = JobState.UNKNOWN

_PBS_ALIVE_STATES = {"Q", "R", "H", "W", "T", "S", "B", "M"}
_SLURM_ALIVE_STATES = {"PENDING", "RUNNING", "SUSPENDED", "COMPLETING",
                       "CONFIGURING", "RESIZING", "REQUEUED", "SIGNALING"}


@dataclass
class Submission:
    """Outcome of one submission attempt."""

    job_id: Optional[str]
    success: bool
    command: List[str] = dc_field(default_factory=list)
    output: str = ""


class SchedulerBackend(ABC):
    """A workload manager this tool submits to.

    The scheduler is chosen by configuration, never detected. Detection would
    guess, and a wrong guess produces scripts whose directives the other
    scheduler silently ignores, so every job would run with no resources
    requested.

    A subclass supplies exactly the parts that differ between schedulers
    (command syntax, directive prefix, job-state parsing); everything about
    *running* a submission -- subprocess execution, error handling, threading
    dependencies through a pipeline -- is identical between schedulers and
    lives here once.
    """

    kind: ClassVar[str]
    submit_binary: ClassVar[str]
    #: How the chaining stage's self-submission pastes its KEY=VALUE
    #: environment onto the command line: PBS wants it as a separate argv
    #: word after "-v", so this carries a trailing space; Slurm glues it
    #: onto "--export=ALL," with no space. The C and shell node helpers
    #: read this (via write_facts) rather than knowing PBS/Slurm syntax
    #: themselves, so this string -- not a helper-side branch -- is the one
    #: place that knowledge lives.
    export_flag: ClassVar[str]
    #: The dependency flag's own prefix, e.g. "-W depend=" or
    #: "--dependency="; the node helpers glue "<type>:<jobid>" onto it.
    depend_flag: ClassVar[str]

    # -- what a subclass supplies -----------------------------------------

    @property
    @abstractmethod
    def directive_prefix(self) -> str:
        """The per-line directive marker, e.g. ``#PBS`` or ``#SBATCH``."""

    @property
    @abstractmethod
    def jobid_env_expr(self) -> str:
        """Shell expression for this scheduler's own job-id variable."""

    @abstractmethod
    def build_submit_command(self, script_path: str, exported: str,
                             depends_on: Optional[str],
                             depends_type: str) -> List[str]:
        """The full submit command line for one script."""

    @abstractmethod
    def parse_job_id(self, stdout: str) -> Optional[str]:
        """Extract the job identifier from a submit command's output."""

    @abstractmethod
    def query_job_state(self, job_id: str) -> str:
        """Consult the scheduler directly; ALIVE, FINISHED, or UNKNOWN."""

    @abstractmethod
    def cancel_command(self, job_id: str) -> List[str]:
        """The command line that cancels one job."""

    @abstractmethod
    def render_directives(self, resources: Dict[str, Any], run_name: str,
                          stage: str, row_name: str,
                          log_dir: str) -> List[str]:
        """Render this scheduler's resource directives for one script."""

    # -- shared behavior, identical across schedulers ----------------------

    @property
    def available(self) -> bool:
        return shutil.which(self.submit_binary) is not None

    def require_available(self) -> None:
        if not self.available:
            raise SchedulerError(
                f"'{self.submit_binary}' is not on PATH, so nothing can be "
                f"submitted to {self.kind} from this host"
            )

    def submit(self, script_path: str, environment: Dict[str, str],
               depends_on: Optional[str] = None,
               depends_type: str = "afterok") -> Submission:
        """Submit one script, optionally dependent on another job."""
        exported = ",".join(f"{k}={v}" for k, v in sorted(environment.items()))
        command = self.build_submit_command(script_path, exported, depends_on,
                                            depends_type)

        trace("submitting: %s", " ".join(command))
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SchedulerError(
                f"could not execute {self.submit_binary}: {exc}") from exc

        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if completed.returncode != 0:
            return Submission(job_id=None, success=False, command=command,
                              output=output)
        return Submission(job_id=self.parse_job_id(completed.stdout),
                          success=True, command=command, output=output)

    def submit_pipeline(self, entries: Sequence[ManifestEntry],
                        environment: Dict[str, str]) -> List[Tuple[str, Submission]]:
        """Submit a row's stages in order, threading dependency job ids.

        Returns one result per stage attempted. Submission stops at the first
        rejection; the caller cancels what was already submitted, because a
        pipeline missing a middle stage would leave its successors waiting on
        a dependency that can never be satisfied.
        """
        results: List[Tuple[str, Submission]] = []
        previous: Optional[str] = None
        for stage, depends, script in entries:
            submission = self.submit(
                script, environment,
                depends_on=previous if depends and depends != "-" else None,
                depends_type=depends if depends and depends != "-" else "afterok",
            )
            results.append((stage, submission))
            if not submission.success:
                break
            previous = submission.job_id
        return results

    def job_state(self, job_id: str) -> str:
        if not job_id:
            return UNKNOWN
        return self.query_job_state(job_id)

    def cancel(self, job_id: str) -> Tuple[bool, str]:
        command = self.cancel_command(job_id)
        completed = _capture(command)
        if completed is None:
            return False, f"{command[0]} is not available on PATH"
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        return completed.returncode == 0, output

    def write_facts(self, home: str) -> None:
        """Write this backend's submission syntax as small files under a run.

        The compute-node helper (C or shell) self-chains the next row's
        submission without a Python interpreter, so it cannot ask a
        SchedulerBackend anything at run time. Rather than have the helper
        re-derive qsub/sbatch syntax from an environment variable -- two
        independent copies of scheduler knowledge, one per implementation --
        it reads these facts instead. This is the only place PBS/Slurm
        command syntax is expressed; claiming, marking, and self-submission
        stay scheduler-agnostic everywhere else.
        """
        _write_text(os.path.join(home, "scheduler.submit_cmd"), self.submit_binary + "\n")
        _write_text(os.path.join(home, "scheduler.export_flag"), self.export_flag + "\n")
        _write_text(os.path.join(home, "scheduler.depend_flag"), self.depend_flag + "\n")


class PBSBackend(SchedulerBackend):
    kind = PBS
    submit_binary = "qsub"
    export_flag = "-v "
    depend_flag = "-W depend="

    @property
    def directive_prefix(self) -> str:
        return "#PBS"

    @property
    def jobid_env_expr(self) -> str:
        return "${PBS_JOBID:-}"

    def build_submit_command(self, script_path: str, exported: str,
                             depends_on: Optional[str],
                             depends_type: str) -> List[str]:
        command = [self.submit_binary]
        if depends_on:
            command += ["-W", f"depend={depends_type}:{depends_on}"]
        command += ["-v", exported, script_path]
        return command

    def parse_job_id(self, stdout: str) -> Optional[str]:
        text = (stdout or "").strip()
        if not text:
            return None
        # qsub prints the identifier alone, but site prologues sometimes
        # add banner lines, so the last non-empty line is the safer pick.
        return text.splitlines()[-1].strip()

    def query_job_state(self, job_id: str) -> str:
        completed = _capture(["qstat", "-f", "-x", job_id])
        if completed is None:
            return UNKNOWN
        if completed.returncode != 0:
            # PBS reports an unknown job id as an error once history has aged
            # the job out; that means finished, not indeterminate.
            return FINISHED
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("job_state"):
                _, _, value = stripped.partition("=")
                return ALIVE if value.strip() in _PBS_ALIVE_STATES else FINISHED
        return FINISHED

    def cancel_command(self, job_id: str) -> List[str]:
        return ["qdel", job_id]

    def render_directives(self, resources: Dict[str, Any], run_name: str,
                          stage: str, row_name: str,
                          log_dir: str) -> List[str]:
        prefix = self.directive_prefix
        lines: List[str] = []
        job_name = f"{run_name}-{stage}-{row_name}"

        def value(key: str) -> Any:
            result = resources.get(key)
            return result if result not in (None, "", 0) else None

        lines.append(f"{prefix} -N {job_name}")
        select = [str(value("nodes") or 1)]
        for key, label in (("ncpus", "ncpus"), ("mem", "mem"), ("ngpus", "ngpus")):
            if value(key):
                select.append(f"{label}={resources[key]}")
        lines.append(f"{prefix} -l select=" + ":".join(select))
        if value("walltime"):
            lines.append(f"{prefix} -l walltime={resources['walltime']}")
        if value("queue"):
            lines.append(f"{prefix} -q {resources['queue']}")
        if value("account"):
            lines.append(f"{prefix} -A {resources['account']}")
        lines.append(f"{prefix} -j oe")
        lines.append(f"{prefix} -o {os.path.join(log_dir, stage + '.log')}")

        _append_shared_directives(lines, prefix, resources)
        return lines


class SlurmBackend(SchedulerBackend):
    kind = SLURM
    submit_binary = "sbatch"
    export_flag = "--export=ALL,"
    depend_flag = "--dependency="

    @property
    def directive_prefix(self) -> str:
        return "#SBATCH"

    @property
    def jobid_env_expr(self) -> str:
        return "${SLURM_JOB_ID:-}"

    def build_submit_command(self, script_path: str, exported: str,
                             depends_on: Optional[str],
                             depends_type: str) -> List[str]:
        command = [self.submit_binary]
        if depends_on:
            command += [f"--dependency={depends_type}:{depends_on}"]
        command += [f"--export=ALL,{exported}", script_path]
        return command

    def parse_job_id(self, stdout: str) -> Optional[str]:
        text = (stdout or "").strip()
        if not text:
            return None
        return text.split()[-1].strip()

    def query_job_state(self, job_id: str) -> str:
        completed = _capture(["squeue", "-h", "-j", job_id, "-o", "%T"])
        if completed is not None and completed.returncode == 0:
            lines = completed.stdout.strip().splitlines()
            if lines:
                return ALIVE if lines[0].strip().upper() in _SLURM_ALIVE_STATES \
                    else FINISHED
        completed = _capture(["sacct", "-n", "-P", "-j", job_id, "-o", "State"])
        if completed is None or completed.returncode != 0 or not completed.stdout.strip():
            return FINISHED
        first = completed.stdout.strip().splitlines()[0].strip().upper()
        return ALIVE if first.split()[0] in _SLURM_ALIVE_STATES else FINISHED

    def cancel_command(self, job_id: str) -> List[str]:
        return ["scancel", job_id]

    def render_directives(self, resources: Dict[str, Any], run_name: str,
                          stage: str, row_name: str,
                          log_dir: str) -> List[str]:
        prefix = self.directive_prefix
        lines: List[str] = []
        job_name = f"{run_name}-{stage}-{row_name}"

        def value(key: str) -> Any:
            result = resources.get(key)
            return result if result not in (None, "", 0) else None

        lines.append(f"{prefix} --job-name={job_name}")
        if value("nodes"):
            lines.append(f"{prefix} --nodes={resources['nodes']}")
        if value("ncpus"):
            lines.append(f"{prefix} --cpus-per-task={resources['ncpus']}")
        if value("mem"):
            lines.append(f"{prefix} --mem={resources['mem']}")
        if value("ngpus"):
            lines.append(f"{prefix} --gpus-per-node={resources['ngpus']}")
        if value("walltime"):
            lines.append(f"{prefix} --time={resources['walltime']}")
        if value("queue"):
            lines.append(f"{prefix} --partition={resources['queue']}")
        if value("account"):
            lines.append(f"{prefix} --account={resources['account']}")
        lines.append(f"{prefix} --output={os.path.join(log_dir, stage + '-%j.log')}")

        _append_shared_directives(lines, prefix, resources)
        return lines


def _append_shared_directives(lines: List[str], prefix: str,
                              resources: Dict[str, Any]) -> None:
    """Directives common to both schedulers: passthrough and env exports."""
    # Site-specific directives pass through verbatim, so an option this tool
    # does not model is never out of reach.
    for extra in resources.get("extra_directives") or []:
        lines.append(extra if str(extra).startswith("#") else f"{prefix} {extra}")

    for key, value_text in sorted((resources.get("env") or {}).items()):
        lines.append(f"export {key}='{value_text}'")


_BACKENDS: Dict[str, type] = {PBS: PBSBackend, SLURM: SlurmBackend}


def Scheduler(kind: str = PBS) -> SchedulerBackend:
    """Build the scheduler backend for ``kind`` ("pbs" or "slurm").

    Kept as a function, not a class, so every existing call site
    (``Scheduler(config.scheduler)``) is unaffected by there being more than
    one concrete backend underneath it.
    """
    kind = (kind or PBS).lower()
    backend_cls = _BACKENDS.get(kind)
    if backend_cls is None:
        raise SchedulerError(f"unknown scheduler '{kind}'; expected pbs or slurm")
    return backend_cls()


def build_directives(resources: Dict[str, Any], scheduler: SchedulerBackend,
                     run_name: str, stage: str, row_name: str,
                     log_dir: str) -> List[str]:
    """Deprecated: use ``scheduler.render_directives(...)`` directly.

    Kept as a thin wrapper so existing callers of the old free-function form
    are unaffected by directive rendering moving onto SchedulerBackend.
    """
    return scheduler.render_directives(resources, run_name, stage, row_name,
                                       log_dir)


class NullScheduler(SchedulerBackend):
    """A scheduler stand-in used by dry runs.

    Nothing is submitted and no external command is consulted, so a dry run
    behaves identically whether or not a scheduler client is installed.
    Directive rendering and submit-command shape still need to match the
    configured scheduler (so a dry run's preview is accurate), so this wraps
    a real backend for those parts rather than reimplementing them.
    """

    def __init__(self, kind: str = PBS):
        self._backend = Scheduler(kind)
        self._counter = 0

    @property
    def kind(self) -> str:  # type: ignore[override]
        return self._backend.kind

    @property
    def submit_binary(self) -> str:  # type: ignore[override]
        return self._backend.submit_binary

    @property
    def export_flag(self) -> str:  # type: ignore[override]
        return self._backend.export_flag

    @property
    def depend_flag(self) -> str:  # type: ignore[override]
        return self._backend.depend_flag

    @property
    def directive_prefix(self) -> str:
        return self._backend.directive_prefix

    @property
    def jobid_env_expr(self) -> str:
        return self._backend.jobid_env_expr

    def build_submit_command(self, script_path: str, exported: str,
                             depends_on: Optional[str],
                             depends_type: str) -> List[str]:
        return self._backend.build_submit_command(script_path, exported,
                                                   depends_on, depends_type)

    def parse_job_id(self, stdout: str) -> Optional[str]:
        return self._backend.parse_job_id(stdout)

    def query_job_state(self, job_id: str) -> str:
        return UNKNOWN

    def cancel_command(self, job_id: str) -> List[str]:
        return self._backend.cancel_command(job_id)

    def render_directives(self, resources: Dict[str, Any], run_name: str,
                          stage: str, row_name: str,
                          log_dir: str) -> List[str]:
        return self._backend.render_directives(resources, run_name, stage,
                                                row_name, log_dir)

    @property
    def available(self) -> bool:
        return True

    def require_available(self) -> None:
        return None

    def submit(self, script_path: str, environment: Dict[str, str],
               depends_on: Optional[str] = None,
               depends_type: str = "afterok") -> Submission:
        self._counter += 1
        get_logger().debug("dry run: would submit %s%s", script_path,
                           f" after {depends_type}:{depends_on}" if depends_on else "")
        return Submission(job_id=f"dry-{self._counter}", success=True)

    def job_state(self, job_id: str) -> str:
        return UNKNOWN

    def cancel(self, job_id: str) -> Tuple[bool, str]:
        return True, ""


def _capture(command: List[str]) -> Optional[subprocess.CompletedProcess]:
    """Run a query command, returning None when the binary is unavailable."""
    if shutil.which(command[0]) is None:
        trace("%s is not on PATH", command[0])
        return None
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        get_logger().warning("%s failed: %s", command[0], exc)
        return None


def describe_environment() -> Dict[str, str]:
    """Collect scheduler-related facts for the doctor command."""
    facts = {}
    for binary in ("qsub", "qstat", "qdel", "sbatch", "squeue", "sacct", "scancel"):
        facts[binary] = shutil.which(binary) or "not found"
    facts["PBS_O_WORKDIR"] = os.environ.get("PBS_O_WORKDIR", "(unset)")
    facts["SLURM_JOB_ID"] = os.environ.get("SLURM_JOB_ID", "(unset)")
    return facts


# ---------------------------------------------------------------------------
# Contexts handed to stage classes
# ---------------------------------------------------------------------------


class RunContext:
    """Run-wide facts a stage class cannot know for itself.

    Created by jobchain, on the submit host, before any script is written.
    The scheduler never sees it and it does not exist at job time.
    """

    def __init__(self, name: str, home: str, scheduler: SchedulerBackend,
                 node_binary: str, work_dir_template: str, log_dir_template: str):
        self.name = name
        self.home = home
        self.scheduler = scheduler.kind
        self._scheduler = scheduler
        self.node_binary = node_binary
        self._work_dir_template = work_dir_template
        self._log_dir_template = log_dir_template

    @property
    def log_dir(self) -> str:
        from .config import expand_template
        return expand_template(self._log_dir_template, self.name, self.home)

    def work_dir(self, row: Any, row_name: str, row_index: int = 0,
                 generation: int = 1) -> str:
        """Expand the configured work directory template for one row.

        The row name is required rather than defaulted: a template using
        {row.name} that silently expanded to nothing would give every row the
        same directory, and the collision would only surface as missing
        output.
        """
        from .config import expand_template
        if not row_name:
            raise StateError("work_dir requires the row name")
        return expand_template(self._work_dir_template, self.name, self.home,
                               row=dict(row), row_name=row_name,
                               row_index=row_index, generation=generation)

    def __repr__(self) -> str:
        return f"<RunContext {self.name!r} scheduler={self.scheduler}>"


class RowContext:
    """Row-specific paths, and the shell text a script should contain.

    The methods return strings to be embedded in a script, so a stage author
    never reconstructs a helper invocation by hand.
    """

    def __init__(self, run: RunContext, row_name: str, row_index: int,
                 stage: str, generation: int, work_dir: str,
                 chains_next: bool, script_path: str = ""):
        self.run = run
        self.row_name = row_name
        self.row_index = row_index
        self.stage = stage
        self.generation = generation
        self.work_dir = work_dir
        self.chains_next = chains_next
        #: Set by jobchain once the stage has chosen its output directory.
        self.script_path = script_path

    @property
    def row_dir(self) -> str:
        return os.path.join(self.run.home, "rows", self.row_name)

    @property
    def run_dir(self) -> str:
        return os.path.join(self.row_dir, f"run-{self.generation}")

    @property
    def env_file(self) -> str:
        return os.path.join(self.row_dir, "env")

    @property
    def handoff(self) -> str:
        """This generation's handoff file, written by its own stages."""
        return os.path.join(self.run_dir, "handoff")

    @property
    def handoff_seed(self) -> str:
        """Values carried forward from the previous generation, if any."""
        return os.path.join(self.row_dir, "handoff.seed")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.run.log_dir, self.row_name)

    # -- shell fragments -------------------------------------------------

    def directives(self, resources: Dict[str, Any]) -> str:
        """Render resource directives for the active scheduler."""
        return "\n".join(self.run._scheduler.render_directives(
            resources, self.run.name, self.stage, self.row_name, self.log_dir))

    def preamble(self) -> str:
        """Load the row's parameters and mark this stage running."""
        jobid = self.run._scheduler.jobid_env_expr
        # JC_RUN is taken from the environment when jobchain supplied one,
        # and otherwise falls back to the generation this script was written
        # for. That is what lets a script be re-submitted at a later
        # generation without regenerating it, while a bare qsub months later
        # still records against the attempt it belongs to.
        return (
            f'JC_HOME="{self.run.home}"\n'
            f'JC_RUN_NAME="{self.run.name}"\n'
            f'JC_ROW="{self.row_name}"\n'
            f'JC_RUN="${{JC_RUN:-{self.run_dir}}}"\n'
            f'JC_STAGE="{self.stage}"\n'
            f'JC_NODE="{self.run.node_binary}"\n'
            f'JC_SCHEDULER="{self.run.scheduler}"\n'
            f'export JC_HOME JC_RUN_NAME JC_ROW JC_RUN JC_STAGE JC_NODE JC_SCHEDULER\n'
            f'\n'
            f'[ -r "{self.env_file}" ] && . "{self.env_file}"\n'
            f'[ -r "{self.handoff_seed}" ] && . "{self.handoff_seed}"\n'
            f'[ -r "$JC_RUN/handoff" ] && . "$JC_RUN/handoff"\n'
            f'mkdir -p "{self.work_dir}"\n'
            f'"$JC_NODE" mark --run "$JC_RUN" --stage {self.stage} '
            f'--status RUNNING --jobid "{jobid}"'
        )

    def emit(self, key: str, value: str) -> str:
        """Publish a handoff value for later stages of this row.

        `value` is treated as a literal string known at script-generation
        time (Python side), such as ``ctx.work_dir + "/result.h5"``, and is
        shell-quoted here so it reaches jobchain-node intact even if it
        contains spaces or shell metacharacters. Because the quoting is a
        single-quoted literal, this value is NOT expanded by the shell at
        run time: passing a shell variable reference such as ``"$mesh"``
        publishes the four literal characters ``$mesh``, not the variable's
        contents. To publish a value only known once the script is
        running (the result of a shell computation, a command's output,
        etc.), use `emit_shell_expr` instead.
        """
        return f'"$JC_NODE" emit --run "$JC_RUN" {key}={shell_quote(value)}'

    def emit_shell_expr(self, key: str, shell_expr: str) -> str:
        """Publish a handoff value computed at run time by the shell.

        `shell_expr` is embedded in double quotes, so the shell expands it
        (variable references, command substitution, etc.) before the
        result reaches jobchain-node. Use this for values not known until
        the script runs, e.g. ``ctx.emit_shell_expr("mesh_file", "$mesh")``
        or ``ctx.emit_shell_expr("count", "$(wc -l < "$out")")``. For a
        literal string already known at generation time, use `emit`
        instead, which is safe against embedded shell metacharacters that
        `shell_expr` is not.
        """
        return f'"$JC_NODE" emit --run "$JC_RUN" {key}="{shell_expr}"'

    def epilogue(self) -> str:
        """Record the terminal status, and chain if this stage chains.

        The chaining call is guarded by JC_CHAIN, which jobchain exports when
        it submits and a bare qsub does not. Resubmitting a script by hand
        therefore records its status and stops, instead of claiming a row and
        launching a fresh pipeline.
        """
        lines = [
            'if [ "$rc" -eq 0 ]; then',
            f'    "$JC_NODE" mark --run "$JC_RUN" --stage {self.stage} --status DONE',
            'else',
            (f'    "$JC_NODE" mark --run "$JC_RUN" --stage {self.stage} '
             f'--status FAILED --error "exit status $rc"'),
            'fi',
        ]
        if self.chains_next:
            lines += [
                '',
                "# Claim and submit the next row. Not conditional on rc: one",
                "# bad row must not stall the run.",
                'if [ "${JC_CHAIN:-0}" = "1" ]; then',
                '    "$JC_NODE" submit --home "$JC_HOME" --next',
                'fi',
            ]
        return "\n".join(lines)

    def expand(self, text: str, row: Dict[str, Any]) -> str:
        """Expand row and run placeholders in shell text (a `command:` stage).

        A ``{row.<column>}`` reference expands to a ``$JC_<column>`` shell
        variable, not the value itself -- see ``expand_template``'s
        ``shell=True`` docstring for why that is what keeps row data from
        being able to inject shell syntax.
        """
        from .config import expand_template
        return expand_template(text, self.run.name, self.run.home, row=row,
                               row_name=self.row_name, row_index=self.row_index,
                               generation=self.generation, shell=True)

    def write(self, text: str) -> str:
        """Write the script, make it executable, and return its path."""
        return write_script(self.script_path, text)


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------


def write_script(path: str, text: str) -> str:
    """Write a script atomically and make it executable."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, os.stat(temporary).st_mode | stat.S_IXUSR | stat.S_IXGRP)
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
    trace("wrote script %s", path)
    return path


def verify_script(path: str) -> Optional[str]:
    """Check a generated script minimally; return a reason if unusable.

    Verification is deliberately shallow: the script belongs to whoever wrote
    the stage class, and enforcing more would constrain what a stage may do.
    A script that is empty or does not parse is a defect either way.
    """
    if not os.path.isfile(path):
        return f"script was not written: {path}"
    if os.path.getsize(path) == 0:
        return f"script is empty: {path}"
    completed = subprocess.run(["sh", "-n", path], capture_output=True,
                               text=True, check=False)
    if completed.returncode != 0:
        return f"script is not valid shell: {completed.stderr.strip()}"
    return None
