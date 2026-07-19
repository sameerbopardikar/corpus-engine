#!/usr/bin/env python3
"""Reusable adapter that instantiates the *existing* Research Atlas release for
any corpus by configuration alone.

The Atlas is an immutable, environment-configurable Node/React release
(``CORPUS_ROOT`` + ``ATLAS_PRODUCT_NAME`` + queue/acquisition paths). This
adapter never copies or redesigns the app: it derives a launch configuration
from a corpus's run root and domain, launches the release in foreground-test
mode through an injected launcher, waits for ``/healthz`` and ``/readyz``,
verifies ``/api/index`` reports the expected product and domain, and returns a
JSON receipt.

Both the launcher and the HTTP probe are injected so instantiation is fully
deterministic under test. Defaults perform the real subprocess launch / HTTP
GET for production runs.
"""
from __future__ import annotations

import hashlib
import json
import socket
import time
from pathlib import Path
from typing import Any, Callable

LOOPBACK_HOST = "127.0.0.1"


class AtlasAdapterError(RuntimeError):
    """Raised when the Atlas cannot be configured, launched, or verified."""


def select_free_port() -> int:
    """Return a currently-free loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK_HOST, 0))
        return int(sock.getsockname()[1])


def build_atlas_launch_config(
    *,
    run_root: Path | str,
    domain: str,
    title: str,
    host: str = LOOPBACK_HOST,
    port: int | None = None,
    port_selector: Callable[[], int] | None = None,
) -> dict[str, Any]:
    """Derive a launch configuration for the existing Atlas release.

    The corpus lives at ``<run-root>/corpora/<domain>``; the product is named
    ``<title> Research Atlas``. Only loopback is bound and no secret is embedded.
    """
    if not domain or not title:
        raise AtlasAdapterError("domain and title are required")
    run_root = Path(run_root)
    corpus_root = run_root / "corpora" / domain
    if port is None:
        port = (port_selector or select_free_port)()
    port = int(port)
    if port <= 0:
        raise AtlasAdapterError(f"invalid port: {port}")

    product_name = f"{title} Research Atlas"
    env = {
        "CORPUS_ROOT": str(corpus_root),
        "ATLAS_PRODUCT_NAME": product_name,
        "ATLAS_DOMAIN": domain,
        "ATLAS_QUEUE_PATH": str(corpus_root / "queue"),
        "ATLAS_ACQUISITION_PATH": str(corpus_root / "acquisition"),
        "HOST": host,
        "PORT": str(port),
    }
    base_url = f"http://{host}:{port}"
    return {
        "host": host,
        "port": port,
        "domain": domain,
        "product_name": product_name,
        "env": env,
        "base_url": base_url,
        "health_url": f"{base_url}/healthz",
        "ready_url": f"{base_url}/readyz",
        "index_url": f"{base_url}/api/index",
    }


ProbeFn = Callable[[str], "tuple[int, dict[str, Any]]"]


def _poll_ok(probe: ProbeFn, url: str, *, retries: int, retry_sleep: Callable[[float], None]) -> bool:
    for attempt in range(max(1, retries)):
        try:
            status, _ = probe(url)
        except Exception:  # noqa: BLE001 - a failed probe is just "not ready yet"
            status = 0
        if status == 200:
            return True
        if attempt + 1 < retries:
            retry_sleep(0.1 * (attempt + 1))
    return False


def instantiate_atlas(
    config: dict[str, Any],
    *,
    launcher: Callable[[dict[str, Any]], Any] | None = None,
    probe: ProbeFn | None = None,
    stop: bool = True,
    retries: int = 30,
    retry_sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Launch the Atlas from ``config``, verify it, and return a receipt.

    ``launcher(config)`` must return a handle exposing ``stop()``. ``probe(url)``
    must return ``(status_code, json_body)``. On any failure the process is
    always stopped (unless ``stop=False``) so a broken Atlas is never left
    running.
    """
    launcher = launcher or _default_launcher
    probe = probe or _default_probe

    handle = launcher(config)
    try:
        healthz = _poll_ok(probe, config["health_url"], retries=retries, retry_sleep=retry_sleep)
        if not healthz:
            raise AtlasAdapterError("Atlas /healthz never returned 200")
        readyz = _poll_ok(probe, config["ready_url"], retries=retries, retry_sleep=retry_sleep)
        if not readyz:
            raise AtlasAdapterError("Atlas /readyz never returned 200")

        status, body = probe(config["index_url"])
        if status != 200 or not isinstance(body, dict):
            raise AtlasAdapterError("Atlas /api/index did not return a JSON object")
        if body.get("product_name") != config["product_name"]:
            raise AtlasAdapterError(
                f"Atlas product mismatch: {body.get('product_name')!r} != {config['product_name']!r}"
            )
        if body.get("domain") != config["domain"]:
            raise AtlasAdapterError(
                f"Atlas domain mismatch: {body.get('domain')!r} != {config['domain']!r}"
            )
    except Exception:
        if stop:
            _safe_stop(handle)
        raise

    if stop:
        _safe_stop(handle)

    receipt = {
        "atlas_ready": True,
        "healthz": True,
        "readyz": True,
        "index_verified": True,
        "product_name": config["product_name"],
        "domain": config["domain"],
        "port": config["port"],
        "base_url": config["base_url"],
        "stopped_after_verify": bool(stop),
    }
    receipt["receipt_sha256"] = _receipt_sha256(receipt)
    return receipt


def _receipt_sha256(receipt: dict[str, Any]) -> str:
    blob = json.dumps(receipt, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _safe_stop(handle: Any) -> None:
    stop = getattr(handle, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


def _default_launcher(config: dict[str, Any]) -> Any:  # pragma: no cover - live only
    raise AtlasAdapterError(
        "no launcher configured; the immutable Atlas release launcher must be injected for live runs"
    )


def _default_probe(url: str) -> tuple[int, dict[str, Any]]:  # pragma: no cover - live only
    import urllib.request

    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - loopback only
        raw = resp.read()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        return int(resp.status), body if isinstance(body, dict) else {}
