"""Retry policy, matching the rules @sanity/client uses.

The JS client retries a request up to five times when:

* it is a ``GET`` or ``HEAD``, or a GROQ query (``/data/query``, also over
  ``POST``), and the response is 429, 502 or 503, or
* it is a ``GET`` or ``HEAD`` and the connection failed before any response.

Mutations and actions are never retried, so a write that may have reached
the server is not sent twice. The delay before retry ``n`` (from 0) is
``min(100ms * 2**n + random(0..100ms), 10s)``.

One addition: when the response carries ``Retry-After``, the client waits
that long instead (up to ``max_retry_after``).
"""

from __future__ import annotations

import datetime
import email.utils
import random
import time
from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Mapping, Optional

__all__ = ["RetryPolicy", "parse_retry_after"]

SAFE_METHODS: FrozenSet[str] = frozenset({"GET", "HEAD"})


@dataclass(frozen=True)
class RetryPolicy:
    """How the client retries failed requests.

    Attributes:
        max_retries: Retries after the first attempt. ``0`` turns retries off.
        base_delay: Seconds for the first backoff step, doubled per retry.
        max_jitter: Up to this many random seconds are added to each delay.
        max_delay: Upper bound for a computed delay.
        max_retry_after: Longest ``Retry-After`` the client will wait. Longer
            requests raise the error straight away.
        retry_statuses: Status codes that may be retried.
        retry_writes: Also retry mutations, actions and other non-query
            ``POST``/``PUT``/``PATCH``/``DELETE`` requests. Off by default.
    """

    max_retries: int = 5
    base_delay: float = 0.1
    max_jitter: float = 0.1
    max_delay: float = 10.0
    max_retry_after: float = 60.0
    retry_statuses: FrozenSet[int] = frozenset({429, 502, 503})
    retry_writes: bool = False
    random: Callable[[], float] = field(default=random.random, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be 0 or more")
        if min(self.base_delay, self.max_jitter, self.max_delay, self.max_retry_after) < 0:
            raise ValueError("delays must be 0 or more")

    def _eligible(self, method: str, path: str) -> bool:
        if self.retry_writes:
            return True
        return method.upper() in SAFE_METHODS or _is_query_path(path)

    def should_retry_status(self, method: str, path: str, status: int, attempt: int) -> bool:
        """Whether a response with ``status`` on try ``attempt`` (from 0) is retried.

        ``path`` is the request path after the API version, for example
        ``/data/query/production``.
        """
        if attempt >= self.max_retries or status not in self.retry_statuses:
            return False
        return self._eligible(method, path)

    def should_retry_error(self, method: str, path: str, attempt: int) -> bool:
        """Whether a connection error or timeout on try ``attempt`` is retried."""
        if attempt >= self.max_retries:
            return False
        return self.retry_writes or method.upper() in SAFE_METHODS

    def backoff(self, attempt: int) -> float:
        """Computed delay before retry number ``attempt + 1``."""
        delay = self.base_delay * (2.0**attempt) + self.random() * self.max_jitter
        return min(delay, self.max_delay)

    def delay_for(self, attempt: int, headers: Mapping[str, str], status: int) -> Optional[float]:
        """Seconds to wait before the next try, or ``None`` to give up.

        ``Retry-After`` wins over the computed backoff. A ``Retry-After``
        longer than ``max_retry_after`` gives ``None``.
        """
        requested = parse_retry_after(headers)
        if requested is None:
            return self.backoff(attempt)
        if requested > self.max_retry_after:
            return None
        return requested


def _is_query_path(path: str) -> bool:
    return path.startswith("/data/query")


def parse_retry_after(headers: Mapping[str, str], *, now: Optional[float] = None) -> Optional[float]:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP date).

    Returns ``None`` when the header is missing or unreadable. Past dates
    give ``0.0``.
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    current = time.time() if now is None else now
    return max(0.0, parsed.timestamp() - current)
