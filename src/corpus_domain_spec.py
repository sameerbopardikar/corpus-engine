#!/usr/bin/env python3
"""Strict declarative domain specifications for the generalized Corpus Engine.

A :class:`DomainSpec` lets a new corpus domain be created from data alone — an
identity, a provisional ontology, evidence-lane weights, source-family and
acquisition policy, a feedback profile, bootstrap budgets, corpus roots, and
eval requirements — with no domain-specific engine code. Loading validates and
never promotes: the spec is a contract, not adopted belief.

The rights vocabulary and the "shared corpus never carries private evidence"
invariant intentionally mirror :mod:`corpus_adapters.types` so a spec cannot
declare an unsafe acquisition or export posture.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from corpus_adapters.types import RightsState
from corpus_rights import CANONICAL_RIGHTS, is_shared_corpus_eligible

SPEC_SCHEMA_VERSION = 1

# Canonical rights vocabulary — the single source shared with intake, adapters,
# discovery, priority, and projections via corpus_rights.
RIGHTS_STATES = set(CANONICAL_RIGHTS)
# Only fully rights-cleared families may feed the shared corpus. Everything else
# (private or unclear) must stay in private/personal storage.
_SHARED_ELIGIBLE_RIGHTS = {
    state.value for state in RightsState if is_shared_corpus_eligible(state)
}

_TOP_FIELDS = {
    "schema_version", "domain", "title", "objective", "epistemic_policy",
    "seed_ref", "roots", "ontology", "evidence_lanes", "source_families",
    "feedback_profile", "acquisition_policy", "bootstrap", "promotion",
    "eval_requirements",
}
_ROOT_FIELDS = {"corpus_root", "state_root", "archive_root", "output_root"}
_ONTOLOGY_FIELDS = {"axes"}
_AXIS_FIELDS = {"key", "title", "topics"}
_FAMILY_FIELDS = {"family", "acquisition_mode", "rights_state", "shared_corpus_eligible"}
_FEEDBACK_FIELDS = {
    "corroboration_increment", "starvation_age_boost_per_day",
    "starvation_max_boost", "yield_target_per_cycle",
}
_ACQUISITION_FIELDS = {"max_suggestions", "family_priority"}
_BOOTSTRAP_FIELDS = {
    "max_items_per_cycle", "max_deep_acquisitions_per_utc_day",
    "max_llm_tasks_per_cycle", "max_cost_usd_per_utc_day",
}
_PROMOTION_FIELDS = {
    "automatic_promotion_enabled", "probationary_threshold",
    "promotion_threshold", "rejection_threshold",
}
_EVAL_FIELDS = {
    "min_corpus_pages_scanned", "require_axes_in_field_map",
    "forbid_private_export_to_shared", "require_next_gap",
}


class DomainSpecError(ValueError):
    """Raised when a domain spec is malformed or unsafe to load."""


class DomainBootstrapError(DomainSpecError):
    """Raised when a domain cannot be bootstrapped safely."""


# The generalized engine binds every domain to one shared GBrain source. Bootstrap
# never mints a per-domain source, scheduler, or cron.
CANONICAL_SOURCE_ID = "corpora"
BOOTSTRAP_MARKER = "domain-state.json"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainSpecError(f"{field_name} must be a non-empty string")
    return value.strip()


def _kebab(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-"
    if text != text.lower() or any(char not in allowed for char in text):
        raise DomainSpecError(f"{field_name} must be lowercase kebab-case")
    if text.startswith("-") or text.endswith("-") or "--" in text:
        raise DomainSpecError(f"{field_name} must be lowercase kebab-case")
    return text


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise DomainSpecError(f"{field_name} must be a boolean")
    return value


def _number(value: Any, field_name: str, *, minimum: float = 0.0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainSpecError(f"{field_name} must be a finite number")
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise DomainSpecError(f"{field_name} must be a finite number")
    if numeric < minimum:
        raise DomainSpecError(f"{field_name} must be >= {minimum}")
    if maximum is not None and numeric > maximum:
        raise DomainSpecError(f"{field_name} must be <= {maximum}")
    return numeric


def _exact_fields(data: Any, allowed: set[str], name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DomainSpecError(f"{name} must be an object")
    unknown = set(data) - allowed
    missing = allowed - set(data)
    if unknown or missing:
        raise DomainSpecError(f"{name} fields mismatch: missing={sorted(missing)} unknown={sorted(unknown)}")
    return data


def _relative_path(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    pure = PurePosixPath(text)
    if pure.is_absolute():
        raise DomainSpecError(f"{field_name} must be a relative path")
    if ".." in pure.parts or "~" in text:
        raise DomainSpecError(f"{field_name} must not traverse outside its domain root")
    return text


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DomainSpecError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise DomainSpecError(f"non-finite JSON number: {value}")


def _strict_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except DomainSpecError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainSpecError(f"cannot read strict domain spec {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DomainSpecError("domain spec root must be an object")
    return data


@dataclass(frozen=True)
class OntologyAxis:
    key: str
    title: str
    topics: tuple[str, ...]


@dataclass(frozen=True)
class SourceFamily:
    family: str
    acquisition_mode: str
    rights_state: str
    shared_corpus_eligible: bool


@dataclass(frozen=True)
class DomainSpec:
    schema_version: int
    domain: str
    title: str
    objective: str
    epistemic_policy: str
    seed_ref: str
    roots: dict[str, str]
    ontology_axes: tuple[OntologyAxis, ...]
    evidence_lanes: dict[str, float]
    source_families: tuple[SourceFamily, ...]
    feedback_profile: dict[str, Any]
    acquisition_policy: dict[str, Any]
    bootstrap: dict[str, Any]
    promotion: dict[str, Any]
    eval_requirements: dict[str, Any]

    @property
    def axis_topics(self) -> tuple[str, ...]:
        seen: list[str] = []
        for axis in self.ontology_axes:
            for topic in axis.topics:
                if topic not in seen:
                    seen.append(topic)
        return tuple(seen)


def _parse_ontology(raw: Any) -> tuple[OntologyAxis, ...]:
    ontology = _exact_fields(raw, _ONTOLOGY_FIELDS, "ontology")
    raw_axes = ontology["axes"]
    if not isinstance(raw_axes, list) or not raw_axes:
        raise DomainSpecError("ontology.axes must be a non-empty list")
    axes: list[OntologyAxis] = []
    for entry in raw_axes:
        fields = _exact_fields(entry, _AXIS_FIELDS, "ontology axis")
        topics = fields["topics"]
        if not isinstance(topics, list) or not topics:
            raise DomainSpecError("axis topics must be a non-empty list")
        normalized = tuple(_text(topic, "axis topic") for topic in topics)
        if len(normalized) != len(set(normalized)):
            raise DomainSpecError("axis topics must be unique within an axis")
        axes.append(
            OntologyAxis(
                key=_kebab(fields["key"], "axis key"),
                title=_text(fields["title"], "axis title"),
                topics=normalized,
            )
        )
    keys = [axis.key for axis in axes]
    if len(keys) != len(set(keys)):
        raise DomainSpecError("ontology axis keys must be unique")
    return tuple(axes)


def _parse_evidence_lanes(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise DomainSpecError("evidence_lanes must be a non-empty object")
    if "default" not in raw:
        raise DomainSpecError("evidence_lanes must include a 'default' weight")
    lanes: dict[str, float] = {}
    for lane, weight in raw.items():
        lanes[_text(lane, "evidence lane")] = _number(weight, f"evidence_lanes[{lane}]", minimum=0.0)
    return lanes


def _parse_source_families(raw: Any) -> tuple[SourceFamily, ...]:
    if not isinstance(raw, list) or not raw:
        raise DomainSpecError("source_families must be a non-empty list")
    families: list[SourceFamily] = []
    for entry in raw:
        fields = _exact_fields(entry, _FAMILY_FIELDS, "source family")
        rights_state = _text(fields["rights_state"], "rights_state")
        if rights_state not in RIGHTS_STATES:
            raise DomainSpecError(f"unknown rights_state: {rights_state!r}")
        shared = _bool(fields["shared_corpus_eligible"], "shared_corpus_eligible")
        if shared and rights_state not in _SHARED_ELIGIBLE_RIGHTS:
            raise DomainSpecError(
                "private or rights-unclear source families must not export into the shared corpus"
            )
        families.append(
            SourceFamily(
                family=_kebab(fields["family"], "source family name"),
                acquisition_mode=_text(fields["acquisition_mode"], "acquisition_mode"),
                rights_state=rights_state,
                shared_corpus_eligible=shared,
            )
        )
    names = [family.family for family in families]
    if len(names) != len(set(names)):
        raise DomainSpecError("source family names must be unique")
    return tuple(families)


def _parse_feedback_profile(raw: Any) -> dict[str, Any]:
    fields = _exact_fields(raw, _FEEDBACK_FIELDS, "feedback_profile")
    return {
        "corroboration_increment": _number(fields["corroboration_increment"], "corroboration_increment", maximum=1.0),
        "starvation_age_boost_per_day": _number(fields["starvation_age_boost_per_day"], "starvation_age_boost_per_day", maximum=1.0),
        "starvation_max_boost": _number(fields["starvation_max_boost"], "starvation_max_boost", maximum=1.0),
        "yield_target_per_cycle": int(_number(fields["yield_target_per_cycle"], "yield_target_per_cycle")),
    }


def _parse_acquisition_policy(raw: Any, family_names: set[str]) -> dict[str, Any]:
    fields = _exact_fields(raw, _ACQUISITION_FIELDS, "acquisition_policy")
    priority = fields["family_priority"]
    if not isinstance(priority, list) or not priority:
        raise DomainSpecError("acquisition_policy.family_priority must be a non-empty list")
    normalized = [_kebab(name, "family_priority entry") for name in priority]
    if len(normalized) != len(set(normalized)):
        raise DomainSpecError("acquisition_policy.family_priority entries must be unique")
    unknown = set(normalized) - family_names
    if unknown:
        raise DomainSpecError(f"acquisition_policy.family_priority references unknown families: {sorted(unknown)}")
    return {
        "max_suggestions": int(_number(fields["max_suggestions"], "max_suggestions", minimum=1.0)),
        "family_priority": tuple(normalized),
    }


def _parse_bootstrap(raw: Any) -> dict[str, Any]:
    fields = _exact_fields(raw, _BOOTSTRAP_FIELDS, "bootstrap")
    return {
        "max_items_per_cycle": int(_number(fields["max_items_per_cycle"], "max_items_per_cycle", minimum=1.0)),
        "max_deep_acquisitions_per_utc_day": int(_number(fields["max_deep_acquisitions_per_utc_day"], "max_deep_acquisitions_per_utc_day", minimum=0.0)),
        "max_llm_tasks_per_cycle": int(_number(fields["max_llm_tasks_per_cycle"], "max_llm_tasks_per_cycle", minimum=0.0)),
        "max_cost_usd_per_utc_day": _number(fields["max_cost_usd_per_utc_day"], "max_cost_usd_per_utc_day", minimum=0.0),
    }


def _parse_promotion(raw: Any) -> dict[str, Any]:
    fields = _exact_fields(raw, _PROMOTION_FIELDS, "promotion")
    if _bool(fields["automatic_promotion_enabled"], "automatic_promotion_enabled"):
        raise DomainSpecError("automatic_promotion_enabled must be false: the generalized engine never auto-promotes")
    rejection = _number(fields["rejection_threshold"], "rejection_threshold", maximum=1.0)
    probationary = _number(fields["probationary_threshold"], "probationary_threshold", maximum=1.0)
    promotion = _number(fields["promotion_threshold"], "promotion_threshold", maximum=1.0)
    if not rejection <= probationary <= promotion:
        raise DomainSpecError("promotion thresholds must satisfy rejection <= probationary <= promotion")
    return {
        "automatic_promotion_enabled": False,
        "rejection_threshold": rejection,
        "probationary_threshold": probationary,
        "promotion_threshold": promotion,
    }


def _parse_eval_requirements(raw: Any) -> dict[str, Any]:
    fields = _exact_fields(raw, _EVAL_FIELDS, "eval_requirements")
    return {
        "min_corpus_pages_scanned": int(_number(fields["min_corpus_pages_scanned"], "min_corpus_pages_scanned", minimum=0.0)),
        "require_axes_in_field_map": _bool(fields["require_axes_in_field_map"], "require_axes_in_field_map"),
        "forbid_private_export_to_shared": _bool(fields["forbid_private_export_to_shared"], "forbid_private_export_to_shared"),
        "require_next_gap": _bool(fields["require_next_gap"], "require_next_gap"),
    }


def _parse_roots(raw: Any) -> dict[str, str]:
    fields = _exact_fields(raw, _ROOT_FIELDS, "roots")
    return {name: _relative_path(fields[name], f"roots.{name}") for name in sorted(_ROOT_FIELDS)}


def load_domain_spec(path: Path | str) -> DomainSpec:
    spec_path = Path(path)
    data = _exact_fields(_strict_json(spec_path), _TOP_FIELDS, "domain spec")
    if data["schema_version"] != SPEC_SCHEMA_VERSION:
        raise DomainSpecError(f"unsupported schema_version: {data['schema_version']!r}")
    families = _parse_source_families(data["source_families"])
    return DomainSpec(
        schema_version=SPEC_SCHEMA_VERSION,
        domain=_kebab(data["domain"], "domain"),
        title=_text(data["title"], "title"),
        objective=_text(data["objective"], "objective"),
        epistemic_policy=_text(data["epistemic_policy"], "epistemic_policy"),
        seed_ref=_relative_path(data["seed_ref"], "seed_ref"),
        roots=_parse_roots(data["roots"]),
        ontology_axes=_parse_ontology(data["ontology"]),
        evidence_lanes=_parse_evidence_lanes(data["evidence_lanes"]),
        source_families=families,
        feedback_profile=_parse_feedback_profile(data["feedback_profile"]),
        acquisition_policy=_parse_acquisition_policy(data["acquisition_policy"], {f.family for f in families}),
        bootstrap=_parse_bootstrap(data["bootstrap"]),
        promotion=_parse_promotion(data["promotion"]),
        eval_requirements=_parse_eval_requirements(data["eval_requirements"]),
    )


def _spec_identity(spec: DomainSpec) -> dict[str, Any]:
    """Deterministic identity of a spec's full content (for conflict detection)."""
    return {
        "schema_version": spec.schema_version,
        "domain": spec.domain,
        "title": spec.title,
        "objective": spec.objective,
        "epistemic_policy": spec.epistemic_policy,
        "seed_ref": spec.seed_ref,
        "roots": spec.roots,
        "ontology_axes": [asdict(axis) for axis in spec.ontology_axes],
        "evidence_lanes": spec.evidence_lanes,
        "source_families": [asdict(family) for family in spec.source_families],
        "feedback_profile": spec.feedback_profile,
        "acquisition_policy": {
            "max_suggestions": spec.acquisition_policy["max_suggestions"],
            "family_priority": list(spec.acquisition_policy["family_priority"]),
        },
        "bootstrap": spec.bootstrap,
        "promotion": spec.promotion,
        "eval_requirements": spec.eval_requirements,
    }


def spec_digest(spec: DomainSpec) -> str:
    """SHA-256 over a spec's canonicalized full content."""
    blob = json.dumps(
        _spec_identity(spec), sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def bootstrap_domain(
    spec: DomainSpec,
    base_dir: Path | str,
    *,
    now: str,
    source_id: str = CANONICAL_SOURCE_ID,
) -> dict[str, Any]:
    """Physically create a domain's directories and state marker only.

    Idempotent: a repeat call with the same spec is a byte-level no-op. Every
    unsafe condition — overriding the canonical source, a root that escapes the
    base (traversal or symlink), or a conflicting spec/version already present —
    fails closed *before* any directory or marker is created. Bootstrap never
    creates a GBrain source, scheduler, or cron.
    """
    if source_id != CANONICAL_SOURCE_ID:
        raise DomainBootstrapError(
            f"domain bootstrap may not override the canonical source id {CANONICAL_SOURCE_ID!r}"
        )
    base = Path(base_dir)
    if base.is_symlink():
        raise DomainBootstrapError(f"base_dir must not be a symlink: {base}")
    resolved_base = base.resolve()

    # Resolve and guard every root before any mutation.
    planned: dict[str, Path] = {}
    for name in sorted(spec.roots):
        target = base / spec.roots[name]
        resolved = target.resolve()
        if resolved != resolved_base and resolved_base not in resolved.parents:
            raise DomainBootstrapError(f"root {name!r} escapes base_dir: {resolved}")
        planned[name] = target

    digest = spec_digest(spec)
    marker = planned["state_root"] / BOOTSTRAP_MARKER
    if marker.exists():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainBootstrapError(f"unreadable domain-state marker: {exc}") from exc
        if existing.get("domain") != spec.domain or existing.get("spec_digest") != digest:
            raise DomainBootstrapError(
                "conflicting domain spec/version already bootstrapped at this location"
            )
        return existing  # physical no-op

    for target in planned.values():
        target.mkdir(parents=True, exist_ok=True, mode=0o700)

    receipt = {
        "schema_version": SPEC_SCHEMA_VERSION,
        "domain": spec.domain,
        "spec_digest": digest,
        "source_id": CANONICAL_SOURCE_ID,
        "roots": {name: str(planned[name]) for name in sorted(planned)},
        "created_at": now,
        "gbrain_source_created": False,
        "scheduler_created": False,
    }
    encoded = json.dumps(receipt, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temp = marker.with_suffix(marker.suffix + ".tmp")
    temp.write_text(encoded, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, marker)
    return receipt
