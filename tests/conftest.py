from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from sanity_client import AsyncSanityClient, SanityClient

FIXTURES = Path(__file__).parent / "fixtures"

BASE: Dict[str, Any] = {
    "project_id": "abc123",
    "dataset": "production",
    "api_version": "2025-02-19",
    "use_cdn": False,
}

MUTATE_RESPONSE = {
    "transactionId": "trx-1",
    "results": [
        {"id": "doc-1", "operation": "update", "document": {"_id": "doc-1", "_type": "post", "_rev": "rev-1"}}
    ],
}

SSE_BODY = (
    'event: welcome\ndata: {"listenerName":"L1"}\nid: evt-0\n\n'
    'event: mutation\nid: evt-1\ndata: {"documentId":"doc-1","transactionId":"trx-1","transition":"update",'
    '"identity":"p-1","visibility":"query","eventId":"trx-1#doc-1","effects":{},'
    '"timestamp":"2026-11-13T00:00:00Z"}\n\n'
)


def default_response_body(url: str) -> Any:
    """The same canned bodies the JS capture script answers with."""
    if "/data/query/" in url:
        return {"query": "*", "result": [], "ms": 1}
    if "/data/doc/" in url:
        return {"documents": [{"_id": "doc-1", "_type": "post"}], "omitted": []}
    if "/data/mutate/" in url:
        return MUTATE_RESPONSE
    if "/data/actions/" in url:
        return {"transactionId": "trx-1"}
    if "/assets/" in url:
        return {"document": {"_id": "image-abc-10x10-png", "_type": "sanity.imageAsset"}}
    if "/projects" in url or "/datasets" in url:
        return []
    return {}


class Recorder:
    """httpx handler that records every request and answers like the JS mock."""

    def __init__(self, handler: Optional[Callable[[httpx.Request], httpx.Response]] = None) -> None:
        self.requests: List[httpx.Request] = []
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        if "text/event-stream" in request.headers.get("accept", ""):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=SSE_BODY)
        return httpx.Response(200, json=default_response_body(str(request.url)))

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def json_body(request: httpx.Request) -> Any:
    return json.loads(request.content.decode("utf-8"))


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def make_client(recorder: Recorder) -> Callable[..., SanityClient]:
    def factory(**extra: Any) -> SanityClient:
        client = SanityClient(**{**BASE, **extra}, transport=httpx.MockTransport(recorder))
        client._sleep = lambda _seconds: None
        return client

    return factory


@pytest.fixture
def make_async_client(recorder: Recorder) -> Callable[..., AsyncSanityClient]:
    async def no_sleep(_seconds: float) -> None:
        return None

    def factory(**extra: Any) -> AsyncSanityClient:
        client = AsyncSanityClient(**{**BASE, **extra}, transport=httpx.MockTransport(recorder))
        client._sleep = no_sleep
        return client

    return factory


def load_js_fixtures() -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads((FIXTURES / "js_requests.json").read_text(encoding="utf-8"))
    return data


def js_body_bytes(body: Dict[str, Any]) -> bytes:
    kind = body["kind"]
    if kind == "none":
        return b""
    if kind == "text":
        return str(body["text"]).encode("utf-8")
    if kind == "bytes":
        return base64.b64decode(body["base64"])
    raise AssertionError(f"unexpected body kind {kind}")
