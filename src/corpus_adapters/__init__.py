"""Shared bounded source-adapter contracts for the corpus engine."""

from .base import AdapterFailure, AdapterRunner, SourceAdapter
from .types import (
    FailureKind,
    InventoryRequest,
    NormalizedObservation,
    ObservationBatch,
    RightsState,
    SourceSpec,
    TransportPayload,
)

__all__ = [
    "AdapterFailure",
    "AdapterRunner",
    "FailureKind",
    "InventoryRequest",
    "NormalizedObservation",
    "ObservationBatch",
    "RightsState",
    "SourceAdapter",
    "SourceSpec",
    "TransportPayload",
]
