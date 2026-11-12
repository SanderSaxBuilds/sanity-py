"""Helpers for draft, version and published document IDs.

Same rules as ``@sanity/client/csm``: drafts live under ``drafts.<id>`` and
release versions under ``versions.<releaseId>.<id>``.
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "DRAFTS_PREFIX",
    "VERSION_PREFIX",
    "get_draft_id",
    "get_published_id",
    "get_version_from_id",
    "get_version_id",
    "is_draft_id",
    "is_published_id",
    "is_version_id",
]

DRAFTS_PREFIX = "drafts."
VERSION_PREFIX = "versions."


def is_draft_id(document_id: str) -> bool:
    """``True`` for ``drafts.<id>``."""
    return document_id.startswith(DRAFTS_PREFIX)


def is_version_id(document_id: str) -> bool:
    """``True`` for ``versions.<releaseId>.<id>``."""
    return document_id.startswith(VERSION_PREFIX)


def is_published_id(document_id: str) -> bool:
    """``True`` when the ID is neither a draft nor a version."""
    return not is_draft_id(document_id) and not is_version_id(document_id)


def get_published_id(document_id: str) -> str:
    """Strip a ``drafts.`` or ``versions.<releaseId>.`` prefix."""
    if is_version_id(document_id):
        return ".".join(document_id.split(".")[2:])
    if is_draft_id(document_id):
        return document_id[len(DRAFTS_PREFIX) :]
    return document_id


def get_draft_id(document_id: str) -> str:
    """The draft ID for any published, draft or version ID."""
    if is_version_id(document_id):
        return DRAFTS_PREFIX + get_published_id(document_id)
    return document_id if is_draft_id(document_id) else DRAFTS_PREFIX + document_id


def get_version_id(document_id: str, release_id: str) -> str:
    """The ID of ``document_id`` inside release ``release_id``."""
    if release_id in ("drafts", "published"):
        raise ValueError('Version can not be "published" or "drafts"')
    return f"{VERSION_PREFIX}{release_id}.{get_published_id(document_id)}"


def get_version_from_id(document_id: str) -> Optional[str]:
    """The release ID of a version ID, ``None`` for anything else."""
    if not is_version_id(document_id):
        return None
    parts = document_id.split(".")
    return parts[1] if len(parts) > 1 else None
