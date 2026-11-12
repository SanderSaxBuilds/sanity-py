from __future__ import annotations

import io
import warnings
from pathlib import Path
from typing import Any, Callable, Dict

import httpx
import pytest

from sanity_client import (
    MutationResult,
    NotFoundError,
    QueryParseError,
    SanityApiWarning,
    SanityClient,
    SanityDeprecationWarning,
    __version__,
    create_client,
)
from sanity_client._core import _printed_warnings

from .conftest import BASE, Recorder, json_body


def answering(body: Any, status: int = 200, headers: Dict[str, str] | None = None) -> Recorder:
    return Recorder(lambda request: httpx.Response(status, json=body, headers=headers or {}))


def client_for(recorder: Recorder, **extra: Any) -> SanityClient:
    return SanityClient(**{**BASE, **extra}, transport=httpx.MockTransport(recorder))


def test_fetch_returns_result_or_whole_response() -> None:
    rec = answering({"query": "*", "result": [{"title": "Alien"}], "ms": 3, "syncTags": ["s1"]})
    client = client_for(rec)
    assert client.fetch("*") == [{"title": "Alien"}]
    full = client.fetch("*", filter_response=False)
    assert full["ms"] == 3 and full["syncTags"] == ["s1"]
    # Same as the JS client: the query echo is only asked for with the full response.
    assert rec.requests[0].url.params["returnQuery"] == "false"
    assert "returnQuery" not in rec.requests[1].url.params


def test_fetch_return_query_false_is_sent_only_with_full_response() -> None:
    rec = answering({"result": 1})
    client = client_for(rec)
    client.fetch("*", filter_response=False, return_query=False)
    assert "returnQuery=false" in str(rec.last.url)


def test_typed_errors_come_through_the_client() -> None:
    rec = answering({"error": "Not Found", "message": "Dataset not found", "statusCode": 404}, 404)
    with pytest.raises(NotFoundError, match="Dataset not found"):
        client_for(rec).fetch("*")


def test_query_parse_error_through_the_client() -> None:
    body = {"error": {"type": "queryParseError", "description": "bad", "query": "*[", "start": 1, "end": 2}}
    with pytest.raises(QueryParseError) as info:
        client_for(answering(body, 400)).fetch("*[", tag="page")
    assert info.value.status_code == 400
    assert info.value.tag == "page"


def test_large_queries_use_post(make_client: Callable[..., SanityClient], recorder: Recorder) -> None:
    query = "*[_id in $ids]"
    ids = [f"id-{i:05d}" for i in range(1200)]
    make_client().fetch(query, {"ids": ids})
    assert recorder.last.method == "POST"
    assert json_body(recorder.last) == {"query": query, "params": {"ids": ids}}


def test_get_document_and_missing_document() -> None:
    client = client_for(answering({"documents": [{"_id": "a"}]}))
    assert client.get_document("a") == {"_id": "a"}
    assert client_for(answering({"documents": []})).get_document("a") is None
    assert client_for(answering({"documents": []})).get_document("a", include_all_versions=True) == []


def test_get_documents_keeps_the_asked_order() -> None:
    client = client_for(answering({"documents": [{"_id": "b"}, {"_id": "a"}], "omitted": [{"id": "c"}]}))
    assert client.get_documents(["a", "b", "c"]) == [{"_id": "a"}, {"_id": "b"}, None]


def test_get_documents_rejects_a_plain_string() -> None:
    with pytest.raises(TypeError):
        client_for(Recorder()).get_documents("a,b")


def test_documents_exists_uses_omitted_existence() -> None:
    body = {"documents": [], "omitted": [{"id": "b", "reason": "existence"}, {"id": "c", "reason": "permission"}]}
    rec = answering(body)
    assert client_for(rec).documents_exists(["a", "b", "c"]) == {"a", "c"}
    assert "excludeContent=true" in str(rec.last.url)


def test_documents_exists_batches_by_hundred() -> None:
    rec = answering({"documents": [], "omitted": []})
    ids = [f"d{i}" for i in range(250)]
    assert client_for(rec).documents_exists(ids) == set(ids)
    assert len(rec.requests) == 3


def test_get_document_release_id() -> None:
    rec = Recorder()
    client = client_for(rec)
    client.get_document("abc", release_id="r1")
    assert rec.last.url.path.endswith("/doc/production/versions.r1.abc")
    with pytest.raises(ValueError, match="is a draft"):
        client.get_document("drafts.abc", release_id="r1")
    with pytest.raises(ValueError, match="does not match"):
        client.get_document("versions.r2.abc", release_id="r1")


def test_mutation_result_properties(make_client: Callable[..., SanityClient]) -> None:
    result = make_client(token="t").create({"_type": "post"})
    assert isinstance(result, MutationResult)
    assert result.transaction_id == "trx-1"
    assert result.document_ids == ["doc-1"]
    assert result.documents == [{"_id": "doc-1", "_type": "post", "_rev": "rev-1"}]
    assert result.raw["results"][0]["operation"] == "update"


def test_mutation_result_without_documents() -> None:
    result = MutationResult.from_response({"transactionId": "t", "results": [{"id": "a", "operation": "create"}]})
    assert result.document_id == "a"
    assert result.document is None
    assert result.documents == []
    empty = MutationResult.from_response({"transactionId": "t", "results": []})
    assert empty.document_id is None


def test_upload_from_path_guesses_name_and_type(
    tmp_path: Path, make_client: Callable[..., SanityClient], recorder: Recorder
) -> None:
    image = tmp_path / "cover.png"
    image.write_bytes(b"\x89PNG fake")
    doc = make_client(token="t").assets.upload("image", image)
    assert doc["_type"] == "sanity.imageAsset"
    request = recorder.last
    assert request.url.path == "/v2025-02-19/assets/images/production"
    assert request.url.params["filename"] == "cover.png"
    assert request.headers["content-type"] == "image/png"
    assert request.content == b"\x89PNG fake"


def test_upload_from_file_object(make_client: Callable[..., SanityClient], recorder: Recorder) -> None:
    make_client(token="t").assets.upload("file", io.BytesIO(b"data"), filename="a.txt")
    assert recorder.last.url.path.endswith("/assets/files/production")
    assert "content-type" not in recorder.last.headers
    assert recorder.last.content == b"data"


def test_upload_rejects_str_and_text_files(make_client: Callable[..., SanityClient]) -> None:
    client = make_client(token="t")
    with pytest.raises(TypeError, match="not str"):
        client.assets.upload("file", "C:/path.txt")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="binary mode"):
        client.assets.upload("file", io.StringIO("text"))  # type: ignore[arg-type]


def test_history_transactions_parse_ndjson_and_exclude_content() -> None:
    lines = '{"id":"t1","documentIDs":["a"]}\n\n{"id":"t2","documentIDs":["a"]}\n'
    rec = Recorder(lambda r: httpx.Response(200, text=lines, headers={"content-type": "application/x-ndjson"}))
    client = client_for(rec, token="t")
    out = client.history.transactions(["a", "b"], limit=2, authors=["p1", "p2"])
    assert [t["id"] for t in out] == ["t1", "t2"]
    url = rec.last.url
    assert url.path == "/v2025-02-19/data/history/production/transactions/a,b"
    assert url.params["excludeContent"] == "true"
    assert url.params["authors"] == "p1,p2"
    assert url.params["limit"] == "2"


def test_history_get_document() -> None:
    rec = answering({"documents": [{"_id": "a", "_rev": "r1"}]})
    client = client_for(rec, token="t")
    assert client.history.get_document("a", revision="r1") == {"_id": "a", "_rev": "r1"}
    assert rec.last.url.params["revision"] == "r1"
    with pytest.raises(ValueError, match="not both"):
        client.history.get_document("a", revision="r1", time="2026-01-01T00:00:00Z")


def test_dataset_calls_never_send_a_tag(recorder: Recorder) -> None:
    client = client_for(recorder, token="t", request_tag_prefix="web")
    client.datasets.list()
    client.datasets.create("staging", acl_mode="private")
    assert all("tag" not in r.url.params for r in recorder.requests)
    assert json_body(recorder.last) == {"aclMode": "private"}
    with pytest.raises(ValueError):
        client.datasets.delete("Bad Name")


def test_datasets_without_project_hostname(recorder: Recorder) -> None:
    client = SanityClient(
        project_id="abc123", api_version="2025-02-19", use_project_hostname=False, transport=httpx.MockTransport(recorder)
    )
    client.datasets.list()
    assert recorder.last.url.path == "/v2025-02-19/projects/abc123/datasets"
    assert recorder.last.headers["x-sanity-project-id"] == "abc123"


def test_request_tag_prefix_applies_to_queries(recorder: Recorder) -> None:
    client_for(recorder, request_tag_prefix="web").fetch("*", tag="home")
    assert recorder.last.url.params["tag"] == "web.home"


def test_api_warning_header_warns_once() -> None:
    _printed_warnings.discard("This API version is deprecated")
    rec = answering({"result": 1}, headers={"X-Sanity-Warning": "This API version is deprecated"})
    client = client_for(rec)
    with pytest.warns(SanityApiWarning, match="deprecated"):
        client.fetch("*")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        client.fetch("*")


def test_drafts_on_cdn_goes_to_live_api(recorder: Recorder) -> None:
    client = client_for(recorder, use_cdn=True)
    with pytest.warns(UserWarning, match="live API"):
        client.fetch("*", perspective="drafts")
    assert recorder.last.url.host == "abc123.api.sanity.io"
    client.fetch("*")
    assert recorder.last.url.host == "abc123.apicdn.sanity.io"


def test_preview_drafts_is_deprecated(recorder: Recorder) -> None:
    with pytest.warns(SanityDeprecationWarning):
        client_for(recorder).fetch("*", perspective="previewDrafts")
    assert recorder.last.url.params["perspective"] == "previewDrafts"


def test_with_config_shares_the_connection_pool(recorder: Recorder) -> None:
    client = client_for(recorder)
    drafts = client.with_config(perspective="drafts", dataset="staging")
    assert drafts._http is client._http
    drafts.fetch("*")
    assert "/data/query/staging" in recorder.last.url.path
    assert recorder.last.url.params["perspective"] == "drafts"
    drafts.close()
    client.fetch("*")
    assert client.clone().config == client.config
    client.close()


def test_own_http_client_is_left_open(recorder: Recorder) -> None:
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    with SanityClient(**BASE, http_client=http) as client:
        client.fetch("*")
    assert not http.is_closed
    http.close()


def test_token_and_user_agent_headers(recorder: Recorder) -> None:
    client_for(recorder, token="sk-test").fetch("*")
    assert recorder.last.headers["authorization"] == "Bearer sk-test"
    assert recorder.last.headers["user-agent"] == f"sanity-client-py {__version__}"


def test_per_call_token_and_headers(recorder: Recorder) -> None:
    client_for(recorder, token="a").fetch("*", token="b", headers={"X-Extra": "1"})
    assert recorder.last.headers["authorization"] == "Bearer b"
    assert recorder.last.headers["x-extra"] == "1"


def test_raw_request(recorder: Recorder) -> None:
    client = client_for(recorder, token="t")
    client.request("/users/me")
    assert recorder.last.url.path == "/v2025-02-19/users/me"
    client.request("/custom", method="POST", body=b"raw bytes")
    assert recorder.last.content == b"raw bytes"
    client.request("/custom", method="POST", body={"a": 1}, query={"x": "y"})
    assert json_body(recorder.last) == {"a": 1}
    assert recorder.last.url.params["x"] == "y"


def test_action_returns_body(make_client: Callable[..., SanityClient], recorder: Recorder) -> None:
    out = make_client(token="t").action({"actionType": "sanity.action.document.publish", "draftId": "drafts.a", "publishedId": "a"})
    assert out == {"transactionId": "trx-1"}
    assert json_body(recorder.last)["actions"][0]["draftId"] == "drafts.a"


def test_projects_and_users(recorder: Recorder) -> None:
    client = client_for(recorder, token="t")
    client.projects.list(include_members=False)
    assert recorder.last.url.params["includeMembers"] == "false"
    client.projects.get_by_id("p1")
    assert recorder.last.url.path == "/v2025-02-19/projects/p1"
    client.users.get_by_id("me")
    assert recorder.last.url.path == "/v2025-02-19/users/me"


def test_create_client_helper() -> None:
    client = create_client(project_id="abc123", dataset="production", api_version="2025-02-19")
    assert isinstance(client, SanityClient)
    assert client.get_url("/x") == "https://abc123.api.sanity.io/v2025-02-19/x"
    assert client.get_url("/x", use_cdn=True) == "https://abc123.apicdn.sanity.io/v2025-02-19/x"
    assert client.get_data_url("query") == "/data/query/production"
    assert "use_cdn=True" in repr(client)
    client.close()


def test_mutation_options_reach_the_query_string(make_client: Callable[..., SanityClient], recorder: Recorder) -> None:
    make_client(token="t").create(
        {"_type": "post"},
        visibility="async",
        dry_run=True,
        auto_generate_array_keys=True,
        return_documents=False,
        tag="import",
    )
    params = recorder.last.url.params
    assert params["visibility"] == "async"
    assert params["dryRun"] == "true"
    assert params["autoGenerateArrayKeys"] == "true"
    assert params["returnIds"] == "true"
    assert "returnDocuments" not in params
    assert params["tag"] == "import"


def test_non_json_success_body_is_text() -> None:
    rec = Recorder(lambda r: httpx.Response(200, text="plain ok", headers={"content-type": "text/plain"}))
    assert client_for(rec).request("/ping") == "plain ok"


def test_empty_success_body_is_none() -> None:
    rec = Recorder(lambda r: httpx.Response(204))
    assert client_for(rec).request("/ping", method="DELETE") is None


def test_listen_tag_gets_prefix(recorder: Recorder) -> None:
    client = client_for(recorder, request_tag_prefix="web")
    next(client.listen("*", tag="feed"))
    assert recorder.last.url.params["tag"] == "web.feed"
    assert recorder.last.headers["accept"] == "text/event-stream"
