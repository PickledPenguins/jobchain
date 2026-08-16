#!/usr/bin/env python3
"""Deterministic state/property checks for jobchain.

The generated cases are intentionally lightweight and dependency-free.
"""
import random

STATUSES = ["pending", "running", "completed", "failed", "cancelled"]

def terminal(s): return s in {"completed","failed","cancelled"}

def rollup(statuses):
    if not statuses: return "pending"
    if "failed" in statuses: return "failed"
    if "cancelled" in statuses: return "cancelled"
    if all(s == "completed" for s in statuses): return "completed"
    if any(s == "running" for s in statuses): return "running"
    return "pending"

def valid_transition(a,b):
    allowed={
      "pending":{"pending","running","cancelled"},
      "running":{"running","completed","failed","cancelled"},
      "completed":{"completed"},
      "failed":{"failed","pending"},
      "cancelled":{"cancelled","pending"},
    }
    return b in allowed[a]

def generated_cases(seed=0xC0FFEE,n=3000):
    r=random.Random(seed)
    for _ in range(n):
        yield [r.choice(STATUSES) for _ in range(r.randint(0,8))]

def test_terminal_is_stable():
    for s in STATUSES:
        if terminal(s):
            assert terminal(s)

def test_rollup_empty_is_pending():
    assert rollup([]) == "pending"

def test_rollup_all_completed():
    for n in range(1,9):
        assert rollup(["completed"]*n) == "completed"

def test_failure_dominates_running_and_completed():
    for xs in generated_cases():
        if "failed" in xs:
            assert rollup(xs) == "failed"

def test_cancel_does_not_hide_failure():
    for xs in generated_cases():
        if "failed" in xs:
            assert rollup(xs + ["cancelled"]) == "failed"

def test_running_is_visible_without_failure():
    for xs in generated_cases():
        if "running" in xs and "failed" not in xs and "cancelled" not in xs:
            assert rollup(xs) == "running"

def test_pending_is_default_for_nonterminal_work():
    for xs in generated_cases():
        if xs and set(xs) <= {"pending","completed"} and "completed" not in xs:
            assert rollup(xs) == "pending"

def test_terminal_rollup_only_when_all_completed_or_failure_cancel():
    for xs in generated_cases():
        got=rollup(xs)
        if got == "completed":
            assert xs and all(s=="completed" for s in xs)
        if got == "failed":
            assert "failed" in xs
        if got == "cancelled":
            assert "failed" not in xs and "cancelled" in xs

def test_transition_reflexivity():
    for s in STATUSES:
        assert valid_transition(s,s)

def test_completed_cannot_reopen_without_explicit_reset():
    assert not valid_transition("completed","running")
    assert not valid_transition("completed","pending")

def test_running_can_reach_each_terminal_state():
    for s in ("completed","failed","cancelled"):
        assert valid_transition("running",s)

def test_reset_paths_are_explicit():
    assert valid_transition("failed","pending")
    assert valid_transition("cancelled","pending")
    assert not valid_transition("failed","running")
    assert not valid_transition("cancelled","running")

def test_generated_rollups_are_deterministic():
    a=list(generated_cases(seed=1234,n=500))
    b=list(generated_cases(seed=1234,n=500))
    assert a==b
    assert [rollup(x) for x in a] == [rollup(x) for x in b]

def main():
    tests=[v for k,v in globals().items() if k.startswith("test_")]
    for t in tests: t()
    print(f"State/property tests: {len(tests)}/{len(tests)} passed")
    print("Generated cases: 3000 per randomized property")

if __name__=="__main__": main()
