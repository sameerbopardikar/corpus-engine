#!/usr/bin/env python3
"""Strict topic-only bootstrap packet for the ``corpus-start`` orchestrator.

Hermes may reason about an arbitrary topic and produce ONE packet describing the
domain's identity, a provisional ontology, evidence-lane weights, source
families, and scouted public candidate locators. The packet is deliberately
*thin*: it carries domain content only. All engine defaults (feedback profile,
bootstrap budgets, promotion thresholds, corpus roots, eval requirements) are
generated here in code so no domain-specific values leak in — and so the packet
can never hand-author canonical corpus pages.

:func:`validate_bootstrap_packet` fails closed on any structural or safety
violation. :func:`compile_domain_inputs` turns a validated packet into a loadable
:class:`~corpus_domain_spec.DomainSpec` JSON document and a candidate seed bundle
(both consumed unchanged by the existing engine).
"""
from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit

PACKET_SCHEMA_VERSION = 1

# Diversity floors — a topic corpus that cannot describe itself across several
# axes / families is not worth bootstrapping. The clean Nutrition proof exceeds
# every floor; these are the minimum, not the target.
MIN_AXES = 2
MIN_TOPICS_TOTAL = 4
MIN_SOURCE_FAMILIES = 2
MIN_CANDIDATE_LOCATORS = 3

_PACKET_FIELDS = {
    "schema_version", "topic_input", "domain", "title", "objective", "axes",
    "evidence_lanes", "source_families", "candidate_locators",
    "scout_provenance", "existing_context_refs",
}
_AXIS_FIELDS = {"key", "title", "topics"}
_FAMILY_FIELDS = {"family", "acquisition_mode"}
_LOCATOR_FIELDS = {"url", "title", "topics"}
_PROVENANCE_FIELDS = {"query", "result_url"}

# Generic mapping from a declared acquisition mode to the rights posture the
# engine will enforce. Only fully rights-clear open-access fetches may feed the
# shared corpus; everything else stays metadata-only. Unknown modes fail closed.
_ACQUISITION_MODE_RIGHTS = {
    "public_open_access_fetch": ("public_rights_clear", True),
    "public_metadata_capture": ("public_metadata_only", False),
}

_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BootstrapPacketError(ValueError):
    """Raised when a bootstrap packet is malformed or unsafe."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BootstrapPacketError(f"{field} must be a non-empty string")
    return value.strip()


def _kebab(value: Any, field: str) -> str:
    text = _text(value, field)
    if not _KEBAB_RE.match(text):
        raise BootstrapPacketError(f"{field} must be lowercase kebab-case")
    return text


def _exact(data: Any, allowed: set[str], name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise BootstrapPacketError(f"{name} must be an object")
    unknown = set(data) - allowed
    missing = allowed - set(data)
    if unknown or missing:
        raise BootstrapPacketError(
            f"{name} fields mismatch: missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    return data


def _finite(value: Any, field: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BootstrapPacketError(f"{field} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise BootstrapPacketError(f"{field} must be a finite number")
    if numeric < minimum:
        raise BootstrapPacketError(f"{field} must be >= {minimum}")
    return numeric


def _https(value: Any, field: str) -> str:
    url = _text(value, field)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise BootstrapPacketError(f"{field} must be an https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise BootstrapPacketError(f"{field} must not embed credentials")
    return url


def _topics(raw: Any, field: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise BootstrapPacketError(f"{field} must be a non-empty list")
    topics = [_kebab(topic, f"{field} entry") for topic in raw]
    if len(topics) != len(set(topics)):
        raise BootstrapPacketError(f"{field} must be unique")
    return topics


def validate_bootstrap_packet(data: Any, *, clean: bool = True) -> dict[str, Any]:
    """Validate a bootstrap packet and return a normalized copy.

    ``clean=True`` (the empty-root proof) forbids any ``existing_context_refs``
    so the run cannot smuggle in prior corpus/brain material.
    """
    packet = _exact(data, _PACKET_FIELDS, "bootstrap packet")
    if packet["schema_version"] != PACKET_SCHEMA_VERSION:
        raise BootstrapPacketError(f"unsupported schema_version: {packet['schema_version']!r}")

    topic_input = _text(packet["topic_input"], "topic_input")
    domain = _kebab(packet["domain"], "domain")
    title = _text(packet["title"], "title")
    objective = _text(packet["objective"], "objective")

    axes = _validate_axes(packet["axes"])
    evidence_lanes = _validate_evidence_lanes(packet["evidence_lanes"])
    families = _validate_source_families(packet["source_families"])
    locators = _validate_locators(packet["candidate_locators"])
    provenance = _validate_provenance(packet["scout_provenance"])
    refs = _validate_existing_refs(packet["existing_context_refs"], clean=clean)

    return {
        "schema_version": PACKET_SCHEMA_VERSION,
        "topic_input": topic_input,
        "domain": domain,
        "title": title,
        "objective": objective,
        "axes": axes,
        "evidence_lanes": evidence_lanes,
        "source_families": families,
        "candidate_locators": locators,
        "scout_provenance": provenance,
        "existing_context_refs": refs,
    }


def _validate_axes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) < MIN_AXES:
        raise BootstrapPacketError(f"at least {MIN_AXES} axes are required")
    axes: list[dict[str, Any]] = []
    keys: list[str] = []
    total_topics: set[str] = set()
    for entry in raw:
        fields = _exact(entry, _AXIS_FIELDS, "axis")
        key = _kebab(fields["key"], "axis key")
        title = _text(fields["title"], "axis title")
        topics = _topics(fields["topics"], "axis topics")
        keys.append(key)
        total_topics.update(topics)
        axes.append({"key": key, "title": title, "topics": topics})
    if len(keys) != len(set(keys)):
        raise BootstrapPacketError("axis keys must be unique")
    if len(total_topics) < MIN_TOPICS_TOTAL:
        raise BootstrapPacketError(f"at least {MIN_TOPICS_TOTAL} distinct topics are required")
    return axes


def _validate_evidence_lanes(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise BootstrapPacketError("evidence_lanes must be a non-empty object")
    if "default" not in raw:
        raise BootstrapPacketError("evidence_lanes must include a 'default' weight")
    lanes: dict[str, float] = {}
    for lane, weight in raw.items():
        lanes[_text(lane, "evidence lane")] = _finite(weight, f"evidence_lanes[{lane}]")
    return lanes


def _validate_source_families(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) < MIN_SOURCE_FAMILIES:
        raise BootstrapPacketError(f"at least {MIN_SOURCE_FAMILIES} source families are required")
    families: list[dict[str, str]] = []
    names: list[str] = []
    for entry in raw:
        fields = _exact(entry, _FAMILY_FIELDS, "source family")
        family = _kebab(fields["family"], "source family name")
        mode = _text(fields["acquisition_mode"], "acquisition_mode")
        if mode not in _ACQUISITION_MODE_RIGHTS:
            raise BootstrapPacketError(f"unknown acquisition_mode: {mode!r}")
        names.append(family)
        families.append({"family": family, "acquisition_mode": mode})
    if len(names) != len(set(names)):
        raise BootstrapPacketError("source family names must be unique")
    return families


def _validate_locators(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) < MIN_CANDIDATE_LOCATORS:
        raise BootstrapPacketError(f"at least {MIN_CANDIDATE_LOCATORS} candidate locators are required")
    locators: list[dict[str, Any]] = []
    urls: list[str] = []
    for entry in raw:
        fields = _exact(entry, _LOCATOR_FIELDS, "candidate locator")
        url = _https(fields["url"], "candidate locator url")
        title = _text(fields["title"], "candidate locator title")
        topics = _topics(fields["topics"], "candidate locator topics")
        urls.append(url)
        locators.append({"url": url, "title": title, "topics": topics})
    if len(urls) != len(set(urls)):
        raise BootstrapPacketError("candidate locator urls must be unique")
    return locators


def _validate_provenance(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise BootstrapPacketError("scout_provenance must be a non-empty list")
    provenance: list[dict[str, str]] = []
    for entry in raw:
        fields = _exact(entry, _PROVENANCE_FIELDS, "scout provenance")
        provenance.append(
            {
                "query": _text(fields["query"], "scout query"),
                "result_url": _https(fields["result_url"], "scout result_url"),
            }
        )
    return provenance


def _validate_existing_refs(raw: Any, *, clean: bool) -> list[str]:
    if not isinstance(raw, list):
        raise BootstrapPacketError("existing_context_refs must be a list")
    if clean and raw:
        raise BootstrapPacketError(
            "clean mode forbids existing_context_refs: the proof root must be empty"
        )
    return [_text(ref, "existing_context_ref") for ref in raw]


def build_topic_bootstrap_packet(topic: str) -> dict[str, Any]:
    """Build the domain-agnostic packet used by the one-command start path.

    Candidate locators are public discovery systems, not authored evidence.
    The acquisition phase still performs live topic-specific discovery and
    admits only rights-clear retrieved sources.
    """
    topic = _text(topic, "topic")
    domain = _slugify(topic, "corpus")
    display = " ".join(word.capitalize() for word in re.split(r"[-_\s]+", topic) if word)
    packet = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "topic_input": topic,
        "domain": domain,
        "title": f"{display} Research Corpus",
        "objective": f"Build an evidence-ranked, continuously improving private corpus on {topic}.",
        "axes": [
            {"key": "foundations", "title": "Foundations and mechanisms", "topics": [f"{domain}-foundations", f"{domain}-mechanisms"]},
            {"key": "evidence", "title": "Evidence and outcomes", "topics": [f"{domain}-interventions", f"{domain}-outcomes"]},
            {"key": "practice", "title": "Practice, risks, and implementation", "topics": [f"{domain}-practice", f"{domain}-risks"]},
        ],
        "evidence_lanes": {"default": 1.0, "scholarly": 1.2, "institutional": 1.1},
        "source_families": [
            {"family": "scholarly-open-access", "acquisition_mode": "public_open_access_fetch"},
            {"family": "public-institutions", "acquisition_mode": "public_metadata_capture"},
        ],
        "candidate_locators": [
            {"url": "https://openalex.org/", "title": "OpenAlex discovery index", "topics": [f"{domain}-foundations"]},
            {"url": "https://europepmc.org/", "title": "Europe PMC open literature", "topics": [f"{domain}-evidence"]},
            {"url": "https://pubmed.ncbi.nlm.nih.gov/", "title": "PubMed research index", "topics": [f"{domain}-outcomes"]},
        ],
        "scout_provenance": [
            {"query": f"{topic} open access research evidence", "result_url": "https://openalex.org/"},
            {"query": f"{topic} systematic review public full text", "result_url": "https://europepmc.org/"},
        ],
        "existing_context_refs": [],
    }
    return validate_bootstrap_packet(packet, clean=True)


# --------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------

# Generic engine defaults. These are domain-agnostic and must never carry
# domain-specific values.
_DEFAULT_EPISTEMIC_POLICY = "capture-all-promote-by-evidence-v1"
_DEFAULT_SEED_POLICY = "topic-bootstrap-packet-v1"
_SEED_STATUS = "candidate-seed-not-promoted"
_DEFAULT_ENTITY_TYPE = "document"
_DEFAULT_SEED_EVIDENCE_LANE = "default"
_DEFAULT_SEED_AUTHORITY_TIER = "unranked"
_DEFAULT_SEED_REFRESH_CLASS = "monthly"
_DEFAULT_SEED_SOURCE_TYPE = "topic-bootstrap-candidate"


def _default_roots(domain: str) -> dict[str, str]:
    return {
        "corpus_root": domain,
        "state_root": f"{domain}/self-expansion-v1",
        "archive_root": f"{domain}/archive",
        "output_root": f"{domain}/discovery/domain-v1",
    }


def _slugify(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or fallback


def compile_domain_inputs(data: Any, *, now: str, clean: bool = True) -> dict[str, Any]:
    """Compile a validated packet into a DomainSpec document and seed bundle.

    The returned mapping has exactly two keys: ``domain_spec`` and
    ``seed_bundle``. It never emits authored corpus pages.
    """
    packet = validate_bootstrap_packet(data, clean=clean)
    domain = packet["domain"]

    families = [
        {
            "family": fam["family"],
            "acquisition_mode": fam["acquisition_mode"],
            "rights_state": _ACQUISITION_MODE_RIGHTS[fam["acquisition_mode"]][0],
            "shared_corpus_eligible": _ACQUISITION_MODE_RIGHTS[fam["acquisition_mode"]][1],
        }
        for fam in packet["source_families"]
    ]

    domain_spec = {
        "schema_version": 1,
        "domain": domain,
        "title": packet["title"],
        "objective": packet["objective"],
        "epistemic_policy": _DEFAULT_EPISTEMIC_POLICY,
        "seed_ref": f"seeds/{domain}-seed-candidates.json",
        "roots": _default_roots(domain),
        "ontology": {"axes": packet["axes"]},
        "evidence_lanes": packet["evidence_lanes"],
        "source_families": families,
        "feedback_profile": {
            "corroboration_increment": 0.1,
            "starvation_age_boost_per_day": 0.03,
            "starvation_max_boost": 0.3,
            "yield_target_per_cycle": 1,
        },
        "acquisition_policy": {
            "max_suggestions": 3,
            "family_priority": [fam["family"] for fam in families],
        },
        "bootstrap": {
            "max_items_per_cycle": 2,
            "max_deep_acquisitions_per_utc_day": 1,
            "max_llm_tasks_per_cycle": 0,
            "max_cost_usd_per_utc_day": 0.0,
        },
        "promotion": {
            "automatic_promotion_enabled": False,
            "probationary_threshold": 0.35,
            "promotion_threshold": 0.65,
            "rejection_threshold": 0.1,
        },
        "eval_requirements": {
            "min_corpus_pages_scanned": 0,
            "require_axes_in_field_map": True,
            "forbid_private_export_to_shared": True,
            "require_next_gap": True,
        },
    }

    candidates = []
    for index, locator in enumerate(packet["candidate_locators"], start=1):
        candidates.append(
            {
                "id": f"cand-{index:02d}-{_slugify(locator['title'], f'candidate-{index}')}",
                "entity_type": _DEFAULT_ENTITY_TYPE,
                "canonical_url": locator["url"],
                "source_type": _DEFAULT_SEED_SOURCE_TYPE,
                "evidence_lane": _DEFAULT_SEED_EVIDENCE_LANE,
                "authority_tier": _DEFAULT_SEED_AUTHORITY_TIER,
                "refresh_class": _DEFAULT_SEED_REFRESH_CLASS,
                "topics": locator["topics"],
            }
        )

    seed_bundle = {
        "schema_version": 1,
        "domain": domain,
        "status": _SEED_STATUS,
        "generated_at": now,
        "policy": _DEFAULT_SEED_POLICY,
        "candidates": candidates,
    }

    return {"domain_spec": domain_spec, "seed_bundle": seed_bundle}
