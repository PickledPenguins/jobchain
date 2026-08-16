"""Tests for the compiled compute-node helper.

These exercise the real binary, because the claim protocol is the part of the
system where a subtle mistake produces two jobs running the same parameters,
and a Python reimplementation would not prove anything about what runs on a
node.

The concurrency tests are the centre of the suite. They start many
simultaneous claimers against one set of rows and assert that every row was
won exactly once.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import unittest
from typing import List, Tuple

from tests.helpers import NODE_BINARY, TempProject, require_node_binary


def setUpModule() -> None:
    require_node_binary()


def _claim_once(home: str) -> Tuple[int, str]:
    """Run one claim and return its exit code and the row it won."""
    completed = subprocess.run([NODE_BINARY, "claim", "--home", home],
                               capture_output=True, text=True, check=False)
    row = ""
    for line in completed.stdout.splitlines():
        if line.startswith("JC_NEXT_ROW="):
            row = line.partition("=")[2].strip()
    return completed.returncode, row


class NodeHelperCase(TempProject):
    """Base class that lays out a state directory by hand.

    Building the layout directly rather than through the Python front end
    keeps these tests focused on the helper's contract with the filesystem.
    """

    def make_home(self, rows: int = 5, generation: int = 1) -> str:
        home = self.path("home")
        os.makedirs(os.path.join(home, "rows"), exist_ok=True)
        names: List[str] = []
        for index in range(rows):
            name = f"{index + 1:06d}"
            row_dir = os.path.join(home, "rows", name)
            os.makedirs(row_dir, exist_ok=True)
            with open(os.path.join(row_dir, "gen"), "w", encoding="utf-8") as handle:
                handle.write(f"{generation}\n")
            with open(os.path.join(row_dir, "env"), "w", encoding="utf-8") as handle:
                handle.write(f"JC_index='{index}'\nexport JC_index\n")
            # A row is claimable only once it has a manifest: an invalid row
            # has state but no scripts, and must not be picked up.
            with open(os.path.join(row_dir, "manifest"), "w",
                      encoding="utf-8") as handle:
                handle.write("only\t-\t/bin/true\n")
            names.append(name)
        with open(os.path.join(home, "rows.idx"), "w", encoding="utf-8") as handle:
            handle.write("".join(f"{n}\n" for n in names))
        return home

    def status_of(self, home: str, row: str, generation: int = 1,
                  stage: str = "only") -> str:
        path = os.path.join(home, "rows", row, f"run-{generation}",
                            f"status.{stage}")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()


class TestClaim(NodeHelperCase):
    def test_claims_rows_in_index_order(self):
        home = self.make_home(3)
        for expected in ["000001", "000002", "000003"]:
            code, row = _claim_once(home)
            self.assertEqual(code, 0)
            self.assertEqual(row, expected)

    def test_exhaustion_reports_code_three(self):
        # Three is distinct from every error code so a chain can end cleanly
        # without the shell treating completion as a failure.
        home = self.make_home(1)
        self.assertEqual(_claim_once(home)[0], 0)
        self.assertEqual(_claim_once(home)[0], 3)

    def test_a_claim_creates_the_generation_directory(self):
        home = self.make_home(1)
        _claim_once(home)
        self.assertTrue(os.path.isdir(os.path.join(home, "rows", "000001", "run-1")))

    def test_claim_records_who_took_it(self):
        home = self.make_home(1)
        _claim_once(home)
        claim = os.path.join(home, "rows", "000001", "run-1", "claim")
        with open(claim, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("host=", text)
        self.assertIn("pid=", text)

    def test_a_higher_generation_makes_a_row_claimable_again(self):
        home = self.make_home(1)
        self.assertEqual(_claim_once(home)[0], 0)
        self.assertEqual(_claim_once(home)[0], 3)
        with open(os.path.join(home, "rows", "000001", "gen"), "w",
                  encoding="utf-8") as handle:
            handle.write("2\n")
        code, row = _claim_once(home)
        self.assertEqual(code, 0)
        self.assertEqual(row, "000001")
        # The earlier attempt is left intact, so history survives a retry.
        self.assertTrue(os.path.isdir(os.path.join(home, "rows", "000001", "run-1")))
        self.assertTrue(os.path.isdir(os.path.join(home, "rows", "000001", "run-2")))

    def test_a_held_row_is_skipped(self):
        home = self.make_home(2)
        open(os.path.join(home, "rows", "000001", "hold"), "w").close()
        self.assertEqual(_claim_once(home)[1], "000002")

    def test_releasing_a_hold_restores_the_row(self):
        home = self.make_home(1)
        hold = os.path.join(home, "rows", "000001", "hold")
        open(hold, "w").close()
        self.assertEqual(_claim_once(home)[0], 3)
        os.unlink(hold)
        self.assertEqual(_claim_once(home)[0], 0)

    def test_a_row_without_a_generation_file_is_skipped(self):
        home = self.make_home(2)
        os.unlink(os.path.join(home, "rows", "000001", "gen"))
        self.assertEqual(_claim_once(home)[1], "000002")

    def test_a_missing_index_is_a_state_error(self):
        home = self.make_home(1)
        os.unlink(os.path.join(home, "rows.idx"))
        self.assertEqual(_claim_once(home)[0], 6)

    def test_output_is_shell_evaluable(self):
        home = self.make_home(1)
        completed = subprocess.run(
            ["sh", "-c",
             f'eval "$({NODE_BINARY} claim --home {home})"; echo "$JC_NEXT_ROW"'],
            capture_output=True, text=True, check=False)
        self.assertEqual(completed.stdout.strip(), "000001")


class TestConcurrency(NodeHelperCase):
    """The property the whole design rests on: one row, one winner."""

    def _claim_many(self, home: str, workers: int) -> List[Tuple[int, str]]:
        with multiprocessing.Pool(workers) as pool:
            return pool.map(_claim_once, [home] * workers)

    def test_simultaneous_claimers_never_take_the_same_row(self):
        home = self.make_home(40)
        results = self._claim_many(home, 24)
        won = [row for code, row in results if code == 0]
        self.assertEqual(len(won), 24)
        self.assertEqual(len(set(won)), 24, f"a row was claimed twice: {won}")

    def test_contention_for_fewer_rows_than_claimers(self):
        # More claimers than rows is the interesting case: the surplus must
        # report exhaustion rather than duplicating work.
        home = self.make_home(5)
        results = self._claim_many(home, 20)
        won = [row for code, row in results if code == 0]
        exhausted = [code for code, _ in results if code == 3]
        self.assertEqual(sorted(won), [f"{i:06d}" for i in range(1, 6)])
        self.assertEqual(len(exhausted), 15)

    def test_a_single_row_has_exactly_one_winner(self):
        home = self.make_home(1)
        results = self._claim_many(home, 32)
        self.assertEqual(len([c for c, _ in results if c == 0]), 1)

    def test_repeated_rounds_stay_consistent(self):
        home = self.make_home(60)
        won: List[str] = []
        for _ in range(3):
            won.extend(row for code, row in self._claim_many(home, 20) if code == 0)
        self.assertEqual(len(won), 60)
        self.assertEqual(len(set(won)), 60)


class TestCrashInjection(NodeHelperCase):
    def test_a_stopped_run_claims_nothing(self):
        # A stop marker is checked before claiming, so a stop reaches every
        # chain at its next advance without touching any node.
        home = self.make_home(3)
        open(os.path.join(home, "stopped"), "w").close()
        self.assertEqual(_claim_once(home)[0], 3)
        os.unlink(os.path.join(home, "stopped"))
        self.assertEqual(_claim_once(home)[0], 0)

    def test_a_row_without_a_manifest_is_skipped(self):
        # An invalid row has state but no scripts, so it must not be claimed
        # until it is corrected.
        home = self.make_home(2)
        os.unlink(os.path.join(home, "rows", "000001", "manifest"))
        self.assertEqual(_claim_once(home)[1], "000002")

    def test_emit_writes_a_sourceable_handoff(self):
        home = self.make_home(1)
        _claim_once(home)
        run_dir = os.path.join(home, "rows", "000001", "run-1")
        self.run_node("emit", "--run", run_dir, "mesh=/data/m.h5",
                      "note=it's fine")
        script = os.path.join(self.tmp, "read.sh")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(f'. "{run_dir}/handoff"\n'
                         'printf "%s|%s" "$JC_OUT_mesh" "$JC_OUT_note"\n')
        completed = subprocess.run(["sh", script], capture_output=True,
                                   text=True, check=False)
        self.assertEqual(completed.stdout, "/data/m.h5|it's fine")

    def test_emit_rejects_a_pair_without_a_value(self):
        home = self.make_home(1)
        _claim_once(home)
        run_dir = os.path.join(home, "rows", "000001", "run-1")
        self.assertEqual(self.run_node("emit", "--run", run_dir, "nope").returncode,
                         1)

    def test_a_claimer_killed_mid_run_does_not_release_its_row(self):
        # A row claimed by a process that then dies stays claimed. That is
        # correct: releasing it automatically could run the same parameters
        # twice. Recovery is deliberate, through doctor.
        home = self.make_home(2)
        _claim_once(home)
        self.assertTrue(os.path.isdir(os.path.join(home, "rows", "000001", "run-1")))
        self.assertEqual(_claim_once(home)[1], "000002")

    def test_killing_a_helper_leaves_no_partial_status_file(self):
        home = self.make_home(1)
        _claim_once(home)
        run_dir = os.path.join(home, "rows", "000001", "run-1")
        process = subprocess.Popen(
            [NODE_BINARY, "mark", "--run", run_dir, "--stage", "only",
             "--status", "RUNNING"])
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
        # Status is written by rename, so a reader sees the old value or the
        # new one, never a truncated word.
        status_file = os.path.join(run_dir, "status.only")
        if os.path.exists(status_file):
            with open(status_file, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), "RUNNING")

    def test_no_temporary_files_survive_a_completed_write(self):
        home = self.make_home(1)
        _claim_once(home)
        run_dir = os.path.join(home, "rows", "000001", "run-1")
        subprocess.run([NODE_BINARY, "mark", "--run", run_dir, "--stage", "only",
                        "--status", "DONE"], check=True)
        leftovers = [n for n in os.listdir(run_dir) if ".tmp." in n]
        self.assertEqual(leftovers, [])


class TestMarkAndEvent(NodeHelperCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.make_home(1)
        _claim_once(self.home)
        self.run_dir = os.path.join(self.home, "rows", "000001", "run-1")

    def mark(self, *arguments: str) -> subprocess.CompletedProcess:
        return self.run_node("mark", "--run", self.run_dir, "--stage", "only",
                             *arguments)

    def test_status_is_recorded(self):
        self.assertEqual(self.mark("--status", "RUNNING").returncode, 0)
        self.assertEqual(self.status_of(self.home, "000001"), "RUNNING")

    def test_a_job_id_can_be_recorded_without_touching_the_status(self):
        # A submitter uses this: by the time the submit command returns, the
        # job may already have written its own status.
        self.mark("--status", "RUNNING")
        self.assertEqual(self.mark("--jobid", "99.head").returncode, 0)
        self.assertEqual(self.status_of(self.home, "000001"), "RUNNING")
        with open(os.path.join(self.run_dir, "jobid.only"), encoding="utf-8") as h:
            self.assertEqual(h.read().strip(), "99.head")

    def test_jobid_and_error_are_recorded(self):
        self.mark("--status", "FAILED", "--jobid", "77.head", "--error", "boom")
        with open(os.path.join(self.run_dir, "jobid.only"), encoding="utf-8") as h:
            self.assertEqual(h.read().strip(), "77.head")
        with open(os.path.join(self.run_dir, "error.only"), encoding="utf-8") as h:
            self.assertEqual(h.read().strip(), "boom")

    def test_each_mark_appends_a_timeline_entry(self):
        self.mark("--status", "RUNNING")
        self.mark("--status", "DONE")
        with open(os.path.join(self.run_dir, "timeline"), encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 2)  # RUNNING, DONE
        self.assertIn("status=DONE", lines[-1])
        self.assertIn("stage=only", lines[-1])

    def test_marking_a_missing_run_directory_is_a_state_error(self):
        result = self.run_node("mark", "--run", self.path("nope"), "--stage",
                               "only", "--status", "DONE")
        self.assertEqual(result.returncode, 6)

    def test_missing_required_options_are_usage_errors(self):
        self.assertEqual(self.run_node("mark", "--status", "DONE").returncode, 1)
        self.assertEqual(
            self.run_node("mark", "--run", self.run_dir, "--status", "X").returncode,
            1)
        self.assertEqual(self.run_node("claim").returncode, 1)
        self.assertEqual(self.run_node("event", "--home", self.home).returncode, 1)

    def test_events_from_concurrent_writers_stay_whole(self):
        # Appends are single short writes under O_APPEND, so entries from
        # different jobs interleave in order rather than tearing.
        processes = [
            subprocess.Popen([NODE_BINARY, "event", "--home", self.home,
                              "--message", f"message-{i:03d}"])
            for i in range(30)
        ]
        for process in processes:
            process.wait(timeout=30)
        with open(os.path.join(self.home, "events.log"), encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        self.assertEqual(len(lines), 30)
        self.assertEqual(
            sorted(line.split()[-1] for line in lines),
            sorted(f"message-{i:03d}" for i in range(30)),
        )


class TestSelftestAndUsage(NodeHelperCase):
    def test_selftest_passes_on_a_normal_filesystem(self):
        home = self.make_home(1)
        result = self.run_node("selftest", "--home", home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mkdir exclusion", result.stdout)

    def test_selftest_removes_its_probe(self):
        home = self.make_home(1)
        self.run_node("selftest", "--home", home)
        leftovers = [n for n in os.listdir(home) if n.startswith(".selftest")]
        self.assertEqual(leftovers, [])

    def test_version_and_help(self):
        self.assertIn("0.5", self.run_node("version").stdout)
        self.assertEqual(self.run_node("--help").returncode, 0)

    def test_unknown_command_is_a_usage_error(self):
        result = self.run_node("frobnicate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown command", result.stderr)

    def test_no_arguments_is_a_usage_error(self):
        self.assertEqual(self.run_node().returncode, 1)


if __name__ == "__main__":
    unittest.main()


class TestShellImplementation(NodeHelperCase):
    """The shell helper must implement the same protocol as the compiled one.

    Running the same checks against both is what stops the two drifting
    apart, which matters because a site may be using either.
    """

    def setUp(self) -> None:
        super().setUp()
        self.shell = os.path.join(os.path.dirname(NODE_BINARY),
                                  "jobchain-node.sh")
        if not os.path.isfile(self.shell):
            self.skipTest("the shell helper is not present")

    def run_shell(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.shell, *arguments], capture_output=True,
                              text=True, check=False, timeout=60)

    def test_it_claims_rows_in_index_order(self):
        home = self.make_home(3)
        for expected in ("000001", "000002", "000003"):
            result = self.run_shell("claim", "--home", home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"JC_NEXT_ROW={expected}", result.stdout)

    def test_exhaustion_reports_code_three(self):
        home = self.make_home(1)
        self.run_shell("claim", "--home", home)
        self.assertEqual(self.run_shell("claim", "--home", home).returncode, 3)

    def test_a_stopped_run_claims_nothing(self):
        home = self.make_home(2)
        open(os.path.join(home, "stopped"), "w").close()
        self.assertEqual(self.run_shell("claim", "--home", home).returncode, 3)

    def test_it_records_status_per_stage(self):
        home = self.make_home(1)
        self.run_shell("claim", "--home", home)
        run_dir = os.path.join(home, "rows", "000001", "run-1")
        self.run_shell("mark", "--run", run_dir, "--stage", "only",
                       "--status", "RUNNING", "--jobid", "7.head")
        self.assertEqual(self.status_of(home, "000001"), "RUNNING")

    def test_it_records_a_job_id_without_touching_status(self):
        home = self.make_home(1)
        self.run_shell("claim", "--home", home)
        run_dir = os.path.join(home, "rows", "000001", "run-1")
        self.run_shell("mark", "--run", run_dir, "--stage", "only",
                       "--status", "RUNNING")
        self.run_shell("mark", "--run", run_dir, "--stage", "only",
                       "--jobid", "9.head")
        self.assertEqual(self.status_of(home, "000001"), "RUNNING")

    def test_its_handoff_is_sourceable_and_quote_safe(self):
        home = self.make_home(1)
        self.run_shell("claim", "--home", home)
        run_dir = os.path.join(home, "rows", "000001", "run-1")
        self.run_shell("emit", "--run", run_dir, "mesh=/data/m.h5",
                       "note=it's fine")
        script = os.path.join(self.tmp, "read.sh")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(f'. "{run_dir}/handoff"\n'
                         'printf "%s|%s" "$JC_OUT_mesh" "$JC_OUT_note"\n')
        completed = subprocess.run(["sh", script], capture_output=True,
                                   text=True, check=False)
        self.assertEqual(completed.stdout, "/data/m.h5|it's fine")

    def test_its_selftest_passes(self):
        home = self.make_home(1)
        result = self.run_shell("selftest", "--home", home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mkdir exclusion", result.stdout)

    def test_both_implementations_agree_on_claiming(self):
        # The compiled helper claims one row, the shell helper the next:
        # neither may take a row the other already holds.
        home = self.make_home(2)
        compiled = _claim_once(home)
        self.assertEqual(compiled[0], 0)
        result = self.run_shell("claim", "--home", home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f"JC_NEXT_ROW={compiled[1]}", result.stdout)

    def test_usage_errors_match_the_compiled_helper(self):
        self.assertEqual(self.run_shell("claim").returncode, 1)
        self.assertEqual(self.run_shell("frobnicate").returncode, 1)
        self.assertIn("0.5", self.run_shell("version").stdout)
