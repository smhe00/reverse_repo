from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Generic, TypeVar

EXECUTION_SOURCE_FILES = (
    "repo_execution_state_machine.py",
    "repo_execution_core.py",
    "repo_failure_alert.py",
    "repo_live_enable_manifest.py",
    "repo_simulation_validation.py",
    "verify_repo_release_gate.py",
    "gc001_live_daily_90pct_093042.py",
    "gc001_r001_live_afternoon_sweep.py",
    "reverse_repo_runtime.ps1",
    "run_gc001_daily_90pct_093042.ps1",
    "run_gc001_r001_afternoon_sweep.ps1",
    "manage_reverse_repo_tasks.ps1",
)


class InvalidTransition(RuntimeError):
    """Raised when an event is not legal in the current machine state."""


class InvariantViolation(RuntimeError):
    """Raised when a machine state violates a safety invariant."""


class MorningState(str, Enum):
    NEW = "new"
    PREFLIGHT = "preflight"
    RECOVERY = "recovery"
    WAIT_TRIGGER = "wait_trigger"
    SNAPSHOT = "snapshot"
    READY = "ready"
    INTENT = "intent_persisted"
    SUBMIT_UNKNOWN = "submission_outcome_unknown"
    ORDER_ACTIVE = "order_active"
    CANCEL_PENDING = "cancel_pending"
    RECONCILE = "reconcile_terminal_order"
    DONE_FILLED = "done_filled"
    DONE_PARTIAL = "done_partial"
    SKIPPED = "skipped_non_trading_day"
    HALTED = "safe_halt"


class MorningEvent(str, Enum):
    BEGIN = "begin"
    PREFLIGHT_OK = "preflight_ok"
    NON_TRADING_DAY = "non_trading_day"
    RECOVERY_CLEAR = "recovery_clear"
    RECOVERY_ACTIVE = "recovery_active"
    RECOVERY_CANCEL_PENDING = "recovery_cancel_pending"
    RECOVERY_TERMINAL = "recovery_terminal"
    RECOVERY_AMBIGUOUS = "recovery_ambiguous"
    TRIGGER = "trigger"
    SNAPSHOT_OK = "snapshot_ok"
    SNAPSHOT_RETRY = "snapshot_retry"
    DEADLINE = "deadline"
    NO_FUNDS = "no_funds"
    INTENT_PERSISTED = "intent_persisted"
    SUBMIT_ACCEPTED = "submit_accepted"
    SUBMIT_REJECTED = "submit_rejected"
    SUBMIT_EXCEPTION = "submit_exception"
    RECOVERED_ACTIVE = "recovered_active"
    RECOVERED_CANCEL_PENDING = "recovered_cancel_pending"
    RECOVERED_TERMINAL = "recovered_terminal"
    RECOVERED_NO_MATCH = "recovered_no_match"
    ORDER_STILL_ACTIVE = "order_still_active"
    ORDER_TERMINAL = "order_terminal"
    ORDER_QUERY_AMBIGUOUS = "order_query_ambiguous"
    ORDER_STATUS_UNKNOWN = "order_status_unknown"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_REJECTED = "cancel_rejected"
    CANCEL_STILL_PENDING = "cancel_still_pending"
    CANCEL_TERMINAL = "cancel_terminal"
    CANCEL_TIMEOUT = "cancel_timeout"
    RECONCILED_FULL = "reconciled_full"
    RECONCILED_RETRY = "reconciled_retry_remaining"
    RECONCILED_PARTIAL = "reconciled_partial"
    RECONCILED_ZERO = "reconciled_zero"
    RECONCILE_FAILED = "reconcile_failed"
    FAULT = "fault"
    RESTART = "restart"


class AfternoonState(str, Enum):
    NEW = "new"
    PREFLIGHT = "preflight"
    RECOVERY = "recovery"
    WAIT_WINDOW = "wait_window"
    SCAN = "scan"
    WAIT_FUNDS = "wait_funds"
    WAIT_BOOK = "wait_book"
    READY = "ready"
    INTENT = "intent_persisted"
    SUBMIT_UNKNOWN = "submission_outcome_unknown"
    ORDER_ACTIVE = "order_active"
    CANCEL_PENDING = "cancel_pending"
    RECONCILE = "reconcile_terminal_order"
    BACKOFF = "submission_backoff"
    COMPLETE = "complete_at_hard_stop"
    SKIPPED = "skipped_non_trading_day"
    HALTED = "safe_halt"


class AfternoonEvent(str, Enum):
    BEGIN = "begin"
    PREFLIGHT_OK = "preflight_ok"
    NON_TRADING_DAY = "non_trading_day"
    RECOVERY_CLEAR = "recovery_clear"
    RECOVERY_ACTIVE = "recovery_active"
    RECOVERY_CANCEL_PENDING = "recovery_cancel_pending"
    RECOVERY_TERMINAL = "recovery_terminal"
    RECOVERY_AMBIGUOUS = "recovery_ambiguous"
    TRIGGER = "trigger"
    SCAN_READY = "scan_ready"
    NO_FUNDS = "no_funds"
    NO_BOOK = "no_book"
    RETRY_SCAN = "retry_scan"
    INTENT_PERSISTED = "intent_persisted"
    SUBMIT_ACCEPTED = "submit_accepted"
    SUBMIT_REJECTED = "submit_rejected"
    SUBMIT_EXCEPTION = "submit_exception"
    RECOVERED_ACTIVE = "recovered_active"
    RECOVERED_CANCEL_PENDING = "recovered_cancel_pending"
    RECOVERED_TERMINAL = "recovered_terminal"
    RECOVERED_NO_MATCH = "recovered_no_match"
    ORDER_STILL_ACTIVE = "order_still_active"
    ORDER_TERMINAL = "order_terminal"
    ORDER_QUERY_AMBIGUOUS = "order_query_ambiguous"
    ORDER_STATUS_UNKNOWN = "order_status_unknown"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_REJECTED = "cancel_rejected"
    CANCEL_STILL_PENDING = "cancel_still_pending"
    CANCEL_TERMINAL = "cancel_terminal"
    CANCEL_TIMEOUT = "cancel_timeout"
    RECONCILED = "reconciled"
    RECONCILE_FAILED = "reconcile_failed"
    RETRY_SUBMIT = "retry_submit"
    HARD_STOP_CLEAR = "hard_stop_clear"
    HARD_STOP_RESIDUAL = "hard_stop_residual"
    FAULT = "fault"
    RESTART = "restart"


@dataclass(frozen=True)
class SafetyFacts:
    environment_verified: bool = False
    account_verified: bool = False
    orders_reconciled: bool = False
    cash_verified: bool = False
    quote_verified: bool = False
    intent_persisted: bool = False
    unresolved_order: bool = False
    terminal_order_confirmed: bool = False
    submitted_once: bool = False


StateT = TypeVar("StateT", bound=Enum)


@dataclass(frozen=True)
class MachineSnapshot(Generic[StateT]):
    state: StateT
    facts: SafetyFacts = SafetyFacts()


MORNING_TRANSITIONS: Mapping[
    MorningState, Mapping[MorningEvent, MorningState]
] = {
    MorningState.NEW: {
        MorningEvent.BEGIN: MorningState.PREFLIGHT,
    },
    MorningState.PREFLIGHT: {
        MorningEvent.PREFLIGHT_OK: MorningState.RECOVERY,
        MorningEvent.NON_TRADING_DAY: MorningState.SKIPPED,
        MorningEvent.FAULT: MorningState.HALTED,
        MorningEvent.RESTART: MorningState.PREFLIGHT,
    },
    MorningState.RECOVERY: {
        MorningEvent.RECOVERY_CLEAR: MorningState.WAIT_TRIGGER,
        MorningEvent.RECOVERY_ACTIVE: MorningState.ORDER_ACTIVE,
        MorningEvent.RECOVERY_CANCEL_PENDING: MorningState.CANCEL_PENDING,
        MorningEvent.RECOVERY_TERMINAL: MorningState.RECONCILE,
        MorningEvent.RECOVERY_AMBIGUOUS: MorningState.HALTED,
        MorningEvent.FAULT: MorningState.HALTED,
        MorningEvent.RESTART: MorningState.RECOVERY,
    },
    MorningState.WAIT_TRIGGER: {
        MorningEvent.TRIGGER: MorningState.SNAPSHOT,
        MorningEvent.DEADLINE: MorningState.HALTED,
        MorningEvent.FAULT: MorningState.HALTED,
        MorningEvent.RESTART: MorningState.RECOVERY,
    },
    MorningState.SNAPSHOT: {
        MorningEvent.SNAPSHOT_OK: MorningState.READY,
        MorningEvent.SNAPSHOT_RETRY: MorningState.SNAPSHOT,
        MorningEvent.NO_FUNDS: MorningState.HALTED,
        MorningEvent.DEADLINE: MorningState.HALTED,
        MorningEvent.FAULT: MorningState.HALTED,
        MorningEvent.RESTART: MorningState.RECOVERY,
    },
    MorningState.READY: {
        MorningEvent.INTENT_PERSISTED: MorningState.INTENT,
        MorningEvent.FAULT: MorningState.HALTED,
        MorningEvent.RESTART: MorningState.RECOVERY,
    },
    MorningState.INTENT: {
        MorningEvent.SUBMIT_ACCEPTED: MorningState.ORDER_ACTIVE,
        MorningEvent.SUBMIT_REJECTED: MorningState.HALTED,
        MorningEvent.SUBMIT_EXCEPTION: MorningState.SUBMIT_UNKNOWN,
        MorningEvent.RESTART: MorningState.RECOVERY,
    },
    MorningState.SUBMIT_UNKNOWN: {
        MorningEvent.RECOVERED_ACTIVE: MorningState.ORDER_ACTIVE,
        MorningEvent.RECOVERED_CANCEL_PENDING: MorningState.CANCEL_PENDING,
        MorningEvent.RECOVERED_TERMINAL: MorningState.RECONCILE,
        MorningEvent.RECOVERED_NO_MATCH: MorningState.HALTED,
        MorningEvent.RECOVERY_AMBIGUOUS: MorningState.HALTED,
        MorningEvent.RESTART: MorningState.RECOVERY,
    },
    MorningState.ORDER_ACTIVE: {
        MorningEvent.ORDER_STILL_ACTIVE: MorningState.ORDER_ACTIVE,
        MorningEvent.ORDER_TERMINAL: MorningState.RECONCILE,
        MorningEvent.CANCEL_REQUESTED: MorningState.CANCEL_PENDING,
        MorningEvent.ORDER_QUERY_AMBIGUOUS: MorningState.HALTED,
        MorningEvent.ORDER_STATUS_UNKNOWN: MorningState.HALTED,
        MorningEvent.FAULT: MorningState.HALTED,
        MorningEvent.RESTART: MorningState.RECOVERY,
    },
    MorningState.CANCEL_PENDING: {
        MorningEvent.CANCEL_STILL_PENDING: MorningState.CANCEL_PENDING,
        MorningEvent.CANCEL_TERMINAL: MorningState.RECONCILE,
        MorningEvent.CANCEL_REJECTED: MorningState.HALTED,
        MorningEvent.CANCEL_TIMEOUT: MorningState.HALTED,
        MorningEvent.ORDER_QUERY_AMBIGUOUS: MorningState.HALTED,
        MorningEvent.ORDER_STATUS_UNKNOWN: MorningState.HALTED,
        MorningEvent.FAULT: MorningState.HALTED,
        MorningEvent.RESTART: MorningState.RECOVERY,
    },
    MorningState.RECONCILE: {
        MorningEvent.RECONCILED_FULL: MorningState.DONE_FILLED,
        MorningEvent.RECONCILED_RETRY: MorningState.SNAPSHOT,
        MorningEvent.RECONCILED_PARTIAL: MorningState.DONE_PARTIAL,
        MorningEvent.RECONCILED_ZERO: MorningState.HALTED,
        MorningEvent.RECONCILE_FAILED: MorningState.HALTED,
        MorningEvent.RESTART: MorningState.RECOVERY,
    },
    MorningState.DONE_FILLED: {},
    MorningState.DONE_PARTIAL: {},
    MorningState.SKIPPED: {},
    MorningState.HALTED: {},
}


AFTERNOON_TRANSITIONS: Mapping[
    AfternoonState, Mapping[AfternoonEvent, AfternoonState]
] = {
    AfternoonState.NEW: {
        AfternoonEvent.BEGIN: AfternoonState.PREFLIGHT,
    },
    AfternoonState.PREFLIGHT: {
        AfternoonEvent.PREFLIGHT_OK: AfternoonState.RECOVERY,
        AfternoonEvent.NON_TRADING_DAY: AfternoonState.SKIPPED,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.PREFLIGHT,
    },
    AfternoonState.RECOVERY: {
        AfternoonEvent.RECOVERY_CLEAR: AfternoonState.WAIT_WINDOW,
        AfternoonEvent.RECOVERY_ACTIVE: AfternoonState.ORDER_ACTIVE,
        AfternoonEvent.RECOVERY_CANCEL_PENDING: AfternoonState.CANCEL_PENDING,
        AfternoonEvent.RECOVERY_TERMINAL: AfternoonState.RECONCILE,
        AfternoonEvent.RECOVERY_AMBIGUOUS: AfternoonState.HALTED,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.WAIT_WINDOW: {
        AfternoonEvent.TRIGGER: AfternoonState.SCAN,
        AfternoonEvent.HARD_STOP_CLEAR: AfternoonState.COMPLETE,
        AfternoonEvent.HARD_STOP_RESIDUAL: AfternoonState.HALTED,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.SCAN: {
        AfternoonEvent.SCAN_READY: AfternoonState.READY,
        AfternoonEvent.NO_FUNDS: AfternoonState.WAIT_FUNDS,
        AfternoonEvent.NO_BOOK: AfternoonState.WAIT_BOOK,
        AfternoonEvent.HARD_STOP_CLEAR: AfternoonState.COMPLETE,
        AfternoonEvent.HARD_STOP_RESIDUAL: AfternoonState.HALTED,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.WAIT_FUNDS: {
        AfternoonEvent.RETRY_SCAN: AfternoonState.SCAN,
        AfternoonEvent.HARD_STOP_CLEAR: AfternoonState.COMPLETE,
        AfternoonEvent.HARD_STOP_RESIDUAL: AfternoonState.HALTED,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.WAIT_BOOK: {
        AfternoonEvent.RETRY_SCAN: AfternoonState.SCAN,
        AfternoonEvent.HARD_STOP_CLEAR: AfternoonState.COMPLETE,
        AfternoonEvent.HARD_STOP_RESIDUAL: AfternoonState.HALTED,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.READY: {
        AfternoonEvent.INTENT_PERSISTED: AfternoonState.INTENT,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.INTENT: {
        AfternoonEvent.SUBMIT_ACCEPTED: AfternoonState.ORDER_ACTIVE,
        AfternoonEvent.SUBMIT_REJECTED: AfternoonState.BACKOFF,
        AfternoonEvent.SUBMIT_EXCEPTION: AfternoonState.SUBMIT_UNKNOWN,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.SUBMIT_UNKNOWN: {
        AfternoonEvent.RECOVERED_ACTIVE: AfternoonState.ORDER_ACTIVE,
        AfternoonEvent.RECOVERED_CANCEL_PENDING: AfternoonState.CANCEL_PENDING,
        AfternoonEvent.RECOVERED_TERMINAL: AfternoonState.RECONCILE,
        AfternoonEvent.RECOVERED_NO_MATCH: AfternoonState.HALTED,
        AfternoonEvent.RECOVERY_AMBIGUOUS: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.ORDER_ACTIVE: {
        AfternoonEvent.ORDER_STILL_ACTIVE: AfternoonState.ORDER_ACTIVE,
        AfternoonEvent.ORDER_TERMINAL: AfternoonState.RECONCILE,
        AfternoonEvent.CANCEL_REQUESTED: AfternoonState.CANCEL_PENDING,
        AfternoonEvent.ORDER_QUERY_AMBIGUOUS: AfternoonState.HALTED,
        AfternoonEvent.ORDER_STATUS_UNKNOWN: AfternoonState.HALTED,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.CANCEL_PENDING: {
        AfternoonEvent.CANCEL_STILL_PENDING: AfternoonState.CANCEL_PENDING,
        AfternoonEvent.CANCEL_TERMINAL: AfternoonState.RECONCILE,
        AfternoonEvent.CANCEL_REJECTED: AfternoonState.HALTED,
        AfternoonEvent.CANCEL_TIMEOUT: AfternoonState.HALTED,
        AfternoonEvent.ORDER_QUERY_AMBIGUOUS: AfternoonState.HALTED,
        AfternoonEvent.ORDER_STATUS_UNKNOWN: AfternoonState.HALTED,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.RECONCILE: {
        AfternoonEvent.RECONCILED: AfternoonState.SCAN,
        AfternoonEvent.RECONCILE_FAILED: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.BACKOFF: {
        AfternoonEvent.RETRY_SUBMIT: AfternoonState.SCAN,
        AfternoonEvent.HARD_STOP_CLEAR: AfternoonState.COMPLETE,
        AfternoonEvent.HARD_STOP_RESIDUAL: AfternoonState.HALTED,
        AfternoonEvent.FAULT: AfternoonState.HALTED,
        AfternoonEvent.RESTART: AfternoonState.RECOVERY,
    },
    AfternoonState.COMPLETE: {},
    AfternoonState.SKIPPED: {},
    AfternoonState.HALTED: {},
}


MORNING_TERMINAL_STATES = {
    MorningState.DONE_FILLED,
    MorningState.DONE_PARTIAL,
    MorningState.SKIPPED,
    MorningState.HALTED,
}
AFTERNOON_TERMINAL_STATES = {
    AfternoonState.COMPLETE,
    AfternoonState.SKIPPED,
    AfternoonState.HALTED,
}


def initial_morning_snapshot() -> MachineSnapshot[MorningState]:
    return MachineSnapshot(MorningState.NEW)


def initial_afternoon_snapshot() -> MachineSnapshot[AfternoonState]:
    return MachineSnapshot(AfternoonState.NEW)


def advance_morning(
    snapshot: MachineSnapshot[MorningState],
    event: MorningEvent,
) -> MachineSnapshot[MorningState]:
    next_state = _next_state(MORNING_TRANSITIONS, snapshot.state, event)
    facts = snapshot.facts
    if event is MorningEvent.PREFLIGHT_OK:
        facts = replace(
            facts,
            environment_verified=True,
            account_verified=True,
        )
    elif event is MorningEvent.RECOVERY_CLEAR:
        facts = replace(
            facts,
            orders_reconciled=True,
            unresolved_order=False,
            terminal_order_confirmed=False,
        )
    elif event in {
        MorningEvent.RECOVERY_ACTIVE,
        MorningEvent.RECOVERY_CANCEL_PENDING,
        MorningEvent.RECOVERED_ACTIVE,
        MorningEvent.RECOVERED_CANCEL_PENDING,
        MorningEvent.SUBMIT_ACCEPTED,
    }:
        facts = replace(
            facts,
            orders_reconciled=True,
            unresolved_order=True,
            submitted_once=True,
            intent_persisted=True,
        )
    elif event is MorningEvent.RECOVERY_TERMINAL:
        facts = replace(
            facts,
            orders_reconciled=True,
            unresolved_order=False,
            terminal_order_confirmed=True,
            submitted_once=True,
            intent_persisted=True,
        )
    elif event is MorningEvent.SNAPSHOT_OK:
        facts = replace(
            facts,
            cash_verified=True,
            quote_verified=True,
        )
    elif event is MorningEvent.INTENT_PERSISTED:
        facts = replace(facts, intent_persisted=True)
    elif event is MorningEvent.SUBMIT_EXCEPTION:
        facts = replace(
            facts,
            unresolved_order=True,
            submitted_once=True,
        )
    elif event is MorningEvent.SUBMIT_REJECTED:
        facts = replace(facts, unresolved_order=False)
    elif event in {
        MorningEvent.ORDER_TERMINAL,
        MorningEvent.CANCEL_TERMINAL,
        MorningEvent.RECOVERED_TERMINAL,
    }:
        facts = replace(
            facts,
            unresolved_order=False,
            terminal_order_confirmed=True,
            submitted_once=True,
        )
    elif event is MorningEvent.RECONCILED_RETRY:
        facts = replace(
            facts,
            cash_verified=False,
            quote_verified=False,
            intent_persisted=False,
            unresolved_order=False,
            terminal_order_confirmed=False,
        )
    elif event is MorningEvent.RESTART:
        facts = _restart_facts(snapshot)
    elif next_state is MorningState.HALTED:
        unresolved = facts.unresolved_order or snapshot.state in {
            MorningState.INTENT,
            MorningState.SUBMIT_UNKNOWN,
            MorningState.ORDER_ACTIVE,
            MorningState.CANCEL_PENDING,
        }
        facts = replace(facts, unresolved_order=unresolved)
    result = MachineSnapshot(next_state, facts)
    assert_morning_invariants(result)
    return result


def advance_afternoon(
    snapshot: MachineSnapshot[AfternoonState],
    event: AfternoonEvent,
) -> MachineSnapshot[AfternoonState]:
    next_state = _next_state(AFTERNOON_TRANSITIONS, snapshot.state, event)
    facts = snapshot.facts
    if event is AfternoonEvent.PREFLIGHT_OK:
        facts = replace(
            facts,
            environment_verified=True,
            account_verified=True,
        )
    elif event is AfternoonEvent.RECOVERY_CLEAR:
        facts = replace(
            facts,
            orders_reconciled=True,
            unresolved_order=False,
            terminal_order_confirmed=False,
        )
    elif event in {
        AfternoonEvent.RECOVERY_ACTIVE,
        AfternoonEvent.RECOVERY_CANCEL_PENDING,
        AfternoonEvent.RECOVERED_ACTIVE,
        AfternoonEvent.RECOVERED_CANCEL_PENDING,
        AfternoonEvent.SUBMIT_ACCEPTED,
    }:
        facts = replace(
            facts,
            orders_reconciled=True,
            unresolved_order=True,
            submitted_once=True,
            intent_persisted=True,
        )
    elif event is AfternoonEvent.RECOVERY_TERMINAL:
        facts = replace(
            facts,
            orders_reconciled=True,
            unresolved_order=False,
            terminal_order_confirmed=True,
            submitted_once=True,
            intent_persisted=True,
        )
    elif event is AfternoonEvent.SCAN_READY:
        facts = replace(
            facts,
            cash_verified=True,
            quote_verified=True,
        )
    elif event is AfternoonEvent.INTENT_PERSISTED:
        facts = replace(facts, intent_persisted=True)
    elif event is AfternoonEvent.SUBMIT_EXCEPTION:
        facts = replace(
            facts,
            unresolved_order=True,
            submitted_once=True,
        )
    elif event in {
        AfternoonEvent.ORDER_TERMINAL,
        AfternoonEvent.CANCEL_TERMINAL,
        AfternoonEvent.RECOVERED_TERMINAL,
    }:
        facts = replace(
            facts,
            unresolved_order=False,
            terminal_order_confirmed=True,
            submitted_once=True,
        )
    elif event in {
        AfternoonEvent.RECONCILED,
        AfternoonEvent.SUBMIT_REJECTED,
    }:
        facts = replace(
            facts,
            cash_verified=False,
            quote_verified=False,
            intent_persisted=False,
            unresolved_order=False,
            terminal_order_confirmed=False,
        )
    elif event is AfternoonEvent.RESTART:
        facts = _restart_facts(snapshot)
    elif next_state is AfternoonState.HALTED:
        unresolved = facts.unresolved_order or snapshot.state in {
            AfternoonState.INTENT,
            AfternoonState.SUBMIT_UNKNOWN,
            AfternoonState.ORDER_ACTIVE,
            AfternoonState.CANCEL_PENDING,
        }
        facts = replace(facts, unresolved_order=unresolved)
    result = MachineSnapshot(next_state, facts)
    assert_afternoon_invariants(result)
    return result


def assert_morning_invariants(
    snapshot: MachineSnapshot[MorningState],
) -> None:
    facts = snapshot.facts
    guarded = {
        MorningState.READY,
        MorningState.INTENT,
        MorningState.SUBMIT_UNKNOWN,
        MorningState.ORDER_ACTIVE,
        MorningState.CANCEL_PENDING,
        MorningState.RECONCILE,
        MorningState.DONE_FILLED,
        MorningState.DONE_PARTIAL,
    }
    if snapshot.state in guarded and not (
        facts.environment_verified
        and facts.account_verified
        and facts.orders_reconciled
    ):
        raise InvariantViolation("morning order path lacks preflight gates")
    if snapshot.state in {
        MorningState.READY,
        MorningState.INTENT,
    } and not (facts.cash_verified and facts.quote_verified):
        raise InvariantViolation("morning submission lacks cash or quote gate")
    if snapshot.state in {
        MorningState.INTENT,
        MorningState.SUBMIT_UNKNOWN,
        MorningState.ORDER_ACTIVE,
        MorningState.CANCEL_PENDING,
        MorningState.RECONCILE,
        MorningState.DONE_FILLED,
        MorningState.DONE_PARTIAL,
    } and not facts.intent_persisted:
        raise InvariantViolation("morning external order lacks durable intent")
    _assert_unresolved_shape(
        snapshot.state,
        facts,
        {
            MorningState.RECOVERY,
            MorningState.SUBMIT_UNKNOWN,
            MorningState.ORDER_ACTIVE,
            MorningState.CANCEL_PENDING,
            MorningState.HALTED,
        },
    )
    if snapshot.state in {
        MorningState.DONE_FILLED,
        MorningState.DONE_PARTIAL,
    } and (
        facts.unresolved_order or not facts.terminal_order_confirmed
    ):
        raise InvariantViolation("morning success has an unresolved order")


def assert_afternoon_invariants(
    snapshot: MachineSnapshot[AfternoonState],
) -> None:
    facts = snapshot.facts
    guarded = {
        AfternoonState.READY,
        AfternoonState.INTENT,
        AfternoonState.SUBMIT_UNKNOWN,
        AfternoonState.ORDER_ACTIVE,
        AfternoonState.CANCEL_PENDING,
        AfternoonState.RECONCILE,
    }
    if snapshot.state in guarded and not (
        facts.environment_verified
        and facts.account_verified
        and facts.orders_reconciled
    ):
        raise InvariantViolation("afternoon order path lacks preflight gates")
    if snapshot.state in {
        AfternoonState.READY,
        AfternoonState.INTENT,
    } and not (facts.cash_verified and facts.quote_verified):
        raise InvariantViolation("afternoon submission lacks cash or quote gate")
    if snapshot.state in {
        AfternoonState.INTENT,
        AfternoonState.SUBMIT_UNKNOWN,
        AfternoonState.ORDER_ACTIVE,
        AfternoonState.CANCEL_PENDING,
        AfternoonState.RECONCILE,
    } and not facts.intent_persisted:
        raise InvariantViolation("afternoon external order lacks durable intent")
    _assert_unresolved_shape(
        snapshot.state,
        facts,
        {
            AfternoonState.RECOVERY,
            AfternoonState.SUBMIT_UNKNOWN,
            AfternoonState.ORDER_ACTIVE,
            AfternoonState.CANCEL_PENDING,
            AfternoonState.HALTED,
        },
    )
    if snapshot.state is AfternoonState.COMPLETE and facts.unresolved_order:
        raise InvariantViolation("afternoon completion has unresolved order")


def verify_state_machines() -> dict[str, object]:
    morning = _verify_machine(
        name="morning",
        initial=initial_morning_snapshot(),
        transitions=MORNING_TRANSITIONS,
        terminal_states=MORNING_TERMINAL_STATES,
        advance=advance_morning,
        invariant=assert_morning_invariants,
    )
    afternoon = _verify_machine(
        name="afternoon",
        initial=initial_afternoon_snapshot(),
        transitions=AFTERNOON_TRANSITIONS,
        terminal_states=AFTERNOON_TERMINAL_STATES,
        advance=advance_afternoon,
        invariant=assert_afternoon_invariants,
    )
    source = {
        "morning": _transition_payload(MORNING_TRANSITIONS),
        "afternoon": _transition_payload(AFTERNOON_TRANSITIONS),
    }
    digest = hashlib.sha256(
        json.dumps(source, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "method": "exhaustive explicit-state reachability to fixed point",
        "transition_spec_sha256": digest,
        "execution_source_sha256": execution_source_sha256(),
        "morning": morning,
        "afternoon": afternoon,
        "proved_invariants": [
            "submission requires verified environment and account",
            "submission requires reconciled broker order snapshot",
            "submission requires verified cash and fresh quote",
            "external order requires a durable pre-submit intent",
            "no completion state contains an unresolved order",
            "an unresolved order cannot return to a ready state",
            (
                "a morning reprice requires terminal confirmation of "
                "the prior order"
            ),
            "every reachable nonterminal state can reach a terminal state",
            "every declared state and transition is reachable",
        ],
    }


def execution_source_sha256() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in EXECUTION_SOURCE_FILES:
        path = root / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def snapshot_to_payload(snapshot: MachineSnapshot[Enum]) -> dict[str, object]:
    return {
        "state": str(snapshot.state.value),
        "facts": asdict(snapshot.facts),
    }


def morning_snapshot_from_payload(
    payload: Mapping[str, object],
) -> MachineSnapshot[MorningState]:
    return _snapshot_from_payload(payload, MorningState)


def afternoon_snapshot_from_payload(
    payload: Mapping[str, object],
) -> MachineSnapshot[AfternoonState]:
    return _snapshot_from_payload(payload, AfternoonState)


def _next_state(
    transitions: Mapping[StateT, Mapping[Enum, StateT]],
    state: StateT,
    event: Enum,
) -> StateT:
    try:
        return transitions[state][event]
    except KeyError as exc:
        raise InvalidTransition(
            f"event {event.value!r} is invalid in state {state.value!r}"
        ) from exc


def _snapshot_from_payload(
    payload: Mapping[str, object],
    state_type: type[StateT],
) -> MachineSnapshot[StateT]:
    if not isinstance(payload, Mapping):
        raise InvariantViolation("machine snapshot must be a mapping")
    raw_facts = payload.get("facts")
    if not isinstance(raw_facts, Mapping):
        raise InvariantViolation("machine facts must be a mapping")
    expected = set(SafetyFacts.__dataclass_fields__)
    if set(raw_facts) != expected:
        raise InvariantViolation("machine facts fields do not match schema")
    if any(not isinstance(raw_facts[key], bool) for key in expected):
        raise InvariantViolation("machine facts must all be booleans")
    snapshot = MachineSnapshot(
        state=state_type(str(payload.get("state"))),
        facts=SafetyFacts(**dict(raw_facts)),
    )
    if state_type is MorningState:
        assert_morning_invariants(snapshot)
    else:
        assert_afternoon_invariants(snapshot)
    return snapshot


def _restart_facts(snapshot: MachineSnapshot[Enum]) -> SafetyFacts:
    facts = snapshot.facts
    possibly_sent = facts.intent_persisted or facts.submitted_once
    return replace(
        facts,
        orders_reconciled=False,
        cash_verified=False,
        quote_verified=False,
        unresolved_order=facts.unresolved_order or possibly_sent,
        terminal_order_confirmed=False,
    )


def _assert_unresolved_shape(
    state: Enum,
    facts: SafetyFacts,
    allowed_states: set[Enum],
) -> None:
    if facts.unresolved_order and state not in allowed_states:
        raise InvariantViolation(
            f"unresolved order is illegal in state {state.value}"
        )


def _verify_machine(
    *,
    name: str,
    initial: MachineSnapshot,
    transitions: Mapping,
    terminal_states: set[Enum],
    advance: object,
    invariant: object,
) -> dict[str, object]:
    queue = deque([initial])
    reachable = {initial}
    edges: set[tuple[MachineSnapshot, Enum, MachineSnapshot]] = set()
    phase_events: set[tuple[Enum, Enum]] = set()
    while queue:
        current = queue.popleft()
        invariant(current)
        for event in transitions[current.state]:
            successor = advance(current, event)
            invariant(successor)
            edges.add((current, event, successor))
            phase_events.add((current.state, event))
            if successor not in reachable:
                reachable.add(successor)
                queue.append(successor)

    declared_edges = {
        (state, event)
        for state, event_map in transitions.items()
        for event in event_map
    }
    missing_edges = declared_edges - phase_events
    if missing_edges:
        formatted = sorted(
            f"{state.value}:{event.value}" for state, event in missing_edges
        )
        raise InvariantViolation(
            f"{name} has unreachable transitions: {formatted}"
        )
    reachable_phases = {item.state for item in reachable}
    missing_states = set(transitions) - reachable_phases
    if missing_states:
        raise InvariantViolation(
            f"{name} has unreachable states: "
            f"{sorted(item.value for item in missing_states)}"
        )

    reverse: dict[MachineSnapshot, set[MachineSnapshot]] = {
        item: set() for item in reachable
    }
    for source, _, destination in edges:
        reverse[destination].add(source)
    can_terminate = {
        item for item in reachable if item.state in terminal_states
    }
    frontier = deque(can_terminate)
    while frontier:
        destination = frontier.popleft()
        for source in reverse[destination]:
            if source not in can_terminate:
                can_terminate.add(source)
                frontier.append(source)
    nonterminating = reachable - can_terminate
    if nonterminating:
        raise InvariantViolation(
            f"{name} contains states with no terminal path"
        )

    for source, _, destination in edges:
        if (
            source.facts.unresolved_order
            and destination.state
            in {
                MorningState.READY,
                AfternoonState.READY,
            }
        ):
            raise InvariantViolation(
                f"{name} can submit while an earlier order is unresolved"
            )
        if (
            name == "morning"
            and source.state is MorningState.RECONCILE
            and destination.state is MorningState.SNAPSHOT
            and (
                source.facts.unresolved_order
                or not source.facts.terminal_order_confirmed
            )
        ):
            raise InvariantViolation(
                "morning retry can bypass terminal order confirmation"
            )

    return {
        "reachable_abstract_states": len(reachable),
        "reachable_transitions": len(edges),
        "declared_states": len(transitions),
        "declared_phase_event_edges": len(declared_edges),
        "terminal_abstract_states": len(
            [item for item in reachable if item.state in terminal_states]
        ),
        "unreachable_states": 0,
        "unreachable_transitions": 0,
        "states_without_terminal_path": 0,
        "invariant_violations": 0,
    }


def _transition_payload(transitions: Mapping) -> dict[str, dict[str, str]]:
    return {
        state.value: {
            event.value: destination.value
            for event, destination in event_map.items()
        }
        for state, event_map in transitions.items()
    }
