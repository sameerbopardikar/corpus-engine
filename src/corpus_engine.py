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
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

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
    """True when a run checked or changed at least one due source."""
    return any(result.status != "not_due" for result in results)


def vtt_to_text(raw: str) -> str:
    lines: list[str] = []
    seen = ""
    for line in raw.replace("\r", "").splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or line.startswith("WEBVTT") or "-->" in line or line.isdigit() or line.startswith(("Kind:", "Language:")):
            continue
        line = html.unescape(line)
        if line != seen:
            lines.append(line)
            seen = line
    return "\n".join(lines).strip() + "\n"


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

    def refresh_web(self, entry: dict[str, Any]) -> RefreshResult:
        response = request(entry["url"])
        payload = response.content
        digest = sha256_bytes(payload)
        raw_dir = self.source_dir(entry["id"])
        raw_dir.mkdir(parents=True, exist_ok=True)
        ext = ".md" if "markdown" in response.headers.get("content-type", "") or response.url.endswith(".md") else ".html"
        raw_path = raw_dir / f"snapshot-{digest[:12]}{ext}"
        raw_path.write_bytes(payload)
        if ext == ".md":
            text = payload.decode("utf-8", errors="replace")
            title = entry.get("title", entry["id"])
        else:
            parser = MainTextParser()
            parser.feed(payload.decode(response.encoding or "utf-8", errors="replace"))
            text = parser.text()
            title = entry.get("title") or parser.title or entry["id"]
        if len(text) < entry.get("minimum_text_chars", 500):
            raise RuntimeError(f"normalized text too short: {len(text)} chars")
        normalized_path = raw_dir / f"normalized-{digest[:12]}.txt"
        normalized_path.write_text(text, encoding="utf-8")
        excerpt_chars = int(entry.get("card_excerpt_chars", 24000))
        body = (
            f"> **Epistemic boundary:** {entry.get('epistemic_note', 'This is attributed external evidence, not settled doctrine.')}\n\n"
            f"Canonical source: <{response.url}>\n\n"
            f"## Normalized source snapshot\n\n{text[:excerpt_chars]}"
        )
        slug = self.write_card(entry, title, body, response.url, raw_path, digest)
        return RefreshResult(entry["id"], "refreshed", [slug], f"{len(text)} normalized chars", digest, response.url)

    def refresh_github(self, entry: dict[str, Any]) -> RefreshResult:
        repo = entry["repo"]
        meta_response = request(f"https://api.github.com/repos/{repo}")
        meta = meta_response.json()
        branch = meta.get("default_branch", "main")
        readme_response = request(f"https://api.github.com/repos/{repo}/readme")
        readme_json = readme_response.json()
        download_url = readme_json.get("download_url")
        readme = request(download_url).text if download_url else ""
        commits = request(f"https://api.github.com/repos/{repo}/commits?per_page=1").json()
        head = commits[0].get("sha") if isinstance(commits, list) and commits else None
        snapshot = {"repository": repo, "head": head, "metadata": meta, "readme": readme}
        payload = (json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        digest = sha256_bytes(payload)
        raw_dir = self.source_dir(entry["id"])
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"snapshot-{digest[:12]}.json"
        raw_path.write_bytes(payload)
        body = (
            f"> **Epistemic boundary:** Repository source and maintainer documentation. Runtime behavior still requires code-path inspection and testing.\n\n"
            f"Repository: <https://github.com/{repo}>\n\n"
            f"Observed head: `{head or 'unknown'}` on `{branch}`.\n\n"
            f"## README snapshot\n\n{readme[:24000]}"
        )
        slug = self.write_card(entry, entry.get("title", repo), body, f"https://github.com/{repo}", raw_path, digest, extra={"repository_head": head or "unknown"})
        return RefreshResult(entry["id"], "refreshed", [slug], f"head {head}", digest, f"https://github.com/{repo}")

    def acquire_youtube_transcript(self, video_id: str, destination: Path) -> tuple[Path | None, str | None]:
        destination.mkdir(parents=True, exist_ok=True)
        template = str(destination / f"{video_id}.%(ext)s")
        cmd = [
            "yt-dlp", "--cookies-from-browser", "chromium:/root/.hermes/browser-profiles/youtube-takeout-sameer",
            "--ignore-no-formats", "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", "en-orig,en,en.*", "--sub-format", "vtt", "-o", template,
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=180)
        candidates = sorted(destination.glob(f"{video_id}*.vtt"))
        if not candidates:
            return None, (proc.stderr or proc.stdout)[-1200:]
        raw_path = candidates[0]
        clean_path = destination / f"{video_id}.transcript.txt"
        clean_path.write_text(vtt_to_text(raw_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        return clean_path, None

    def refresh_youtube(self, entry: dict[str, Any]) -> RefreshResult:
        channel_id = entry["channel_id"]
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        response = request(feed_url)
        digest = sha256_bytes(response.content)
        raw_dir = self.source_dir(entry["id"])
        raw_dir.mkdir(parents=True, exist_ok=True)
        feed_path = raw_dir / f"feed-{digest[:12]}.xml"
        feed_path.write_bytes(response.content)
        ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}
        root = ET.fromstring(response.content)
        items: list[dict[str, str]] = []
        for node in root.findall("atom:entry", ns):
            video_id = node.findtext("yt:videoId", default="", namespaces=ns)
            title = node.findtext("atom:title", default=video_id, namespaces=ns)
            published = node.findtext("atom:published", default="", namespaces=ns)
            link_node = node.find("atom:link", ns)
            url = link_node.attrib.get("href") if link_node is not None else f"https://www.youtube.com/watch?v={video_id}"
            items.append({"video_id": video_id, "title": title, "published": published, "url": url})
        pages: list[str] = []
        transcript_count = 0
        max_items = int(entry.get("max_items_per_refresh", 3))
        for item in items[:max_items]:
            transcript_path: Path | None = None
            transcript_error: str | None = None
            if entry.get("acquire_transcripts", True):
                transcript_path, transcript_error = self.acquire_youtube_transcript(item["video_id"], raw_dir / "transcripts")
            transcript = transcript_path.read_text(encoding="utf-8") if transcript_path else ""
            if transcript_path:
                transcript_count += 1
            item_digest = sha256_bytes(transcript.encode()) if transcript else None
            boundary = "Practitioner evidence. A demonstrated workflow is stronger than an unsupported claim, but neither becomes doctrine without comparison or local verification."
            body = (
                f"> **Epistemic boundary:** {boundary}\n\n"
                f"Channel: **{entry.get('title', entry['id'])}**\nPublished: `{item['published']}`\nVideo: <{item['url']}>\n\n"
                f"Transcript status: **{'acquired' if transcript_path else 'pending'}**.\n"
            )
            if transcript_error and not transcript_path:
                body += "\nCaption acquisition did not produce a transcript in this pass; the shared fallback ladder remains queued.\n"
            if transcript:
                body += f"\n## Transcript\n\n{transcript[:60000]}"
            pages.append(self.write_card(entry, item["title"], body, item["url"], transcript_path or feed_path, item_digest or digest, suffix=item["video_id"], extra={"video_id": item["video_id"], "published_at": item["published"], "transcript_status": "acquired" if transcript_path else "pending"}))
        channel_body = (
            f"> **Epistemic boundary:** Practitioner/operator lane. Source credibility is evaluated per topic and claim.\n\n"
            f"Channel feed: <{feed_url}>\n\n"
            f"Items observed this refresh: **{len(items)}**. Cards processed: **{min(len(items), max_items)}**. Transcripts acquired: **{transcript_count}**."
        )
        pages.append(self.write_card(entry, entry.get("title", entry["id"]), channel_body, feed_url, feed_path, digest))
        return RefreshResult(entry["id"], "refreshed", pages, f"{len(items)} feed items; {transcript_count} transcripts", digest, feed_url)

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

    def refresh_entry(self, entry: dict[str, Any]) -> RefreshResult:
        source_type = entry["source_type"]
        if source_type == "web_document":
            return self.refresh_web(entry)
        if source_type == "github_repository":
            return self.refresh_github(entry)
        if source_type == "youtube_channel":
            return self.refresh_youtube(entry)
        if source_type in {"x_discovery", "private_community", "discovery_feed"}:
            return self.refresh_pointer(entry)
        raise RuntimeError(f"unsupported source_type: {source_type}")

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
                result = self.refresh_entry(entry)
                prior.update({"last_checked_at": iso(), "last_success_at": iso(), "last_status": result.status, "last_detail": result.detail, "last_error": None, "content_hash": result.content_hash, "canonical_url": result.canonical_url, "pages": result.pages})
            except Exception as exc:
                result = RefreshResult(source_id, "failed", [], f"{type(exc).__name__}: {exc}")
                prior.update({"last_checked_at": iso(), "last_status": "failed", "last_error": result.detail})
            results.append(result)
        # A scheduler run before any refresh window opens is a read-only
        # no-op. Avoid dirtying state/coverage timestamps and overwriting
        # curated coverage detail on every routine cycle.
        if has_material_refresh_results(results):
            self.state["updated_at"] = iso()
            write_json(self.state_path, self.state)
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
