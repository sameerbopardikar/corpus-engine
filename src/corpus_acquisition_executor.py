#!/usr/bin/env python3
"""Generic, bounded, idempotent acquisition executor.

Given one selected candidate the executor drives the canonical acquisition
lifecycle end to end and emits exactly one durable receipt:

    verify locator → resolve rights → (auto-content only) fetch under limits →
    preserve raw bytes → normalize → stage → evaluate → probationary

Safety invariants enforced here:

* **Rights fail-closed.** Only ``public_rights_clear`` content is fetched.
  Metadata-only preserves metadata and never full text; private/paid/login is
  human-gated; unclear/prohibited fails closed. None of these ever fetch content.
* **Bounded transport.** Timeout, byte-size cap, MIME allowlist, and redirect
  validation (http(s) only, no credentials on any hop). Any breach is an
  explicit ``failed`` receipt and stages nothing.
* **Evidence admission, never doctrine promotion.** Content is staged first; a
  post-staging evaluation failure quarantines the staged bytes, leaving canonical
  clean. Only after every integrity/provenance/retrieval check passes — and only
  when a ``canonical_root`` is configured — is the eval-passed page atomically
  installed into ``<canonical_root>/<domain>/sources/acquired/<slug>.md`` with an
  exact SHA and a rollback receipt. This is evidence admission: ``promotion`` and
  ``production_mutation`` stay disabled; the engine never auto-promotes doctrine.
* **Idempotent.** A stable idempotency key maps to one content-addressed raw
  artifact, one staged page, and one receipt. An exact retry short-circuits on
  the stored receipt and does no further I/O.

All network I/O is injected via ``http_fetch`` so tests are deterministic.
"""
from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from corpus_acquisition_lifecycle import AcquisitionLifecycle, AcquisitionState
from corpus_adapters.common import preserve_bytes, sha256_bytes
from corpus_rights_resolver import RightsEvidence, RightsResolution, resolve_rights
from corpus_ssrf import is_safe_locator

RECEIPT_SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MIN_TEXT_CHARS = 32
DEFAULT_ALLOWED_MIME = frozenset(
    {"text/html", "text/plain", "text/markdown", "application/xhtml+xml"}
)
# Recognized document types that carry real full text but need an extractor the
# engine does not package. These are routed to a human/extractor gate — never
# reported as acquired and never fabricated from a landing shell.
GATED_DOCUMENT_MIME = frozenset({"application/pdf"})
_SAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
_SEMANTIC_TOKEN = re.compile(r"[a-z0-9]+")
# Topic packets use structural suffixes to organize a field map. Those words
# are not, by themselves, evidence that a retrieved document is about the
# requested domain. Keep the domain/topic nouns and discard only this small,
# generic orchestration vocabulary.
_TOPIC_SCAFFOLD_TOKENS = frozenset(
    {
        "foundation", "foundations", "mechanism", "mechanisms",
        "evidence", "outcome", "outcomes", "intervention", "interventions",
        "practice", "practices", "risk", "risks", "implementation",
    }
)


class AcquisitionExecutorError(RuntimeError):
    """Raised only for un-recoverable executor misuse (e.g. receipt conflict)."""


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    final_url: str
    content_type: str
    redirect_chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcquisitionCandidate:
    candidate_id: str
    domain: str
    canonical_locator: str
    content_locator: str | None
    evidence_lane: str
    source_family: str
    title: str
    topics: tuple[str, ...]
    rights_evidence: RightsEvidence
    metadata: dict = field(default_factory=dict)
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        for name in ("candidate_id", "domain", "canonical_locator", "evidence_lane", "source_family", "title", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AcquisitionExecutorError(f"{name} must be a non-blank string")
        if not isinstance(self.rights_evidence, RightsEvidence):
            raise AcquisitionExecutorError("rights_evidence must be a RightsEvidence")


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "svg", "noscript", "template"}:
            self.skip += 1
        if tag in {"p", "div", "section", "article", "li", "h1", "h2", "h3", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "svg", "noscript", "template"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip:
            return
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        value = html.unescape(" ".join(self.parts))
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip() + "\n"


def _normalize_text(body: bytes, content_type: str) -> str:
    base = content_type.split(";", 1)[0].strip().lower()
    decoded = body.decode("utf-8", errors="replace")
    if base in {"text/html", "application/xhtml+xml"}:
        parser = _VisibleText()
        parser.feed(decoded)
        return parser.text()
    return decoded.strip() + "\n"


def retrieval_topic_matches(candidate: AcquisitionCandidate, text: str) -> bool:
    """Fail-closed topical retrieval check over normalized semantic tokens.

    Generated field-map labels such as ``nutrition-foundations`` are routing
    identifiers, not phrases a real paper must contain verbatim. Match their
    discriminative tokens (plus the domain tokens) as whole normalized words in
    the retrieved text or discovered title. Unrelated content still fails:
    generic structural suffixes never count as evidence and an empty semantic
    query cannot pass.
    """
    query_tokens: set[str] = set()
    for value in (candidate.domain, *candidate.topics):
        query_tokens.update(
            token for token in _SEMANTIC_TOKEN.findall(value.lower())
            if len(token) >= 3 and token not in _TOPIC_SCAFFOLD_TOKENS
        )
    if not query_tokens:
        return False
    evidence_tokens = set(_SEMANTIC_TOKEN.findall(f"{candidate.title}\n{text}".lower()))
    return bool(query_tokens & evidence_tokens)


def _safe_http_locator(url: str | None) -> bool:
    # http(s), no credentials, and not a private/loopback/link-local/metadata
    # host in any literal form (see corpus_ssrf). The live transport additionally
    # resolves DNS and pins to a validated peer before any bytes are fetched.
    return is_safe_locator(url)


def _slug(candidate_id: str) -> str:
    return _SAFE_SLUG.sub("-", candidate_id).strip("-") or "candidate"


def _receipt_sha256(receipt: dict[str, Any]) -> str:
    payload = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _preimage_digest(candidate: AcquisitionCandidate) -> str:
    """Canonical digest of the immutable acquisition preimage of a candidate.

    Binds receipt reuse to the exact source identity and rights posture. If the
    same idempotency key is presented with any materially different candidate,
    the digest changes and reuse fails closed.
    """
    rights = candidate.rights_evidence
    payload = {
        "candidate_id": candidate.candidate_id,
        "domain": candidate.domain,
        "canonical_locator": candidate.canonical_locator,
        "content_locator": candidate.content_locator,
        "evidence_lane": candidate.evidence_lane,
        "source_family": candidate.source_family,
        "title": candidate.title,
        "topics": list(candidate.topics),
        "idempotency_key": candidate.idempotency_key,
        "source_revision": candidate.metadata.get("source_revision"),
        "rights": {
            "license": rights.license,
            "access_class": rights.access_class,
            "repository": rights.repository,
            "site_license_grant": rights.site_license_grant,
        },
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _contained_dir(root: Path, *parts: str) -> Path:
    """Resolve ``root/parts`` and confirm it stays within ``root``.

    Guards every domain-derived write against ``..`` traversal and symlink
    escape (``.resolve()`` follows symlinks, so a symlinked component that points
    outside ``root`` is caught by the containment check). Raises
    :class:`AcquisitionExecutorError` on any escape.
    """
    root_resolved = root.resolve()
    target = (root / Path(*parts)).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise AcquisitionExecutorError(f"path {target} escapes root {root_resolved}")
    return target


def _atomic_write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return sha256_bytes(content.encode("utf-8"))


class AcquisitionExecutor:
    def __init__(
        self,
        *,
        raw_root: Path | str,
        staging_root: Path | str,
        receipts_root: Path | str,
        quarantine_root: Path | str,
        http_fetch: Callable[..., FetchResult],
        now: Callable[[], str],
        canonical_root: Path | str | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        allowed_mime: frozenset[str] = DEFAULT_ALLOWED_MIME,
        minimum_text_chars: int = DEFAULT_MIN_TEXT_CHARS,
    ):
        self.raw_root = Path(raw_root)
        self.staging_root = Path(staging_root)
        self.receipts_root = Path(receipts_root)
        self.quarantine_root = Path(quarantine_root)
        self.canonical_root = Path(canonical_root) if canonical_root is not None else None
        self.http_fetch = http_fetch
        self.now = now
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self.allowed_mime = frozenset(allowed_mime)
        self.minimum_text_chars = minimum_text_chars

    # -- receipt helpers ---------------------------------------------------
    def _receipt_path(self, candidate: AcquisitionCandidate) -> Path:
        # _slug strips path separators, so the receipt name cannot traverse; the
        # containment check is a defensive belt against symlinked roots.
        _contained_dir(self.receipts_root)
        return self.receipts_root / f"{_slug(candidate.idempotency_key)}.json"

    @contextmanager
    def _key_lock(self, candidate: AcquisitionCandidate):
        """Serialize same-key attempts across threads AND processes (flock).

        flock is held on a dedicated per-key lock file; two open descriptors
        (from different threads or different processes) are mutually exclusive,
        so simultaneous acquisitions converge to one fetch, one receipt, and one
        canonical page.
        """
        lock_dir = self.receipts_root / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = lock_dir / f"{_slug(candidate.idempotency_key)}.lock"
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(lock_path), flags, 0o600)
        except OSError as exc:
            raise AcquisitionExecutorError(f"cannot safely open acquisition lock {lock_path}: {exc}") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise AcquisitionExecutorError(f"acquisition lock is not a regular file: {lock_path}")
            if stat.S_IMODE(opened.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            # flock protects an inode, not a pathname. Verify the path still names
            # the inode we locked before entering the mutating critical section.
            current = os.lstat(lock_path)
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise AcquisitionExecutorError(f"acquisition lock identity changed before use: {lock_path}")
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _reuse_receipt(self, receipt_path: Path, candidate: AcquisitionCandidate) -> dict[str, Any]:
        """Validate a stored receipt before reusing it (fail closed on drift)."""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(receipt_path), flags)
        except OSError as exc:
            raise AcquisitionExecutorError(f"cannot safely open stored receipt {receipt_path}: {exc}") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise AcquisitionExecutorError(f"stored receipt is not a regular file: {receipt_path}")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                stored = json.load(handle)
        finally:
            if fd >= 0:
                os.close(fd)
        if _receipt_sha256(stored) != stored.get("receipt_sha256"):
            raise AcquisitionExecutorError(f"stored receipt digest mismatch (tampered): {receipt_path}")
        if stored.get("candidate_id") != candidate.candidate_id or stored.get("idempotency_key") != candidate.idempotency_key:
            raise AcquisitionExecutorError(
                f"idempotency key collides with a different candidate: {candidate.idempotency_key!r}"
            )
        if stored.get("preimage_digest") != _preimage_digest(candidate):
            raise AcquisitionExecutorError(
                f"idempotency key {candidate.idempotency_key!r} reused with a changed candidate preimage"
            )
        return stored

    def _finalize(self, receipt: dict[str, Any], candidate: AcquisitionCandidate) -> dict[str, Any]:
        receipt["receipt_sha256"] = _receipt_sha256(receipt)
        path = self._receipt_path(candidate)
        _atomic_write(path, json.dumps(receipt, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        return receipt

    def _base_receipt(self, candidate: AcquisitionCandidate, resolution: RightsResolution | None) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "idempotency_key": candidate.idempotency_key,
            "preimage_digest": _preimage_digest(candidate),
            "candidate_id": candidate.candidate_id,
            "domain": candidate.domain,
            "canonical_locator": candidate.canonical_locator,
            "content_locator": candidate.content_locator,
            "evidence_lane": candidate.evidence_lane,
            "source_family": candidate.source_family,
            "title": candidate.title,
            "topics": list(candidate.topics),
            "acquired_at": self.now(),
            "rights": resolution.to_dict() if resolution is not None else None,
            "raw": None,
            "normalized": None,
            "staged": None,
            "canonical": None,
            "metadata_artifact": None,
            "evaluation": None,
            "quarantine": None,
            "failure": None,
            "promotion_enabled": False,
            "production_mutation": False,
            "canonical_mutated": False,
        }

    # -- public entrypoint -------------------------------------------------
    def acquire(self, candidate: AcquisitionCandidate) -> dict[str, Any]:
        # 1. Idempotency fast path: an exact prior attempt short-circuits with no
        #    lock and no I/O beyond validating the stored receipt.
        receipt_path = self._receipt_path(candidate)
        if receipt_path.is_symlink():
            raise AcquisitionExecutorError(f"stored receipt path is a symlink: {receipt_path}")
        if receipt_path.is_file():
            return self._reuse_receipt(receipt_path, candidate)
        # Serialize concurrent same-key attempts, then re-check under the lock so
        # a racing process that just wrote the receipt is reused, not repeated.
        with self._key_lock(candidate):
            if receipt_path.is_symlink():
                raise AcquisitionExecutorError(f"stored receipt path is a symlink: {receipt_path}")
            if receipt_path.is_file():
                return self._reuse_receipt(receipt_path, candidate)
            return self._acquire_locked(candidate)

    def _acquire_locked(self, candidate: AcquisitionCandidate) -> dict[str, Any]:
        lifecycle = AcquisitionLifecycle()
        receipt = self._base_receipt(candidate, None)

        # 2. Verify locator (http(s), no credentials, safe public host).
        if not _safe_http_locator(candidate.canonical_locator) or not _safe_http_locator(candidate.content_locator or candidate.canonical_locator):
            lifecycle.advance(AcquisitionState.REJECTED)
            receipt.update(disposition="rejected", terminal_state="rejected", lifecycle=list(lifecycle.history),
                           failure={"reason": "locator_invalid", "detail": "locator must be a safe public http(s) URL without credentials"})
            return self._finalize(receipt, candidate)
        lifecycle.advance(AcquisitionState.LOCATOR_VERIFIED)

        # 2b. Guard every domain-derived write path against traversal/symlink
        #     escape before any bytes are staged or installed.
        try:
            _contained_dir(self.staging_root, candidate.domain)
            _contained_dir(self.quarantine_root, _slug(candidate.idempotency_key))
            if self.canonical_root is not None:
                _contained_dir(self.canonical_root, candidate.domain)
        except AcquisitionExecutorError as exc:
            lifecycle.advance(AcquisitionState.REJECTED)
            receipt.update(disposition="failed", terminal_state="rejected", lifecycle=list(lifecycle.history),
                           failure={"reason": "domain_path_unsafe", "detail": str(exc)})
            return self._finalize(receipt, candidate)

        # 3. Resolve rights (single fail-closed enforcement point).
        resolution = resolve_rights(candidate.rights_evidence)
        receipt = self._base_receipt(candidate, resolution)
        lifecycle.advance(AcquisitionState.RIGHTS_RESOLVED)

        if resolution.disposition == "metadata_only":
            return self._preserve_metadata(candidate, resolution, receipt, lifecycle)
        if resolution.disposition in {"human_gate", "blocked", "rights_unclear"}:
            lifecycle.advance(AcquisitionState.REJECTED)
            receipt.update(disposition=resolution.disposition, terminal_state="rejected",
                           lifecycle=list(lifecycle.history))
            return self._finalize(receipt, candidate)

        # 4. Auto content acquisition (public_rights_clear only).
        return self._acquire_content(candidate, resolution, receipt, lifecycle)

    # -- metadata-only path ------------------------------------------------
    def _preserve_metadata(self, candidate, resolution, receipt, lifecycle) -> dict[str, Any]:
        metadata_blob = (json.dumps(
            {"candidate_id": candidate.candidate_id, "canonical_locator": candidate.canonical_locator,
             "title": candidate.title, "metadata": candidate.metadata},
            sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2,
        ) + "\n").encode("utf-8")
        artifact = preserve_bytes(self.raw_root, prefix="metadata", suffix=".json", body=metadata_blob)
        # Metadata-only legitimately stops at rights_resolved: metadata is
        # preserved but the content lifecycle is never entered. The lifecycle
        # history and terminal_state stay consistent with that truth.
        receipt.update(
            disposition="metadata_only", terminal_state="rights_resolved",
            lifecycle=list(lifecycle.history),
            metadata_artifact={"pointer": str(artifact), "sha256": sha256_bytes(metadata_blob)},
        )
        return self._finalize(receipt, candidate)

    # -- content path ------------------------------------------------------
    def _acquire_content(self, candidate, resolution, receipt, lifecycle) -> dict[str, Any]:
        content_locator = candidate.content_locator or candidate.canonical_locator
        lifecycle.advance(AcquisitionState.QUEUED)
        lifecycle.advance(AcquisitionState.ACQUIRING)

        try:
            fetched = self.http_fetch(content_locator, timeout=self.timeout_seconds, max_bytes=self.max_bytes)
        except Exception as exc:  # transport failure -> explicit failed receipt
            return self._fail(candidate, receipt, lifecycle, "transport_error", f"{type(exc).__name__}: {exc}")

        # Redirect + final-URL validation.
        for hop in (*fetched.redirect_chain, fetched.final_url):
            if not _safe_http_locator(hop):
                return self._fail(candidate, receipt, lifecycle, "redirect_invalid", f"unsafe redirect/final hop: {hop!r}")
        # Size cap.
        if len(fetched.body) > self.max_bytes:
            return self._fail(candidate, receipt, lifecycle, "size_cap_exceeded", f"{len(fetched.body)} bytes > {self.max_bytes}")
        # MIME allowlist. A recognized document type without a packaged
        # extractor (PDF) is truthfully human-gated, not fabricated or "failed".
        base_mime = fetched.content_type.split(";", 1)[0].strip().lower()
        if base_mime not in self.allowed_mime:
            if base_mime in GATED_DOCUMENT_MIME:
                return self._gate_content(candidate, receipt, lifecycle, base_mime)
            return self._fail(candidate, receipt, lifecycle, "mime_not_allowed", f"content-type {base_mime!r} not in allowlist")

        # Preserve raw (content-addressed; retries never duplicate).
        raw_path = preserve_bytes(self.raw_root, prefix="raw", suffix=".bin", body=fetched.body)
        raw_sha = sha256_bytes(fetched.body)
        lifecycle.advance(AcquisitionState.RAW_PRESERVED)
        receipt["raw"] = {
            "pointer": str(raw_path), "sha256": raw_sha, "bytes": len(fetched.body),
            "content_type": base_mime, "final_url": fetched.final_url, "source_revision": raw_sha,
        }

        # Normalize.
        text = _normalize_text(fetched.body, fetched.content_type)
        if len(text.strip()) < self.minimum_text_chars:
            return self._fail(candidate, receipt, lifecycle, "normalized_text_too_short", f"{len(text.strip())} chars")
        normalized_bytes = text.encode("utf-8")
        normalized_path = preserve_bytes(self.raw_root, prefix="normalized", suffix=".txt", body=normalized_bytes)
        normalized_sha = sha256_bytes(normalized_bytes)
        lifecycle.advance(AcquisitionState.NORMALIZED)
        receipt["normalized"] = {"pointer": str(normalized_path), "sha256": normalized_sha, "chars": len(text)}

        # Stage (never canonical).
        page_slug = _slug(candidate.candidate_id)
        staged_path = self.staging_root / candidate.domain / f"{page_slug}.md"
        page = self._render_page(candidate, resolution, raw_sha, normalized_sha, text)
        staged_sha = _atomic_write(staged_path, page)
        lifecycle.advance(AcquisitionState.STAGED)
        receipt["staged"] = {"path": str(staged_path), "sha256": staged_sha, "page_slug": page_slug}

        # Evaluate before any promotion.
        lifecycle.advance(AcquisitionState.EVALUATED)
        evaluation = self._evaluate(candidate, receipt, raw_path, normalized_path, staged_path, text)
        receipt["evaluation"] = evaluation
        if not evaluation["passed"]:
            return self._quarantine(candidate, receipt, lifecycle, staged_path, staged_sha, evaluation["failures"])

        lifecycle.advance(AcquisitionState.PROBATIONARY)
        receipt.update(disposition="probationary", terminal_state="probationary", lifecycle=list(lifecycle.history))
        # Evidence admission (NOT doctrine promotion): install the eval-passed
        # probationary page into the canonical domain namespace. Only reached
        # after every integrity/provenance/retrieval check passed. When a
        # canonical root is configured the admission and the receipt are one
        # transaction: a receipt-persistence failure rolls the canonical bytes
        # back to their exact prior state (or deletes a newly-created file).
        if self.canonical_root is not None:
            return self._install_canonical(candidate, receipt, staged_path)
        return self._finalize(receipt, candidate)

    def _install_canonical(self, candidate, receipt, staged_path) -> dict[str, Any]:
        """Transactionally admit the staged evidence page into the canonical corpus.

        The canonical bytes are exactly the eval-passed staged bytes. Before the
        write, the exact prior state (bytes + mtime) is captured *privately* — it
        is never surfaced in the receipt, which records only a hash. The write is
        atomic (mkstemp+rename), then the receipt is persisted. If receipt
        persistence fails after the canonical bytes were written, the canonical
        file is restored to its exact prior state (or deleted if it was newly
        created), so a failed admission leaves no orphan canonical bytes and no
        durable receipt. This is evidence admission only — promotion stays
        disabled.
        """
        acquired_dir = _contained_dir(self.canonical_root, candidate.domain, "sources", "acquired")
        page_slug = _slug(candidate.candidate_id)
        canonical_path = acquired_dir / f"{page_slug}.md"
        # Capture exact prior state privately (bytes + mtime). Never placed in the
        # receipt — a rollback record exposes only the prior content hash.
        if canonical_path.exists():
            prior_bytes: bytes | None = canonical_path.read_bytes()
            prior_stat = canonical_path.stat()
            rollback = {"existed": True, "previous_sha256": sha256_bytes(prior_bytes)}
        else:
            prior_bytes = None
            prior_stat = None
            rollback = {"existed": False, "previous_sha256": None}

        page = staged_path.read_text(encoding="utf-8")
        canonical_sha = _atomic_write(canonical_path, page)
        receipt["canonical"] = {
            "path": str(canonical_path),
            "sha256": canonical_sha,
            "rollback": rollback,
        }
        receipt["canonical_mutated"] = True

        try:
            return self._finalize(receipt, candidate)
        except Exception as final_exc:
            # Receipt persistence failed after canonical bytes were written. The
            # admission is not durable: restore the exact prior canonical state.
            try:
                self._restore_canonical(canonical_path, prior_bytes, prior_stat)
            except Exception as restore_exc:
                # The restore itself failed: do NOT claim a clean rollback. Leave
                # a local emergency marker (hashes only) and raise a compound error.
                self._write_emergency_marker(
                    candidate, canonical_path, rollback, canonical_sha, final_exc, restore_exc
                )
                raise AcquisitionExecutorError(
                    f"canonical admission ROLLBACK FAILED at {canonical_path}: "
                    f"receipt_error={type(final_exc).__name__}: {final_exc}; "
                    f"restore_error={type(restore_exc).__name__}: {restore_exc}; "
                    "canonical bytes may be inconsistent — see emergency marker"
                ) from restore_exc
            raise AcquisitionExecutorError(
                f"receipt finalization failed; canonical admission rolled back to prior state at "
                f"{canonical_path}: {type(final_exc).__name__}: {final_exc}"
            ) from final_exc

    def _restore_canonical(self, canonical_path: Path, prior_bytes: bytes | None, prior_stat) -> None:
        """Restore ``canonical_path`` to the exact prior state captured before a write.

        Previously-absent → delete the newly-created file. Previously-present →
        atomically rewrite the exact prior bytes and restore the prior mtime.
        """
        if prior_bytes is None:
            if canonical_path.exists():
                os.unlink(canonical_path)
            return
        fd, tmp = tempfile.mkstemp(prefix=f".{canonical_path.name}.", dir=canonical_path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(prior_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, canonical_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        if prior_stat is not None:
            os.utime(canonical_path, ns=(prior_stat.st_atime_ns, prior_stat.st_mtime_ns))

    def _write_emergency_marker(
        self, candidate, canonical_path, rollback, canonical_sha, final_exc, restore_exc
    ) -> None:
        """Preserve a local, hash-only marker when a canonical rollback fails.

        The marker carries no prior private text — only content hashes and error
        summaries — so an operator can reconcile the canonical file by hand.
        """
        marker_dir = self.receipts_root / ".emergency"
        try:
            marker_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            marker = {
                "kind": "canonical_rollback_failed",
                "candidate_id": candidate.candidate_id,
                "domain": candidate.domain,
                "canonical_path": str(canonical_path),
                "written_sha256": canonical_sha,
                "prior_existed": rollback["existed"],
                "prior_sha256": rollback["previous_sha256"],
                "receipt_error": f"{type(final_exc).__name__}: {final_exc}",
                "restore_error": f"{type(restore_exc).__name__}: {restore_exc}",
                "recorded_at": self.now(),
            }
            path = marker_dir / f"{_slug(candidate.idempotency_key)}.json"
            _atomic_write(path, json.dumps(marker, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        except Exception:
            # Best-effort: never mask the original compound failure with a marker
            # write error.
            pass

    def _render_page(self, candidate, resolution, raw_sha, normalized_sha, text) -> str:
        # Sanitize the title so live-source punctuation cannot break the YAML.
        safe_title = candidate.title.replace("\\", " ").replace('"', "'").replace("\n", " ").strip()
        front = [
            "---",
            'type: "evidence"',
            f'title: "{safe_title}"',
            f'source_url: "{candidate.canonical_locator}"',
            f'source_revision: "{raw_sha}"',
            f'rights_state: "{resolution.rights_state.value}"',
            f'rights_basis: "{resolution.basis}"',
            f'evidence_lane: "{candidate.evidence_lane}"',
            'candidate_status: "probationary"',
            f'raw_sha256: "{raw_sha}"',
            f'normalized_sha256: "{normalized_sha}"',
            f'acquired_at: "{self.now()}"',
            "---",
            "",
            f"# {candidate.title}",
            "",
            text.strip(),
            "",
        ]
        return "\n".join(front)

    def _evaluate(self, candidate, receipt, raw_path, normalized_path, staged_path, text) -> dict[str, Any]:
        checks: dict[str, bool] = {}
        # Integrity: preserved bytes still hash to the recorded digests.
        checks["raw_integrity"] = sha256_bytes(raw_path.read_bytes()) == receipt["raw"]["sha256"]
        checks["normalized_integrity"] = sha256_bytes(normalized_path.read_bytes()) == receipt["normalized"]["sha256"]
        checks["staged_integrity"] = sha256_bytes(staged_path.read_bytes()) == receipt["staged"]["sha256"]
        # Provenance: the staged page carries rights-cleared provenance.
        staged_text = staged_path.read_text(encoding="utf-8")
        checks["provenance_rights_clear"] = "public_rights_clear" in staged_text and candidate.canonical_locator in staged_text
        # Retrieval: generated composite labels are routing IDs, so compare
        # normalized semantic topic/domain tokens against full text + title.
        checks["retrieval_topic_hit"] = retrieval_topic_matches(candidate, text)
        failures = [name for name, ok in checks.items() if not ok]
        return {"passed": not failures, "checks": checks, "failures": failures}

    # -- terminal helpers --------------------------------------------------
    def _gate_content(self, candidate, receipt, lifecycle, content_type) -> dict[str, Any]:
        """Route recognized-but-unextractable content to a human/extractor gate.

        Nothing is staged or admitted: the disposition is ``human_gate`` so the
        truth ("real full text exists, but this engine cannot extract it") is
        surfaced instead of a false ``acquired`` or a misleading ``failed``.
        """
        lifecycle.advance(AcquisitionState.REJECTED)
        receipt.update(
            disposition="human_gate", terminal_state="rejected", lifecycle=list(lifecycle.history),
            gate={
                "reason": "unsupported_content_type",
                "content_type": content_type,
                "detail": f"{content_type} requires a packaged extractor or human handling before ingestion",
            },
        )
        return self._finalize(receipt, candidate)

    def _fail(self, candidate, receipt, lifecycle, reason, detail) -> dict[str, Any]:
        lifecycle.advance(AcquisitionState.REJECTED)
        receipt.update(disposition="failed", terminal_state="rejected", lifecycle=list(lifecycle.history),
                       failure={"reason": reason, "detail": detail})
        return self._finalize(receipt, candidate)

    def _quarantine(self, candidate, receipt, lifecycle, staged_path, staged_sha, failures) -> dict[str, Any]:
        destination_dir = self.quarantine_root / _slug(candidate.idempotency_key)
        destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = destination_dir / staged_path.name
        shutil.move(str(staged_path), str(destination))
        lifecycle.advance(AcquisitionState.QUARANTINED)
        receipt.update(
            disposition="quarantined", terminal_state="quarantined", lifecycle=list(lifecycle.history),
            quarantine={"path": str(destination), "sha256": staged_sha, "reason": "evaluation_failed", "failures": list(failures)},
        )
        receipt["staged"] = None
        return self._finalize(receipt, candidate)
