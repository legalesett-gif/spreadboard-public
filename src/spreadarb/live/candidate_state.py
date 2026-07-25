"""Shared validation-state labels for opportunity candidates."""

from __future__ import annotations

DISCOVERED_STATE = "discovered"
QUOTE_VERIFIED_STATE = "quote_verified"
IDENTITY_VERIFIED_STATE = "identity_verified"
ROUTE_FEASIBLE_STATE = "route_feasible"
EXECUTOR_READY_STATE = "executor_ready"

VALIDATION_STATES: tuple[str, ...] = (
    DISCOVERED_STATE,
    QUOTE_VERIFIED_STATE,
    IDENTITY_VERIFIED_STATE,
    ROUTE_FEASIBLE_STATE,
    EXECUTOR_READY_STATE,
)
_STATE_RANK = {state: idx for idx, state in enumerate(VALIDATION_STATES)}


def normalize_validation_state(value: object, *, default: str = DISCOVERED_STATE) -> str:
    state = str(value or "").strip().lower()
    if state in _STATE_RANK:
        return state
    return default


def state_at_least(value: object, minimum: str) -> bool:
    state = normalize_validation_state(value)
    floor = normalize_validation_state(minimum)
    return _STATE_RANK[state] >= _STATE_RANK[floor]


def is_executor_ready(value: object) -> bool:
    return normalize_validation_state(value) == EXECUTOR_READY_STATE


def validation_state_label(value: object) -> str:
    return normalize_validation_state(value).replace("_", "-")
