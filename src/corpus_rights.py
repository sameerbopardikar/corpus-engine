#!/usr/bin/env python3
"""One canonical rights vocabulary and its fail-closed conversion boundary.

Every part of the engine — intake, adapters, discovery, priority, domain specs,
and projections — shares the four-value :class:`RightsState` vocabulary defined
in :mod:`corpus_adapters.types`. This module is the single audited place that:

* converts the legacy intake vocabulary (``public``/``owned``/``licensed``/
  ``unknown``) into canonical rights, fail-closed and never upgrading;
* coerces arbitrary values onto canonical rights, rejecting anything unknown
  (including the legacy ``unknown`` string) rather than silently passing it;
* answers whether a rights state may feed the shared corpus.

Rights classification is monotone-restrictive: nothing here can *upgrade* a
source toward a more permissive/shareable state. Public accessibility alone is
never treated as redistribution permission, so no legacy value maps to the only
shared-corpus-eligible state, ``public_rights_clear``.
"""
from __future__ import annotations

from corpus_adapters.types import RightsState

# The canonical vocabulary is exactly the RightsState enum values.
CANONICAL_RIGHTS = frozenset(state.value for state in RightsState)

# The historical intake vocabulary, kept only as a conversion source.
LEGACY_INTAKE_RIGHTS = frozenset({"public", "owned", "licensed", "unknown"})


class RightsError(ValueError):
    """Raised when a rights value cannot be classified safely."""


# Fail-closed legacy -> canonical map. No legacy value proves redistribution
# rights, so none maps to the shared-corpus-eligible ``public_rights_clear``.
_LEGACY_TO_CANONICAL: dict[str, RightsState] = {
    "public": RightsState.PUBLIC_METADATA_ONLY,   # accessible != redistributable
    "owned": RightsState.PRIVATE_AUTHORIZED,       # owner may use privately
    "licensed": RightsState.PRIVATE_AUTHORIZED,    # licensed use, not public release
    "unknown": RightsState.RIGHTS_UNCLEAR,
}

# Permissiveness ordering, lowest = most restrictive. Any increase is an
# "upgrade" and is forbidden for scout/parser/suggestion/feedback events.
_PERMISSIVENESS: dict[RightsState, int] = {
    RightsState.RIGHTS_UNCLEAR: 0,
    RightsState.PUBLIC_METADATA_ONLY: 1,
    RightsState.PRIVATE_AUTHORIZED: 2,
    RightsState.PUBLIC_RIGHTS_CLEAR: 3,
}


def normalize_intake_rights(value: object) -> RightsState:
    """Convert a legacy intake rights string to a canonical RightsState.

    Fails closed: a non-string or an unmapped value raises rather than
    defaulting to any permissive state.
    """
    if not isinstance(value, str):
        raise RightsError(f"legacy intake rights must be a string, got {type(value).__name__}")
    key = value.strip().lower()
    mapped = _LEGACY_TO_CANONICAL.get(key)
    if mapped is None:
        raise RightsError(f"unmappable legacy intake rights: {value!r}")
    return mapped


def coerce_rights(value: object) -> RightsState:
    """Coerce a canonical rights value (enum or string) to a RightsState.

    Rejects anything outside the canonical vocabulary, including legacy strings
    such as ``unknown``, so legacy values cannot leak past this boundary.
    """
    if isinstance(value, RightsState):
        return value
    if isinstance(value, str) and value in CANONICAL_RIGHTS:
        return RightsState(value)
    raise RightsError(f"not a canonical rights state: {value!r}")


def combine_rights(a: RightsState, b: RightsState) -> RightsState:
    """Return the most restrictive of two rights states (never an upgrade)."""
    return a if _PERMISSIVENESS[a] <= _PERMISSIVENESS[b] else b


def is_rights_upgrade(current: RightsState, proposed: RightsState) -> bool:
    """True when ``proposed`` is strictly more permissive than ``current``."""
    return _PERMISSIVENESS[proposed] > _PERMISSIVENESS[current]


def is_shared_corpus_eligible(state: RightsState) -> bool:
    """Only fully rights-cleared sources may feed the shared corpus."""
    return state is RightsState.PUBLIC_RIGHTS_CLEAR
