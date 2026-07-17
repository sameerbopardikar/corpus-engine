from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
EVIDENCE_CLASS = "unverified_discovery_signal"
_ALLOWED_FIELDS = {
    "author",
    "published_at",
    "url",
    "text",
    "thread_id",
    "linked_primary_sources",
}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


class XSignalContractError(ValueError):
    """The deterministic X radar input or durable projection is invalid."""


def _text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise XSignalContractError(f"{name} must be non-blank text")
    if len(value) > maximum:
        raise XSignalContractError(f"{name} exceeds {maximum} characters")
    return value


def _aware_iso(value: Any) -> str:
    value = _text("published_at", value, maximum=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise XSignalContractError("published_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise XSignalContractError("published_at must include a timezone")
    return value


def _http_url(name: str, value: Any) -> str:
    value = _text(name, value, maximum=4096)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise XSignalContractError(f"{name} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise XSignalContractError(f"{name} must not contain credentials")
    return value


def _normalize_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise XSignalContractError("each signal must be an object")
    unknown = set(value) - _ALLOWED_FIELDS
    missing = _ALLOWED_FIELDS - set(value)
    if unknown:
        raise XSignalContractError(f"unknown signal fields: {sorted(unknown)}")
    if missing:
        raise XSignalContractError(f"missing signal fields: {sorted(missing)}")

    url = _http_url("url", value["url"])
    if (urlsplit(url).hostname or "").lower() not in _X_HOSTS:
        raise XSignalContractError("url must identify an X/Twitter post")

    links = value["linked_primary_sources"]
    if not isinstance(links, list):
        raise XSignalContractError("linked_primary_sources must be a list")
    if len(links) > 20:
        raise XSignalContractError("linked_primary_sources exceeds 20 URLs")
    normalized_links: set[str] = set()
    for link in links:
        link = _http_url("linked primary-source URL", link)
        if (urlsplit(link).hostname or "").lower() in _X_HOSTS:
            raise XSignalContractError("linked primary-source URLs cannot be X/Twitter signals")
        normalized_links.add(link)

    return {
        "author": _text("author", value["author"], maximum=256),
        "published_at": _aware_iso(value["published_at"]),
        "url": url,
        "text": _text("text", value["text"], maximum=100_000),
        "thread_id": _text("thread_id", value["thread_id"], maximum=512),
        "linked_primary_sources": sorted(normalized_links),
        "evidence_class": EVIDENCE_CLASS,
        "doctrine_eligible": False,
    }


def _read_existing(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"non-finite value: {item}")),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise XSignalContractError(f"invalid existing X projection: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("evidence_class") != EVIDENCE_CLASS
        or value.get("doctrine_eligible") is not False
        or not isinstance(value.get("signals"), list)
        or not isinstance(value.get("threads"), list)
    ):
        raise XSignalContractError("invalid existing X projection schema")
    return value


def _project(signals_by_url: dict[str, dict[str, Any]]) -> dict[str, Any]:
    signals = sorted(signals_by_url.values(), key=lambda item: (item["published_at"], item["url"]))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        grouped.setdefault(signal["thread_id"], []).append(signal)

    threads = []
    for thread_id in sorted(grouped):
        members = grouped[thread_id]
        threads.append(
            {
                "thread_id": thread_id,
                "signal_urls": [item["url"] for item in members],
                "linked_primary_sources": sorted(
                    {link for item in members for link in item["linked_primary_sources"]}
                ),
                "evidence_class": EVIDENCE_CLASS,
                "doctrine_eligible": False,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "doctrine_eligible": False,
        "signals": signals,
        "threads": threads,
    }


def _atomic_write(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
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


def ingest_x_signals(signals: Iterable[dict[str, Any]], output_path: Path, *, max_items: int = 100) -> dict[str, Any]:
    """Merge a bounded X radar batch into one deterministic, doctrine-ineligible projection.

    X posts are preserved verbatim as discovery signals. External links are only
    candidate primary-source locators; this function neither acquires them nor
    promotes any X-only claim into doctrine.
    """

    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 100:
        raise XSignalContractError("max_items must be an integer from 1 through 100")
    material = list(signals)
    if len(material) > max_items:
        raise XSignalContractError("signal batch exceeds max_items")
    normalized = [_normalize_signal(item) for item in material]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path.with_suffix(output_path.suffix + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, "r+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        existing = _read_existing(output_path)
        by_url: dict[str, dict[str, Any]] = {}
        if existing is not None:
            for signal in existing["signals"]:
                if not isinstance(signal, dict) or not isinstance(signal.get("url"), str):
                    raise XSignalContractError("invalid signal in existing X projection")
                by_url[signal["url"]] = signal

        for signal in normalized:
            prior = by_url.get(signal["url"])
            if prior is not None and prior != signal:
                raise XSignalContractError(f"conflicting duplicate URL: {signal['url']}")
            by_url[signal["url"]] = signal

        projection = _project(by_url)
        encoded = (json.dumps(projection, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if output_path.exists() and output_path.read_bytes() == encoded:
            return projection
        _atomic_write(output_path, encoded)
        return projection
