from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

import requests

from .base import SourceAdapter
from .common import preserve_bytes, sha256_bytes
from .types import InventoryRequest, NormalizedObservation, SourceSpec, TransportPayload


_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def _default_get(url: str, timeout: float):
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "GBrainCorpusEngine/1.0"})
    response.raise_for_status()
    return response


def parse_feed(body: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(body)
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in root.findall("atom:entry", _NS):
        video_id = node.findtext("yt:videoId", default="", namespaces=_NS).strip()
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        title = node.findtext("atom:title", default=video_id, namespaces=_NS).strip() or video_id
        published = node.findtext("atom:published", default="", namespaces=_NS).strip()
        link_node = node.find("atom:link", _NS)
        url = link_node.attrib.get("href", "") if link_node is not None else ""
        if not url.startswith("https://"):
            url = f"https://www.youtube.com/watch?v={video_id}"
        items.append({"video_id": video_id, "title": title, "published": published, "url": url})
    return items


def parse_inventory(body: bytes) -> list[dict[str, str]]:
    """Parse either the native Atom feed or bounded yt-dlp JSON fallback."""
    if not body.lstrip().startswith(b"{"):
        return parse_feed(body)
    document = json.loads(body)
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("YouTube inventory JSON must contain entries")
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("YouTube inventory entry must be an object")
        video_id = entry.get("id")
        if not isinstance(video_id, str) or not video_id.strip() or video_id in seen:
            continue
        seen.add(video_id)
        title = entry.get("title")
        url = entry.get("url")
        upload_date = entry.get("upload_date")
        items.append(
            {
                "video_id": video_id,
                "title": title.strip() if isinstance(title, str) and title.strip() else video_id,
                "published": upload_date if isinstance(upload_date, str) else "",
                "url": url if isinstance(url, str) and url.startswith("https://") else f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return items


def _yt_dlp_inventory(channel_id: str, max_items: int, timeout: float) -> bytes:
    command = [
        "yt-dlp",
        "--no-warnings",
        "--flat-playlist",
        "--playlist-end",
        str(max_items),
        "--dump-single-json",
        f"https://www.youtube.com/channel/{channel_id}/videos",
    ]
    completed = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return completed.stdout


def inventory_revision(items: list[dict[str, str]]) -> str:
    # Use the stable intersection available from both native Atom feeds and
    # bounded public-inventory fallback. Transport-only fields (including a
    # missing yt-dlp upload date) must not manufacture a source revision.
    stable_items = [
        {key: item[key] for key in ("video_id", "title", "url")}
        for item in items
    ]
    stable = json.dumps(stable_items, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256_bytes(stable)


def normalize_caption_vtt(raw: str) -> str:
    """Normalize native VTT only; reject page summaries masquerading as transcripts."""
    if not raw.lstrip("\ufeff").startswith("WEBVTT"):
        raise ValueError("caption artifact must begin with WEBVTT")
    lines: list[str] = []
    for source_line in raw.replace("\r", "").splitlines():
        line = re.sub(r"<[^>]+>", "", source_line).strip()
        if not line or line.startswith("WEBVTT") or "-->" in line or line.isdigit() or line.startswith(("Kind:", "Language:")):
            continue
        line = html.unescape(line)
        if lines and line == lines[-1]:
            continue
        if lines and line.startswith(lines[-1]):
            lines[-1] = line
            continue
        if lines and lines[-1].startswith(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def write_revision_guarded(path: Path, content: bytes, *, source_revision: str, prior_revision: str | None) -> bool:
    """Write a projection only for a real source revision delta."""
    if prior_revision == source_revision:
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


class YouTubeFeedAdapter(SourceAdapter):
    family = "youtube"

    def __init__(
        self,
        raw_root: Path,
        *,
        channel_id: str,
        http_get: Callable = _default_get,
        inventory_fetch: Callable[[str, int, float], bytes] = _yt_dlp_inventory,
        fetched_at: Callable[[], str],
    ):
        if not channel_id.strip():
            raise ValueError("channel_id must be non-blank")
        self.raw_root = Path(raw_root)
        self.channel_id = channel_id
        self.http_get = http_get
        self.inventory_fetch = inventory_fetch
        self.fetched_at = fetched_at

    def fetch(self, spec: SourceSpec, request: InventoryRequest) -> TransportPayload:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={self.channel_id}"
        try:
            response = self.http_get(feed_url, request.timeout_seconds)
            body = response.content
            final_url = response.url
            suffix = ".xml"
            items = parse_feed(body)
        except Exception:
            body = self.inventory_fetch(self.channel_id, request.max_items, request.timeout_seconds)
            final_url = f"https://www.youtube.com/channel/{self.channel_id}/videos"
            suffix = ".json"
            items = parse_inventory(body)
        revision = inventory_revision(items)
        raw_path = preserve_bytes(self.raw_root, prefix="feed", suffix=suffix, body=body)
        return TransportPayload(
            body=body,
            final_url=final_url,
            fetched_at=self.fetched_at(),
            source_revision=revision,
            raw_pointer=str(raw_path),
        )

    def parse(self, spec: SourceSpec, request: InventoryRequest, payload: TransportPayload):
        items = parse_inventory(payload.body)
        if inventory_revision(items) != payload.source_revision:
            raise ValueError("feed inventory revision mismatch")
        bounded = items[: request.max_items]
        observations: list[NormalizedObservation] = []
        for item in bounded:
            normalized = (json.dumps(item, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            normalized_path = preserve_bytes(self.raw_root / "metadata", prefix=item["video_id"], suffix=".json", body=normalized)
            observations.append(
                NormalizedObservation(
                    canonical_locator=item["url"],
                    title=item["title"],
                    evidence_pointer=payload.final_url,
                    raw_pointer=payload.raw_pointer,
                    raw_sha256=payload.raw_sha256,
                    normalized_pointer=str(normalized_path),
                    normalized_sha256=sha256_bytes(normalized),
                    content_kind="video_metadata",
                    fetched_at=payload.fetched_at,
                    source_revision=payload.source_revision,
                    rights_state=spec.rights_state,
                )
            )
        cursor = request.cursor
        if bounded:
            cursor = f"{bounded[0]['published']}|{bounded[0]['video_id']}"
        return tuple(observations), cursor
