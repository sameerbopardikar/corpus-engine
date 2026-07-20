#!/usr/bin/env python3
"""Canonical, provenance-preserving intake contract for thinker corpora."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from defusedxml.ElementTree import fromstring as safe_fromstring

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
RIGHTS_STATES = {"public", "owned", "licensed", "unknown"}
SAFE_EXTENSIONS = {".txt", ".md", ".markdown", ".json", ".csv", ".pdf", ".docx"}
ORIGIN_KEYS = {"platform", "channel_id", "channel_name", "thread_ts", "message_ts", "message_id", "file_id", "user_id"}
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class IntakeError(ValueError):
    pass


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_root(path: Path) -> Path:
    path = path.absolute()
    if path.is_symlink():
        raise IntakeError("intake root must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    if not path.is_dir():
        raise IntakeError("intake root is not a directory")
    return path


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_title(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise IntakeError("title must be 1-240 characters")
    if any(ord(ch) < 32 and ch not in "\t" for ch in value) or value.strip() in {".", "..", "../"}:
        raise IntakeError("title contains unsafe characters")
    return value


def _validate_url(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 4096:
        raise IntakeError("source_url is malformed")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise IntakeError("source_url must be an http(s) URL without credentials")
    return value


def _validate_origin(origin: dict[str, Any] | None) -> dict[str, str]:
    if origin is None:
        return {"platform": "cli"}
    if not isinstance(origin, dict) or set(origin) - ORIGIN_KEYS:
        raise IntakeError("origin contains unsupported metadata")
    clean: dict[str, str] = {}
    for key, value in origin.items():
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            raise IntakeError(f"origin.{key} is malformed")
        clean[key] = value
    clean.setdefault("platform", "cli")
    return clean


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:72]
    return result or "submission"


def _yaml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


class IntakeStore:
    def __init__(self, root: Path | str, *, max_upload_bytes: int = MAX_UPLOAD_BYTES):
        self.root = _safe_root(Path(root))
        self.max_upload_bytes = int(max_upload_bytes)
        if self.max_upload_bytes <= 0:
            raise IntakeError("max_upload_bytes must be positive")
        self.content_root = self.root / "content"
        self.content_root.mkdir(mode=0o750, exist_ok=True)
        self.evaluation_root = self.root / "evaluations"
        self.evaluation_root.mkdir(mode=0o750, exist_ok=True)
        self.db_path = self.root / "intake.sqlite3"
        if self.db_path.is_symlink():
            raise IntakeError("intake database must not be a symlink")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS submissions (
                  submission_id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                  submitted_text BLOB, source_url TEXT, sha256 TEXT NOT NULL,
                  mime_type TEXT, filename TEXT, rights_state TEXT NOT NULL,
                  blob_path TEXT, created_at TEXT NOT NULL, state TEXT NOT NULL,
                  corpus_path TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS receipts (
                  receipt_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
                  origin_json TEXT NOT NULL, received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
                  status TEXT NOT NULL, at TEXT NOT NULL, detail TEXT
                );
                CREATE INDEX IF NOT EXISTS receipts_received_idx ON receipts(received_at DESC);
            """)
        os.chmod(self.db_path, 0o640)

    def _store_blob(self, digest: str, data: bytes) -> str:
        directory = self.content_root / digest[:2]
        directory.mkdir(mode=0o750, exist_ok=True)
        target = directory / digest
        if target.is_symlink():
            raise IntakeError("content target is a symlink")
        if target.exists():
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise IntakeError("content-address collision")
        else:
            fd, name = tempfile.mkstemp(prefix=".incoming-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(name, 0o440)
                os.replace(name, target)
                _fsync_dir(directory)
            finally:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass
        return str(target.relative_to(self.root))

    def _submit(self, *, kind: str, title: str, submitted_text: str | None, source_url: str | None,
                data: bytes, mime_type: str | None, filename: str | None, rights_state: str,
                blob_path: str | None, origin: dict[str, Any] | None) -> dict[str, Any]:
        title = _validate_title(title)
        source_url = _validate_url(source_url)
        origin_clean = _validate_origin(origin)
        if rights_state not in RIGHTS_STATES:
            raise IntakeError("rights_state must be public, owned, licensed, or unknown")
        digest = hashlib.sha256(data).hexdigest()
        identity = json.dumps({"kind": kind, "title": title, "text_sha256": digest, "source_url": source_url,
                               "filename": filename, "rights_state": rights_state}, sort_keys=True, separators=(",", ":"))
        submission_id = "sub_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        receipt_id = "rcpt_" + uuid.uuid4().hex
        now = utc_iso()
        initial = "pending_review" if rights_state == "unknown" else "queued"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT 1 FROM submissions WHERE submission_id=?", (submission_id,)).fetchone()
            if not existing:
                db.execute("""INSERT INTO submissions
                    (submission_id,kind,title,submitted_text,source_url,sha256,mime_type,filename,rights_state,blob_path,created_at,state)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (submission_id, kind, title, submitted_text, source_url, digest, mime_type, filename,
                     rights_state, blob_path, now, initial))
                db.execute("INSERT INTO history(submission_id,status,at,detail) VALUES(?,?,?,?)",
                           (submission_id, initial, now, "submission accepted into private staging"))
            db.execute("INSERT INTO receipts(receipt_id,submission_id,origin_json,received_at) VALUES(?,?,?,?)",
                       (receipt_id, submission_id, json.dumps(origin_clean, sort_keys=True), now))
            db.commit()
        current = self.get(receipt_id)
        return {"receipt_id": receipt_id, "submission_id": submission_id, "status": current["status"],
                "duplicate": bool(existing), "sha256": digest, "blob_path": current["blob_path"], "received_at": now}

    def submit_suggestion(self, *, title: str, text: str, source_url: str | None = None,
                          origin: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > self.max_upload_bytes:
            raise IntakeError("suggestion text must be non-empty and within the size limit")
        data = text.encode("utf-8")
        return self._submit(kind="suggestion", title=title, submitted_text=text, source_url=source_url,
                            data=data, mime_type="text/plain; charset=utf-8", filename=None,
                            rights_state="owned", blob_path=None, origin=origin)

    def submit_file(self, *, title: str, file_path: Path | str, note: str | None = None,
                    source_url: str | None = None, rights_state: str = "unknown",
                    origin: dict[str, Any] | None = None, filename: str | None = None) -> dict[str, Any]:
        # Validate all metadata before writing immutable content so a rejected request
        # cannot leave an unreferenced blob behind.
        _validate_title(title)
        _validate_url(source_url)
        _validate_origin(origin)
        if rights_state not in RIGHTS_STATES:
            raise IntakeError("rights_state must be public, owned, licensed, or unknown")
        path = Path(file_path)
        if path.is_symlink():
            raise IntakeError("file symlinks are not accepted")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(path, flags)
        except OSError as exc:
            raise IntakeError(f"file cannot be opened safely: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise IntakeError("file must be a regular file")
            if info.st_size > self.max_upload_bytes:
                raise IntakeError(f"file is too large (max {self.max_upload_bytes} bytes)")
            data = b""
            while len(data) <= self.max_upload_bytes:
                block = os.read(fd, min(1024 * 1024, self.max_upload_bytes + 1 - len(data)))
                if not block:
                    break
                data += block
            if len(data) > self.max_upload_bytes:
                raise IntakeError(f"file is too large (max {self.max_upload_bytes} bytes)")
        finally:
            os.close(fd)
        submitted_filename = filename if filename is not None else path.name
        if (not isinstance(submitted_filename, str) or not submitted_filename or
                Path(submitted_filename).name != submitted_filename or len(submitted_filename) > 255 or
                any(ord(ch) < 32 for ch in submitted_filename)):
            raise IntakeError("filename is malformed")
        extension = Path(submitted_filename).suffix.lower()
        if extension not in SAFE_EXTENSIONS:
            raise IntakeError("unsupported file type")
        if data.startswith((b"#!", b"\x7fELF", b"MZ")):
            raise IntakeError("executables and scripts are not accepted")
        if extension in {".txt", ".md", ".markdown", ".json", ".csv"} and b"\x00" in data:
            raise IntakeError("text files containing NUL bytes are not accepted")
        if extension == ".pdf" and not data.startswith(b"%PDF-"):
            raise IntakeError("PDF signature does not match extension")
        if extension == ".docx":
            self._validate_docx(data)
        mime_type = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
                     ".json": "application/json", ".csv": "text/csv", ".pdf": "application/pdf",
                     ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[extension]
        if note is not None and (not isinstance(note, str) or len(note.encode("utf-8")) > 100_000):
            raise IntakeError("note is malformed or too large")
        digest = hashlib.sha256(data).hexdigest()
        blob_path = self._store_blob(digest, data)
        return self._submit(kind="file", title=title, submitted_text=note, source_url=source_url, data=data,
                            mime_type=mime_type, filename=submitted_filename, rights_state=rights_state,
                            blob_path=blob_path, origin=origin)

    @staticmethod
    def _validate_docx(data: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if len(names) > 5000 or sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
                    raise IntakeError("docx archive exceeds safety limits")
                for item in archive.infolist():
                    name = item.filename.replace("\\", "/")
                    if name.startswith("/") or ".." in Path(name).parts or item.is_dir() and name.startswith("../"):
                        raise IntakeError("docx contains an unsafe member")
                    mode = item.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise IntakeError("docx contains a symlink")
                if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                    raise IntakeError("malformed docx")
        except zipfile.BadZipFile as exc:
            raise IntakeError("malformed docx") from exc

    def _row(self, receipt_id: str) -> sqlite3.Row:
        with self._connect() as db:
            row = db.execute("""SELECT r.receipt_id,r.origin_json,r.received_at,s.* FROM receipts r
                JOIN submissions s USING(submission_id) WHERE r.receipt_id=?""", (receipt_id,)).fetchone()
        if not row:
            raise IntakeError("receipt not found")
        return row

    def get(self, receipt_id: str) -> dict[str, Any]:
        row = self._row(receipt_id)
        with self._connect() as db:
            history = [dict(item) for item in db.execute(
                "SELECT status,at,detail FROM history WHERE submission_id=? ORDER BY id", (row["submission_id"],))]
        return {"receipt_id": row["receipt_id"], "submission_id": row["submission_id"], "kind": row["kind"],
                "title": row["title"], "submitted_text": row["submitted_text"], "source_url": row["source_url"],
                "sha256": row["sha256"], "mime_type": row["mime_type"], "filename": row["filename"],
                "rights_state": row["rights_state"], "blob_path": row["blob_path"], "status": row["state"],
                "corpus_path": row["corpus_path"], "error": row["error"],
                "origin": json.loads(row["origin_json"]), "received_at": row["received_at"], "history": history}

    def list_recent(self, limit: int = 25) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise IntakeError("limit must be 1-100")
        with self._connect() as db:
            ids = [row[0] for row in db.execute("SELECT receipt_id FROM receipts ORDER BY received_at DESC,rowid DESC LIMIT ?", (limit,))]
        return [self.get(item) for item in ids]

    def _normalize(self, row: sqlite3.Row) -> str:
        if row["kind"] == "suggestion":
            return row["submitted_text"] or ""
        blob = self.root / row["blob_path"]
        if blob.is_symlink() or not blob.is_file():
            raise IntakeError("stored content is missing or unsafe")
        data = blob.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise IntakeError("stored content digest mismatch")
        ext = Path(row["filename"]).suffix.lower()
        if ext in {".txt", ".md", ".markdown", ".csv"}:
            return data.decode("utf-8")
        if ext == ".json":
            parsed = json.loads(data.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            return json.dumps(parsed, indent=2, ensure_ascii=False, allow_nan=False)
        if ext == ".docx":
            self._validate_docx(data)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                root = safe_fromstring(archive.read("word/document.xml"))
                paragraphs = []
                for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                    text = "".join(node.text or "" for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
                    if text:
                        paragraphs.append(text)
                return "\n\n".join(paragraphs)
        if ext == ".pdf":
            binary = shutil.which("pdftotext")
            if not binary:
                raise IntakeError("PDF normalization unavailable")
            with tempfile.TemporaryDirectory() as tmp:
                src, out = Path(tmp) / "input.pdf", Path(tmp) / "output.txt"
                src.write_bytes(data)
                process = subprocess.run([binary, "-layout", str(src), str(out)], capture_output=True, timeout=60)
                if process.returncode != 0:
                    raise IntakeError("PDF normalization failed")
                return out.read_text(encoding="utf-8", errors="strict")
        raise IntakeError("unsupported stored type")

    def process_next(self, *, corpus_root: Path | str) -> dict[str, Any] | None:
        corpus_root = Path(corpus_root).absolute()
        if corpus_root.is_symlink():
            raise IntakeError("corpus root must not be a symlink")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM submissions WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                db.commit()
                return None
            db.execute("UPDATE submissions SET state='processing' WHERE submission_id=?", (row["submission_id"],))
            db.execute("INSERT INTO history(submission_id,status,at,detail) VALUES(?,?,?,?)",
                       (row["submission_id"], "processing", utc_iso(), "claimed by corpus-engine cycle"))
            db.commit()
        target: Path | None = None
        target_preexisted = False
        evaluation_target: Path | None = None
        evaluation_preexisted = False
        try:
            normalized = self._normalize(row)
            receipt = self._primary_receipt(row["submission_id"])
            relative = Path("intake") / f"{_slug(row['title'])}-{row['submission_id'][4:12]}.md"
            target = corpus_root / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            target_preexisted = target.exists()
            if target_preexisted:
                raise IntakeError("corpus target already exists")
            kind = "candidate_suggestion" if row["kind"] == "suggestion" else "submitted_source"
            lines = ["---", 'type: "source"', f"title: {_yaml(row['title'])}", f"intake_kind: {_yaml(kind)}",
                     f"receipt_id: {_yaml(receipt['receipt_id'])}", f"submission_id: {_yaml(row['submission_id'])}",
                     f"submitted_at: {_yaml(row['created_at'])}", f"source_url: {_yaml(row['source_url'])}",
                     f"source_sha256: {_yaml(row['sha256'])}", f"source_filename: {_yaml(row['filename'])}",
                     f"source_mime: {_yaml(row['mime_type'])}", f"rights_state: {_yaml(row['rights_state'])}",
                     f"origin: {_yaml(json.loads(receipt['origin_json']))}", "adopted_doctrine: false",
                     'candidate_status: "intake-processed"', "---", "", f"# {row['title']}", ""]
            if row["submitted_text"]:
                lines.extend(["## Sameer suggestion", "", row["submitted_text"], ""])
            if row["kind"] == "suggestion":
                lines.extend(["## Intake boundary", "", "This is a candidate discovery signal, not external evidence and not adopted doctrine. The linked source must be independently acquired, evaluated, and ranked before it can support a claim.", ""])
            else:
                lines.extend(["## Normalized submitted source", "", normalized, "", "## Intake boundary", "",
                              "Staged source material. Processing does not imply endorsement, canonical promotion, or adoption as Sameer's doctrine.", ""])
            self._atomic_page(target, "\n".join(lines))
            card_bytes = target.read_bytes()
            card_text = card_bytes.decode("utf-8")
            expected_bytes = "\n".join(lines).encode("utf-8")
            checks = {
                "card_sha256_matches": hashlib.sha256(card_bytes).digest() == hashlib.sha256(expected_bytes).digest(),
                "source_digest_recorded": row["sha256"] in card_text,
                "provenance_recorded": row["submission_id"] in card_text and receipt["receipt_id"] in card_text,
                "doctrine_not_adopted": "adopted_doctrine: false" in card_text,
                "rights_gate_passed": row["rights_state"] in {"public", "owned", "licensed"},
            }
            evaluation = {
                "schema_version": 1,
                "submission_id": row["submission_id"],
                "receipt_id": receipt["receipt_id"],
                "corpus_path": str(relative),
                "evaluated_at": utc_iso(),
                "checks": checks,
                "passed": all(checks.values()),
            }
            if not evaluation["passed"]:
                raise IntakeError("deterministic intake evaluation failed")
            evaluation_relative = Path("evaluations") / f"{row['submission_id']}.json"
            evaluation_target = self.root / evaluation_relative
            evaluation_preexisted = evaluation_target.exists()
            if evaluation_preexisted:
                raise IntakeError("evaluation target already exists")
            self._atomic_json(evaluation_target, evaluation)
            now = utc_iso()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE submissions SET state='processed',corpus_path=?,error=NULL WHERE submission_id=?",
                           (str(relative), row["submission_id"]))
                db.execute("INSERT INTO history(submission_id,status,at,detail) VALUES(?,?,?,?)",
                           (row["submission_id"], "processed", now, f"corpus card: {relative}"))
                db.commit()
            return {"receipt_id": receipt["receipt_id"], "submission_id": row["submission_id"],
                    "status": "processed", "corpus_path": str(relative),
                    "evaluation_receipt": str(evaluation_relative), "processed_at": now}
        except Exception as exc:
            cleanup_errors = []
            for artifact, preexisted in (
                (target, target_preexisted),
                (evaluation_target, evaluation_preexisted),
            ):
                if artifact is None or preexisted or not artifact.exists() or artifact.is_symlink():
                    continue
                try:
                    artifact.unlink()
                    _fsync_dir(artifact.parent)
                except OSError as cleanup_exc:
                    cleanup_errors.append(f"{artifact}: {cleanup_exc}")
            if cleanup_errors:
                exc = IntakeError(f"{exc}; rejected-artifact cleanup failed: {'; '.join(cleanup_errors)}")
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE submissions SET state='rejected',error=? WHERE submission_id=?", (str(exc)[:500], row["submission_id"]))
                db.execute("INSERT INTO history(submission_id,status,at,detail) VALUES(?,?,?,?)",
                           (row["submission_id"], "rejected", utc_iso(), str(exc)[:500]))
                db.commit()
            receipt = self._primary_receipt(row["submission_id"])
            return {"receipt_id": receipt["receipt_id"], "submission_id": row["submission_id"],
                    "status": "rejected", "error": str(exc)[:500]}

    def _primary_receipt(self, submission_id: str) -> sqlite3.Row:
        with self._connect() as db:
            return db.execute("SELECT * FROM receipts WHERE submission_id=? ORDER BY received_at,rowid LIMIT 1", (submission_id,)).fetchone()

    @staticmethod
    def _atomic_page(target: Path, content: str) -> None:
        if target.is_symlink():
            raise IntakeError("corpus target is a symlink")
        fd, name = tempfile.mkstemp(prefix=".intake-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o640)
            os.replace(name, target)
            os.chmod(target.parent, 0o750)
            _fsync_dir(target.parent)
            setfacl = shutil.which("setfacl")
            if setfacl:
                subprocess.run([setfacl, "-m", "u:agentic-dashboard:rX", str(target.parent), str(target)],
                               capture_output=True, check=True)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _atomic_json(target: Path, value: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if target.is_symlink():
            raise IntakeError("evaluation target is a symlink")
        payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        fd, name = tempfile.mkstemp(prefix=".evaluation-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o440)
            os.replace(name, target)
            _fsync_dir(target.parent)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def _origin_from_args(args: argparse.Namespace) -> dict[str, str]:
    result = {"platform": args.platform}
    for key in ("channel_id", "channel_name", "thread_ts", "message_ts", "message_id", "file_id", "user_id"):
        value = getattr(args, key, None)
        if value:
            result[key] = value
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("CORPUS_INTAKE_ROOT", "/var/lib/agentic-corpus-intake")))
    parser.add_argument("--max-upload-bytes", type=int, default=int(os.environ.get("CORPUS_INTAKE_MAX_BYTES", MAX_UPLOAD_BYTES)))
    sub = parser.add_subparsers(dest="command", required=True)
    def common(item):
        item.add_argument("--title", required=True)
        item.add_argument("--source-url")
        item.add_argument("--platform", default="cli")
        for key in ("channel-id", "channel-name", "thread-ts", "message-ts", "message-id", "file-id", "user-id"):
            item.add_argument(f"--{key}")
    suggestion = sub.add_parser("suggest")
    common(suggestion)
    suggestion.add_argument("--text", required=True)
    upload = sub.add_parser("upload")
    common(upload)
    upload.add_argument("--file", type=Path, required=True)
    upload.add_argument("--filename", help="Original filename when --file is a temporary server path")
    upload.add_argument("--note")
    upload.add_argument("--rights", choices=sorted(RIGHTS_STATES), default="unknown")
    status = sub.add_parser("status")
    status.add_argument("receipt_id")
    recent = sub.add_parser("recent")
    recent.add_argument("--limit", type=int, default=25)
    process = sub.add_parser("process")
    process.add_argument("--corpus-root", type=Path, default=Path("/root/corpora/agentic-engineering"))
    process.add_argument("--all", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        store = IntakeStore(args.root, max_upload_bytes=args.max_upload_bytes)
        if args.command == "suggest":
            result = store.submit_suggestion(title=args.title, text=args.text, source_url=args.source_url, origin=_origin_from_args(args))
        elif args.command == "upload":
            result = store.submit_file(title=args.title, file_path=args.file, note=args.note, source_url=args.source_url,
                                       rights_state=args.rights, origin=_origin_from_args(args), filename=args.filename)
        elif args.command == "status":
            result = store.get(args.receipt_id)
        elif args.command == "recent":
            result = {"submissions": store.list_recent(args.limit)}
        else:
            rows = []
            while True:
                item = store.process_next(corpus_root=args.corpus_root)
                if item is None:
                    break
                rows.append(item)
                if not args.all:
                    break
            result = {"processed": rows, "count": len(rows)}
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (IntakeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": "intake_rejected", "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
