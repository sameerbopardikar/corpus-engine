#!/usr/bin/env python3
"""Generic, fail-closed rights resolver for the generalized Corpus Engine.

The resolver turns inspectable evidence about a source — its license, access
class, hosting repository, and any explicit site-license grant — into a single
:class:`RightsResolution` that answers three questions truthfully:

* what canonical :class:`RightsState` applies (shared with the rest of the
  engine via :mod:`corpus_rights`);
* may the engine automatically acquire full source *content*;
* may the engine preserve public *metadata* only.

Every rule fails closed. Public visibility is never treated as a redistribution
basis: an accessible-but-unlicensed page resolves to ``rights_unclear`` and is
neither content- nor metadata-acquirable. Access gates (prohibited, private,
paid, login) are evaluated *before* any license, so a rights-clear license
behind a paywall still fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass

from corpus_adapters.types import RightsState

# Access classes the resolver understands. Anything else is a programming error
# (fail closed at the boundary rather than silently defaulting to permissive).
ACCESS_CLASSES = frozenset(
    {"open_content", "metadata", "paid", "login", "private", "prohibited", "unknown"}
)

# Conservative allowlist for AUTOMATIC normalized-content ingestion. Only
# licenses that permit creating and redistributing a normalized derivative text
# qualify: public-domain dedications, CC-BY, and CC-BY-SA. NonCommercial (NC)
# and NoDerivatives (ND) variants are deliberately excluded — a normalized
# corpus page is a derivative, and NC redistribution is not unambiguously clear,
# so those route to an explicit human gate rather than auto-ingesting.
_AUTO_CONTENT_LICENSES = frozenset(
    {
        "cc0",
        "cc-by",
        "cc-by-sa",
        "public-domain",
        "pd",
    }
)
# Recognized-but-restricted Creative Commons licenses. Metadata may be
# preserved, but full-text normalization needs an explicit, human-reviewed
# policy decision (attribution scope, commercial posture, derivative rights).
_RESTRICTED_CONTENT_LICENSES = frozenset(
    {
        "cc-by-nc",
        "cc-by-nd",
        "cc-by-nc-sa",
        "cc-by-nc-nd",
    }
)
# Repositories that publish redistributable bibliographic metadata only.
_METADATA_REPOSITORIES = frozenset({"openalex", "crossref", "pubmed"})

# Dispositions surfaced truthfully in receipts and the UI projection.
DISPOSITIONS = frozenset(
    {"auto_acquire_content", "metadata_only", "human_gate", "blocked", "rights_unclear"}
)


class RightsResolverError(ValueError):
    """Raised when rights evidence is malformed or cannot be classified."""


@dataclass(frozen=True)
class RightsEvidence:
    """Inspectable evidence about a candidate source's rights posture."""

    license: str | None = None
    access_class: str = "unknown"
    repository: str | None = None
    is_publicly_visible: bool = False
    site_license_grant: bool = False
    source_note: str | None = None

    def __post_init__(self) -> None:
        if self.license is not None and (not isinstance(self.license, str) or not self.license.strip()):
            raise RightsResolverError("license must be a non-blank string or None")
        if self.access_class not in ACCESS_CLASSES:
            raise RightsResolverError(f"unknown access_class: {self.access_class!r}")
        if self.repository is not None and (not isinstance(self.repository, str) or not self.repository.strip()):
            raise RightsResolverError("repository must be a non-blank string or None")
        for flag in ("is_publicly_visible", "site_license_grant"):
            if not isinstance(getattr(self, flag), bool):
                raise RightsResolverError(f"{flag} must be a boolean")
        object.__setattr__(self, "license", self.license.strip().lower() if self.license else None)
        object.__setattr__(self, "repository", self.repository.strip().lower() if self.repository else None)


@dataclass(frozen=True)
class RightsResolution:
    """The truthful outcome of resolving one source's rights."""

    basis: str
    rights_state: RightsState
    disposition: str
    may_acquire_content: bool
    may_preserve_metadata: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "basis": self.basis,
            "rights_state": self.rights_state.value,
            "disposition": self.disposition,
            "may_acquire_content": self.may_acquire_content,
            "may_preserve_metadata": self.may_preserve_metadata,
            "reason": self.reason,
        }


def _content_clear(basis: str, reason: str) -> RightsResolution:
    return RightsResolution(
        basis=basis,
        rights_state=RightsState.PUBLIC_RIGHTS_CLEAR,
        disposition="auto_acquire_content",
        may_acquire_content=True,
        may_preserve_metadata=True,
        reason=reason,
    )


def resolve_rights(evidence: RightsEvidence) -> RightsResolution:
    """Classify a source's rights, fail-closed, from inspectable evidence."""
    if not isinstance(evidence, RightsEvidence):
        raise RightsResolverError("evidence must be a RightsEvidence")

    # 1. Access gates are evaluated first: a permissive license behind a gate
    #    is still not acquirable.
    if evidence.access_class == "prohibited":
        return RightsResolution(
            basis="prohibited",
            rights_state=RightsState.RIGHTS_UNCLEAR,
            disposition="blocked",
            may_acquire_content=False,
            may_preserve_metadata=False,
            reason="source explicitly prohibits acquisition",
        )
    if evidence.access_class == "private":
        return RightsResolution(
            basis="private_source",
            rights_state=RightsState.PRIVATE_AUTHORIZED,
            disposition="human_gate",
            may_acquire_content=False,
            may_preserve_metadata=False,
            reason="private source: owner may use privately; not shared-corpus eligible",
        )
    if evidence.access_class in {"paid", "login"}:
        gate = "paid_gate" if evidence.access_class == "paid" else "login_gate"
        return RightsResolution(
            basis=gate,
            rights_state=RightsState.RIGHTS_UNCLEAR,
            disposition="human_gate",
            may_acquire_content=False,
            may_preserve_metadata=False,
            reason=f"{evidence.access_class} access requires a human-provided authorization",
        )

    # 2. Explicit content-redistribution bases.
    if evidence.license in _AUTO_CONTENT_LICENSES:
        return _content_clear(
            f"open_access_license:{evidence.license}",
            f"recognized open license {evidence.license!r} permits redistribution",
        )
    # A recognized-but-restricted CC license (NC/ND): never auto-normalized.
    if evidence.license in _RESTRICTED_CONTENT_LICENSES:
        return RightsResolution(
            basis=f"restricted_license:{evidence.license}",
            rights_state=RightsState.RIGHTS_UNCLEAR,
            disposition="human_gate",
            may_acquire_content=False,
            may_preserve_metadata=True,
            reason=(
                f"restricted license {evidence.license!r} (NonCommercial/NoDerivatives) "
                "requires a human policy decision before normalized content ingestion"
            ),
        )
    if evidence.site_license_grant:
        return _content_clear("site_license", "explicit site license permits redistribution")

    # NOTE: repository identity alone (PMC, government host, etc.) is NOT a
    # redistribution basis. A PMC or .gov page is admitted as normalized content
    # only when it also carries an explicit compatible license / public-domain
    # assertion (handled above) or an explicit site redistribution grant. Absent
    # that, it falls through to metadata-only (if a metadata repository) or fails
    # closed below — visibility is never mistaken for a redistribution grant.

    # 3. Public bibliographic metadata (never full text).
    if evidence.access_class == "metadata" or evidence.repository in _METADATA_REPOSITORIES:
        return RightsResolution(
            basis="public_metadata_source",
            rights_state=RightsState.PUBLIC_METADATA_ONLY,
            disposition="metadata_only",
            may_acquire_content=False,
            may_preserve_metadata=True,
            reason="public metadata source: metadata may be preserved, full text may not",
        )

    # 4. Everything else fails closed. Public visibility alone is not a basis.
    return RightsResolution(
        basis="rights_unclear",
        rights_state=RightsState.RIGHTS_UNCLEAR,
        disposition="rights_unclear",
        may_acquire_content=False,
        may_preserve_metadata=False,
        reason="no recognized redistribution or metadata basis; failing closed",
    )
