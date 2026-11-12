from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Callable, List

import httpx
import pytest

from sanity_client import (
    AsyncPatch,
    AsyncSanityClient,
    AsyncTransaction,
    MutationResult,
    NetworkError,
    NotFoundError,
    RetryPolicy,
)

from .conftest import BASE, Recorder, json_body


def async_client(recorder: Recorder, **extra: Any) -> AsyncSanityClient:
    async def no_sleep(_s: float) -> None:
        return None

    client = AsyncSanityClient(**{**BASE, **extra}, transport=httpx.MockTransport(recorder))
    client._sleep = no_sleep
    return client


async def test_fetch_and_errors() -> None:
    ok = Recorder(lambda r: httpx.Response(200, json={"result": [1, 2], "ms": 1}))
    async with async_client(ok) as client:
        assert await client.fetch("*[0...2]") == [1, 2]
    missing = Recorder(lambda r: httpx.Response(404, json={"error": "Not Found", "message": "no"}))
    async with async_client(missing) as client:
        with pytest.raises(NotFoundError):
            await client.fetch("*")


async def test_documents(make_async_client: Callable[..., AsyncSanityClient], recorder: Recorder) -> None:
    client = make_async_client()
    assert await client.get_document("doc-1") == {"_id": "doc-1", "_type": "post"}
    assert await client.get_documents(["x", "doc-1"]) == [None, {"_id": "doc-1", "_type": "post"}]
    assert await client.documents_exists(["doc-1"]) == {"doc-1"}
    await client.aclose()


async def test_mutations_patch_and_transaction(
    make_async_client: Callable[..., AsyncSanityClient], recorder: Recorder
) -> None:
    client = make_async_client(token="t")
    result = await client.create({"_type": "post"})
    assert isinstance(result, MutationResult) and result.document_id == "doc-1"
    patch = client.patch("doc-1").set({"title": "x"})
    assert isinstance(patch, AsyncPatch)
    await patch.commit()
    assert json_body(recorder.last)["mutations"] == [{"patch": {"id": "doc-1", "set": {"title": "x"}}}]
    trx = client.transaction(transaction_id="tx").delete("a")
    assert isinstance(trx, AsyncTransaction)
    out = await trx.commit()
    assert out.transaction_id == "trx-1"
    assert "returnDocuments" not in recorder.last.url.params
    assert json_body(recorder.last)["transactionId"] == "tx"
    await client.delete(["a", "b"])
    assert json_body(recorder.last)["mutations"] == [
        {"delete": {"query": "*[_id in $ids]", "params": {"ids": ["a", "b"]}}}
    ]
    await client.aclose()


async def test_retry_and_network_errors() -> None:
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502, text="bad")
        return httpx.Response(200, json={"result": "ok"})

    async with async_client(Recorder(handler)) as client:
        assert await client.fetch("*") == "ok"
    assert len(calls) == 2

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with async_client(Recorder(down), retry=RetryPolicy(max_retries=1)) as client:
        with pytest.raises(NetworkError):
            await client.fetch("*")


async def test_resources(tmp_path: Path, make_async_client: Callable[..., AsyncSanityClient], recorder: Recorder) -> None:
    client = make_async_client(token="t", request_tag_prefix="app")
    image = tmp_path / "a.jpg"
    image.write_bytes(b"jpg")
    doc = await client.assets.upload("image", image)
    assert doc["_id"] == "image-abc-10x10-png"
    assert recorder.last.headers["content-type"] == "image/jpeg"
    await client.assets.upload("file", io.BytesIO(b"x"), filename="x.bin", content_type="application/octet-stream")
    assert recorder.last.url.params["filename"] == "x.bin"
    await client.datasets.list()
    assert "tag" not in recorder.last.url.params
    await client.projects.list()
    await client.users.get_by_id("me")
    assert recorder.last.url.path == "/v2025-02-19/users/me"
    await client.action([{"actionType": "sanity.action.document.discard", "draftId": "drafts.a"}])
    assert recorder.last.url.path == "/v2025-02-19/data/actions/production"
    assert await client.request("/ping") == {}
    await client.aclose()


async def test_history_async() -> None:
    lines = '{"id":"t1"}\n{"id":"t2"}\n'
    rec = Recorder(lambda r: httpx.Response(200, text=lines))
    async with async_client(rec, token="t") as client:
        out = await client.history.transactions()
        assert [t["id"] for t in out] == ["t1", "t2"]
        assert rec.last.url.params["excludeContent"] == "true"


async def test_with_config_shares_http(recorder: Recorder) -> None:
    client = async_client(recorder)
    other = client.with_config(dataset="staging")
    assert other._http is client._http
    await other.fetch("*")
    assert "/data/query/staging" in recorder.last.url.path
    await other.aclose()
    await client.fetch("*")
    await client.aclose()


async def test_own_async_http_client_is_left_open(recorder: Recorder) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    async with AsyncSanityClient(**BASE, http_client=http) as client:
        await client.fetch("*")
    assert not http.is_closed
    await http.aclose()
