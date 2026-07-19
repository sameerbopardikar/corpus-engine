#!/usr/bin/env python3
"""Canonical acquisition lifecycle: durable states and transition validation.

Every source the engine acquires walks one auditable state machine:

    discovered → locator_verified → rights_resolved → queued → acquiring →
    raw_preserved → normalized → staged → evaluated → probationary

with fail-closed branches to ``rejected`` (before the corpus is touched) and
``quarantined`` (after staging, when integrity/retrieval fails). No state may
skip locator or rights verification, content states are unreachable without a
completed queue admission, and terminal states never transition again. The
state machine carries no I/O — the executor drives it and records its history
truthfully in each receipt.
"""
from __future__ import annotations

from enum import Enum


class AcquisitionState(str, Enum):
    DISCOVERED = "discovered"
    LOCATOR_VERIFIED = "locator_verified"
    RIGHTS_RESOLVED = "rights_resolved"
    QUEUED = "queued"
    ACQUIRING = "acquiring"
    RAW_PRESERVED = "raw_preserved"
    NORMALIZED = "normalized"
    STAGED = "staged"
    EVALUATED = "evaluated"
    PROBATIONARY = "probationary"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


ACQUISITION_STATES = frozenset(AcquisitionState)

TERMINAL_STATES = frozenset(
    {
        AcquisitionState.PROBATIONARY,
        AcquisitionState.REJECTED,
        AcquisitionState.QUARANTINED,
    }
)

# Allowed directed edges. ``rejected`` is reachable from every pre-staging state
# (a fail-closed exit before the corpus is touched); ``quarantined`` is only
# reachable once bytes have been staged and then fail integrity/retrieval.
ACQUISITION_TRANSITIONS: dict[AcquisitionState, frozenset[AcquisitionState]] = {
    AcquisitionState.DISCOVERED: frozenset(
        {AcquisitionState.LOCATOR_VERIFIED, AcquisitionState.REJECTED}
    ),
    AcquisitionState.LOCATOR_VERIFIED: frozenset(
        {AcquisitionState.RIGHTS_RESOLVED, AcquisitionState.REJECTED}
    ),
    AcquisitionState.RIGHTS_RESOLVED: frozenset(
        {AcquisitionState.QUEUED, AcquisitionState.REJECTED}
    ),
    AcquisitionState.QUEUED: frozenset(
        {AcquisitionState.ACQUIRING, AcquisitionState.REJECTED}
    ),
    AcquisitionState.ACQUIRING: frozenset(
        {AcquisitionState.RAW_PRESERVED, AcquisitionState.REJECTED}
    ),
    AcquisitionState.RAW_PRESERVED: frozenset(
        {AcquisitionState.NORMALIZED, AcquisitionState.REJECTED}
    ),
    AcquisitionState.NORMALIZED: frozenset(
        {AcquisitionState.STAGED, AcquisitionState.REJECTED}
    ),
    AcquisitionState.STAGED: frozenset(
        {AcquisitionState.EVALUATED, AcquisitionState.QUARANTINED}
    ),
    AcquisitionState.EVALUATED: frozenset(
        {AcquisitionState.PROBATIONARY, AcquisitionState.QUARANTINED}
    ),
    AcquisitionState.PROBATIONARY: frozenset(),
    AcquisitionState.REJECTED: frozenset(),
    AcquisitionState.QUARANTINED: frozenset(),
}


class LifecycleError(ValueError):
    """Raised on an illegal acquisition state transition."""


def validate_transition(current: AcquisitionState, new: AcquisitionState) -> None:
    """Raise :class:`LifecycleError` unless ``current → new`` is a legal edge."""
    if not isinstance(current, AcquisitionState) or not isinstance(new, AcquisitionState):
        raise LifecycleError("transition endpoints must be AcquisitionState values")
    if new not in ACQUISITION_TRANSITIONS[current]:
        raise LifecycleError(f"illegal acquisition transition: {current.value!r} -> {new.value!r}")


class AcquisitionLifecycle:
    """A single candidate's ordered, validated walk through the state machine."""

    def __init__(self, *, start: AcquisitionState = AcquisitionState.DISCOVERED):
        if not isinstance(start, AcquisitionState):
            raise LifecycleError("start must be an AcquisitionState")
        self._state = start
        self._history: list[AcquisitionState] = [start]

    @property
    def state(self) -> AcquisitionState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    @property
    def history(self) -> tuple[str, ...]:
        return tuple(state.value for state in self._history)

    def advance(self, new: AcquisitionState) -> AcquisitionState:
        validate_transition(self._state, new)
        self._state = new
        self._history.append(new)
        return new
