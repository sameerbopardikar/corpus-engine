#!/usr/bin/env python3
"""Capability-bound path traversal for authoritative state files."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class PathAuthorityError(ValueError):
    """A state path could not be bound without following links."""


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise PathAuthorityError("no-follow directory opens are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | nofollow | getattr(os, "O_CLOEXEC", 0)


def _open_directory_at(parent_fd: int, component: str, *, create: bool) -> int:
    try:
        descriptor = os.open(component, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(component, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        descriptor = os.open(component, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise PathAuthorityError("state path contains a non-directory or symlink component") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise PathAuthorityError("state path contains a non-directory or symlink component")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _absolute_parts(path: Path) -> list[str]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = list(absolute.parts)
    if not parts or parts[0] != "/":
        raise PathAuthorityError("state path must resolve from an absolute root")
    if absolute.name in {"", ".", ".."}:
        raise PathAuthorityError("state path requires a leaf name")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        raise PathAuthorityError("state path contains an invalid component")
    return parts


@contextmanager
def bind_parent(path: Path, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """Bind every parent component without following links.

    ``/proc/self/fd/<n>`` is an intentional capability root used by the corpus
    transaction layer. It is handled by duplicating ``n`` rather than following
    the procfs descriptor symlink by pathname.
    """
    parts = _absolute_parts(Path(path))
    if len(parts) >= 6 and parts[1:4] == ["proc", "self", "fd"] and parts[4].isdigit():
        current = os.dup(int(parts[4]))
        try:
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise PathAuthorityError("proc descriptor root is not a directory")
        except BaseException:
            os.close(current)
            raise
        components = parts[5:-1]
    else:
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
        components = parts[1:-1]
    try:
        for component in components:
            next_fd = _open_directory_at(current, component, create=create)
            os.close(current)
            current = next_fd
        yield current, parts[-1]
    finally:
        os.close(current)


def open_regular_at(parent_fd: int, leaf: str, flags: int, *, mode: int = 0o600) -> int:
    """Open one regular leaf relative to a retained directory capability."""
    if not leaf or "/" in leaf or leaf in {".", ".."}:
        raise PathAuthorityError("invalid state leaf name")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise PathAuthorityError("no-follow file opens are unavailable")
    try:
        descriptor = os.open(
            leaf,
            flags
            | nofollow
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PathAuthorityError("state leaf must be a regular non-symlink file") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PathAuthorityError("state leaf must be a regular non-symlink file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
