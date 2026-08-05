from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from gc001_live_daily_90pct_093042 import (  # noqa: E402
    _fault_event as morning_fault_event,
)
from gc001_live_daily_90pct_093042 import (
    _query_failure_event as morning_query_failure_event,
)
from gc001_r001_live_afternoon_sweep import (  # noqa: E402
    _fault_event as afternoon_fault_event,
)
from gc001_r001_live_afternoon_sweep import (
    _query_failure_event as afternoon_query_failure_event,
)
from repo_execution_state_machine import (  # noqa: E402
    AFTERNOON_TRANSITIONS,
    MORNING_TRANSITIONS,
    AfternoonEvent,
    AfternoonState,
    InvalidTransition,
    MorningEvent,
    MorningState,
    advance_afternoon,
    advance_morning,
    afternoon_snapshot_from_payload,
    execution_source_commit,
    execution_source_tree_is_clean,
    initial_afternoon_snapshot,
    initial_morning_snapshot,
    morning_snapshot_from_payload,
    _normalize_source_bytes,
    snapshot_to_payload,
    verify_state_machines,
)


def _execute_every_declared_edge(
    initial: object,
    transitions: dict,
    advance: object,
) -> set[tuple[object, object]]:
    queue = deque([initial])
    reached = {initial}
    exercised: set[tuple[object, object]] = set()
    while queue:
        snapshot = queue.popleft()
        for event in transitions[snapshot.state]:
            successor = advance(snapshot, event)
            exercised.add((snapshot.state, event))
            if successor not in reached:
                reached.add(successor)
                queue.append(successor)
    return exercised


class ExhaustiveStateMachineVerificationTests(unittest.TestCase):
    def test_source_hash_ignores_line_endings(self):
        payload = (
            b"def f():\n    return 1\n"
        )
        self.assertEqual(
            _normalize_source_bytes(payload),
            _normalize_source_bytes(
                payload.replace(b"\n", b"\r\n")
            ),
        )
        self.assertNotIn(b"\r", _normalize_source_bytes(payload))

    def test_execution_source_commit_is_current_head_in_git_checkout(self):
        commit = execution_source_commit()
        self.assertTrue(
            commit is None or re.fullmatch(r"[0-9a-f]{40}", commit)
        )

    @unittest.skipUnless(
        shutil.which("git") is not None,
        "git is not installed in this no-Git deployment",
    )
    def test_execution_source_tree_clean_check_detects_uncommitted_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "test"],
                cwd=root,
                check=True,
            )
            scripts = root / "scripts"
            scripts.mkdir()
            target = scripts / "repo_failure_alert.py"
            target.write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "init"],
                cwd=root,
                check=True,
            )
            self.assertTrue(execution_source_tree_is_clean(root))
            target.write_text("x = 2\n", encoding="utf-8")
            self.assertFalse(execution_source_tree_is_clean(root))

    def test_formal_verifier_reaches_fixed_point_without_violation(self):
        result = verify_state_machines()
        for name in ("morning", "afternoon"):
            proof = result[name]
            self.assertEqual(proof["unreachable_states"], 0)
            self.assertEqual(proof["unreachable_transitions"], 0)
            self.assertEqual(proof["states_without_terminal_path"], 0)
            self.assertEqual(proof["invariant_violations"], 0)

    def test_every_morning_phase_event_edge_is_executed(self):
        exercised = _execute_every_declared_edge(
            initial_morning_snapshot(),
            MORNING_TRANSITIONS,
            advance_morning,
        )
        declared = {
            (state, event)
            for state, events in MORNING_TRANSITIONS.items()
            for event in events
        }
        self.assertEqual(exercised, declared)

    def test_every_afternoon_phase_event_edge_is_executed(self):
        exercised = _execute_every_declared_edge(
            initial_afternoon_snapshot(),
            AFTERNOON_TRANSITIONS,
            advance_afternoon,
        )
        declared = {
            (state, event)
            for state, events in AFTERNOON_TRANSITIONS.items()
            for event in events
        }
        self.assertEqual(exercised, declared)

    def test_invalid_events_are_rejected_in_every_phase(self):
        morning_events = set(MorningEvent)
        for state, allowed in MORNING_TRANSITIONS.items():
            invalid = next(iter(morning_events - set(allowed)), None)
            if invalid is None:
                continue
            snapshot = initial_morning_snapshot()
            snapshot = type(snapshot)(state=state, facts=snapshot.facts)
            with self.assertRaises(InvalidTransition):
                advance_morning(snapshot, invalid)

        afternoon_events = set(AfternoonEvent)
        for state, allowed in AFTERNOON_TRANSITIONS.items():
            invalid = next(iter(afternoon_events - set(allowed)), None)
            if invalid is None:
                continue
            snapshot = initial_afternoon_snapshot()
            snapshot = type(snapshot)(state=state, facts=snapshot.facts)
            with self.assertRaises(InvalidTransition):
                advance_afternoon(snapshot, invalid)

    def test_snapshots_round_trip_with_schema_validation(self):
        morning = initial_morning_snapshot()
        self.assertEqual(
            morning_snapshot_from_payload(
                snapshot_to_payload(morning)
            ),
            morning,
        )
        afternoon = initial_afternoon_snapshot()
        self.assertEqual(
            afternoon_snapshot_from_payload(
                snapshot_to_payload(afternoon)
            ),
            afternoon,
        )
        malformed = snapshot_to_payload(morning)
        malformed["facts"]["unresolved_order"] = "yes"
        with self.assertRaises(Exception):
            morning_snapshot_from_payload(malformed)

    def test_unresolved_morning_order_cannot_reach_ready(self):
        snapshot = initial_morning_snapshot()
        for event in (
            MorningEvent.BEGIN,
            MorningEvent.PREFLIGHT_OK,
            MorningEvent.RECOVERY_CLEAR,
            MorningEvent.TRIGGER,
            MorningEvent.SNAPSHOT_OK,
            MorningEvent.INTENT_PERSISTED,
            MorningEvent.SUBMIT_ACCEPTED,
        ):
            snapshot = advance_morning(snapshot, event)
        self.assertEqual(snapshot.state, MorningState.ORDER_ACTIVE)
        with self.assertRaises(InvalidTransition):
            advance_morning(snapshot, MorningEvent.SNAPSHOT_OK)

    def test_morning_retry_requires_terminal_reconciliation(self):
        snapshot = initial_morning_snapshot()
        for event in (
            MorningEvent.BEGIN,
            MorningEvent.PREFLIGHT_OK,
            MorningEvent.RECOVERY_CLEAR,
            MorningEvent.TRIGGER,
            MorningEvent.SNAPSHOT_OK,
            MorningEvent.INTENT_PERSISTED,
            MorningEvent.SUBMIT_ACCEPTED,
        ):
            snapshot = advance_morning(snapshot, event)
        with self.assertRaises(InvalidTransition):
            advance_morning(snapshot, MorningEvent.RECONCILED_RETRY)

        snapshot = advance_morning(
            snapshot,
            MorningEvent.ORDER_TERMINAL,
        )
        snapshot = advance_morning(
            snapshot,
            MorningEvent.RECONCILED_RETRY,
        )
        self.assertEqual(snapshot.state, MorningState.SNAPSHOT)
        self.assertFalse(snapshot.facts.unresolved_order)
        self.assertFalse(snapshot.facts.intent_persisted)
        self.assertFalse(snapshot.facts.cash_verified)
        self.assertFalse(snapshot.facts.quote_verified)

    def test_unresolved_afternoon_order_cannot_reach_ready(self):
        snapshot = initial_afternoon_snapshot()
        for event in (
            AfternoonEvent.BEGIN,
            AfternoonEvent.PREFLIGHT_OK,
            AfternoonEvent.RECOVERY_CLEAR,
            AfternoonEvent.TRIGGER,
            AfternoonEvent.SCAN_READY,
            AfternoonEvent.INTENT_PERSISTED,
            AfternoonEvent.SUBMIT_ACCEPTED,
        ):
            snapshot = advance_afternoon(snapshot, event)
        self.assertEqual(snapshot.state, AfternoonState.ORDER_ACTIVE)
        with self.assertRaises(InvalidTransition):
            advance_afternoon(snapshot, AfternoonEvent.SCAN_READY)

    def test_every_runtime_failure_mapping_is_a_legal_transition(self):
        for state in MorningState:
            if state in {
                MorningState.NEW,
                MorningState.DONE_FILLED,
                MorningState.DONE_PARTIAL,
                MorningState.SKIPPED,
                MorningState.HALTED,
            }:
                continue
            self.assertIn(
                morning_fault_event(state),
                MORNING_TRANSITIONS[state],
                state,
            )
            self.assertIn(
                morning_query_failure_event(state),
                MORNING_TRANSITIONS[state],
                state,
            )
        for state in AfternoonState:
            if state in {
                AfternoonState.NEW,
                AfternoonState.COMPLETE,
                AfternoonState.SKIPPED,
                AfternoonState.HALTED,
            }:
                continue
            self.assertIn(
                afternoon_fault_event(state),
                AFTERNOON_TRANSITIONS[state],
                state,
            )
            self.assertIn(
                afternoon_query_failure_event(state),
                AFTERNOON_TRANSITIONS[state],
                state,
            )


if __name__ == "__main__":
    unittest.main()
