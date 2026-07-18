#!/usr/bin/env python3
"""Shared GBrain-native corpus registry refresher.

Scans domain registries inside the single /root/corpora source, preserves raw
artifacts under the shared corpus archive, writes citation-addressable source
cards, and maintains one lifecycle-state format. It does not create per-domain
retrievers, databases, schedulers, or cron jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


import requests

_SRC_ROOT = Path(__file__).resolve().parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from corpus_adapters import STANDARD_ADAPTER_FAMILIES, build_adapter
from corpus_adapters.base import AdapterRunner, SourceAdapter
from corpus_adapters.github import GitHubRepositoryAdapter
from corpus_adapters.types import InventoryRequest, RightsState, SourceSpec
from corpus_adapters.web import WebDocumentAdapter
from corpus_adapters.youtube import YouTubeFeedAdapter, inventory_revision, normalize_caption_vtt, parse_feed, preserve_caption_artifacts

CORPUS_REPO = Path(os.environ.get("CORPUS_REPO", "/root/corpora"))
ARCHIVE_ROOT = Path(os.environ.get("CORPUS_ARCHIVE_ROOT", "/root/exports/thinker-corpora"))
DEFAULT_UA = "Mozilla/5.0 (compatible; GBrainCorpusEngine/1.0; +https://hermes-agent.nousresearch.com/)"
REFRESH_DAYS = {"hot": 1, "fast": 3, "weekly": 7, "monthly": 30, "quarterly": 90, "foundational": 180}


class MainTextParser(HTMLParser):
    """Dependency-free visible-text extractor for archival normalization."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[str] = []
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript", "template"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "pre", "code", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript", "template"} and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False
        if tag in {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "pre", "code"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        text = data.strip()
        if not text:
            return
        if self.in_title:
            self.title = f"{self.title} {text}".strip()
        self.parts.append(text)

    def text(self) -> str:
        value = " ".join(self.parts)
        value = html.unescape(value)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip() + "\n"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def request(url: str, timeout: int = 45) -> requests.Response:
    response = requests.get(url, timeout=timeout, allow_redirects=True, headers={"User-Agent": DEFAULT_UA})
    response.raise_for_status()
    return response


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_due(entry: dict[str, Any], prior: dict[str, Any], force: bool) -> bool:
    # Frozen sources are intentionally lifecycle-static (for example, a
    # rights-reviewed private course snapshot). A broad --force refresh must
    # not route them through public-web handlers or overwrite curated cards.
    if entry.get("refresh_class") == "frozen":
        return False
    if force:
        return True
    last = parse_iso(prior.get("last_checked_at"))
    if not last:
        return True
    days = REFRESH_DAYS.get(entry.get("refresh_class", "monthly"), 30)
    return utcnow() >= last + timedelta(days=days)


def has_material_refresh_results(results: list["RefreshResult"]) -> bool:
    """True only when a due source changed a corpus projection."""
    return any(result.status not in {"not_due", "unchanged"} for result in results)


def has_checked_refresh_results(results: list["RefreshResult"]) -> bool:
    """True when a run checked at least one due source, including no-delta checks."""
    return any(result.status != "not_due" for result in results)


def vtt_to_text(raw: str) -> str:
    return normalize_caption_vtt(raw)


@dataclass
class RefreshResult:
    source_id: str
    status: str
    pages: list[str]
    detail: str
    content_hash: str | None = None
    canonical_url: str | None = None


class CorpusEngine:
    def __init__(self, domain: str) -> None:
        self.domain = slugify(domain)
        self.domain_root = CORPUS_REPO / self.domain
        self.registry_path = self.domain_root / "registry" / "sources.json"
        if not self.registry_path.exists():
            raise SystemExit(f"Missing domain registry: {self.registry_path}")
        self.registry = load_json(self.registry_path)
        if self.registry.get("schema_version") != 1:
            raise SystemExit("Unsupported registry schema_version")
        if slugify(self.registry.get("domain", "")) != self.domain:
            raise SystemExit("Registry domain does not match requested domain")
        self.archive_root = ARCHIVE_ROOT / self.domain
        self.state_path = self.archive_root / "engine-state.json"
        self.state = load_json(self.state_path) if self.state_path.exists() else {"schema_version": 1, "domain": self.domain, "sources": {}}

    def source_dir(self, source_id: str) -> Path:
        return self.archive_root / "raw" / slugify(source_id)

    @property
    def adapter_state_path(self) -> Path:
        return self.archive_root / "adapter-state.json"

    def adapter_cursor(self, source_id: str) -> str | None:
        if not self.adapter_state_path.exists():
            return None
        state = load_json(self.adapter_state_path)
        source = state.get("sources", {}).get(source_id, {})
        cursor = source.get("cursor")
        return cursor if isinstance(cursor, str) and cursor else None

    def source_spec(self, entry: dict[str, Any], *, family: str, locator: str) -> SourceSpec:
        rights_name = entry.get("rights_state", RightsState.PUBLIC_METADATA_ONLY.value)
        return SourceSpec(
            source_id=entry["id"],
            domain=self.domain,
            source_family=family,
            canonical_locator=locator,
            evidence_lane=entry.get("evidence_lane", "uncategorized"),
            rights_state=RightsState(rights_name),
        )

    def run_adapter(self, entry: dict[str, Any], adapter: SourceAdapter, spec: SourceSpec, *, max_items: int):
        request_value = InventoryRequest(
            max_items=max_items,
            cursor=self.adapter_cursor(entry["id"]),
            timeout_seconds=float(entry.get("timeout_seconds", 45)),
        )
        return AdapterRunner(self.adapter_state_path).run(adapter, spec, request_value)

    def card_path(self, entry: dict[str, Any], suffix: str | None = None) -> Path:
        lane = slugify(entry.get("evidence_lane", "uncategorized"))
        name = slugify(entry["id"] if suffix is None else f"{entry['id']}-{suffix}")
        return self.domain_root / "sources" / lane / f"{name}.md"

    def write_card(
        self,
        entry: dict[str, Any],
        title: str,
        body: str,
        source_url: str,
        raw_path: Path | None,
        content_hash: str | None,
        suffix: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        path = self.card_path(entry, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        fields: dict[str, Any] = {
            "title": title,
            "type": "source",
            "domain": self.domain,
            "registry_source_id": entry["id"],
            "source_type": entry["source_type"],
            "evidence_lane": entry.get("evidence_lane", "uncategorized"),
            "authority_tier": entry.get("authority_tier", "unrated"),
            "refresh_class": entry.get("refresh_class", "monthly"),
            "epistemic_status": entry.get("epistemic_status", "attributed_source_observation"),
            "promotion_policy": "capture_all_promote_by_evidence",
            "source_url": source_url,
            "raw_available": bool(raw_path),
            "raw_storage_path": str(raw_path) if raw_path else None,
            "raw_sha256": content_hash,
            "retrieved_at": iso(),
            "privacy": "private",
        }
        if extra:
            fields.update(extra)
        fm = ["---"]
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, bool):
                fm.append(f"{key}: {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                fm.append(f"{key}: {value}")
            else:
                fm.append(f"{key}: {json_quote(str(value))}")
        fm.extend(["---", "", f"# {title}", "", body.strip(), ""])
        path.write_text("\n".join(fm), encoding="utf-8")
        return str(path.relative_to(CORPUS_REPO).with_suffix(""))

    def refresh_web(self, entry: dict[str, Any], prior: dict[str, Any] | None = None) -> RefreshResult:
        locator = entry["url"]
        adapter = WebDocumentAdapter(
            self.source_dir(entry["id"]),
            http_get=request,
            fetched_at=iso,
            minimum_text_chars=int(entry.get("minimum_text_chars", 200)),
        )
        spec = self.source_spec(entry, family="web", locator=locator)
        batch = self.run_adapter(entry, adapter, spec, max_items=1)
        observation = batch.observations[0]
        if prior and prior.get("content_hash") == batch.source_revision:
            return RefreshResult(entry["id"], "unchanged", list(prior.get("pages", [])), "source revision unchanged", batch.source_revision, observation.canonical_locator)
        text = Path(observation.normalized_pointer).read_text(encoding="utf-8")
        if len(text) < entry.get("minimum_text_chars", 1):
            raise RuntimeError(f"normalized text too short: {len(text)} chars")
        excerpt_chars = int(entry.get("card_excerpt_chars", 24000))
        body = (
            f"> **Epistemic boundary:** {entry.get('epistemic_note', 'This is attributed external evidence, not settled doctrine.')}\n\n"
            f"Canonical source: <{observation.canonical_locator}>\n\n"
            f"## Normalized source snapshot\n\n{text[:excerpt_chars]}"
        )
        slug = self.write_card(
            entry,
            entry.get("title", observation.title),
            body,
            observation.canonical_locator,
            Path(observation.raw_pointer),
            observation.raw_sha256,
            extra={
                "source_revision": batch.source_revision,
                "normalized_storage_path": observation.normalized_pointer,
                "normalized_sha256": observation.normalized_sha256,
            },
        )
        return RefreshResult(entry["id"], "refreshed", [slug], f"{len(text)} normalized chars", batch.source_revision, observation.canonical_locator)

    def refresh_github(self, entry: dict[str, Any], prior: dict[str, Any] | None = None) -> RefreshResult:
        repo = entry["repo"]
        locator = f"https://github.com/{repo}"
        adapter = GitHubRepositoryAdapter(self.source_dir(entry["id"]), repository=repo, http_get=request, fetched_at=iso)
        spec = self.source_spec(entry, family="github", locator=locator)
        batch = self.run_adapter(entry, adapter, spec, max_items=1)
        observation = batch.observations[0]
        if prior and prior.get("content_hash") == batch.source_revision:
            return RefreshResult(entry["id"], "unchanged", list(prior.get("pages", [])), f"head {batch.source_revision} unchanged", batch.source_revision, locator)
        readme = Path(observation.normalized_pointer).read_text(encoding="utf-8", errors="replace")
        body = (
            "> **Epistemic boundary:** Repository source and maintainer documentation. Runtime behavior still requires code-path inspection and testing.\n\n"
            f"Repository: <{locator}>\n\n"
            f"Observed immutable head: `{batch.source_revision}`.\n\n"
            f"## README snapshot\n\n{readme[:24000]}"
        )
        slug = self.write_card(
            entry,
            entry.get("title", repo),
            body,
            observation.canonical_locator,
            Path(observation.raw_pointer),
            observation.raw_sha256,
            extra={
                "repository_head": batch.source_revision,
                "source_revision": batch.source_revision,
                "normalized_storage_path": observation.normalized_pointer,
                "normalized_sha256": observation.normalized_sha256,
            },
        )
        return RefreshResult(entry["id"], "refreshed", [slug], f"head {batch.source_revision}", batch.source_revision, locator)

    def acquire_youtube_transcript(self, video_id: str, destination: Path) -> tuple[Path | None, str | None]:
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"caption-{slugify(video_id)}-", dir=destination) as temporary:
            temporary_root = Path(temporary)
            template = str(temporary_root / f"{video_id}.%(ext)s")
            cmd = [
                "yt-dlp", "--cookies-from-browser", "chromium:/root/.hermes/browser-profiles/youtube-takeout-sameer",
                "--ignore-no-formats", "--skip-download", "--write-subs", "--write-auto-subs",
                "--sub-langs", "en-orig,en,en.*", "--sub-format", "vtt", "-o", template,
                f"https://www.youtube.com/watch?v={video_id}",
            ]
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=180)
            candidates = sorted(temporary_root.glob(f"{video_id}*.vtt"))
            if not candidates:
                return None, (proc.stderr or proc.stdout)[-1200:]
            artifacts = preserve_caption_artifacts(destination, video_id, candidates[0].read_bytes())
            return artifacts.normalized_path, None

    def prior_youtube_revision(self, entry: dict[str, Any], prior: dict[str, Any] | None) -> str | None:
        if not prior:
            return None
        prior_hash = prior.get("content_hash")
        if not isinstance(prior_hash, str) or len(prior_hash) != 64:
            return None
        # New adapter state stores the inventory revision directly. Legacy state
        # stored the rolling feed byte hash; recover its stable inventory once.
        raw_dir = self.source_dir(entry["id"])
        legacy_feed = raw_dir / f"feed-{prior_hash[:12]}.xml"
        if legacy_feed.exists() and sha256_bytes(legacy_feed.read_bytes()) == prior_hash:
            return inventory_revision(parse_feed(legacy_feed.read_bytes()))
        return prior_hash

    def pending_youtube_items(self, prior: dict[str, Any] | None) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for slug in (prior or {}).get("pages", []):
            path = CORPUS_REPO / f"{slug}.md"
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not re.search(r'^transcript_status:\s*["\']pending["\']\s*$', text, re.MULTILINE):
                continue
            def field(name: str, default: str = "") -> str:
                match = re.search(rf'^{re.escape(name)}:\s*["\']([^"\']*)["\']\s*$', text, re.MULTILINE)
                return match.group(1) if match else default
            video_id = field("video_id")
            if video_id:
                items.append(
                    {
                        "video_id": video_id,
                        "title": field("title", video_id),
                        "published": field("published_at"),
                        "url": field("source_url", f"https://www.youtube.com/watch?v={video_id}"),
                    }
                )
        return items

    def refresh_youtube(self, entry: dict[str, Any], prior: dict[str, Any] | None = None) -> RefreshResult:
        channel_id = entry["channel_id"]
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        raw_dir = self.source_dir(entry["id"])
        adapter = YouTubeFeedAdapter(raw_dir, channel_id=channel_id, http_get=request, fetched_at=iso)
        locator = f"https://www.youtube.com/channel/{channel_id}"
        spec = self.source_spec(entry, family="youtube", locator=locator)
        max_items = min(int(entry.get("max_items_per_refresh", 3)), 100)
        batch = self.run_adapter(entry, adapter, spec, max_items=max_items)
        prior_revision = self.prior_youtube_revision(entry, prior)
        stable_inventory = prior_revision == batch.source_revision

        work_items: list[tuple[dict[str, str], Any | None]] = []
        if stable_inventory:
            work_items = [(item, None) for item in self.pending_youtube_items(prior)]
            if not work_items:
                return RefreshResult(
                    entry["id"], "unchanged", list((prior or {}).get("pages", [])),
                    "inventory revision unchanged; no pending captions", batch.source_revision, feed_url,
                )
        else:
            for observation in batch.observations:
                item = json.loads(Path(observation.normalized_pointer).read_text(encoding="utf-8"))
                work_items.append((item, observation))

        pages: list[str] = list((prior or {}).get("pages", [])) if stable_inventory else []
        transcript_count = 0
        changed = False
        can_acquire_body = spec.rights_state in {RightsState.PUBLIC_RIGHTS_CLEAR, RightsState.PRIVATE_AUTHORIZED}
        for item, observation in work_items:
            transcript_path: Path | None = None
            transcript_error: str | None = None
            if entry.get("acquire_transcripts", True) and can_acquire_body:
                transcript_path, transcript_error = self.acquire_youtube_transcript(item["video_id"], raw_dir / "transcripts")
            if stable_inventory and not transcript_path:
                continue
            transcript = transcript_path.read_text(encoding="utf-8") if transcript_path else ""
            if transcript_path:
                transcript_count += 1
                changed = True
            raw_candidates = sorted(
                (raw_dir / "transcripts" / "raw").glob(f"{item['video_id']}-*.vtt"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            ) if transcript_path else []
            fallback_raw = Path(observation.raw_pointer) if observation is not None else None
            raw_pointer = raw_candidates[0] if raw_candidates else fallback_raw
            raw_digest = sha256_bytes(raw_pointer.read_bytes()) if raw_pointer and raw_pointer.exists() else None
            normalized_digest = sha256_bytes(transcript.encode("utf-8")) if transcript else (observation.normalized_sha256 if observation is not None else None)
            normalized_pointer = str(transcript_path) if transcript_path else (observation.normalized_pointer if observation is not None else None)
            if transcript_path:
                transcript_status = "acquired"
            elif can_acquire_body:
                transcript_status = "pending"
            else:
                transcript_status = "not_authorized_metadata_only"
            boundary = "Practitioner evidence. A demonstrated workflow is stronger than an unsupported claim, but neither becomes doctrine without comparison or local verification."
            body = (
                f"> **Epistemic boundary:** {boundary}\n\n"
                f"Channel: **{entry.get('title', entry['id'])}**\nPublished: `{item['published']}`\nVideo: <{item['url']}>\n\n"
                f"Transcript status: **{transcript_status}**.\n"
            )
            if transcript_error and not transcript_path:
                body += "\nCaption acquisition did not produce a transcript in this pass; the shared fallback ladder remains queued.\n"
            if transcript:
                body += f"\n## Transcript\n\n{transcript[:60000]}"
            slug = self.write_card(
                entry, item["title"], body, item["url"], raw_pointer, raw_digest,
                suffix=item["video_id"],
                extra={
                    "video_id": item["video_id"], "published_at": item["published"],
                    "transcript_status": transcript_status, "source_revision": batch.source_revision,
                    "normalized_storage_path": normalized_pointer, "normalized_sha256": normalized_digest,
                },
            )
            if slug not in pages:
                pages.append(slug)

        if stable_inventory:
            status = "refreshed" if changed else "unchanged"
            detail = f"stable inventory; {transcript_count} pending captions acquired" if changed else "stable inventory; pending captions remain"
            return RefreshResult(entry["id"], status, pages, detail, batch.source_revision, feed_url)

        channel_body = (
            "> **Epistemic boundary:** Practitioner/operator lane. Source credibility is evaluated per topic and claim.\n\n"
            f"Channel feed: <{feed_url}>\n\n"
            f"Items observed this refresh: **{len(batch.observations)}**. Transcripts acquired: **{transcript_count}**."
        )
        feed_path = Path(batch.observations[0].raw_pointer) if batch.observations else raw_dir / "missing-feed"
        pages.append(
            self.write_card(
                entry, entry.get("title", entry["id"]), channel_body, feed_url,
                feed_path if feed_path.exists() else None,
                sha256_bytes(feed_path.read_bytes()) if feed_path.exists() else None,
                extra={"source_revision": batch.source_revision},
            )
        )
        return RefreshResult(entry["id"], "refreshed", pages, f"{len(batch.observations)} metadata items; {transcript_count} transcripts", batch.source_revision, feed_url)

    def refresh_pointer(self, entry: dict[str, Any]) -> RefreshResult:
        body = (
            f"> **Epistemic boundary:** Discovery/pointer source. Content is processed as attributed signal and requires corroboration before promotion.\n\n"
            f"Purpose: {entry.get('purpose', 'Recurring discovery source.')}\n\n"
            f"Acquisition mode: `{entry.get('acquisition_mode', 'agent_review')}`.\n\n"
            f"Current status: `{entry.get('status', 'registered')}`."
        )
        url = entry.get("url", "")
        slug = self.write_card(entry, entry.get("title", entry["id"]), body, url, None, None)
        return RefreshResult(entry["id"], "registered", [slug], entry.get("status", "registered"), None, url)

    def _dispatch_table(self) -> dict[str, Any]:
        """Declarative source_type -> handler registry (no vertical if-ladder).

        Lookup happens before any adapter is constructed or run, so an unknown
        source_type fails closed before any network or filesystem mutation.
        """
        table: dict[str, Any] = {
            "web_document": self.refresh_web,
            "github_repository": self.refresh_github,
            "youtube_channel": self.refresh_youtube,
        }
        for pointer_type in ("x_discovery", "private_community", "discovery_feed"):
            table[pointer_type] = lambda entry, prior=None: self.refresh_pointer(entry)
        for family in STANDARD_ADAPTER_FAMILIES:
            table[family] = (
                lambda entry, prior=None, family=family: self.refresh_generic_adapter(entry, prior, family=family)
            )
        return table

    def refresh_entry(self, entry: dict[str, Any], prior: dict[str, Any] | None = None) -> RefreshResult:
        source_type = entry["source_type"]
        handler = self._dispatch_table().get(source_type)
        if handler is None:
            raise RuntimeError(f"unsupported source_type: {source_type}")
        return handler(entry, prior)

    def refresh_generic_adapter(
        self, entry: dict[str, Any], prior: dict[str, Any] | None = None, *, family: str
    ) -> RefreshResult:
        """Uniform refresh for adapters that read their target from the spec locator.

        Wires arXiv/OpenAlex/benchmark/RSS/postmortem through the shared engine
        without family-specific dispatch. The adapter is constructed via the
        declarative factory registry; the resulting observation becomes a
        citation-addressable source card.
        """
        locator = entry["url"]
        adapter = build_adapter(
            family, self.source_dir(entry["id"]), http_get=request, fetched_at=iso, entry=entry
        )
        spec = self.source_spec(entry, family=family, locator=locator)
        batch = self.run_adapter(entry, adapter, spec, max_items=int(entry.get("max_items", 1)))
        observation = batch.observations[0]
        if prior and prior.get("content_hash") == batch.source_revision:
            return RefreshResult(
                entry["id"], "unchanged", list(prior.get("pages", [])),
                "source revision unchanged", batch.source_revision, observation.canonical_locator,
            )
        text = Path(observation.normalized_pointer).read_text(encoding="utf-8", errors="replace")
        excerpt_chars = int(entry.get("card_excerpt_chars", 24000))
        body = (
            f"> **Epistemic boundary:** {entry.get('epistemic_note', 'This is attributed external evidence, not settled doctrine.')}\n\n"
            f"Canonical source: <{observation.canonical_locator}>\n\n"
            f"Observed source revision: `{batch.source_revision}`.\n\n"
            f"## Normalized source snapshot\n\n{text[:excerpt_chars]}"
        )
        slug = self.write_card(
            entry,
            entry.get("title", observation.title),
            body,
            observation.canonical_locator,
            Path(observation.raw_pointer),
            observation.raw_sha256,
            extra={
                "source_revision": batch.source_revision,
                "normalized_storage_path": observation.normalized_pointer,
                "normalized_sha256": observation.normalized_sha256,
            },
        )
        return RefreshResult(
            entry["id"], "refreshed", [slug], f"{len(text)} normalized chars",
            batch.source_revision, observation.canonical_locator,
        )

    def refresh(self, force: bool = False, only: set[str] | None = None) -> list[RefreshResult]:
        results: list[RefreshResult] = []
        source_state = self.state.setdefault("sources", {})
        for entry in self.registry.get("sources", []):
            source_id = entry["id"]
            if only and source_id not in only:
                continue
            prior = source_state.setdefault(source_id, {})
            if not is_due(entry, prior, force):
                results.append(RefreshResult(source_id, "not_due", [], "refresh window not reached"))
                continue
            try:
                result = self.refresh_entry(entry, prior)
                prior.update({"last_checked_at": iso(), "last_success_at": iso(), "last_status": result.status, "last_detail": result.detail, "last_error": None, "content_hash": result.content_hash, "canonical_url": result.canonical_url, "pages": result.pages})
            except Exception as exc:
                result = RefreshResult(source_id, "failed", [], f"{type(exc).__name__}: {exc}")
                prior.update({"last_checked_at": iso(), "last_status": "failed", "last_error": result.detail})
            results.append(result)
        # Persist due-check timestamps even when source bytes are unchanged, but
        # never rewrite corpus projections for a no-material-delta cycle.
        if has_checked_refresh_results(results):
            self.state["updated_at"] = iso()
            write_json(self.state_path, self.state)
        if has_material_refresh_results(results):
            self.write_coverage(results)
        return results

    def write_coverage(self, results: list[RefreshResult]) -> None:
        entries = {entry["id"]: entry for entry in self.registry.get("sources", [])}
        lines = [
            "---",
            f"title: {json_quote(self.registry.get('title', self.domain) + ' Coverage Ledger')}",
            "type: report",
            f"domain: {json_quote(self.domain)}",
            "epistemic_layer: corpus_coverage",
            f"updated_at: {json_quote(iso())}",
            "privacy: private",
            "---",
            "",
            f"# {self.registry.get('title', self.domain)} Coverage Ledger",
            "",
            "This ledger distinguishes registration, acquisition, normalization, retrieval readiness, and doctrine promotion. Captured information is never silently promoted into settled doctrine.",
            "",
            "## Source registry status",
            "",
            "| Source | Lane | Authority | Refresh | Status | Detail |",
            "|---|---|---|---|---|---|",
        ]
        result_map = {r.source_id: r for r in results}
        for source_id, entry in entries.items():
            state = self.state.get("sources", {}).get(source_id, {})
            result = result_map.get(source_id)
            if result and result.status != "not_due":
                status = result.status
                detail_value = result.detail
            else:
                status = state.get("last_status") or entry.get("status", "registered")
                detail_value = state.get("last_detail") or state.get("last_error") or entry.get("status", "registered")
            detail = detail_value.replace("|", "/").replace("\n", " ")
            lines.append(f"| `{source_id}` | {entry.get('evidence_lane','')} | {entry.get('authority_tier','')} | {entry.get('refresh_class','')} | {status} | {detail[:240]} |")
        lines.extend([
            "",
            "## Promotion contract",
            "",
            "`captured → attributed claim → evidence classified → compared → tested → doctrine candidate → locally validated protocol`",
            "",
            "Source weight governs promotion and retrieval weighting, not whether the information is processed.",
            "",
            "## Known gaps",
            "",
        ])
        for gap in self.registry.get("known_gaps", []):
            lines.append(f"- {gap}")
        lines.append("")
        (self.domain_root / "coverage-ledger.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Shared GBrain corpus registry engine")
    parser.add_argument("action", choices=["validate", "refresh"])
    parser.add_argument("--domain", required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    engine = CorpusEngine(args.domain)
    if args.action == "validate":
        print(json.dumps({"ok": True, "domain": engine.domain, "sources": len(engine.registry.get("sources", [])), "registry": str(engine.registry_path)}))
        return
    results = engine.refresh(force=args.force, only=set(args.only) or None)
    print(json.dumps({"domain": engine.domain, "results": [r.__dict__ for r in results]}, indent=2, ensure_ascii=False))
    if any(r.status == "failed" for r in results):
        sys.exit(2)


if __name__ == "__main__":
    main()
