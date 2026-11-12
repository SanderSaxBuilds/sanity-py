"""Read-only checks against the public movies demo dataset.

Run them with ``pytest -m live``. They need network access and no token.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from sanity_client import (
    AsyncSanityClient,
    AuthenticationError,
    HttpError,
    NotFoundError,
    QueryParseError,
    SanityClient,
)

pytestmark = pytest.mark.live

PROJECT = "zp7mbokg"
DATASET = "production"
API_VERSION = "2025-02-19"


@pytest.fixture
def cdn() -> Iterator[SanityClient]:
    with SanityClient(PROJECT, DATASET, api_version=API_VERSION, use_cdn=True) as client:
        yield client


@pytest.fixture
def api() -> Iterator[SanityClient]:
    with SanityClient(PROJECT, DATASET, api_version=API_VERSION, use_cdn=False) as client:
        yield client


def test_query_with_params(cdn: SanityClient) -> None:
    movies = cdn.fetch(
        '*[_type == $type && defined(title)] | order(title asc)[0...$n]{_id, title}',
        {"type": "movie", "n": 3},
    )
    assert len(movies) == 3
    assert all(isinstance(m["title"], str) for m in movies)
    assert [m["title"] for m in movies] == sorted(m["title"] for m in movies)


def test_full_response_has_timing(api: SanityClient) -> None:
    response = api.fetch("count(*[_type == 'movie'])", filter_response=False)
    assert isinstance(response["result"], int) and response["result"] > 0
    assert "ms" in response
    assert response["query"] == "count(*[_type == 'movie'])"


def test_cdn_and_api_agree(cdn: SanityClient, api: SanityClient) -> None:
    query = "*[_type == 'person'] | order(_id asc)[0...5]._id"
    assert cdn.fetch(query) == api.fetch(query)


def test_unicode_and_float_params(api: SanityClient) -> None:
    echo = api.fetch("{'text': $text, 'num': $num, 'list': $list}", {"text": "ø å ü 🙂 & ?=", "num": 1.5, "list": [1, None]})
    assert echo == {"text": "ø å ü 🙂 & ?=", "num": 1.5, "list": [1, None]}


def test_large_query_goes_through_post(api: SanityClient) -> None:
    ids = [f"missing-{i:05d}" for i in range(1500)]
    assert api.fetch("count(*[_id in $ids])", {"ids": ids}) == 0


def test_get_document_and_documents(api: SanityClient) -> None:
    first = api.fetch("*[_type == 'movie'] | order(_id asc)[0]{_id}")
    doc = api.get_document(first["_id"])
    assert doc is not None and doc["_type"] == "movie"
    docs = api.get_documents([first["_id"], "does-not-exist-123"])
    assert docs[0] is not None and docs[0]["_id"] == first["_id"]
    assert docs[1] is None
    assert api.get_document("does-not-exist-123") is None


def test_documents_exists(api: SanityClient) -> None:
    ids = api.fetch("*[_type == 'movie'] | order(_id asc)[0...2]._id")
    assert api.documents_exists([*ids, "nope-123"]) == set(ids)


def test_query_parse_error_is_typed(api: SanityClient) -> None:
    with pytest.raises(QueryParseError) as info:
        api.fetch("*[_type == ]")
    assert info.value.status_code == 400
    assert "GROQ query parse error" in str(info.value)


def test_unknown_dataset_is_not_found() -> None:
    with SanityClient(PROJECT, "no-such-dataset", api_version=API_VERSION, use_cdn=False) as client:
        with pytest.raises(NotFoundError):
            client.fetch("*[0]")


def test_history_needs_a_token(api: SanityClient) -> None:
    with pytest.raises(HttpError) as info:
        api.history.transactions(limit=1)
    assert info.value.status_code in (401, 403)


def test_users_me_without_token_is_empty(api: SanityClient) -> None:
    # The API answers 200 with an empty object for an anonymous caller.
    assert api.users.get_by_id("me") == {}


def test_projects_list_needs_a_token() -> None:
    with SanityClient(PROJECT, DATASET, api_version=API_VERSION, use_cdn=False) as client:
        with pytest.raises(AuthenticationError):
            client.projects.list()


def test_listen_sends_welcome(api: SanityClient) -> None:
    with api.listen("*[_type == 'movie']", events=["welcome"], read_timeout=30) as events:
        welcome = next(events)
    assert welcome.type == "welcome"
    assert "listenerName" in welcome.data


async def test_async_query_and_listen() -> None:
    async with AsyncSanityClient(PROJECT, DATASET, api_version=API_VERSION, use_cdn=True) as client:
        count = await client.fetch("count(*[_type == 'screening'])")
        assert isinstance(count, int)
        async with client.listen("*[_type == 'movie']", events=["welcome"], read_timeout=30) as events:
            welcome = await events.__anext__()
        assert welcome.type == "welcome"
