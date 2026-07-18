"""Filesystem layout. Paths are resolved at call time so tests can
repoint everything with the RAVENS_NEST_DATA environment variable."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("RAVENS_NEST_DATA", "data"))


def events_dir() -> Path:
    return data_dir() / "events"


def assets_dir() -> Path:
    return data_dir() / "assets"


def cache_path() -> Path:
    return data_dir() / "cache.db"


def repo_root() -> Path:
    """Root of the Git repository that holds the data directory."""
    override = os.environ.get("RAVENS_NEST_REPO")
    if override:
        return Path(override)
    return data_dir().parent


def inbox_dir() -> Path:
    """Folder watched for new photos. Point RAVENS_NEST_INBOX at an
    iCloud/Dropbox folder for offline capture."""
    override = os.environ.get("RAVENS_NEST_INBOX")
    return Path(override) if override else data_dir() / "inbox"


def pending_dir() -> Path:
    """Capture cards awaiting review — transient state, not committed."""
    return data_dir() / "pending"


DEFAULT_VISION_MODEL = "claude-opus-4-8"


def vision_model() -> str:
    """Model for photo identification — swappable as models change."""
    return os.environ.get("RAVENS_NEST_VISION_MODEL", DEFAULT_VISION_MODEL)


def load_env_file() -> None:
    """Load .env from the repo root (ANTHROPIC_API_KEY) without overriding
    variables already set in the real environment."""
    env_path = repo_root() / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
