#!/usr/bin/env python3
"""Freeze a blinded worker packet and scan every worker-visible byte.

A blinded holdout is only blinded if the target is absent from *everything* the
worker can see - not just the seed packet, but the artifact bytes, their titles,
their URLs, and any surrounding configuration. This module hashes the complete
packet and each referenced artifact body, then scans the whole worker-visible
surface for the hidden evaluator's target label, its accepted close labels, and
its forbidden terms.

It reads the hidden evaluator only to obtain the terms to search for; the
evaluator itself is never mixed into the worker-visible surface.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

SCHEMA_VERSION = 1
_TEXT_FIELDS = ("transcript_excerpt", "content", "text", "body", "excerpt")
_TERM_FIELDS = ("target_label", "accepted_close_labels", "forbidden_seed_terms")


class HoldoutFreezeError(ValueError):
    """A worker packet or hidden evaluator cannot be frozen or scanned."""


def _object(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HoldoutFreezeError(f"{name} must be an object")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def hidden_terms(hidden_evaluator: dict[str, Any]) -> list[str]:
    """Collect every term whose presence would unblind the holdout."""
    hidden_evaluator = _object("hidden_evaluator", hidden_evaluator)
    terms: set[str] = set()
    for field in _TERM_FIELDS:
        value = hidden_evaluator.get(field)
        if isinstance(value, str):
            terms.add(value)
        elif isinstance(value, list):
            terms.update(item for item in value if isinstance(item, str))
    resolved = sorted(term.strip() for term in terms if isinstance(term, str) and term.strip())
    if not resolved:
        raise HoldoutFreezeError("hidden evaluator declares no target or forbidden terms")
    return resolved


def _artifacts(packet: Any, path: str = "") -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(packet, dict):
        if isinstance(packet.get("url"), str) and any(
            isinstance(packet.get(field), str) for field in _TEXT_FIELDS
        ):
            yield path, packet
        for key, value in packet.items():
            yield from _artifacts(value, f"{path}.{key}" if path else str(key))
    elif isinstance(packet, list):
        for index, value in enumerate(packet):
            yield from _artifacts(value, f"{path}[{index}]")


def freeze_worker_packet(
    packet: dict[str, Any],
    *,
    hidden_evaluator: dict[str, Any],
    packet_label: str,
) -> dict[str, Any]:
    """Hash the packet and its artifact bodies, then scan for hidden terms."""
    packet = _object("worker packet", packet)
    terms = hidden_terms(hidden_evaluator)
    packet_bytes = _canonical_bytes(packet)
    haystack = packet_bytes.decode("utf-8").casefold()

    artifacts: list[dict[str, Any]] = []
    bodies: dict[str, str] = {}
    for path, artifact in _artifacts(packet):
        field = next(field for field in _TEXT_FIELDS if isinstance(artifact.get(field), str))
        body = artifact[field].encode("utf-8")
        bodies[path] = _canonical_bytes(artifact).decode("utf-8").casefold()
        artifacts.append(
            {
                "packet_path": path,
                "url": artifact["url"],
                "text_field": field,
                "sha256": hashlib.sha256(body).hexdigest(),
                "byte_length": len(body),
                "declared_source_sha256": artifact.get("source_sha256"),
            }
        )
    artifacts.sort(key=lambda item: (item["url"], item["packet_path"]))

    findings = []
    for term in terms:
        folded = term.casefold()
        if folded not in haystack:
            continue
        locations = sorted(path for path, text in bodies.items() if folded in text)
        findings.append({"term": term, "locations": locations or ["packet"]})
    leaked = [finding["term"] for finding in findings]
    return {
        "schema_version": SCHEMA_VERSION,
        "packet_label": packet_label,
        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "packet_byte_length": len(packet_bytes),
        "worker_visible_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "scanned_terms": terms,
        "leaked_terms": leaked,
        "leakage_findings": findings,
        "leakage_absent": not leaked,
    }
