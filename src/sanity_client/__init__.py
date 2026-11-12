"""Python client for the Sanity Content Lake HTTP API.

Sync and async, typed, and request-for-request compatible with
``@sanity/client``::

    from sanity_client import SanityClient

    client = SanityClient(project_id="zp7mbokg", dataset="production", api_version="2025-02-19")
    client.fetch('*[_type == "movie"] | order(releaseDate desc)[0...5]{title}')
"""

from typing import Any

from ._client import AsyncSanityClient, SanityClient
from ._core import SanityApiWarning, SanityDeprecationWarning
from ._listen import AsyncListener, Listener
from ._version import __version__
from .config import ClientConfig, SanityConfigWarning
from .errors import (
    AuthenticationError,
    ChannelError,
    ClientError,
    ConflictError,
    ConnectionFailedError,
    DisconnectError,
    HttpError,
    ListenError,
    MessageError,
    MessageParseError,
    MutationError,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    QueryParseError,
    RateLimitError,
    RequestTimeoutError,
    SanityError,
    ServerError,
)
from .ids import (
    get_draft_id,
    get_published_id,
    get_version_from_id,
    get_version_id,
    is_draft_id,
    is_published_id,
    is_version_id,
)
from .patch import AsyncPatch, Patch
from .retry import RetryPolicy
from .transaction import AsyncTransaction, Transaction
from .types import ListenEvent, MutationResult, RawQueryResponse

__all__ = [
    "AsyncListener",
    "AsyncPatch",
    "AsyncSanityClient",
    "AsyncTransaction",
    "AuthenticationError",
    "ChannelError",
    "ClientConfig",
    "ClientError",
    "ConflictError",
    "ConnectionFailedError",
    "DisconnectError",
    "HttpError",
    "ListenError",
    "ListenEvent",
    "Listener",
    "MessageError",
    "MessageParseError",
    "MutationError",
    "MutationResult",
    "NetworkError",
    "NotFoundError",
    "Patch",
    "PermissionDeniedError",
    "QueryParseError",
    "RateLimitError",
    "RawQueryResponse",
    "RequestTimeoutError",
    "RetryPolicy",
    "SanityApiWarning",
    "SanityClient",
    "SanityConfigWarning",
    "SanityDeprecationWarning",
    "SanityError",
    "ServerError",
    "Transaction",
    "__version__",
    "create_client",
    "get_draft_id",
    "get_published_id",
    "get_version_from_id",
    "get_version_id",
    "is_draft_id",
    "is_published_id",
    "is_version_id",
]


def create_client(**config: Any) -> SanityClient:
    """Same as ``SanityClient(**config)``, named like the JS ``createClient``."""
    return SanityClient(**config)
