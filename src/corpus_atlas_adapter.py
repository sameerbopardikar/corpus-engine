#!/usr/bin/env python3
"""Instantiate the existing environment-configurable Research Atlas release."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_ATLAS_RELEASE = Path("/opt/training-research-atlas/current")


class AtlasAdapterError(RuntimeError):
    """Raised when the Atlas cannot be configured, launched, or verified."""


def select_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK_HOST, 0))
        return int(sock.getsockname()[1])


def _product_name(title: str) -> str:
    base = title.strip()
    for suffix in (" Research Corpus", " Corpus"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip()
            break
    return f"{base} Research Atlas"


def build_atlas_launch_config(
    *,
    run_root: Path | str,
    domain: str,
    title: str,
    host: str = LOOPBACK_HOST,
    port: int | None = None,
    port_selector: Callable[[], int] | None = None,
    release_root: Path | str = DEFAULT_ATLAS_RELEASE,
    corpus_root: Path | str | None = None,
) -> dict[str, Any]:
    """Derive a secret-free launch configuration for the shared Atlas release."""
    if not domain or not title:
        raise AtlasAdapterError("domain and title are required")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise AtlasAdapterError("Atlas proof launches must bind to loopback")
    run_root = Path(run_root)
    corpus_root = Path(corpus_root) if corpus_root is not None else run_root / "corpora" / domain
    release_root = Path(release_root)
    if port is None:
        port = (port_selector or select_free_port)()
    port = int(port)
    if not 0 < port < 65536:
        raise AtlasAdapterError(f"invalid port: {port}")

    product_name = _product_name(title)
    intake_root = run_root / ".corpus-start" / "atlas-intake"
    env = {
        "CORPUS_ROOT": str(corpus_root),
        "ATLAS_PRODUCT_NAME": product_name,
        "ATLAS_AUTH_REALM": product_name,
        "CORPUS_QUEUE_PATH": "discovery/domain-v1/latest.json",
        "CORPUS_ACQUISITION_PATH": "acquisition/latest.json",
        "CORPUS_INTAKE_ROOT": str(intake_root),
        "CORPUS_INTAKE_CLI": "/usr/local/bin/corpus-intake",
        "CACHE_MS": "0",
        "HOST": host,
        "PORT": str(port),
    }
    base_url = f"http://{host}:{port}"
    return {
        "host": host,
        "port": port,
        "domain": domain,
        "product_name": product_name,
        "release_root": str(release_root),
        "env": env,
        "base_url": base_url,
        "health_url": f"{base_url}/healthz",
        "ready_url": f"{base_url}/readyz",
        "index_url": f"{base_url}/api/index",
        "acquisition_url": f"{base_url}/api/acquisition",
    }


ProbeFn = Callable[[str], "tuple[int, dict[str, Any]]"]


class _AtlasProcess:
    def __init__(self, process: subprocess.Popen[str], username: str, password: str):
        self.process = process
        self._authorization = "Basic " + base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")

    def probe(self, url: str) -> tuple[int, dict[str, Any]]:
        return _default_probe(url, authorization=self._authorization)

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def _poll_ok(probe: ProbeFn, url: str, *, retries: int, retry_sleep: Callable[[float], None]) -> bool:
    for attempt in range(max(1, retries)):
        try:
            status, _ = probe(url)
        except Exception:
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
    """Launch, authenticate to, verify, and optionally stop one Atlas instance."""
    handle = (launcher or _default_launcher)(config)
    active_probe = probe or getattr(handle, "probe", None) or _default_probe
    try:
        if not _poll_ok(active_probe, config["health_url"], retries=retries, retry_sleep=retry_sleep):
            raise AtlasAdapterError("Atlas /healthz never returned 200")
        if not _poll_ok(active_probe, config["ready_url"], retries=retries, retry_sleep=retry_sleep):
            raise AtlasAdapterError("Atlas /readyz never returned 200")

        status, index = active_probe(config["index_url"])
        if status != 200 or not isinstance(index, dict):
            raise AtlasAdapterError("Atlas /api/index did not return a JSON object")
        actual_product = index.get("product", {}).get("name") if isinstance(index.get("product"), dict) else index.get("product_name")
        if actual_product != config["product_name"]:
            raise AtlasAdapterError(
                f"Atlas product mismatch: {actual_product!r} != {config['product_name']!r}"
            )

        status, acquisition = active_probe(config["acquisition_url"])
        if status != 200 or not isinstance(acquisition, dict):
            raise AtlasAdapterError("Atlas /api/acquisition did not return a JSON object")
        if acquisition.get("domain") != config["domain"]:
            raise AtlasAdapterError(
                f"Atlas domain mismatch: {acquisition.get('domain')!r} != {config['domain']!r}"
            )
    except Exception:
        _safe_stop(handle)
        raise

    if stop:
        _safe_stop(handle)

    receipt = {
        "atlas_ready": True,
        "healthz": True,
        "readyz": True,
        "index_verified": True,
        "acquisition_verified": True,
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
        except Exception:
            pass


def _default_launcher(config: dict[str, Any]) -> _AtlasProcess:  # pragma: no cover - exercised in live proof
    release = Path(config["release_root"]).resolve()
    server = release / "server.mjs"
    if not server.is_file() or not (release / "dist" / "index.html").is_file():
        raise AtlasAdapterError(f"immutable Atlas release is incomplete: {release}")
    username = "corpus-start-proof"
    password = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in config["env"].items()})
    env["DASHBOARD_USER"] = username
    env["DASHBOARD_PASSWORD"] = password
    process = subprocess.Popen(
        ["/usr/bin/node", str(server)],
        cwd=str(release), env=env, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return _AtlasProcess(process, username, password)


def _default_probe(
    url: str, *, authorization: str | None = None
) -> tuple[int, dict[str, Any]]:  # pragma: no cover - exercised in live proof
    import urllib.request

    request = urllib.request.Request(url)
    if authorization:
        request.add_header("Authorization", authorization)
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        return int(response.status), body if isinstance(body, dict) else {}
