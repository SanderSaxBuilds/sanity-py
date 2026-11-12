from __future__ import annotations

from typing import Callable, List

import httpx
import pytest

from sanity_client import NetworkError, RateLimitError, RequestTimeoutError, RetryPolicy, SanityClient, ServerError
from sanity_client.retry import parse_retry_after

from .conftest import BASE


def test_defaults_match_the_js_client() -> None:
    policy = RetryPolicy()
    assert policy.max_retries == 5
    assert policy.retry_statuses == frozenset({429, 502, 503})


@pytest.mark.parametrize(
    ("method", "path", "status", "expected"),
    [
        ("GET", "/data/doc/d/x", 429, True),
        ("GET", "/data/doc/d/x", 502, True),
        ("HEAD", "/projects", 503, True),
        ("GET", "/data/doc/d/x", 500, False),
        ("GET", "/data/doc/d/x", 504, False),
        ("POST", "/data/query/d", 503, True),
        ("POST", "/data/mutate/d", 429, False),
        ("POST", "/data/actions/d", 503, False),
        ("PUT", "/datasets/x", 502, False),
    ],
)
def test_which_statuses_retry(method: str, path: str, status: int, expected: bool) -> None:
    assert RetryPolicy().should_retry_status(method, path, status, 0) is expected


def test_retry_writes_opt_in() -> None:
    assert RetryPolicy(retry_writes=True).should_retry_status("POST", "/data/mutate/d", 429, 0)
    assert RetryPolicy(retry_writes=True).should_retry_error("POST", "/data/mutate/d", 0)


def test_attempt_limit() -> None:
    policy = RetryPolicy(max_retries=2)
    assert policy.should_retry_status("GET", "/x", 503, 1)
    assert not policy.should_retry_status("GET", "/x", 503, 2)
    assert not RetryPolicy(max_retries=0).should_retry_error("GET", "/x", 0)


def test_network_errors_retry_only_for_safe_methods() -> None:
    policy = RetryPolicy()
    assert policy.should_retry_error("GET", "/data/query/d", 0)
    assert not policy.should_retry_error("POST", "/data/query/d", 0)


def test_backoff_doubles_with_jitter_and_cap() -> None:
    policy = RetryPolicy(random=lambda: 0.5)
    assert policy.backoff(0) == pytest.approx(0.15)
    assert policy.backoff(1) == pytest.approx(0.25)
    assert policy.backoff(3) == pytest.approx(0.85)
    assert policy.backoff(20) == 10.0


def test_retry_after_wins_and_has_a_ceiling() -> None:
    policy = RetryPolicy(max_retry_after=5)
    assert policy.delay_for(0, {"retry-after": "2"}, 429) == 2.0
    assert policy.delay_for(0, {"retry-after": "30"}, 429) is None
    assert policy.delay_for(0, {}, 429) == pytest.approx(policy.backoff(0), abs=0.1)


def test_parse_retry_after_forms() -> None:
    assert parse_retry_after({"retry-after": "3"}) == 3.0
    assert parse_retry_after({"retry-after": "-1"}) == 0.0
    assert parse_retry_after({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, now=1445412470.0) == 10.0
    assert parse_retry_after({"retry-after": "soon"}) is None
    assert parse_retry_after({}) is None


def test_invalid_policies() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=-1)


def _client(handler: Callable[[httpx.Request], httpx.Response], sleeps: List[float], **extra: object) -> SanityClient:
    client = SanityClient(**{**BASE, **extra}, transport=httpx.MockTransport(handler))
    client._sleep = sleeps.append
    return client


def test_client_retries_503_then_succeeds() -> None:
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": "Unavailable", "message": "try later"})
        return httpx.Response(200, json={"result": [1], "ms": 1})

    sleeps: List[float] = []
    assert _client(handler, sleeps).fetch("*") == [1]
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_client_honors_retry_after() -> None:
    responses = [httpx.Response(429, headers={"retry-after": "1.5"}, json={}), httpx.Response(200, json={"result": 1})]
    sleeps: List[float] = []
    assert _client(lambda r: responses.pop(0), sleeps).fetch("*") == 1
    assert sleeps == [1.5]


def test_client_gives_up_after_max_retries() -> None:
    sleeps: List[float] = []
    client = _client(
        lambda r: httpx.Response(429, json={"error": "Too Many", "message": "slow down"}),
        sleeps,
        retry=RetryPolicy(max_retries=2),
    )
    with pytest.raises(RateLimitError):
        client.fetch("*")
    assert len(sleeps) == 2


def test_mutations_are_not_retried() -> None:
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": "Unavailable", "message": "x"})

    with pytest.raises(ServerError):
        _client(handler, [], token="t").create({"_type": "a"})
    assert len(calls) == 1


def test_network_error_is_retried_then_raised() -> None:
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused", request=request)

    sleeps: List[float] = []
    with pytest.raises(NetworkError, match="refused"):
        _client(handler, sleeps, retry=RetryPolicy(max_retries=3)).fetch("*")
    assert len(calls) == 4
    assert len(sleeps) == 3


def test_timeout_maps_to_request_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(RequestTimeoutError):
        _client(handler, [], retry=RetryPolicy(max_retries=0)).fetch("*")
