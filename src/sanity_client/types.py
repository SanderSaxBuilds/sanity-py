"""Public result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from typing_extensions import NotRequired, TypedDict

from ._types import AssetType, Document, IdSelection, ListenVisibility, MutationSelection, QueryParams, Visibility

__all__ = [
    "AssetType",
    "Document",
    "IdSelection",
    "ListenEvent",
    "ListenVisibility",
    "MutationResult",
    "MutationSelection",
    "QueryParams",
    "RawQueryResponse",
    "Visibility",
]


class RawQueryResponse(TypedDict):
    """The full ``/data/query`` body, returned when ``filter_response=False``."""

    result: Any
    ms: int
    query: NotRequired[str]
    resultSourceMap: NotRequired[Dict[str, Any]]
    syncTags: NotRequired[List[str]]


@dataclass(frozen=True)
class MutationResult:
    """What a mutation returned.

    Attributes:
        transaction_id: ID of the transaction the mutations ran in.
        results: One entry per mutated document, ``{"id", "operation", "document"?}``.
        raw: The response body as received.
    """

    transaction_id: Optional[str]
    results: List[Dict[str, Any]]
    raw: Dict[str, Any] = field(repr=False)

    @classmethod
    def from_response(cls, body: Any) -> "MutationResult":
        body = body if isinstance(body, dict) else {}
        results = body.get("results")
        return cls(
            transaction_id=body.get("transactionId"),
            results=[r for r in results if isinstance(r, dict)] if isinstance(results, list) else [],
            raw=body,
        )

    @property
    def document_ids(self) -> List[str]:
        """IDs of every document the mutations touched, in order."""
        return [str(r["id"]) for r in self.results if "id" in r]

    @property
    def document_id(self) -> Optional[str]:
        """ID of the first document touched, or ``None``."""
        ids = self.document_ids
        return ids[0] if ids else None

    @property
    def documents(self) -> List[Document]:
        """The documents after the mutation. Empty unless ``return_documents`` was on."""
        return [r["document"] for r in self.results if isinstance(r.get("document"), dict)]

    @property
    def document(self) -> Optional[Document]:
        """The first returned document, or ``None``."""
        docs = self.documents
        return docs[0] if docs else None


@dataclass(frozen=True)
class ListenEvent:
    """One event from :meth:`~sanity_client.SanityClient.listen`.

    ``type`` is ``"welcome"``, ``"mutation"``, ``"reconnect"``,
    ``"welcomeback"``, ``"reset"`` or ``"open"``. ``data`` is the parsed JSON
    payload. For mutation events it holds ``documentId``, ``transition``,
    ``result`` and the rest of the fields the API sends.
    """

    type: str
    data: Mapping[str, Any] = field(default_factory=dict)
    id: Optional[str] = None

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """``{"type": ..., **data}``, the shape the JS client emits."""
        return {"type": self.type, **dict(self.data)}

    @property
    def document_id(self) -> Optional[str]:
        value = self.data.get("documentId")
        return value if isinstance(value, str) else None

    @property
    def transition(self) -> Optional[str]:
        value = self.data.get("transition")
        return value if isinstance(value, str) else None

    @property
    def result(self) -> Optional[Document]:
        value = self.data.get("result")
        return value if isinstance(value, dict) else None

    @property
    def previous(self) -> Optional[Document]:
        value = self.data.get("previous")
        return value if isinstance(value, dict) else None

    @property
    def transaction_id(self) -> Optional[str]:
        value = self.data.get("transactionId")
        return value if isinstance(value, str) else None
