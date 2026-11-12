"""Transaction builders, the Python side of ``client.transaction()``.

All mutations in a transaction succeed or fail together::

    (
        client.transaction()
        .create({"_type": "movie", "title": "Alien"})
        .patch("person_1", lambda p: p.inc({"movies": 1}))
        .delete("movie_old")
        .commit()
    )
"""

from __future__ import annotations

import copy
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Mapping,
    Optional,
    Sequence,
    TypeVar,
    Union,
)

from typing_extensions import Self

from ._types import Visibility
from .config import validate_document_id
from .patch import AsyncPatch, BasePatch, Patch

if TYPE_CHECKING:
    from ._client import AsyncSanityClient, SanityClient
    from .types import MutationResult

__all__ = ["AsyncTransaction", "BaseTransaction", "Transaction"]

Mutation = Dict[str, Any]
P = TypeVar("P", bound=BasePatch)


def _validate_object(op: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{op}() takes an object of properties")
    return value


def _require_document_id(op: str, doc: Mapping[str, Any]) -> None:
    if not doc.get("_id"):
        raise ValueError(f'{op}() requires that the document contains an ID ("_id" property)')
    validate_document_id(op, doc["_id"])


class BaseTransaction(Generic[P]):
    """Operations shared by :class:`Transaction` and :class:`AsyncTransaction`."""

    def __init__(
        self,
        operations: Optional[Sequence[Mapping[str, Any]]] = None,
        transaction_id: Optional[str] = None,
    ) -> None:
        self.operations: List[Mutation] = [dict(op) for op in operations or []]
        self._transaction_id = transaction_id

    @property
    def transaction_id(self) -> Optional[str]:
        """The ID this transaction will be committed with, if one was set."""
        return self._transaction_id

    def set_transaction_id(self, transaction_id: str) -> Self:
        """Commit under this transaction ID instead of a generated one."""
        self._transaction_id = transaction_id
        return self

    def create(self, document: Mapping[str, Any]) -> Self:
        """Create a document. Without ``_id`` the Content Lake generates one."""
        _validate_object("create", document)
        return self._add({"create": dict(document)})

    def create_if_not_exists(self, document: Mapping[str, Any]) -> Self:
        """Create the document unless one with the same ``_id`` exists."""
        op = "createIfNotExists"
        _validate_object(op, document)
        _require_document_id(op, document)
        return self._add({op: dict(document)})

    def create_or_replace(self, document: Mapping[str, Any]) -> Self:
        """Create the document, or replace the one with the same ``_id``."""
        op = "createOrReplace"
        _validate_object(op, document)
        _require_document_id(op, document)
        return self._add({op: dict(document)})

    def delete(self, document_id: str) -> Self:
        """Delete the document with this ID."""
        validate_document_id("delete", document_id)
        return self._add({"delete": {"id": document_id}})

    def patch(
        self,
        target: Union[str, Mapping[str, Any], BasePatch],
        operations: Union[Mapping[str, Any], Callable[[P], BasePatch], None] = None,
    ) -> Self:
        """Add a patch.

        ``target`` is a document ID, a ``{"query": ..., "params": ...}``
        selection, or a patch built with ``client.patch()``. ``operations`` is a
        mapping such as ``{"set": {...}}`` or a function that receives a fresh
        patch and returns it with operations added.
        """
        if isinstance(target, BasePatch):
            return self._add({"patch": target.serialize()})
        if callable(operations):
            patch = operations(self._new_patch(target))
            if not isinstance(patch, BasePatch):
                raise TypeError("function passed to `patch()` must return the patch")
            return self._add({"patch": patch.serialize()})
        if isinstance(target, Mapping) and ("query" in target or "id" in target):
            return self._add({"patch": self._new_patch(target, operations).serialize()})
        if not isinstance(target, str):
            raise TypeError("patch() takes a document ID, a selection or a patch")
        return self._add({"patch": {"id": target, **dict(operations or {})}})

    def serialize(self) -> List[Mutation]:
        """The mutations as the API expects them."""
        return copy.deepcopy(self.operations)

    def to_json(self) -> List[Mutation]:
        """Same as :meth:`serialize`."""
        return self.serialize()

    def reset(self) -> Self:
        """Drop every mutation."""
        self.operations = []
        return self

    def __len__(self) -> int:
        return len(self.operations)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.operations!r}, transaction_id={self._transaction_id!r})"

    def _new_patch(self, selection: Any, operations: Optional[Mapping[str, Any]] = None) -> P:
        raise NotImplementedError

    def _add(self, mutation: Mutation) -> Self:
        self.operations.append(mutation)
        return self


class Transaction(BaseTransaction[Patch]):
    """A transaction bound to a :class:`~sanity_client.SanityClient`."""

    def __init__(
        self,
        operations: Optional[Sequence[Mapping[str, Any]]] = None,
        client: Optional["SanityClient"] = None,
        transaction_id: Optional[str] = None,
    ) -> None:
        super().__init__(operations, transaction_id)
        self._client = client

    def _new_patch(self, selection: Any, operations: Optional[Mapping[str, Any]] = None) -> Patch:
        return Patch(selection, operations, self._client)

    def clone(self) -> "Transaction":
        """An independent copy with the same client and transaction ID."""
        return Transaction(self.serialize(), self._client, self._transaction_id)

    def commit(
        self,
        *,
        return_documents: Optional[bool] = False,
        visibility: Optional[Visibility] = None,
        dry_run: Optional[bool] = None,
        auto_generate_array_keys: Optional[bool] = None,
        skip_cross_dataset_reference_validation: Optional[bool] = None,
        transaction_id: Optional[str] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> "MutationResult":
        """Send every mutation in one transaction."""
        if self._client is None:
            raise RuntimeError(
                "No `client` passed to transaction, either provide one or pass the "
                "transaction to a client's `mutate()` method"
            )
        return self._client.mutate(
            self,
            return_documents=return_documents,
            visibility=visibility,
            dry_run=dry_run,
            auto_generate_array_keys=auto_generate_array_keys,
            skip_cross_dataset_reference_validation=skip_cross_dataset_reference_validation,
            transaction_id=transaction_id or self._transaction_id,
            tag=tag,
            timeout=timeout,
        )


class AsyncTransaction(BaseTransaction[AsyncPatch]):
    """A transaction bound to an :class:`~sanity_client.AsyncSanityClient`."""

    def __init__(
        self,
        operations: Optional[Sequence[Mapping[str, Any]]] = None,
        client: Optional["AsyncSanityClient"] = None,
        transaction_id: Optional[str] = None,
    ) -> None:
        super().__init__(operations, transaction_id)
        self._client = client

    def _new_patch(self, selection: Any, operations: Optional[Mapping[str, Any]] = None) -> AsyncPatch:
        return AsyncPatch(selection, operations, self._client)

    def clone(self) -> "AsyncTransaction":
        """An independent copy with the same client and transaction ID."""
        return AsyncTransaction(self.serialize(), self._client, self._transaction_id)

    async def commit(
        self,
        *,
        return_documents: Optional[bool] = False,
        visibility: Optional[Visibility] = None,
        dry_run: Optional[bool] = None,
        auto_generate_array_keys: Optional[bool] = None,
        skip_cross_dataset_reference_validation: Optional[bool] = None,
        transaction_id: Optional[str] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> "MutationResult":
        """Send every mutation in one transaction."""
        if self._client is None:
            raise RuntimeError(
                "No `client` passed to transaction, either provide one or pass the "
                "transaction to a client's `mutate()` method"
            )
        return await self._client.mutate(
            self,
            return_documents=return_documents,
            visibility=visibility,
            dry_run=dry_run,
            auto_generate_array_keys=auto_generate_array_keys,
            skip_cross_dataset_reference_validation=skip_cross_dataset_reference_validation,
            transaction_id=transaction_id or self._transaction_id,
            tag=tag,
            timeout=timeout,
        )
