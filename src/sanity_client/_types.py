"""Small shared type aliases. Public ones are re-exported from ``sanity_client.types``."""

from __future__ import annotations

from typing import Any, Dict, Literal, Mapping

from typing_extensions import NotRequired, TypedDict

Visibility = Literal["sync", "async", "deferred"]
"""When a mutation's changes become visible to queries."""

AssetType = Literal["image", "file"]

ListenVisibility = Literal["transaction", "query"]

Document = Dict[str, Any]
"""A Sanity document, a plain ``dict`` with ``_id``, ``_type`` and friends."""

QueryParams = Mapping[str, Any]


class MutationSelection(TypedDict):
    """Documents picked by a GROQ filter, for ``delete`` and ``patch``."""

    query: str
    params: NotRequired[Mapping[str, Any]]


class IdSelection(TypedDict):
    id: str
