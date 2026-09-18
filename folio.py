#!/usr/bin/env python3
"""Folio: a dependency-free local response library and Markdown reader."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import html
import json
import os
import re
import selectors
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


APP_NAME = "Folio"
VERSION = "0.3.0"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_CATEGORY = "Inbox"
STATIC_ROOT = Path(__file__).resolve().parent / "static"
ALLOWED_FINAL_PHASES = {"final_answer", "final"}
ENTRY_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[a-f0-9]{10}$")
CATEGORY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
ADAPTER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
MAX_BLOCKQUOTE_DEPTH = 24
MAX_API_BODY_BYTES = 4096
MAX_INGEST_BYTES = 10 * 1024 * 1024
MAX_ENTRY_API_BODY_BYTES = MAX_INGEST_BYTES + 8192
KNOWN_PROVIDERS = {
    "codex": "Codex",
    "claude": "Claude",
    "grok": "Grok",
    "chatgpt": "ChatGPT",
    "gemini": "Gemini",
    "generic": "Other",
}
PROVIDER_ALIASES = {
    "other": "generic",
    "markdown": "generic",
}
SOURCE_SUPPORT_LEVELS = {"native", "imported"}
SOURCE_TRANSPORTS = {"session", "file", "stdin", "clipboard", "web", "direct"}
SERVER_STATE_VERSION = 2
AUTH_COOKIE_NAME = "folio_session"


class FolioError(Exception):
    """Expected user-facing Folio failure."""


def auth_cookie_name(instance_id: str) -> str:
    digest = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:16]
    return f"{AUTH_COOKIE_NAME}_{digest}"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z")


def default_home() -> Path:
    override = os.environ.get("FOLIO_HOME")
    if override:
        return Path(os.path.abspath(Path(override).expanduser()))
    return Path(os.path.abspath(Path.home() / "Library" / "Application Support" / APP_NAME))


def sessions_root() -> Path:
    override = os.environ.get("FOLIO_CODEX_SESSIONS")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".codex" / "sessions").resolve()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    ensure_private_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        file_descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise FolioError(f"Cannot open private lock file {path}: {error}") from error
    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FolioError(f"Lock path is not a regular file: {path}")
        if metadata.st_uid != os.getuid():
            raise FolioError(f"Lock file is not owned by the current user: {path}")
        os.fchmod(file_descriptor, 0o600)
        handle = os.fdopen(file_descriptor, "a+b")
    except Exception:
        os.close(file_descriptor)
        raise
    with handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise FolioError(f"Private data directory cannot be a symlink: {path}")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise FolioError(f"Cannot prepare private data directory {path}: {error}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise FolioError(f"Private data path is not a directory: {path}")
    if metadata.st_uid != os.getuid():
        raise FolioError(f"Private data directory is not owned by the current user: {path}")
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except OSError as error:
        raise FolioError(f"Cannot secure private data directory {path}: {error}") from error


def ensure_private_file(path: Path) -> None:
    if path.is_symlink():
        raise FolioError(f"Private data file cannot be a symlink: {path}")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise FolioError(f"Cannot inspect private data file {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise FolioError(f"Private data path is not a regular file: {path}")
    if metadata.st_uid != os.getuid():
        raise FolioError(f"Private data file is not owned by the current user: {path}")
    try:
        os.chmod(path, 0o600, follow_symlinks=False)
    except OSError as error:
        raise FolioError(f"Cannot secure private data file {path}: {error}") from error


def validate_category_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise FolioError("Category name cannot be empty.")
    if "/" in normalized or "\\" in normalized:
        raise FolioError("Category names cannot contain '/' or '\\'.")
    if normalized in {".", ".."}:
        raise FolioError("Category name is invalid.")
    if len(normalized) > 80:
        raise FolioError("Category names must be 80 characters or fewer.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise FolioError("Category names cannot contain control characters.")
    return normalized


def category_id_for(name: str) -> str:
    if name.casefold() == DEFAULT_CATEGORY.casefold():
        return "inbox"
    folded = name.casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:36] or "category"
    digest = hashlib.sha256(folded.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def validate_display_label(value: str, field_name: str, maximum: int = 80) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise FolioError(f"{field_name} cannot be empty.")
    if len(normalized) > maximum:
        raise FolioError(f"{field_name} must be {maximum} characters or fewer.")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise FolioError(f"{field_name} cannot contain control characters.")
    return normalized


def provider_descriptor(
    agent: str | None,
    source_label: str | None = None,
) -> dict[str, str]:
    requested = validate_display_label(agent or "generic", "Agent name", 60)
    known_id = PROVIDER_ALIASES.get(requested.casefold(), requested.casefold())
    if known_id in KNOWN_PROVIDERS:
        provider_id = known_id
        default_label = KNOWN_PROVIDERS[known_id]
    elif PROVIDER_ID_RE.fullmatch(known_id):
        provider_id = known_id
        default_label = requested
    else:
        folded = requested.casefold()
        slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:30] or "agent"
        digest = hashlib.sha256(folded.encode("utf-8")).hexdigest()[:10]
        provider_id = f"{slug}-{digest}"
        default_label = requested
    label = (
        validate_display_label(source_label, "Source label", 60)
        if source_label is not None
        else default_label
    )
    return {"id": provider_id, "label": label}


def source_descriptor(
    *,
    agent: str | None,
    source_label: str | None = None,
    support_level: str,
    transport: str,
    adapter: str,
    conversation_id: str | None = None,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider = provider_descriptor(agent, source_label)
    if support_level not in SOURCE_SUPPORT_LEVELS:
        raise FolioError("Source support level is invalid.")
    if transport not in SOURCE_TRANSPORTS:
        raise FolioError("Source transport is invalid.")
    if not ADAPTER_ID_RE.fullmatch(adapter):
        raise FolioError("Source adapter identifier is invalid.")
    if support_level == "native":
        if (
            provider != {"id": "codex", "label": "Codex"}
            or transport != "session"
            or adapter != "codex-session-v1"
            or conversation_id is None
        ):
            raise FolioError(
                "Native source metadata is reserved for exact Codex session capture."
            )
    elif transport == "session" or adapter == "codex-session-v1":
        raise FolioError("Imported source metadata cannot claim a native session adapter.")
    value: dict[str, Any] = {
        "provider": provider["id"],
        "label": provider["label"],
        "support_level": support_level,
        "transport": transport,
        "adapter": adapter,
    }
    if conversation_id is not None:
        if not THREAD_ID_RE.fullmatch(conversation_id):
            raise FolioError("Conversation ID contains unsupported characters.")
        value["conversation_id"] = conversation_id
    if locator:
        allowed_locator: dict[str, Any] = {}
        session_file = locator.get("session_file")
        if isinstance(session_file, str):
            safe_name = Path(session_file).name
            if safe_name == session_file and len(safe_name) <= 255:
                allowed_locator["session_file"] = safe_name
        record_line = locator.get("record_line")
        if isinstance(record_line, int) and record_line > 0:
            allowed_locator["record_line"] = record_line
        phase = locator.get("phase")
        if isinstance(phase, str) and phase in ALLOWED_FINAL_PHASES:
            allowed_locator["phase"] = phase
        file_name = locator.get("file_name")
        if isinstance(file_name, str):
            safe_name = Path(file_name).name
            if safe_name == file_name and len(safe_name) <= 255:
                allowed_locator["file_name"] = safe_name
        if allowed_locator:
            value["locator"] = allowed_locator
    return value


def normalize_source_for_storage(source: dict[str, Any] | None) -> dict[str, Any]:
    if not source:
        return source_descriptor(
            agent="generic",
            support_level="imported",
            transport="direct",
            adapter="folio-direct-v1",
        )
    if "provider" in source:
        allowed_keys = {
            "provider",
            "label",
            "support_level",
            "transport",
            "adapter",
            "conversation_id",
            "locator",
        }
        if set(source) - allowed_keys:
            raise FolioError("Source metadata contains unsupported fields.")
        provider = source.get("provider")
        label = source.get("label")
        support_level = source.get("support_level")
        transport = source.get("transport")
        adapter = source.get("adapter")
        conversation_id = source.get("conversation_id")
        locator = source.get("locator")
        if not all(
            isinstance(value, str)
            for value in (provider, label, support_level, transport, adapter)
        ):
            raise FolioError("Source metadata is invalid.")
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise FolioError("Source conversation ID is invalid.")
        if locator is not None and not isinstance(locator, dict):
            raise FolioError("Source locator is invalid.")
        descriptor = source_descriptor(
            agent=provider,
            source_label=label,
            support_level=support_level,
            transport=transport,
            adapter=adapter,
            conversation_id=conversation_id,
            locator=locator,
        )
        if descriptor["provider"] != provider:
            raise FolioError("Source provider identifier is invalid.")
        return descriptor
    kind = source.get("kind")
    if kind == "codex":
        allowed_keys = {
            "kind",
            "thread_id",
            "session_file",
            "record_line",
            "phase",
        }
        if set(source) - allowed_keys:
            raise FolioError("Legacy Codex source metadata contains unsupported fields.")
        thread_id = source.get("thread_id")
        if not isinstance(thread_id, str):
            raise FolioError("Legacy Codex source thread ID is invalid.")
        return source_descriptor(
            agent="codex",
            support_level="native",
            transport="session",
            adapter="codex-session-v1",
            conversation_id=thread_id,
            locator=source,
        )
    if kind == "file":
        if set(source) - {"kind", "path"}:
            raise FolioError("Legacy file source metadata contains unsupported fields.")
        path_value = source.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise FolioError("Legacy file source path is invalid.")
        file_name = Path(path_value).name
        return source_descriptor(
            agent="generic",
            support_level="imported",
            transport="file",
            adapter="folio-file-v1",
            locator={"file_name": file_name} if file_name else None,
        )
    raise FolioError("Source metadata is invalid.")


def entry_source(metadata: dict[str, Any]) -> dict[str, Any]:
    source = metadata.get("source")
    if isinstance(source, dict):
        return normalize_source_for_storage(source)
    raise FolioError("Entry source metadata is invalid.")


def source_badge_text(source: dict[str, Any]) -> str:
    support = "Native" if source["support_level"] == "native" else "Imported"
    return f"{source['label']} · {support}"


def clean_title_text(value: str) -> str:
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*_~>#]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120].rstrip()


def derive_title(markdown: str, explicit: str | None = None) -> str:
    if explicit is not None:
        title = clean_title_text(explicit)
        if not title:
            raise FolioError("Title cannot be empty.")
        return title
    in_fence = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        candidate = heading.group(1) if heading else line
        title = clean_title_text(candidate)
        if title:
            return title
    return f"Response saved {utc_now().strftime('%Y-%m-%d %H:%M UTC')}"


def validate_metadata_title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FolioError("Entry title is invalid.")
    if len(value) > 120:
        raise FolioError("Entry title is too long.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise FolioError("Entry title contains control characters.")
    return value


def validate_iso_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise FolioError("Entry creation timestamp is invalid.")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FolioError("Entry creation timestamp is invalid.") from error
    if parsed.tzinfo is None:
        raise FolioError("Entry creation timestamp must include a timezone.")
    return value


class FolioStore:
    def __init__(self, home: Path | None = None):
        requested_home = (home or default_home()).expanduser()
        self.home = Path(os.path.abspath(requested_home))
        self.library_dir = self.home / "library"
        self.run_dir = self.home / "run"
        self.locks_dir = self.home / "locks"
        self.lock_path = self.home / "locks" / "library.lock"
        self.server_lock_path = self.home / "locks" / "server.lock"
        self.server_state_path = self.run_dir / "server.json"
        self._layout_secured = False

    def ensure_layout(self) -> None:
        if self._layout_secured:
            return
        ensure_private_directory(self.home)
        ensure_private_directory(self.library_dir)
        ensure_private_directory(self.run_dir)
        ensure_private_directory(self.locks_dir)
        inbox_dir = self.library_dir / "inbox"
        ensure_private_directory(inbox_dir)
        category_path = inbox_dir / "category.json"
        if not category_path.exists():
            atomic_write(
                category_path,
                json_bytes(
                    {
                        "version": 1,
                        "id": "inbox",
                        "name": DEFAULT_CATEGORY,
                        "created_at": iso_now(),
                    }
                ),
            )
        else:
            ensure_private_file(category_path)
        self._migrate_permissions()
        self._layout_secured = True

    def _migrate_permissions(self) -> None:
        for parent in (self.run_dir, self.locks_dir):
            for child in parent.iterdir():
                if child.is_symlink():
                    continue
                try:
                    metadata = child.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    ensure_private_file(child)
        for category_dir in self.library_dir.iterdir():
            if category_dir.is_symlink():
                continue
            try:
                metadata = category_dir.lstat()
            except OSError:
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            ensure_private_directory(category_dir)
            for child in category_dir.iterdir():
                if child.is_symlink():
                    continue
                try:
                    child_metadata = child.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(child_metadata.st_mode):
                    ensure_private_file(child)

    def _safe_category_directories(self) -> list[tuple[Path, Path]]:
        self.ensure_layout()
        try:
            library_resolved = self.library_dir.resolve(strict=True)
        except OSError as error:
            raise FolioError(f"Library directory is unavailable: {error}") from error
        categories: list[tuple[Path, Path]] = []
        for category_dir in self.library_dir.iterdir():
            if category_dir.is_symlink():
                continue
            try:
                metadata = category_dir.lstat()
                resolved = category_dir.resolve(strict=True)
            except OSError:
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            if resolved.parent != library_resolved:
                continue
            if not CATEGORY_ID_RE.fullmatch(category_dir.name):
                continue
            categories.append((category_dir, resolved))
        return categories

    @staticmethod
    def _safe_regular_file(path: Path, category_resolved: Path) -> Path:
        if path.is_symlink():
            raise FolioError(f"Entry file cannot be a symlink: {path.name}")
        try:
            metadata = path.lstat()
            parent_resolved = path.parent.resolve(strict=True)
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise FolioError(f"Entry file is unavailable: {path.name}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise FolioError(f"Entry path is not a regular file: {path.name}")
        if parent_resolved != category_resolved or resolved.parent != category_resolved:
            raise FolioError("Entry file escapes its category directory.")
        ensure_private_file(path)
        return resolved

    def categories(self) -> list[dict[str, Any]]:
        self.ensure_layout()
        values: list[dict[str, Any]] = []
        for category_dir, category_resolved in self._safe_category_directories():
            path = category_dir / "category.json"
            try:
                self._safe_regular_file(path, category_resolved)
                value = json.loads(path.read_text(encoding="utf-8"))
            except (FolioError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(value, dict)
                and isinstance(value.get("id"), str)
                and isinstance(value.get("name"), str)
                and category_dir.name == value["id"]
            ):
                values.append(value)
        values.sort(key=lambda item: (item["id"] != "inbox", item["name"].casefold()))
        return values

    def _resolve_category_locked(self, requested_name: str) -> dict[str, Any]:
        name = validate_category_name(requested_name)
        for category in self.categories():
            if category["name"].casefold() == name.casefold():
                return category
        category_id = category_id_for(name)
        category_dir = self.library_dir / category_id
        try:
            category_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        except OSError as error:
            raise FolioError(f"Cannot create category {name!r}: {error}") from error
        ensure_private_directory(category_dir)
        category = {
            "version": 1,
            "id": category_id,
            "name": name,
            "created_at": iso_now(),
        }
        atomic_write(category_dir / "category.json", json_bytes(category))
        return category

    def add_entry(
        self,
        markdown: str,
        category_name: str = DEFAULT_CATEGORY,
        title: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(markdown, str):
            raise FolioError("Markdown source must be text.")
        if not markdown.strip():
            raise FolioError("Markdown source is empty.")
        if len(markdown.encode("utf-8")) > MAX_INGEST_BYTES:
            raise FolioError(
                f"Markdown source exceeds Folio's "
                f"{MAX_INGEST_BYTES // (1024 * 1024)} MB limit."
            )
        self.ensure_layout()
        with exclusive_lock(self.lock_path):
            category = self._resolve_category_locked(category_name)
            created_at = iso_now()
            entry_id = utc_now().strftime("%Y%m%dT%H%M%S%fZ") + f"-{secrets.token_hex(5)}"
            category_dir = self.library_dir / category["id"]
            markdown_path = category_dir / f"{entry_id}.md"
            metadata_path = category_dir / f"{entry_id}.json"
            digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            metadata = {
                "version": 2,
                "id": entry_id,
                "title": derive_title(markdown, title),
                "category": {"id": category["id"], "name": category["name"]},
                "created_at": created_at,
                "sha256": digest,
                "source": normalize_source_for_storage(source),
                "markdown_file": markdown_path.name,
                "reader_path": f"/entry/{entry_id}",
            }
            atomic_write(markdown_path, markdown.encode("utf-8"))
            try:
                atomic_write(metadata_path, json_bytes(metadata))
            except Exception:
                with contextlib.suppress(FileNotFoundError):
                    markdown_path.unlink()
                raise
            return {
                "metadata": metadata,
                "markdown_path": str(markdown_path),
                "metadata_path": str(metadata_path),
            }

    def _read_entry_metadata(
        self,
        category_dir: Path,
        category_resolved: Path,
        metadata_path: Path,
    ) -> tuple[dict[str, Any], str]:
        self._safe_regular_file(metadata_path, category_resolved)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FolioError(f"Entry metadata is unreadable: {metadata_path.name}") from error
        if not isinstance(metadata, dict):
            raise FolioError("Entry metadata must be an object.")
        expected_fields = {
            "version",
            "id",
            "title",
            "category",
            "created_at",
            "sha256",
            "source",
            "markdown_file",
            "reader_path",
        }
        allowed_fields = expected_fields | {"updated_at"}
        if not expected_fields.issubset(metadata) or set(metadata) - allowed_fields:
            raise FolioError("Entry metadata fields are invalid.")
        metadata_version = metadata.get("version")
        if isinstance(metadata_version, bool) or metadata_version not in {1, 2}:
            raise FolioError("Entry metadata has an unsupported version.")
        entry_id = metadata.get("id")
        if not isinstance(entry_id, str) or not ENTRY_ID_RE.fullmatch(entry_id):
            raise FolioError("Entry metadata has an invalid entry ID.")
        if metadata_path.name != f"{entry_id}.json":
            raise FolioError("Entry metadata filename does not match its entry ID.")
        expected_markdown_name = f"{entry_id}.md"
        markdown_name = metadata.get("markdown_file")
        if markdown_name != expected_markdown_name:
            raise FolioError("Entry Markdown filename does not match its entry ID.")
        if (
            Path(markdown_name).is_absolute()
            or "/" in markdown_name
            or "\\" in markdown_name
        ):
            raise FolioError("Entry Markdown filename is unsafe.")
        entry_category = metadata.get("category")
        if (
            not isinstance(entry_category, dict)
            or set(entry_category) != {"id", "name"}
            or entry_category.get("id") != category_dir.name
            or not isinstance(entry_category.get("name"), str)
        ):
            raise FolioError("Entry category metadata is invalid.")
        if not CATEGORY_ID_RE.fullmatch(entry_category["id"]):
            raise FolioError("Entry category identifier is invalid.")
        validate_category_name(entry_category["name"])
        validate_metadata_title(metadata.get("title"))
        validate_iso_timestamp(metadata.get("created_at"))
        if "updated_at" in metadata:
            validate_iso_timestamp(metadata.get("updated_at"))
        if metadata.get("reader_path") != f"/entry/{entry_id}":
            raise FolioError("Entry reader path is invalid.")
        digest = metadata.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise FolioError("Entry digest is invalid.")
        source = metadata.get("source")
        if not isinstance(source, dict):
            raise FolioError("Entry source metadata is invalid.")
        normalized_source = normalize_source_for_storage(source)
        if metadata_version == 2:
            if normalized_source != source:
                raise FolioError("Entry source metadata is not canonical.")
        markdown_path = category_dir / expected_markdown_name
        self._safe_regular_file(markdown_path, category_resolved)
        try:
            markdown = markdown_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise FolioError(f"Entry Markdown is unreadable: {markdown_path.name}") from error
        if hashlib.sha256(markdown.encode("utf-8")).hexdigest() != digest:
            raise FolioError("Entry Markdown digest does not match its metadata.")
        return metadata, markdown

    def load_entries(
        self,
        category_id: str | None = None,
        query: str | None = None,
        provider_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_layout()
        query_folded = (query or "").strip().casefold()
        entries: list[dict[str, Any]] = []
        for category_dir, category_resolved in self._safe_category_directories():
            for metadata_path in category_dir.iterdir():
                if metadata_path.name == "category.json" or metadata_path.suffix != ".json":
                    continue
                try:
                    metadata, markdown = self._read_entry_metadata(
                        category_dir, category_resolved, metadata_path
                    )
                except FolioError:
                    continue
                entry_category = metadata["category"]
                if category_id and entry_category["id"] != category_id:
                    continue
                source = entry_source(metadata)
                if provider_id and source["provider"] != provider_id:
                    continue
                if query_folded:
                    haystack = (
                        f"{metadata.get('title', '')}\n"
                        f"{entry_category.get('name', '')}\n"
                        f"{source.get('label', '')}\n"
                        f"{source_badge_text(source)}\n{markdown}"
                    ).casefold()
                    if query_folded not in haystack:
                        continue
                summary = re.sub(r"\s+", " ", clean_title_text(markdown))[:220]
                value = dict(metadata)
                value["_markdown"] = markdown
                value["_summary"] = summary
                value["_source"] = source
                entries.append(value)
        entries.sort(key=lambda item: (item.get("created_at", ""), item["id"]), reverse=True)
        return entries

    def get_entry(self, entry_id: str) -> tuple[dict[str, Any], str]:
        _category_dir, _category_resolved, _metadata_path, metadata, markdown = (
            self._locate_entry(entry_id)
        )
        return metadata, markdown

    def _locate_entry(
        self,
        entry_id: str,
    ) -> tuple[Path, Path, Path, dict[str, Any], str]:
        if not ENTRY_ID_RE.fullmatch(entry_id):
            raise FolioError("Entry not found.")
        matches: list[tuple[Path, Path, Path, dict[str, Any], str]] = []
        for category_dir, category_resolved in self._safe_category_directories():
            metadata_path = category_dir / f"{entry_id}.json"
            if not metadata_path.exists() and not metadata_path.is_symlink():
                continue
            metadata, markdown = self._read_entry_metadata(
                category_dir,
                category_resolved,
                metadata_path,
            )
            matches.append(
                (
                    category_dir,
                    category_resolved,
                    metadata_path,
                    metadata,
                    markdown,
                )
            )
        if len(matches) != 1:
            raise FolioError("Entry not found.")
        return matches[0]

    def move_entry(self, entry_id: str, category_name: str) -> dict[str, Any]:
        requested_name = validate_category_name(category_name)
        self.ensure_layout()
        with exclusive_lock(self.lock_path):
            (
                source_dir,
                _source_resolved,
                source_metadata_path,
                metadata,
                _markdown,
            ) = self._locate_entry(entry_id)
            target_category = self._resolve_category_locked(requested_name)
            return self._move_entry_locked(
                source_dir,
                source_metadata_path,
                metadata,
                target_category,
            )

    def _move_entry_locked(
        self,
        source_dir: Path,
        source_metadata_path: Path,
        metadata: dict[str, Any],
        target_category: dict[str, Any],
    ) -> dict[str, Any]:
        entry_id = metadata["id"]
        if target_category["id"] == source_dir.name:
            return {
                "metadata": metadata,
                "moved": False,
                "markdown_path": str(source_dir / f"{entry_id}.md"),
                "metadata_path": str(source_metadata_path),
            }

        target_dir = self.library_dir / target_category["id"]
        ensure_private_directory(target_dir)
        target_resolved = target_dir.resolve(strict=True)
        if target_resolved.parent != self.library_dir.resolve(strict=True):
            raise FolioError("Target category escapes the Folio library.")

        source_markdown_path = source_dir / f"{entry_id}.md"
        target_markdown_path = target_dir / f"{entry_id}.md"
        target_metadata_path = target_dir / f"{entry_id}.json"
        if (
            target_markdown_path.exists()
            or target_markdown_path.is_symlink()
            or target_metadata_path.exists()
            or target_metadata_path.is_symlink()
        ):
            raise FolioError("The target category already contains this entry.")

        updated_metadata = dict(metadata)
        updated_metadata["category"] = {
            "id": target_category["id"],
            "name": target_category["name"],
        }
        updated_metadata["updated_at"] = iso_now()

        atomic_write(target_metadata_path, json_bytes(updated_metadata))
        try:
            os.replace(source_markdown_path, target_markdown_path)
            os.chmod(target_markdown_path, 0o600, follow_symlinks=False)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                target_metadata_path.unlink()
            raise
        try:
            source_metadata_path.unlink()
        except Exception:
            with contextlib.suppress(Exception):
                os.replace(target_markdown_path, source_markdown_path)
            with contextlib.suppress(FileNotFoundError):
                target_metadata_path.unlink()
            raise

        return {
            "metadata": updated_metadata,
            "moved": True,
            "markdown_path": str(target_markdown_path),
            "metadata_path": str(target_metadata_path),
        }

    def delete_category(self, category_id: str) -> dict[str, Any]:
        if not isinstance(category_id, str) or not CATEGORY_ID_RE.fullmatch(category_id):
            raise FolioError("Category not found.")
        if category_id == "inbox":
            raise FolioError("Inbox cannot be deleted.")
        self.ensure_layout()
        with exclusive_lock(self.lock_path):
            category = next(
                (item for item in self.categories() if item["id"] == category_id),
                None,
            )
            if category is None:
                raise FolioError("Category not found.")

            source_dir = self.library_dir / category_id
            try:
                source_resolved = source_dir.resolve(strict=True)
                library_resolved = self.library_dir.resolve(strict=True)
            except OSError as error:
                raise FolioError("Category not found.") from error
            if source_resolved.parent != library_resolved:
                raise FolioError("Category escapes the Folio library.")

            category_path = source_dir / "category.json"
            self._safe_regular_file(category_path, source_resolved)
            entries: list[tuple[Path, dict[str, Any]]] = []
            expected_names = {"category.json"}
            for metadata_path in sorted(source_dir.glob("*.json")):
                if metadata_path.name == "category.json":
                    continue
                metadata, _markdown = self._read_entry_metadata(
                    source_dir,
                    source_resolved,
                    metadata_path,
                )
                entry_id = metadata["id"]
                expected_names.update({f"{entry_id}.json", f"{entry_id}.md"})
                entries.append((metadata_path, metadata))

            actual_names = {child.name for child in source_dir.iterdir()}
            if actual_names != expected_names:
                raise FolioError(
                    "Category contains unrecognized files and was not deleted."
                )

            inbox = self._resolve_category_locked(DEFAULT_CATEGORY)
            inbox_dir = self.library_dir / inbox["id"]
            for _metadata_path, metadata in entries:
                entry_id = metadata["id"]
                for suffix in (".md", ".json"):
                    target = inbox_dir / f"{entry_id}{suffix}"
                    if target.exists() or target.is_symlink():
                        raise FolioError(
                            "Inbox already contains an entry with the same ID."
                        )

            for metadata_path, metadata in entries:
                self._move_entry_locked(
                    source_dir,
                    metadata_path,
                    metadata,
                    inbox,
                )

            remaining_names = {child.name for child in source_dir.iterdir()}
            if remaining_names != {"category.json"}:
                raise FolioError(
                    "Category changed while it was being deleted; retry the deletion."
                )
            category_path.unlink()
            try:
                source_dir.rmdir()
            except OSError as error:
                atomic_write(category_path, json_bytes(category))
                raise FolioError(f"Could not delete category: {error}") from error

            return {
                "category": category,
                "moved_count": len(entries),
                "destination": inbox,
            }

    def delete_entry(self, entry_id: str) -> dict[str, Any]:
        self.ensure_layout()
        with exclusive_lock(self.lock_path):
            (
                source_dir,
                _source_resolved,
                source_metadata_path,
                metadata,
                _markdown,
            ) = self._locate_entry(entry_id)
            source_markdown_path = source_dir / f"{entry_id}.md"
            try:
                source_metadata_path.unlink()
            except OSError as error:
                raise FolioError(f"Could not delete response metadata: {error}") from error
            try:
                source_markdown_path.unlink()
            except OSError as error:
                atomic_write(source_metadata_path, json_bytes(metadata))
                raise FolioError(f"Could not delete response Markdown: {error}") from error
            return {"metadata": metadata}


def session_declares_thread(path: Path, thread_id: str) -> bool:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= 100:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "session_meta":
                    payload = record.get("payload")
                    return isinstance(payload, dict) and payload.get("id") == thread_id
    except (OSError, UnicodeDecodeError):
        return False
    return False


def validate_session_candidate(path: Path, resolved_root: Path) -> Path:
    if path.is_symlink():
        raise FolioError(f"Codex session candidate cannot be a symlink: {path}")
    current = path.parent
    while current != resolved_root:
        if current.is_symlink():
            raise FolioError(f"Codex session path cannot contain symlinks: {path}")
        if resolved_root not in current.parents:
            raise FolioError(f"Codex session candidate escapes the sessions root: {path}")
        current = current.parent
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise FolioError(f"Codex session candidate is unsafe: {path}") from error
    if not stat.S_ISREG(metadata.st_mode) or not resolved.is_file():
        raise FolioError(f"Codex session candidate is not a regular file: {path}")
    return resolved


def find_session_file(thread_id: str, root: Path | None = None) -> Path:
    if not THREAD_ID_RE.fullmatch(thread_id):
        raise FolioError("Thread ID contains unsupported characters.")
    try:
        base = (root or sessions_root()).expanduser().resolve(strict=True)
    except OSError as error:
        raise FolioError(f"Codex sessions directory is unavailable: {error}") from error
    if not base.is_dir():
        raise FolioError(f"Codex sessions directory is unavailable: {base}")
    filename_candidates = [path for path in base.rglob("*.jsonl") if thread_id in path.name]
    candidates: list[Path] = []
    for path in filename_candidates:
        validated = validate_session_candidate(path, base)
        if session_declares_thread(validated, thread_id):
            candidates.append(validated)
    if len(candidates) != 1:
        raise FolioError(
            f"Expected exactly one Codex session for thread {thread_id!r}; found {len(candidates)}."
        )
    return candidates[0]


def extract_latest_final(path: Path) -> tuple[str, dict[str, Any]]:
    latest: tuple[str, dict[str, Any]] | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise FolioError(
                        f"Session schema mismatch: invalid JSON at line {line_number}."
                    ) from error
                if not isinstance(record, dict):
                    raise FolioError(
                        f"Session schema mismatch: line {line_number} is not an object."
                    )
                if record.get("type") != "response_item":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "message":
                    continue
                if payload.get("role") != "assistant":
                    continue
                phase = payload.get("phase")
                if phase not in ALLOWED_FINAL_PHASES:
                    continue
                content = payload.get("content")
                if not isinstance(content, list):
                    raise FolioError(
                        f"Session schema mismatch: final assistant content at line {line_number} is not a list."
                    )
                text_parts: list[str] = []
                for item in content:
                    if not isinstance(item, dict):
                        raise FolioError(
                            f"Session schema mismatch: content item at line {line_number} is not an object."
                        )
                    if item.get("type") != "output_text":
                        continue
                    value = item.get("text")
                    if not isinstance(value, str):
                        raise FolioError(
                            f"Session schema mismatch: output_text at line {line_number} has no text."
                        )
                    text_parts.append(value)
                if not text_parts:
                    raise FolioError(
                        f"Session schema mismatch: final assistant message at line {line_number} has no text."
                    )
                latest = (
                    "".join(text_parts),
                    {
                        "kind": "codex",
                        "session_file": path.name,
                        "record_line": line_number,
                        "phase": phase,
                    },
                )
    except (OSError, UnicodeDecodeError) as error:
        raise FolioError(f"Cannot read Codex session: {error}") from error
    if latest is None:
        raise FolioError("No completed assistant final answer was found in the Codex session.")
    return latest


def capture_response(thread_id: str | None = None) -> tuple[str, dict[str, Any]]:
    resolved_thread = thread_id or os.environ.get("CODEX_THREAD_ID")
    if not resolved_thread:
        raise FolioError("No thread ID supplied. Use --thread-id or set CODEX_THREAD_ID.")
    session_path = find_session_file(resolved_thread)
    markdown, locator = extract_latest_final(session_path)
    return (
        markdown,
        source_descriptor(
            agent="codex",
            support_level="native",
            transport="session",
            adapter="codex-session-v1",
            conversation_id=resolved_thread,
            locator=locator,
        ),
    )


def capture_agent_response(
    agent: str,
    conversation_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    provider = provider_descriptor(agent)
    if provider["id"] != "codex":
        raise FolioError(
            f"Folio has no native capture adapter for {provider['label']}. "
            f"Use 'folio add --clipboard --agent {provider['id']}' or "
            "'folio add --stdin'."
        )
    return capture_response(conversation_id)


def decode_ingest_bytes(data: bytes, source_name: str) -> str:
    if len(data) > MAX_INGEST_BYTES:
        raise FolioError(
            f"{source_name} exceeds Folio's {MAX_INGEST_BYTES // (1024 * 1024)} MB limit."
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FolioError(f"{source_name} is not valid UTF-8.") from error


def read_stdin_markdown(stream: Any | None = None) -> str:
    active_stream = stream or getattr(sys.stdin, "buffer", sys.stdin)
    try:
        value = active_stream.read(MAX_INGEST_BYTES + 1)
    except OSError as error:
        raise FolioError(f"Cannot read standard input: {error}") from error
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_INGEST_BYTES:
            raise FolioError(
                f"Standard input exceeds Folio's {MAX_INGEST_BYTES // (1024 * 1024)} MB limit."
            )
        return value
    if not isinstance(value, bytes):
        raise FolioError("Standard input did not provide text.")
    return decode_ingest_bytes(value, "Standard input")


def read_clipboard_markdown() -> str:
    candidates = [
        ("pbpaste", ["pbpaste"]),
        ("wl-paste", ["wl-paste", "--no-newline"]),
        ("xclip", ["xclip", "-selection", "clipboard", "-o"]),
    ]
    command: list[str] | None = None
    for executable, candidate in candidates:
        resolved = shutil.which(executable)
        if resolved:
            command = [resolved, *candidate[1:]]
            break
    if command is None:
        raise FolioError(
            "No supported clipboard reader was found. "
            "Install pbpaste, wl-paste, or xclip, or use --stdin."
        )
    data = read_process_output_bounded(command, "Clipboard content", timeout=8)
    return decode_ingest_bytes(data, "Clipboard content")


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def read_process_output_bounded(
    command: list[str],
    source_name: str,
    *,
    timeout: float,
) -> bytes:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as error:
        raise FolioError(f"Cannot read {source_name.casefold()}: {error}") from error
    if process.stdout is None:
        terminate_process(process)
        raise FolioError(f"Cannot read {source_name.casefold()}.")
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        reached_eof = False
        while not reached_eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process(process)
                raise FolioError(f"Timed out while reading {source_name.casefold()}.")
            events = selector.select(remaining)
            if not events:
                terminate_process(process)
                raise FolioError(f"Timed out while reading {source_name.casefold()}.")
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    reached_eof = True
                    break
                total += len(chunk)
                if total > MAX_INGEST_BYTES:
                    terminate_process(process)
                    raise FolioError(
                        f"{source_name} exceeds Folio's "
                        f"{MAX_INGEST_BYTES // (1024 * 1024)} MB limit."
                    )
                chunks.append(chunk)
        return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired) as error:
        terminate_process(process)
        raise FolioError(f"Cannot read {source_name.casefold()}: {error}") from error
    finally:
        selector.close()
        process.stdout.close()
    if return_code != 0:
        raise FolioError(f"Cannot read {source_name.casefold()}.")
    return b"".join(chunks)


def read_markdown_file(source_path: Path) -> tuple[str, Path]:
    requested = Path(os.path.abspath(source_path.expanduser()))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(requested, flags)
    except OSError as error:
        raise FolioError(f"Cannot open Markdown source safely: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise FolioError("Markdown source is not a regular file.")
        if metadata.st_size > MAX_INGEST_BYTES:
            raise FolioError(
                f"Markdown source exceeds Folio's "
                f"{MAX_INGEST_BYTES // (1024 * 1024)} MB limit."
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(MAX_INGEST_BYTES + 1)
    except OSError as error:
        raise FolioError(f"Cannot read Markdown source: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return decode_ingest_bytes(data, "Markdown source"), requested


def safe_link(url: str) -> str | None:
    candidate = html.unescape(url.strip())
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https", "mailto"}:
        return None
    if parsed.scheme.casefold() in {"http", "https"} and not parsed.netloc:
        return None
    return candidate


def render_inline(text: str, depth: int = 0) -> str:
    if depth > 8:
        return html.escape(text)
    result: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] == "`":
            closing = text.find("`", index + 1)
            if closing != -1:
                result.append(f"<code>{html.escape(text[index + 1:closing])}</code>")
                index = closing + 1
                continue
        if text.startswith("**", index) or text.startswith("__", index):
            marker = text[index : index + 2]
            closing = text.find(marker, index + 2)
            if closing > index + 2:
                inner = render_inline(text[index + 2 : closing], depth + 1)
                result.append(f"<strong>{inner}</strong>")
                index = closing + 2
                continue
        if text[index] in {"*", "_"}:
            marker = text[index]
            closing = text.find(marker, index + 1)
            if closing > index + 1:
                inner = render_inline(text[index + 1 : closing], depth + 1)
                result.append(f"<em>{inner}</em>")
                index = closing + 1
                continue
        if text[index] == "[":
            label_end = text.find("](", index + 1)
            if label_end != -1:
                url_end = text.find(")", label_end + 2)
                if url_end != -1:
                    label = render_inline(text[index + 1 : label_end], depth + 1)
                    raw_url = text[label_end + 2 : url_end]
                    allowed = safe_link(raw_url)
                    if allowed is None:
                        result.append(f'<span class="unsafe-link">{label}</span>')
                    else:
                        href = html.escape(allowed, quote=True)
                        result.append(
                            f'<a href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>'
                        )
                    index = url_end + 1
                    continue
        next_special = index + 1
        while next_special < length and text[next_special] not in "`[*_":
            next_special += 1
        result.append(html.escape(text[index:next_special]))
        index = next_special
    return "".join(result)


def heading_slug(text: str, seen: dict[str, int]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", clean_title_text(text).casefold()).strip("-") or "section"
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else f"{base}-{count + 1}"


def split_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def is_table_separator(line: str) -> bool:
    cells = split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def is_block_start(lines: list[str], index: int) -> bool:
    line = lines[index]
    stripped = line.strip()
    if not stripped:
        return True
    if re.match(r"^\s*(```|~~~)", line):
        return True
    if re.match(r"^\s*#{1,6}\s+", line):
        return True
    if re.match(r"^\s*>\s?", line):
        return True
    if re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line):
        return True
    return index + 1 < len(lines) and "|" in line and is_table_separator(lines[index + 1])


def render_markdown(
    markdown: str,
    blockquote_depth: int = 0,
) -> tuple[str, list[tuple[int, str, str]]]:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    headings: list[tuple[int, str, str]] = []
    seen_slugs: dict[str, int] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = re.match(r"^\s*(```|~~~)\s*([A-Za-z0-9_+.-]*)\s*$", line)
        if fence:
            marker = fence.group(1)
            language = fence.group(2)
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not re.match(
                rf"^\s*{re.escape(marker)}\s*$", lines[index]
            ):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            label = html.escape(language or "text")
            language_class = (
                f' class="language-{html.escape(language, quote=True)}"' if language else ""
            )
            code = html.escape("\n".join(code_lines))
            output.append(
                '<div class="code-block">'
                f'<div class="code-toolbar"><span>{label}</span>'
                '<button type="button" data-copy-code>Copy</button></div>'
                f"<pre><code{language_class}>{code}</code></pre></div>"
            )
            continue
        heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2)
            slug = heading_slug(text, seen_slugs)
            headings.append((level, clean_title_text(text), slug))
            output.append(
                f'<h{level} id="{html.escape(slug, quote=True)}">'
                f"{render_inline(text)}"
                f'<a class="heading-anchor" href="#{html.escape(slug, quote=True)}" '
                f'aria-label="Link to this section">#</a></h{level}>'
            )
            index += 1
            continue
        if line.lstrip().startswith(">"):
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            if blockquote_depth >= MAX_BLOCKQUOTE_DEPTH:
                flattened = [
                    re.sub(r"^(?:\s*>\s?)+", "", quote_line)
                    for quote_line in quote_lines
                ]
                quote_html = f"<p>{render_inline(' '.join(flattened))}</p>"
            else:
                quote_html, _ = render_markdown(
                    "\n".join(quote_lines),
                    blockquote_depth=blockquote_depth + 1,
                )
            output.append(f"<blockquote>{quote_html}</blockquote>")
            continue
        list_match = re.match(r"^\s*([-+*]|\d+[.)])\s+(.+)$", line)
        if list_match:
            ordered = list_match.group(1)[0].isdigit()
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while index < len(lines):
                match = re.match(r"^\s*([-+*]|\d+[.)])\s+(.+)$", lines[index])
                if not match or match.group(1)[0].isdigit() != ordered:
                    break
                items.append(f"<li>{render_inline(match.group(2))}</li>")
                index += 1
            output.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        if index + 1 < len(lines) and "|" in line and is_table_separator(lines[index + 1]):
            headers = split_table_row(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(split_table_row(lines[index]))
                index += 1
            head = "".join(f"<th>{render_inline(cell)}</th>" for cell in headers)
            body_rows = []
            for row in rows:
                padded = row + [""] * max(0, len(headers) - len(row))
                cells = "".join(f"<td>{render_inline(cell)}</td>" for cell in padded[: len(headers)])
                body_rows.append(f"<tr>{cells}</tr>")
            output.append(
                '<div class="table-scroll"><table><thead><tr>'
                f"{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
            )
            continue
        paragraph_lines = [line.strip()]
        index += 1
        while index < len(lines) and not is_block_start(lines, index):
            paragraph_lines.append(lines[index].strip())
            index += 1
        output.append(f"<p>{render_inline(' '.join(paragraph_lines))}</p>")
    return "\n".join(output), headings


def logo_svg() -> str:
    return (
        '<svg class="logo-mark" viewBox="0 0 24 24" aria-hidden="true" fill="none">'
        '<path d="M4.5 5.5h11l3 3v12h-14z" stroke="currentColor" stroke-width="1.7"/>'
        '<path d="M6.5 3.5h8l4 4M14.5 5.5v3h3" stroke="currentColor" stroke-width="1.7"/>'
        '<path d="m8 11 2 2-2 2M12 15h3" stroke="currentColor" stroke-width="1.7" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )


def page_shell(title: str, body: str, description: str = "Local response library") -> str:
    safe_title = html.escape(title)
    safe_description = html.escape(description, quote=True)
    return (
        "<!doctype html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<meta name=\"description\" content=\"{safe_description}\">"
        f"<title>{safe_title} · Folio</title>"
        "<link rel=\"stylesheet\" href=\"/static/folio.css\">"
        "<script src=\"/static/folio.js\" defer></script>"
        "</head><body><a class=\"skip-link\" href=\"#main\">Skip to content</a>"
        "<header class=\"site-header\"><a class=\"brand\" href=\"/library\">"
        f"{logo_svg()}<span>Folio</span></a>"
        "<nav aria-label=\"Application\"><a href=\"/library\">Library</a>"
        "<button class=\"theme-toggle\" type=\"button\" data-theme-toggle "
        "aria-label=\"Toggle color theme\">Theme</button></nav></header>"
        f"{body}</body></html>"
    )


def format_timestamp(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%b %-d, %Y · %-I:%M %p")
    except (ValueError, TypeError):
        return value


def render_library_page(
    store: FolioStore,
    category_id: str | None,
    query: str,
    provider_id: str | None = None,
) -> str:
    categories = store.categories()
    all_entries = store.load_entries()
    entries = store.load_entries(
        category_id=category_id,
        query=query,
        provider_id=provider_id,
    )
    counts: dict[str, int] = {}
    providers: dict[str, str] = {}
    for entry in all_entries:
        item_category = entry["category"]["id"]
        counts[item_category] = counts.get(item_category, 0) + 1
        providers[entry["_source"]["provider"]] = entry["_source"]["label"]
    nav_items = [
        (
            None,
            "All",
            len(all_entries),
        )
    ] + [(item["id"], item["name"], counts.get(item["id"], 0)) for item in categories]
    nav_html: list[str] = []
    for nav_id, name, count in nav_items:
        selected = nav_id == category_id
        params: dict[str, str] = {}
        if nav_id:
            params["category"] = nav_id
        if query:
            params["q"] = query
        if provider_id:
            params["agent"] = provider_id
        href = "/library"
        if params:
            href += "?" + urllib.parse.urlencode(params)
        nav_html.append(
            f'<a class="category-link{" selected" if selected else ""}" '
            f'href="{html.escape(href, quote=True)}"{" aria-current=\"page\"" if selected else ""}>'
            f"<span>{html.escape(name)}</span><span>{count}</span></a>"
        )
    cards: list[str] = []
    for entry in entries:
        reader_path = html.escape(entry["reader_path"], quote=True)
        entry_id = html.escape(entry["id"], quote=True)
        entry_title = html.escape(entry["title"], quote=True)
        source = entry["_source"]
        source_class = (
            "source-native"
            if source["support_level"] == "native"
            else "source-imported"
        )
        cards.append(
            f'<article class="entry-card" data-entry-card><a class="entry-card-link" '
            f'href="{reader_path}">'
            f"<h2>{html.escape(entry['title'])}</h2>"
            f'<p class="entry-summary">{html.escape(entry["_summary"] or "Markdown response")}</p>'
            '<div class="entry-meta">'
            f"<span>{html.escape(entry['category']['name'])}</span>"
            f'<span class="source-badge {source_class}">'
            f"{html.escape(source_badge_text(source))}</span>"
            f"<time>{html.escape(format_timestamp(entry['created_at']))}</time>"
            "</div></a>"
            '<form class="delete-entry-form" data-delete-entry '
            f'data-entry-id="{entry_id}" data-entry-title="{entry_title}">'
            '<button class="entry-delete-button" type="submit">Delete</button>'
            '<p class="delete-entry-status" data-delete-entry-status '
            'role="status" aria-live="polite"></p></form></article>'
        )
    if not cards:
        message = "No responses match this search." if query else "This category has no saved responses yet."
        cards.append(
            '<div class="empty-state"><h2>Nothing here yet</h2>'
            f"<p>{html.escape(message)}</p></div>"
        )
    search_value = html.escape(query, quote=True)
    hidden_category = (
        f'<input type="hidden" name="category" value="{html.escape(category_id, quote=True)}">'
        if category_id
        else ""
    )
    provider_options = ['<option value="">All agents</option>']
    for item_id, item_label in sorted(
        providers.items(),
        key=lambda item: item[1].casefold(),
    ):
        selected = " selected" if item_id == provider_id else ""
        provider_options.append(
            f'<option value="{html.escape(item_id, quote=True)}"{selected}>'
            f"{html.escape(item_label)}</option>"
        )
    agent_datalist = "".join(
        f'<option value="{html.escape(label, quote=True)}"></option>'
        for label in dict.fromkeys([*KNOWN_PROVIDERS.values(), *providers.values()])
    )
    category_datalist = "".join(
        f'<option value="{html.escape(item["name"], quote=True)}"></option>'
        for item in categories
    )
    add_response_form = (
        '<details class="add-response-panel"><summary>Add an AI response</summary>'
        '<form class="add-response-form" data-add-entry>'
        '<div class="add-response-grid">'
        '<label>Agent<input name="agent" list="library-agents" value="Other" '
        'maxlength="60" required autocomplete="off"></label>'
        f'<datalist id="library-agents">{agent_datalist}</datalist>'
        '<label>Category<input name="category" list="library-categories" value="Inbox" '
        'maxlength="80" required autocomplete="off"></label>'
        f'<datalist id="library-categories">{category_datalist}</datalist>'
        '<label class="add-response-title">Title <span>(optional)</span>'
        '<input name="title" maxlength="120" autocomplete="off"></label>'
        '<label class="add-response-markdown">Response Markdown'
        '<textarea name="markdown" rows="12" maxlength="10485760" required '
        'placeholder="Paste a response from Claude, Grok, ChatGPT, Gemini, or another agent">'
        "</textarea></label></div>"
        '<div class="add-response-actions">'
        '<p class="add-response-status" data-add-entry-status role="status" '
        'aria-live="polite"></p>'
        '<button type="submit">Save and open</button></div></form></details>'
    )
    selected_category = next(
        (item for item in categories if item["id"] == category_id),
        None,
    )
    delete_category_form = ""
    if selected_category and selected_category["id"] != "inbox":
        selected_count = counts.get(selected_category["id"], 0)
        response_label = "response" if selected_count == 1 else "responses"
        delete_category_form = (
            '<form class="delete-category-form" data-delete-category '
            f'data-category-id="{html.escape(selected_category["id"], quote=True)}" '
            f'data-category-name="{html.escape(selected_category["name"], quote=True)}" '
            f'data-entry-count="{selected_count}">'
            '<div><strong>Delete this category</strong>'
            f"<span>{selected_count} {response_label} will be moved to Inbox.</span></div>"
            '<button class="danger-button" type="submit">Delete category</button>'
            '<p class="delete-category-status" data-delete-category-status '
            'role="status" aria-live="polite"></p></form>'
        )
    body = (
        '<main id="main" class="library-layout">'
        '<aside class="category-panel" aria-label="Categories"><h2>Categories</h2>'
        f"{''.join(nav_html)}</aside>"
        '<section class="library-content"><div class="library-heading">'
        "<div><p class=\"eyebrow\">Local, private, durable</p><h1>Response library</h1></div>"
        '<form class="search-form" action="/library" method="get" role="search">'
        f"{hidden_category}<label class=\"sr-only\" for=\"search\">Search responses</label>"
        f'<input id="search" name="q" type="search" value="{search_value}" '
        'placeholder="Search titles and content">'
        '<label class="sr-only" for="agent-filter">Filter by agent</label>'
        f'<select id="agent-filter" name="agent">{"".join(provider_options)}</select>'
        '<button type="submit">Search</button></form>'
        f"</div>{add_response_form}{delete_category_form}"
        f'<div class="entry-grid">{"".join(cards)}</div>'
        "</section></main>"
    )
    return page_shell("Library", body)


def render_entry_page(
    metadata: dict[str, Any],
    markdown: str,
    categories: list[dict[str, Any]],
) -> str:
    rendered, headings = render_markdown(markdown)
    toc_items = "".join(
        f'<li class="toc-level-{level}"><a href="#{html.escape(slug, quote=True)}">'
        f"{html.escape(text)}</a></li>"
        for level, text, slug in headings
        if level <= 3
    )
    toc = (
        '<aside class="toc" aria-label="Table of contents"><h2>Contents</h2>'
        f"<ol>{toc_items}</ol></aside>"
        if toc_items
        else ""
    )
    title = metadata.get("title", "Response")
    category = metadata.get("category", {}).get("name", DEFAULT_CATEGORY)
    source = entry_source(metadata)
    source_class = (
        "source-native"
        if source["support_level"] == "native"
        else "source-imported"
    )
    created = format_timestamp(metadata.get("created_at", ""))
    entry_id = metadata.get("id", "")
    category_options = "".join(
        f'<option value="{html.escape(item["name"], quote=True)}"></option>'
        for item in categories
        if isinstance(item.get("name"), str)
    )
    category_input_id = f"entry-category-{entry_id}"
    category_form = (
        f'<form class="category-form" data-category-form '
        f'data-entry-id="{html.escape(entry_id, quote=True)}">'
        f'<label for="{html.escape(category_input_id, quote=True)}">Move to category</label>'
        '<div class="category-form-row">'
        f'<input id="{html.escape(category_input_id, quote=True)}" name="category" '
        f'list="folio-categories" value="{html.escape(category, quote=True)}" '
        'maxlength="80" required autocomplete="off">'
        '<button type="submit">Move</button></div>'
        f'<datalist id="folio-categories">{category_options}</datalist>'
        '<p class="category-status" data-category-status role="status" '
        'aria-live="polite"></p></form>'
    )
    body = (
        '<main id="main" class="reader-shell">'
        f"{toc}<article class=\"reader\"><header class=\"reader-header\">"
        f'<a class="back-link" href="/library">← Library</a>'
        f"<h1>{html.escape(title)}</h1>"
        '<div class="reader-meta">'
        f'<span data-category-label>{html.escape(category)}</span>'
        f'<span class="source-badge {source_class}">'
        f"{html.escape(source_badge_text(source))}</span>"
        f"<time>{html.escape(created)}</time>"
        '<button type="button" data-print>Print</button></div></header>'
        f"{category_form}"
        f'<div class="markdown-body">{rendered}</div>'
        '<footer class="reader-footer"><a href="/library">Return to Library</a></footer>'
        "</article></main>"
    )
    return page_shell(title, body, description=f"Saved Folio response: {title}")


class FolioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(
    store: FolioStore,
    instance_id: str,
    auth_token: str,
    route_prefix: str,
) -> type[BaseHTTPRequestHandler]:
    session_cookie_name = auth_cookie_name(instance_id)

    class FolioHandler(BaseHTTPRequestHandler):
        server_version = f"Folio/{VERSION}"

        def log_message(self, _format_string: str, *_args: Any) -> None:
            return

        def valid_host_header(self) -> bool:
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                return False
            active_port = self.server.server_address[1]
            allowed = {
                f"{LOOPBACK_HOST}:{active_port}",
                f"localhost:{active_port}",
            }
            return hosts[0].strip().casefold() in allowed

        def valid_write_request(self) -> bool:
            if self.headers.get("X-Folio-Request") != "1":
                return False
            origins = self.headers.get_all("Origin", [])
            if len(origins) != 1:
                return False
            active_port = self.server.server_address[1]
            allowed = {
                f"http://{LOOPBACK_HOST}:{active_port}",
                f"http://localhost:{active_port}",
            }
            return origins[0].strip().casefold() in allowed

        def presented_auth_token(self) -> str | None:
            authorization = self.headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                return authorization[7:]
            cookie_header = self.headers.get("Cookie")
            if not cookie_header:
                return None
            try:
                cookies = SimpleCookie()
                cookies.load(cookie_header)
            except Exception:
                return None
            cookie = cookies.get(session_cookie_name)
            return cookie.value if cookie is not None else None

        def authenticated(self) -> bool:
            presented = self.presented_auth_token()
            return (
                isinstance(presented, str)
                and hmac.compare_digest(presented, auth_token)
            )

        def send_unauthorized(self) -> None:
            content = b"Folio authentication required.\n"
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("WWW-Authenticate", 'Bearer realm="Folio"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(content)

        def establish_browser_session(
            self,
            parsed: urllib.parse.SplitResult,
        ) -> None:
            parameters = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            supplied = parameters.get("token", [])
            if (
                len(supplied) != 1
                or not hmac.compare_digest(supplied[0], auth_token)
            ):
                self.send_unauthorized()
                return
            destinations = parameters.get("next", ["/library"])
            destination = destinations[0] if len(destinations) == 1 else "/library"
            destination_parts = urllib.parse.urlsplit(destination)
            if (
                destination_parts.scheme
                or destination_parts.netloc
                or not destination_parts.path.startswith("/")
                or destination_parts.path.startswith("//")
                or destination_parts.path == "/auth"
            ):
                destination = "/library"
            scoped_destination = f"{route_prefix}{destination}"
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", scoped_destination)
            self.send_header(
                "Set-Cookie",
                f"{session_cookie_name}={auth_token}; "
                f"Path={route_prefix}/; HttpOnly; SameSite=Strict",
            )
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

        def scoped_request(
            self,
            parsed: urllib.parse.SplitResult,
        ) -> urllib.parse.SplitResult | None:
            if parsed.path == route_prefix:
                return parsed._replace(path="/")
            if not parsed.path.startswith(f"{route_prefix}/"):
                return None
            return parsed._replace(path=parsed.path[len(route_prefix) :])

        def send_content(
            self,
            status: HTTPStatus,
            content: bytes,
            content_type: str,
            cache_control: str = "no-store",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Permissions-Policy",
                "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
            )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(content)

        def send_html(self, status: HTTPStatus, value: str) -> None:
            scoped = value.replace(
                '<html lang="en">',
                f'<html lang="en" data-folio-base="{route_prefix}">',
                1,
            )
            for attribute in ("href", "src", "action"):
                scoped = scoped.replace(
                    f'{attribute}="/',
                    f'{attribute}="{route_prefix}/',
                )
            self.send_content(
                status,
                scoped.encode("utf-8"),
                "text/html; charset=utf-8",
            )

        def send_json(self, status: HTTPStatus, value: Any) -> None:
            self.send_content(status, json_bytes(value), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            if not self.valid_host_header():
                self.send_content(
                    HTTPStatus.MISDIRECTED_REQUEST,
                    b"Invalid Host header.\n",
                    "text/plain; charset=utf-8",
                )
                return
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/auth":
                self.establish_browser_session(parsed)
                return
            if not self.authenticated():
                self.send_unauthorized()
                return
            parsed = self.scoped_request(parsed)
            if parsed is None:
                self.send_html(
                    HTTPStatus.NOT_FOUND,
                    page_shell("Not found", not_found_body()),
                )
                return
            if parsed.path == "/health":
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "app": APP_NAME,
                        "version": VERSION,
                        "instance_id": instance_id,
                    },
                )
                return
            if parsed.path in {"/", "/library"}:
                parameters = urllib.parse.parse_qs(parsed.query)
                category = parameters.get("category", [None])[0]
                query = parameters.get("q", [""])[0][:200]
                provider = parameters.get("agent", [None])[0]
                known = {item["id"] for item in store.categories()}
                if category and category not in known:
                    self.send_html(HTTPStatus.NOT_FOUND, page_shell("Not found", not_found_body()))
                    return
                if provider and not PROVIDER_ID_RE.fullmatch(provider):
                    self.send_html(
                        HTTPStatus.NOT_FOUND,
                        page_shell("Not found", not_found_body()),
                    )
                    return
                self.send_html(
                    HTTPStatus.OK,
                    render_library_page(store, category, query, provider),
                )
                return
            entry_match = re.fullmatch(r"/entry/([^/]+)", parsed.path)
            if entry_match:
                try:
                    metadata, markdown = store.get_entry(entry_match.group(1))
                except FolioError:
                    self.send_html(HTTPStatus.NOT_FOUND, page_shell("Not found", not_found_body()))
                    return
                self.send_html(
                    HTTPStatus.OK,
                    render_entry_page(metadata, markdown, store.categories()),
                )
                return
            if parsed.path in {"/static/folio.css", "/static/folio.js"}:
                filename = parsed.path.rsplit("/", 1)[-1]
                path = STATIC_ROOT / filename
                try:
                    content = path.read_bytes()
                except OSError:
                    self.send_html(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        page_shell("Asset unavailable", error_body("A required Folio asset is missing.")),
                    )
                    return
                content_type = (
                    "text/css; charset=utf-8"
                    if filename.endswith(".css")
                    else "text/javascript; charset=utf-8"
                )
                self.send_content(
                    HTTPStatus.OK,
                    content,
                    content_type,
                    cache_control="public, max-age=300",
                )
                return
            if parsed.path == "/favicon.ico":
                self.send_content(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return
            self.send_html(HTTPStatus.NOT_FOUND, page_shell("Not found", not_found_body()))

        def do_POST(self) -> None:
            if not self.valid_host_header():
                self.send_content(
                    HTTPStatus.MISDIRECTED_REQUEST,
                    b"Invalid Host header.\n",
                    "text/plain; charset=utf-8",
                )
                return
            if not self.authenticated():
                self.send_unauthorized()
                return
            parsed = urllib.parse.urlsplit(self.path)
            parsed = self.scoped_request(parsed)
            if parsed is None:
                self.send_json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "error", "error": "Endpoint not found."},
                )
                return
            if not self.valid_write_request():
                self.send_json(
                    HTTPStatus.FORBIDDEN,
                    {"status": "error", "error": "Invalid write request."},
                )
                return
            entry_create = parsed.path == "/api/entry"
            entry_match = re.fullmatch(r"/api/entry/([^/]+)/category", parsed.path)
            entry_delete_match = re.fullmatch(
                r"/api/entry/([^/]+)/delete",
                parsed.path,
            )
            category_delete_match = re.fullmatch(
                r"/api/category/([^/]+)/delete",
                parsed.path,
            )
            if (
                not entry_create
                and not entry_match
                and not entry_delete_match
                and not category_delete_match
            ):
                self.send_json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "error", "error": "Endpoint not found."},
                )
                return
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().casefold() != "application/json":
                self.send_json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"status": "error", "error": "Expected application/json."},
                )
                return
            try:
                content_length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                content_length = -1
            maximum_body = (
                MAX_ENTRY_API_BODY_BYTES if entry_create else MAX_API_BODY_BYTES
            )
            if not 1 <= content_length <= maximum_body:
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "error", "error": "Invalid request size."},
                )
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "error", "error": "Invalid JSON request."},
                )
                return
            if not isinstance(payload, dict):
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "error", "error": "JSON request must be an object."},
                )
                return
            if entry_create:
                allowed_fields = {"markdown", "category", "title", "agent"}
                if set(payload) - allowed_fields:
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "status": "error",
                            "error": "Entry request contains unsupported fields.",
                        },
                    )
                    return
                markdown = payload.get("markdown")
                category = payload.get("category", DEFAULT_CATEGORY)
                title = payload.get("title")
                agent = payload.get("agent", "generic")
                if not isinstance(markdown, str):
                    error_message = "Response Markdown must be text."
                elif not isinstance(category, str):
                    error_message = "Category must be text."
                elif title is not None and not isinstance(title, str):
                    error_message = "Title must be text."
                elif not isinstance(agent, str):
                    error_message = "Agent must be text."
                else:
                    error_message = ""
                if error_message:
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"status": "error", "error": error_message},
                    )
                    return
                try:
                    source = source_descriptor(
                        agent=agent,
                        support_level="imported",
                        transport="web",
                        adapter="folio-web-v1",
                    )
                    saved = store.add_entry(
                        markdown,
                        category_name=category,
                        title=title or None,
                        source=source,
                    )
                except FolioError as error:
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"status": "error", "error": str(error)},
                    )
                    return
                metadata = saved["metadata"]
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "status": "saved",
                        "entry_id": metadata["id"],
                        "title": metadata["title"],
                        "category": metadata["category"]["name"],
                        "agent": metadata["source"]["label"],
                        "reader_path": metadata["reader_path"],
                    },
                )
                return
            if category_delete_match:
                try:
                    deleted = store.delete_category(category_delete_match.group(1))
                except FolioError as error:
                    status = (
                        HTTPStatus.NOT_FOUND
                        if str(error) == "Category not found."
                        else HTTPStatus.BAD_REQUEST
                    )
                    self.send_json(
                        status,
                        {"status": "error", "error": str(error)},
                    )
                    return
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "status": "deleted",
                        "category": deleted["category"]["name"],
                        "category_id": deleted["category"]["id"],
                        "moved_count": deleted["moved_count"],
                        "destination": deleted["destination"]["name"],
                        "library_path": "/library?category=inbox",
                    },
                )
                return
            if entry_delete_match:
                try:
                    deleted = store.delete_entry(entry_delete_match.group(1))
                except FolioError as error:
                    status = (
                        HTTPStatus.NOT_FOUND
                        if str(error) == "Entry not found."
                        else HTTPStatus.BAD_REQUEST
                    )
                    self.send_json(
                        status,
                        {"status": "error", "error": str(error)},
                    )
                    return
                metadata = deleted["metadata"]
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "status": "deleted",
                        "entry_id": metadata["id"],
                        "title": metadata["title"],
                        "category": metadata["category"]["name"],
                    },
                )
                return
            category = payload.get("category") if isinstance(payload, dict) else None
            if not isinstance(category, str):
                self.send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "error", "error": "Category must be text."},
                )
                return
            try:
                moved = store.move_entry(entry_match.group(1), category)
            except FolioError as error:
                status = (
                    HTTPStatus.NOT_FOUND
                    if str(error) == "Entry not found."
                    else HTTPStatus.BAD_REQUEST
                )
                self.send_json(
                    status,
                    {"status": "error", "error": str(error)},
                )
                return
            metadata = moved["metadata"]
            self.send_json(
                HTTPStatus.OK,
                {
                    "status": "moved" if moved["moved"] else "unchanged",
                    "entry_id": metadata["id"],
                    "category": metadata["category"]["name"],
                    "category_id": metadata["category"]["id"],
                    "reader_path": metadata["reader_path"],
                },
            )

    return FolioHandler


def not_found_body() -> str:
    return (
        '<main id="main" class="message-page"><h1>Page not found</h1>'
        '<p>This local Folio page does not exist.</p><a href="/library">Open Library</a></main>'
    )


def error_body(message: str) -> str:
    return (
        '<main id="main" class="message-page"><h1>Folio could not render this page</h1>'
        f"<p>{html.escape(message)}</p><a href=\"/library\">Open Library</a></main>"
    )


def read_server_state(store: FolioStore) -> dict[str, Any] | None:
    try:
        value = json.loads(store.server_state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if not isinstance(value.get("port"), int) or not isinstance(value.get("pid"), int):
        return None
    if not isinstance(value.get("instance_id"), str):
        return None
    auth_token = value.get("auth_token")
    if auth_token is not None and not isinstance(auth_token, str):
        return None
    app_version = value.get("app_version")
    if app_version is not None and not isinstance(app_version, str):
        return None
    route_prefix = value.get("route_prefix")
    if route_prefix is not None and (
        not isinstance(route_prefix, str)
        or not re.fullmatch(r"/s/[A-Za-z0-9_-]{16,80}", route_prefix)
    ):
        return None
    return value


def health_for_state(state: dict[str, Any], timeout: float = 0.35) -> dict[str, Any] | None:
    port = state.get("port")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return None
    try:
        route_prefix = state.get("route_prefix")
        health_path = (
            f"{route_prefix}/health"
            if isinstance(route_prefix, str)
            else "/health"
        )
        request = urllib.request.Request(
            f"http://{LOOPBACK_HOST}:{port}{health_path}",
        )
        auth_token = state.get("auth_token")
        if isinstance(auth_token, str):
            request.add_header("Authorization", f"Bearer {auth_token}")
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError):
        return None
    if (
        isinstance(value, dict)
        and value.get("status") == "ok"
        and value.get("instance_id") == state.get("instance_id")
    ):
        return value
    return None


def current_server_matches(
    state: dict[str, Any],
    health: dict[str, Any],
) -> bool:
    return (
        state.get("version") == SERVER_STATE_VERSION
        and state.get("app_version") == VERSION
        and isinstance(state.get("auth_token"), str)
        and isinstance(state.get("route_prefix"), str)
        and health.get("version") == VERSION
    )


def server_browser_url(state: dict[str, Any], destination: str) -> str:
    port = state["port"]
    token = state.get("auth_token")
    if not isinstance(token, str):
        return f"http://{LOOPBACK_HOST}:{port}{destination}"
    query = urllib.parse.urlencode({"token": token, "next": destination})
    return f"http://{LOOPBACK_HOST}:{port}/auth?{query}"


def remove_state_if_owned(store: FolioStore, instance_id: str) -> None:
    state = read_server_state(store)
    if state and state.get("instance_id") == instance_id:
        with contextlib.suppress(FileNotFoundError):
            store.server_state_path.unlink()


def stop_verified_server(
    store: FolioStore,
    state: dict[str, Any],
    timeout: float = 4.0,
) -> bool:
    if not health_for_state(state):
        return False
    try:
        os.kill(state["pid"], signal.SIGTERM)
    except ProcessLookupError:
        with contextlib.suppress(FileNotFoundError):
            store.server_state_path.unlink()
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not health_for_state(state, timeout=0.15):
            with contextlib.suppress(FileNotFoundError):
                store.server_state_path.unlink()
            return True
        time.sleep(0.05)
    return False


def run_server(store: FolioStore, port: int) -> None:
    if not 0 <= port <= 65535:
        raise FolioError("Port must be between 0 and 65535.")
    store.ensure_layout()
    existing = read_server_state(store)
    if existing and health_for_state(existing):
        raise FolioError(
            f"Folio is already running at http://{LOOPBACK_HOST}:{existing['port']}."
        )
    if existing:
        with contextlib.suppress(FileNotFoundError):
            store.server_state_path.unlink()
    instance_id = secrets.token_urlsafe(18)
    auth_token = secrets.token_urlsafe(32)
    route_prefix = f"/s/{instance_id}"
    try:
        server = FolioHTTPServer(
            (LOOPBACK_HOST, port),
            make_handler(store, instance_id, auth_token, route_prefix),
        )
    except OSError as error:
        raise FolioError(f"Cannot bind Folio server on loopback: {error}") from error
    actual_port = server.server_address[1]
    state = {
        "version": SERVER_STATE_VERSION,
        "app_version": VERSION,
        "pid": os.getpid(),
        "host": LOOPBACK_HOST,
        "port": actual_port,
        "instance_id": instance_id,
        "auth_token": auth_token,
        "route_prefix": route_prefix,
        "started_at": iso_now(),
    }
    atomic_write(store.server_state_path, json_bytes(state))

    def request_shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
    print(
        json.dumps(
            {
                "status": "serving",
                "url": server_browser_url(state, "/library"),
            }
        )
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        remove_state_if_owned(store, instance_id)


def ensure_server(store: FolioStore, timeout: float = 6.0) -> dict[str, Any]:
    store.ensure_layout()
    with exclusive_lock(store.server_lock_path):
        state = read_server_state(store)
        if state:
            health = health_for_state(state)
            if health and current_server_matches(state, health):
                return state
            if health and not stop_verified_server(store, state):
                raise FolioError("An older Folio server did not stop during upgrade.")
        if state:
            with contextlib.suppress(FileNotFoundError):
                store.server_state_path.unlink()
        log_path = store.run_dir / "server.log"
        environment = os.environ.copy()
        environment["FOLIO_HOME"] = str(store.home)
        command = [sys.executable, str(Path(__file__).resolve()), "serve", "--port", "0"]
        with log_path.open("ab") as log_handle:
            os.fchmod(log_handle.fileno(), 0o600)
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                env=environment,
                start_new_session=True,
                close_fds=True,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = read_server_state(store)
            if state:
                health = health_for_state(state)
                if health and current_server_matches(state, health):
                    return state
            time.sleep(0.05)
    raise FolioError(f"Folio server did not start. See {store.run_dir / 'server.log'}.")


def open_local_url(url: str, no_open: bool) -> bool:
    if no_open:
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        return False


def stop_server(store: FolioStore, timeout: float = 4.0) -> dict[str, Any]:
    state = read_server_state(store)
    if not state:
        return {"status": "stopped", "was_running": False}
    if not health_for_state(state):
        with contextlib.suppress(FileNotFoundError):
            store.server_state_path.unlink()
        return {"status": "stopped", "was_running": False, "recovered_stale_state": True}
    if stop_verified_server(store, state, timeout=timeout):
        return {"status": "stopped", "was_running": True}
    raise FolioError("Folio server did not stop after SIGTERM.")


def status_payload(store: FolioStore) -> dict[str, Any]:
    state = read_server_state(store)
    health = health_for_state(state) if state else None
    if state and health:
        return {
            "status": "running",
            "pid": state["pid"],
            "host": state["host"],
            "port": state["port"],
            "version": health.get("version"),
            "url": server_browser_url(state, "/library"),
            "started_at": state.get("started_at"),
        }
    stale = state is not None
    if stale:
        with contextlib.suppress(FileNotFoundError):
            store.server_state_path.unlink()
    return {"status": "stopped", "recovered_stale_state": stale}


def doctor_payload(store: FolioStore) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "name": "python",
            "ok": sys.version_info >= (3, 11),
            "detail": sys.version.split()[0],
            "required": True,
        }
    )
    static_missing = [
        name for name in ("folio.css", "folio.js") if not (STATIC_ROOT / name).is_file()
    ]
    checks.append(
        {
            "name": "static_assets",
            "ok": not static_missing,
            "detail": "available" if not static_missing else f"missing: {', '.join(static_missing)}",
            "required": True,
        }
    )
    try:
        store.ensure_layout()
        probe = store.run_dir / f".doctor-{secrets.token_hex(4)}"
        probe_descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(probe_descriptor, "w", encoding="utf-8") as probe_handle:
            probe_handle.write("ok")
            probe_handle.flush()
            os.fsync(probe_handle.fileno())
        probe.unlink()
        home_ok = True
        home_detail = str(store.home)
    except OSError as error:
        home_ok = False
        home_detail = str(error)
    checks.append(
        {
            "name": "folio_home",
            "ok": home_ok,
            "detail": home_detail,
            "required": True,
        }
    )
    root = sessions_root()
    checks.append(
        {
            "name": "codex_adapter",
            "ok": root.is_dir() and os.access(root, os.R_OK),
            "detail": str(root),
            "required": False,
        }
    )
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
            probe_socket.bind((LOOPBACK_HOST, 0))
        loopback_ok = True
        loopback_detail = LOOPBACK_HOST
    except OSError as error:
        loopback_ok = False
        loopback_detail = str(error)
    checks.append(
        {
            "name": "loopback_bind",
            "ok": loopback_ok,
            "detail": loopback_detail,
            "required": True,
        }
    )
    return {
        "status": (
            "ok"
            if all(check["ok"] for check in checks if check["required"])
            else "issues"
        ),
        "checks": checks,
        "folio_home": str(store.home),
    }


def positive_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="folio",
        description="Save AI responses to a private local Markdown library.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser(
        "capture",
        help="Capture through an available native agent adapter.",
    )
    capture.add_argument("--agent", default="codex")
    capture.add_argument("--category", default=DEFAULT_CATEGORY)
    capture.add_argument("--title")
    capture.add_argument("--conversation-id")
    capture.add_argument("--thread-id")
    capture.add_argument("--no-open", action="store_true")

    add = commands.add_parser(
        "add",
        help="Import AI response Markdown from a file, stdin, or clipboard.",
    )
    add_source = add.add_mutually_exclusive_group(required=True)
    add_source.add_argument("--source", type=Path)
    add_source.add_argument("--stdin", action="store_true")
    add_source.add_argument("--clipboard", action="store_true")
    add.add_argument("--agent", default="generic")
    add.add_argument("--source-label")
    add.add_argument("--category", default=DEFAULT_CATEGORY)
    add.add_argument("--title")
    add.add_argument("--no-open", action="store_true")

    library = commands.add_parser("library", help="Open the local response library.")
    library.add_argument("--no-open", action="store_true")

    serve = commands.add_parser("serve", help="Run the Folio server in the foreground.")
    serve.add_argument("--port", type=positive_port, default=0)

    commands.add_parser("status", help="Show Folio server status.")
    commands.add_parser("stop", help="Stop the Folio server.")
    commands.add_parser("doctor", help="Check Folio's local environment.")
    return parser


def entry_command_result(
    store: FolioStore,
    markdown: str,
    category: str,
    title: str | None,
    source: dict[str, Any],
    no_open: bool,
) -> dict[str, Any]:
    saved = store.add_entry(markdown, category_name=category, title=title, source=source)
    state = ensure_server(store)
    url = server_browser_url(state, saved["metadata"]["reader_path"])
    opened = open_local_url(url, no_open)
    return {
        "status": "saved",
        "entry_id": saved["metadata"]["id"],
        "title": saved["metadata"]["title"],
        "category": saved["metadata"]["category"]["name"],
        "agent": saved["metadata"]["source"]["label"],
        "source": source_badge_text(saved["metadata"]["source"]),
        "markdown_path": saved["markdown_path"],
        "metadata_path": saved["metadata_path"],
        "url": url,
        "browser_opened": opened,
    }


def execute(args: argparse.Namespace, store: FolioStore) -> dict[str, Any] | None:
    if args.command == "capture":
        if (
            args.conversation_id
            and args.thread_id
            and args.conversation_id != args.thread_id
        ):
            raise FolioError("--conversation-id and --thread-id must match.")
        conversation_id = args.conversation_id or args.thread_id
        markdown, source = capture_agent_response(args.agent, conversation_id)
        return entry_command_result(
            store, markdown, args.category, args.title, source, args.no_open
        )
    if args.command == "add":
        if args.source is not None:
            markdown, source_path = read_markdown_file(args.source)
            transport = "file"
            locator = {"file_name": source_path.name}
            adapter = "folio-file-v1"
        elif args.stdin:
            markdown = read_stdin_markdown()
            transport = "stdin"
            locator = None
            adapter = "folio-stdin-v1"
        else:
            markdown = read_clipboard_markdown()
            transport = "clipboard"
            locator = None
            adapter = "folio-clipboard-v1"
        source = source_descriptor(
            agent=args.agent,
            source_label=args.source_label,
            support_level="imported",
            transport=transport,
            adapter=adapter,
            locator=locator,
        )
        return entry_command_result(
            store, markdown, args.category, args.title, source, args.no_open
        )
    if args.command == "library":
        state = ensure_server(store)
        url = server_browser_url(state, "/library")
        return {
            "status": "ready",
            "url": url,
            "browser_opened": open_local_url(url, args.no_open),
        }
    if args.command == "serve":
        run_server(store, args.port)
        return None
    if args.command == "status":
        return status_payload(store)
    if args.command == "stop":
        return stop_server(store)
    if args.command == "doctor":
        return doctor_payload(store)
    raise FolioError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = FolioStore()
    try:
        result = execute(args, store)
    except FolioError as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}), file=sys.stderr)
        return 130
    if result is not None:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
