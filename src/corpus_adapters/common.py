from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def preserve_bytes(root: Path, *, prefix: str, suffix: str, body: bytes) -> Path:
    """Content-address and preserve bytes without rewriting an existing artifact."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    digest = sha256_bytes(body)
    path = root / f"{prefix}-{digest[:12]}{suffix}"
    if path.exists():
        if path.read_bytes() != body:
            raise RuntimeError(f"content-address collision at {path}")
        return path
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=root)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != body:
                raise RuntimeError(f"content-address collision at {path}")
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path
