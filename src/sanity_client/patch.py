"""Patch builders, the Python side of ``client.patch()`` in @sanity/client.

A patch targets one document ID, a list of IDs, or a GROQ selection, and
collects operations that are sent together::

    client.patch("movie_123").set({"title": "Alien"}).inc({"views": 1}).commit()

Serialization is key-for-key the same as the JS ``Patch.serialize()``.
"""

from __future__ import annotations

import copy
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from typing_extensions import Self

from ._types import MutationSelection, Visibility

if TYPE_CHECKING:
    from ._client import AsyncSanityClient, SanityClient
    from .types import MutationResult

__all__ = ["AsyncPatch", "BasePatch", "InsertPosition", "Patch", "PatchSelection", "get_selection"]

InsertPosition = Literal["before", "after", "replace"]
PatchSelection = Union[str, Sequence[str], MutationSelection]
"""A document ID, a list of IDs, or ``{"query": ..., "params": ...}``."""

_INSERT_POSITIONS = ("before", "after", "replace")


def get_selection(selection: Any) -> Dict[str, Any]:
    """Normalize a selection the way @sanity/client's ``getSelection`` does.

    * ``"id"`` becomes ``{"id": "id"}``
    * ``["a", "b"]`` becomes ``{"query": "*[_id in $ids]", "params": {"ids": [...]}}``
    * ``{"query": ..., "params": ...}`` is kept (``params`` only if it is a mapping)
    * ``{"id": ...}`` is kept as is
    """
    if isinstance(selection, str):
        return {"id": selection}
    if isinstance(selection, (list, tuple)):
        return {"query": "*[_id in $ids]", "params": {"ids": list(selection)}}
    if isinstance(selection, Mapping):
        query = selection.get("query")
        if isinstance(query, str):
            params = selection.get("params")
            if isinstance(params, Mapping):
                return {"query": query, "params": dict(params)}
            return {"query": query}
        if "id" in selection:
            return {"id": selection["id"]}
    raise ValueError(
        "Unknown selection - must be one of:\n\n"
        "* Document ID (<docId>)\n"
        "* List of document IDs\n"
        "* Object containing `query`"
    )


class BasePatch:
    """Operations shared by :class:`Patch` and :class:`AsyncPatch`.

    Every method returns the patch itself, so calls chain. ``set``,
    ``set_if_missing``, ``diff_match_patch``, ``inc`` and ``dec`` merge into
    earlier calls of the same kind. ``unset`` replaces the earlier list.
    """

    def __init__(
        self,
        selection: PatchSelection,
        operations: Optional[Mapping[str, Any]] = None,
    ) -> None:
        get_selection(selection)  # validate early, like the JS constructor's callers
        self.selection: PatchSelection = selection
        self.operations: Dict[str, Any] = dict(operations or {})

    # -- operations ---------------------------------------------------------

    def set(self, attrs: Mapping[str, Any]) -> Self:
        """Set attributes, replacing current values. Keys may be JSONMatch paths."""
        return self._assign("set", attrs)

    def set_if_missing(self, attrs: Mapping[str, Any]) -> Self:
        """Set attributes only where they are not set yet."""
        return self._assign("setIfMissing", attrs)

    def diff_match_patch(self, attrs: Mapping[str, str]) -> Self:
        """Apply diff-match-patch strings to string attributes."""
        return self._assign("diffMatchPatch", attrs)

    def unset(self, attrs: Sequence[str]) -> Self:
        """Remove the given attribute paths."""
        if isinstance(attrs, str) or not isinstance(attrs, (list, tuple)):
            raise TypeError(
                "unset(attrs) takes a list of attributes to unset, non-list given"
            )
        if not all(isinstance(attr, str) for attr in attrs):
            raise TypeError("unset(attrs) takes a list of attribute paths as strings")
        self.operations["unset"] = list(attrs)
        return self

    def inc(self, attrs: Mapping[str, Union[int, float]]) -> Self:
        """Add to numeric attributes."""
        return self._assign("inc", attrs)

    def dec(self, attrs: Mapping[str, Union[int, float]]) -> Self:
        """Subtract from numeric attributes."""
        return self._assign("dec", attrs)

    def insert(self, at: InsertPosition, selector: str, items: Sequence[Any]) -> Self:
        """Insert ``items`` ``before``/``after`` the selector, or ``replace`` it.

        The Content Lake accepts one ``insert`` per patch. A second call
        raises ``ValueError``. Use a transaction with two patches instead.
        (@sanity/client merges the two into one object, which keeps only the
        second ``items`` list and drops the first without an error.)
        """
        if at not in _INSERT_POSITIONS:
            valid = ", ".join(f'"{loc}"' for loc in _INSERT_POSITIONS)
            raise ValueError(f'insert(at, selector, items) takes an "at"-argument which is one of: {valid}')
        if not isinstance(selector, str):
            raise TypeError('insert(at, selector, items) takes a "selector"-argument which must be a string')
        if isinstance(items, (str, bytes)) or not isinstance(items, (list, tuple)):
            raise TypeError('insert(at, selector, items) takes an "items"-argument which must be a list')
        if "insert" in self.operations:
            raise ValueError(
                "a patch can hold one insert operation. Put the next insert, append, "
                "prepend or splice in its own patch, for example in a transaction"
            )
        self.operations["insert"] = {at: selector, "items": list(items)}
        return self

    def append(self, selector: str, items: Sequence[Any]) -> Self:
        """Append ``items`` to the array at ``selector``."""
        return self.insert("after", f"{selector}[-1]", items)

    def prepend(self, selector: str, items: Sequence[Any]) -> Self:
        """Prepend ``items`` to the array at ``selector``."""
        return self.insert("before", f"{selector}[0]", items)

    def splice(
        self,
        selector: str,
        start: int,
        delete_count: Optional[int] = None,
        items: Optional[Sequence[Any]] = None,
    ) -> Self:
        """Change an array like JavaScript's ``Array.prototype.splice``.

        ``delete_count=None`` (or ``-1``) removes everything from ``start``.
        Negative ``start`` counts from the end.
        """
        delete_all = delete_count is None or delete_count == -1
        start_index = start - 1 if start < 0 else start
        del_count = -1 if delete_all else max(0, start + (delete_count or 0))
        del_range: Union[int, str] = "" if start_index < 0 and del_count >= 0 else del_count
        range_selector = f"{selector}[{start_index}:{del_range}]"
        return self.insert("replace", range_selector, list(items or []))

    def if_revision_id(self, rev: str) -> Self:
        """Apply the patch only if the document is still at revision ``rev``."""
        self.operations["ifRevisionID"] = rev
        return self

    # -- output -------------------------------------------------------------

    def serialize(self) -> Dict[str, Any]:
        """The patch as the API expects it, selection first."""
        return {**get_selection(self.selection), **copy.deepcopy(self.operations)}

    def to_json(self) -> Dict[str, Any]:
        """Same as :meth:`serialize`. Used when a patch is put in a request body."""
        return self.serialize()

    def reset(self) -> Self:
        """Drop every operation."""
        self.operations = {}
        return self

    def _assign(self, op: str, props: Mapping[str, Any], merge: bool = True) -> Self:
        if not isinstance(props, Mapping):
            raise TypeError(f"{op}() takes an object of properties")
        current = self.operations.get(op) if merge else None
        merged: Dict[str, Any] = dict(current) if isinstance(current, dict) else {}
        merged.update(props)
        self.operations[op] = merged
        return self

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.serialize()!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BasePatch):
            return NotImplemented
        return self.serialize() == other.serialize()

    __hash__ = None  # type: ignore[assignment]


class Patch(BasePatch):
    """A patch bound to a :class:`~sanity_client.SanityClient`."""

    def __init__(
        self,
        selection: PatchSelection,
        operations: Optional[Mapping[str, Any]] = None,
        client: Optional["SanityClient"] = None,
    ) -> None:
        super().__init__(selection, operations)
        self._client = client

    def clone(self) -> "Patch":
        """An independent copy with the same client."""
        return Patch(self.selection, copy.deepcopy(self.operations), self._client)

    def commit(
        self,
        *,
        return_documents: Optional[bool] = True,
        visibility: Optional[Visibility] = None,
        dry_run: Optional[bool] = None,
        auto_generate_array_keys: Optional[bool] = None,
        skip_cross_dataset_reference_validation: Optional[bool] = None,
        transaction_id: Optional[str] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> "MutationResult":
        """Send the patch. Returns the mutation result."""
        if self._client is None:
            raise RuntimeError(
                "No `client` passed to patch, either provide one or pass the patch to a client's `mutate()` method"
            )
        return self._client.mutate(
            self,
            return_documents=return_documents,
            visibility=visibility,
            dry_run=dry_run,
            auto_generate_array_keys=auto_generate_array_keys,
            skip_cross_dataset_reference_validation=skip_cross_dataset_reference_validation,
            transaction_id=transaction_id,
            tag=tag,
            timeout=timeout,
        )


class AsyncPatch(BasePatch):
    """A patch bound to an :class:`~sanity_client.AsyncSanityClient`."""

    def __init__(
        self,
        selection: PatchSelection,
        operations: Optional[Mapping[str, Any]] = None,
        client: Optional["AsyncSanityClient"] = None,
    ) -> None:
        super().__init__(selection, operations)
        self._client = client

    def clone(self) -> "AsyncPatch":
        """An independent copy with the same client."""
        return AsyncPatch(self.selection, copy.deepcopy(self.operations), self._client)

    async def commit(
        self,
        *,
        return_documents: Optional[bool] = True,
        visibility: Optional[Visibility] = None,
        dry_run: Optional[bool] = None,
        auto_generate_array_keys: Optional[bool] = None,
        skip_cross_dataset_reference_validation: Optional[bool] = None,
        transaction_id: Optional[str] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> "MutationResult":
        """Send the patch. Returns the mutation result."""
        if self._client is None:
            raise RuntimeError(
                "No `client` passed to patch, either provide one or pass the patch to a client's `mutate()` method"
            )
        return await self._client.mutate(
            self,
            return_documents=return_documents,
            visibility=visibility,
            dry_run=dry_run,
            auto_generate_array_keys=auto_generate_array_keys,
            skip_cross_dataset_reference_validation=skip_cross_dataset_reference_validation,
            transaction_id=transaction_id,
            tag=tag,
            timeout=timeout,
        )

