"""Byte-for-byte request parity with @sanity/client.

``tests/fixtures/js_requests.json`` is written by ``tools/js-capture/capture.mjs``,
which runs @sanity/client with ``fetch`` swapped for a recorder. Each case
below makes the same call through this package, sync and async, and checks
that method, URL, headers and body come out identical.

Headers the HTTP stack adds on its own (``host``, ``accept-encoding``,
``connection``, ``content-length``) and ``user-agent`` are left out of the
comparison. ``accept`` is compared for listener requests, where the client
sets it.
"""

from __future__ import annotations

import base64
import inspect
import warnings
from typing import Any, Callable, Dict, List

import httpx
import pytest

from sanity_client import AsyncListener, AsyncSanityClient, Listener, SanityClient

from .conftest import BASE, Recorder, js_body_bytes, load_js_fixtures

FIXTURES = load_js_fixtures()

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
LONG_QUERY = '*[_type == "post" && title in $titles]{' + "title, " * 2000 + "_id}"
TOKEN = "sk-test"

Factory = Callable[..., Any]
CASES: Dict[str, Callable[[Factory], Any]] = {
    # GROQ queries
    "query_simple": lambda c: c().fetch('*[_type == "post"]'),
    "query_cdn": lambda c: c(use_cdn=True).fetch('*[_type == "post"]'),
    "query_params_scalar": lambda c: c().fetch(
        "*[_type == $type && rating > $min && featured == $flag][0...$limit]",
        {"type": "post", "min": 3.5, "flag": True, "limit": 10},
    ),
    "query_params_complex": lambda c: c().fetch(
        "*[_id in $ids && category == $cat && $none == null]",
        {"ids": ["a", "b.c", "drafts.d"], "cat": {"_ref": "cat-1", "nested": {"deep": [1, 2.25, -3]}}, "none": None},
    ),
    "query_params_unicode": lambda c: c().fetch(
        "*[title match $q || author == $name]",
        {"q": "naïve café ~*'()! & = + / ? #", "name": "Søren Ærø 日本 😀"},
    ),
    "query_params_numbers": lambda c: c().fetch(
        "*[a == $a && b == $b && c == $c && d == $d && e == $e]",
        {"a": 1.0, "b": -0.5, "c": 1e21, "d": 123456789012, "e": 0.1},
    ),
    "query_params_undefined_dropped": lambda c: c().fetch("*[_type == $type]", {"type": "post"}),
    "query_tag": lambda c: c().fetch('*[_type == "post"]', {}, tag="homepage"),
    "query_tag_prefix": lambda c: c(request_tag_prefix="website").fetch('*[_type == "post"]', {}, tag="homepage"),
    "query_tag_prefix_only": lambda c: c(request_tag_prefix="website.").fetch('*[_type == "post"]'),
    "query_perspective_published": lambda c: c(use_cdn=True, perspective="published").fetch('*[_type == "post"]'),
    "query_perspective_drafts_cdn": lambda c: c(use_cdn=True, perspective="drafts", token=TOKEN).fetch(
        '*[_type == "post"]'
    ),
    "query_perspective_raw": lambda c: c(perspective="raw", token=TOKEN).fetch("*"),
    "query_perspective_stack": lambda c: c(use_cdn=True).fetch("*", {}, perspective=["rSummer", "drafts"]),
    "query_perspective_per_call": lambda c: c(perspective="published").fetch("*", {}, perspective="drafts"),
    "query_token": lambda c: c(token=TOKEN).fetch('*[_type == "post"]'),
    "query_filter_response_false": lambda c: c().fetch('*[_type == "post"]', {}, filter_response=False),
    "query_return_query_false_explicit": lambda c: c().fetch("*", {}, filter_response=False, return_query=False),
    "query_result_source_map": lambda c: c().fetch("*", {}, result_source_map="withKeyArraySelector"),
    "query_result_source_map_true": lambda c: c().fetch("*", {}, result_source_map=True),
    "query_long_post": lambda c: c().fetch(LONG_QUERY, {"titles": ["a", "b"]}),
    "query_long_post_tag": lambda c: c(perspective="published").fetch(LONG_QUERY, {"titles": ["x"]}, tag="big"),
    "query_api_version_v_prefix": lambda c: c(api_version="v2021-06-07").fetch("*"),
    "query_api_version_x": lambda c: c(api_version="X").fetch("*"),
    "query_custom_headers": lambda c: c(headers={"X-Custom": "yes"}).fetch("*"),
    "query_cdn_no_stale": lambda c: c(use_cdn=True).fetch("*", {}, cache_mode="noStale"),
    "query_last_live_event_id": lambda c: c(use_cdn=True).fetch("*", {}, last_live_event_id="live-1"),
    "query_custom_api_host": lambda c: c(api_host="https://api.example.test", use_cdn=True).fetch("*"),
    "query_no_project_hostname": lambda c: c(use_project_hostname=False, api_host="https://api.sanity.io").fetch("*"),
    "query_preview_drafts": lambda c: c(use_cdn=True, perspective="previewDrafts").fetch("*"),
    "query_result_source_map_false": lambda c: c().fetch("*", {}, result_source_map=False),
    "query_long_post_unicode": lambda c: c().fetch(
        LONG_QUERY + " // ø日😀", {"titles": ["ü", "x\ny", 'quote " and \\ backslash']}
    ),
    # Documents
    "get_document": lambda c: c().get_document("doc-1"),
    "get_document_cdn": lambda c: c(use_cdn=True).get_document("doc-1"),
    "get_document_tag": lambda c: c().get_document("drafts.doc-1", tag="doc.read"),
    "get_document_all_versions": lambda c: c().get_document("doc-1", include_all_versions=True),
    "get_document_all_versions_false": lambda c: c().get_document("doc-1", include_all_versions=False),
    "get_document_release": lambda c: c().get_document("doc-1", release_id="rSummer"),
    "get_documents": lambda c: c().get_documents(["doc-1", "doc-2", "drafts.doc-3"]),
    "get_documents_tag": lambda c: c(token=TOKEN).get_documents(["a", "b"], tag="many"),
    "documents_exists": lambda c: c().documents_exists(["doc-1", "doc 2"]),
    "documents_exists_batched": lambda c: c().documents_exists([f"doc-{i}" for i in range(150)]),
    # Mutations
    "create": lambda c: c(token=TOKEN).create({"_type": "post", "title": "Hello"}),
    "create_options": lambda c: c(token=TOKEN).create(
        {"_type": "post", "title": "Hi"},
        return_documents=False,
        visibility="async",
        dry_run=True,
        auto_generate_array_keys=True,
    ),
    "create_false_flags": lambda c: c(token=TOKEN).create(
        {"_type": "post"},
        dry_run=False,
        auto_generate_array_keys=False,
        skip_cross_dataset_reference_validation=False,
    ),
    "create_if_not_exists": lambda c: c(token=TOKEN).create_if_not_exists({"_id": "doc-1", "_type": "post", "title": "A"}),
    "create_or_replace": lambda c: c(token=TOKEN).create_or_replace({"_id": "doc-1", "_type": "post", "title": "B"}),
    "create_or_replace_tag": lambda c: c(token=TOKEN).create_or_replace(
        {"_id": "doc-1", "_type": "post"},
        tag="import",
        visibility="deferred",
        skip_cross_dataset_reference_validation=True,
    ),
    "delete_id": lambda c: c(token=TOKEN).delete("doc-1"),
    "delete_query": lambda c: c(token=TOKEN).delete({"query": "*[_type == $t]", "params": {"t": "old"}}),
    "delete_query_no_params": lambda c: c(token=TOKEN).delete({"query": '*[_type == "old"]'}),
    "mutate_raw": lambda c: c(token=TOKEN).mutate(
        [
            {"create": {"_type": "post", "title": "one"}},
            {"patch": {"id": "doc-1", "set": {"title": "two"}}},
            {"delete": {"id": "doc-2"}},
        ],
        return_documents=True,
        transaction_id="my-trx",
    ),
    "mutate_return_first": lambda c: c(token=TOKEN).mutate([{"delete": {"id": "doc-2"}}]),
    "patch_full": lambda c: c(token=TOKEN)
    .patch("doc-1")
    .set({"title": "New", "nested.prop": 1})
    .set({"other": True})
    .set_if_missing({"tags": []})
    .unset(["old", "nested.gone"])
    .inc({"views": 1})
    .inc({"likes": 2})
    .dec({"stock": 3})
    .insert("after", "tags[-1]", ["x"])
    .if_revision_id("rev-9")
    .commit(),
    "patch_append": lambda c: c(token=TOKEN).patch("doc-1").append("tags", ["a", "b"]).commit(),
    "patch_prepend": lambda c: c(token=TOKEN).patch("doc-1").prepend("tags", [{"_key": "k1"}]).commit(),
    "patch_insert_replace": lambda c: c(token=TOKEN).patch("doc-1").insert("replace", "items[2]", [{"n": 1}]).commit(),
    "patch_insert_before": lambda c: c(token=TOKEN).patch("doc-1").insert("before", "items[0]", ["z"]).commit(),
    "patch_splice": lambda c: c(token=TOKEN).patch("doc-1").splice("items", 1, 2, ["q"]).commit(),
    "patch_splice_negative": lambda c: c(token=TOKEN).patch("doc-1").splice("items", -2, 1).commit(),
    "patch_splice_all": lambda c: c(token=TOKEN).patch("doc-1").splice("items", 0).commit(),
    "patch_diff_match_patch": lambda c: c(token=TOKEN)
    .patch("doc-1")
    .diff_match_patch({"body": "@@ -1,3 +1,4 @@\n-abc\n+abcd\n"})
    .commit(),
    "patch_query_selection": lambda c: c(token=TOKEN)
    .patch({"query": '*[_type == "post" && views > $n]', "params": {"n": 10}})
    .set({"popular": True})
    .commit(),
    "patch_ids_selection": lambda c: c(token=TOKEN).patch(["a", "b"]).unset(["flag"]).commit(return_documents=False),
    "patch_initial_ops": lambda c: c(token=TOKEN).patch("doc-1", {"set": {"a": 1}}).set({"b": 2}).commit(visibility="async"),
    "patch_via_mutate": lambda c: c(token=TOKEN).mutate(c().patch("doc-1").inc({"n": 1})),
    "transaction_full": lambda c: c(token=TOKEN)
    .transaction()
    .create({"_type": "post", "title": "T"})
    .create_if_not_exists({"_id": "doc-2", "_type": "post"})
    .create_or_replace({"_id": "doc-3", "_type": "post", "n": 1})
    .patch("doc-1", lambda p: p.set({"title": "x"}).inc({"n": 1}))
    .patch("doc-4", {"unset": ["a"]})
    .patch(c().patch("doc-5").set_if_missing({"b": []}))
    .patch({"query": '*[_type == "x"]'}, {"set": {"y": 1}})
    .delete("doc-6")
    .commit(),
    "transaction_id": lambda c: c(token=TOKEN)
    .transaction()
    .set_transaction_id("trx-custom")
    .delete("doc-1")
    .commit(tag="cleanup", visibility="async"),
    "transaction_return_documents": lambda c: c(token=TOKEN)
    .transaction([{"create": {"_type": "a"}}])
    .commit(return_documents=True, auto_generate_array_keys=True),
    "transaction_via_mutate": lambda c: c(token=TOKEN).mutate(c().transaction().delete("doc-1"), dry_run=True),
    "action_single": lambda c: c(token=TOKEN).action(
        {"actionType": "sanity.action.document.publish", "draftId": "drafts.doc-1", "publishedId": "doc-1"}
    ),
    "action_many": lambda c: c(token=TOKEN).action(
        [
            {"actionType": "sanity.action.document.discard", "draftId": "drafts.doc-1"},
            {"actionType": "sanity.action.document.unpublish", "draftId": "drafts.doc-2", "publishedId": "doc-2"},
        ],
        transaction_id="act-1",
        dry_run=True,
        tag="actions",
    ),
    # Listen
    "listen_simple": lambda c: c().listen('*[_type == "post"]'),
    "listen_params_options": lambda c: c(token=TOKEN).listen(
        "*[_type == $type]",
        {"type": "post"},
        include_result=False,
        include_previous_revision=True,
        visibility="query",
        events=["welcome", "mutation"],
        tag="live",
        effect_format="mendoza",
    ),
    "listen_all_versions": lambda c: c(request_tag_prefix="site").listen(
        "*", {}, include_all_versions=True, enable_resume=True, tag="l", events=["welcome"]
    ),
    "listen_include_mutations_false": lambda c: c().listen("*", {}, include_mutations=False, events=["welcome"]),
    # Assets
    "upload_image": lambda c: c(token=TOKEN).assets.upload("image", PNG, filename="pixel.png", content_type="image/png"),
    "upload_image_meta": lambda c: c(token=TOKEN).assets.upload(
        "image",
        PNG,
        filename="pixel.png",
        content_type="image/png",
        extract=["palette", "location"],
        label="lbl",
        title="A pixel",
        description="One pixel",
        credit_line="Me & you",
        tag="upload",
        source={"id": "src-1", "name": "unsplash", "url": "https://example.test/p?id=1"},
    ),
    "upload_image_extract_none": lambda c: c(token=TOKEN).assets.upload(
        "image", PNG, extract=[], content_type="image/png"
    ),
    "upload_file": lambda c: c(token=TOKEN).assets.upload(
        "file", b"hello,world\n", filename="data.csv", content_type="text/csv"
    ),
    "upload_file_no_type": lambda c: c(token=TOKEN).assets.upload("file", b"raw"),
    # Functions
    "functions_invoke_async_config_stack": lambda c: c(
        token=TOKEN, stack_id="config-stack", use_cdn=True, request_tag_prefix="app"
    ).functions.invoke("send-email"),
    "functions_invoke_sync_request_stack": lambda c: c(token=TOKEN, stack_id="config-stack").functions.invoke(
        "summarize", {"stack_id": "request-stack", "event": {"data": {"id": "doc-1"}}, "timeout": 2.5}, sync=True
    ),
    "functions_invoke_org_override": lambda c: c(token=TOKEN, organization_id="config-org").functions.invoke(
        "index", {"stack_id": "org-stack", "organization_id": "request-org", "event": {"data": False}}, sync=False
    ),
    "functions_invoke_org_config": lambda c: c(
        project_id=None, dataset=None, use_project_hostname=False, organization_id="config-org"
    ).functions.invoke("index", {"stack_id": "org-stack", "event": {"data": None}}),
    "functions_callback_success": lambda c: c(token=TOKEN).functions.durables.callback_success(
        "a/b+=", {"status": "approved"}, timeout=0.5
    ),
    "functions_callback_success_omitted": lambda c: c(
        project_id=None, dataset=None, use_project_hostname=False
    ).functions.durables.callback_success("a/b+="),
    "functions_callback_failure": lambda c: c().functions.durables.callback_failure(
        "a/b+=", {"error_message": "Declined", "error_data": '{"code":42}', "stack_trace": ["at callback"]}
    ),
    "functions_callback_failure_omitted": lambda c: c().functions.durables.callback_failure("a/b+="),
    "functions_callback_failure_error": lambda c: c().functions.durables.callback_failure(
        "a/b+=", RuntimeError("Declined")
    ),
    "functions_stop_execution_project": lambda c: c(token=TOKEN).functions.durables.stop_execution("fn-1", "run/42"),
    "functions_stop_execution_org": lambda c: c(
        token=TOKEN, organization_id="config-org"
    ).functions.durables.stop_execution("fn-1", "run/42", organization_id="request-org", timeout=0),
    # Projects, datasets, raw requests
    "projects_list": lambda c: c(token=TOKEN).projects.list(),
    "projects_list_options": lambda c: c(token=TOKEN).projects.list(include_members=False, organization_id="org-1"),
    "projects_get": lambda c: c(token=TOKEN).projects.get_by_id("abc123"),
    "datasets_list": lambda c: c(token=TOKEN).datasets.list(),
    "request_users_me": lambda c: c(token=TOKEN).request("/users/me"),
    "request_history": lambda c: c(token=TOKEN).request(
        "/data/history/production/documents/doc-1", query={"revision": "rev-1"}
    ),
}

# Cases where this package deliberately differs and raises instead.
DIVERGENT = {"patch_two_inserts"}

TRANSPORT_HEADERS = {"host", "accept-encoding", "connection", "content-length", "user-agent"}


def _normalize_python(request: httpx.Request, compare_accept: bool) -> Dict[str, Any]:
    ignored = TRANSPORT_HEADERS if compare_accept else TRANSPORT_HEADERS | {"accept"}
    headers = {k.lower(): v for k, v in request.headers.items() if k.lower() not in ignored}
    return {"method": request.method, "url": str(request.url), "headers": headers, "body": request.content}


def _normalize_js(record: Dict[str, Any]) -> Dict[str, Any]:
    headers = {k: v for k, v in record["headers"].items() if k not in TRANSPORT_HEADERS}
    return {"method": record["method"], "url": record["url"], "headers": headers, "body": js_body_bytes(record["body"])}


def _compare(name: str, recorded: List[httpx.Request]) -> None:
    expected = [_normalize_js(r) for r in FIXTURES["cases"][name]["requests"]]
    compare_accept = name.startswith("listen_")
    actual = [_normalize_python(r, compare_accept) for r in recorded]
    assert len(actual) == len(expected), f"{name}: {len(actual)} requests, JS sent {len(expected)}"
    for got, want in zip(actual, expected):
        assert got["method"] == want["method"]
        assert got["url"] == want["url"]
        assert got["headers"] == want["headers"]
        assert got["body"] == want["body"]


def test_fixture_file_is_complete() -> None:
    assert FIXTURES["clientVersion"] == "8.17.0"
    names = set(FIXTURES["cases"])
    assert names == set(CASES) | DIVERGENT
    assert not [n for n, c in FIXTURES["cases"].items() if c.get("error")]
    for name, case in FIXTURES["cases"].items():
        expected_agent = None if name.startswith("listen_") else "@sanity/client 8.17.0"
        for request in case["requests"]:
            assert request["headers"].get("user-agent") == expected_agent


@pytest.mark.parametrize("name", sorted(CASES))
def test_sync_request_matches_js(name: str, recorder: Recorder) -> None:
    clients: List[SanityClient] = []

    def factory(**extra: Any) -> SanityClient:
        client = SanityClient(**{**BASE, **extra}, transport=httpx.MockTransport(recorder))
        clients.append(client)
        return client

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = CASES[name](factory)
        if isinstance(result, Listener):
            with result:
                next(result)
    for client in clients:
        client.close()
    _compare(name, recorder.requests)


@pytest.mark.parametrize("name", sorted(CASES))
async def test_async_request_matches_js(name: str, recorder: Recorder) -> None:
    clients: List[AsyncSanityClient] = []

    def factory(**extra: Any) -> AsyncSanityClient:
        client = AsyncSanityClient(**{**BASE, **extra}, transport=httpx.MockTransport(recorder))
        clients.append(client)
        return client

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = CASES[name](factory)
        if isinstance(result, AsyncListener):
            async with result:
                await result.__anext__()
        elif inspect.isawaitable(result):
            await result
    for client in clients:
        await client.aclose()
    _compare(name, recorder.requests)


def _request_from_fixture(record: Dict[str, Any], **change: Any) -> httpx.Request:
    fields = {
        "method": record["method"],
        "url": record["url"],
        "headers": record["headers"],
        "content": js_body_bytes(record["body"]),
        **change,
    }
    return httpx.Request(fields["method"], fields["url"], headers=fields["headers"], content=fields["content"])


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://abc123.api.sanity.io/v2025-02-19/data/query/production?query=*&tag=x"},
        {"method": "PUT"},
        {"headers": {"authorization": "Bearer other"}},
        {"content": b"{}"},
    ],
)
def test_the_comparison_catches_a_difference(change: Dict[str, Any]) -> None:
    name = "create"
    record = FIXTURES["cases"][name]["requests"][0]
    _compare(name, [_request_from_fixture(record)])
    with pytest.raises(AssertionError):
        _compare(name, [_request_from_fixture(record, **change)])
    with pytest.raises(AssertionError):
        _compare(name, [])


def test_two_inserts_in_one_patch_raise_instead_of_losing_items(make_client: Callable[..., SanityClient]) -> None:
    # @sanity/client merges both inserts into one object. The API then sees
    # the second ``items`` only, and the first list is silently dropped.
    js_body = FIXTURES["cases"]["patch_two_inserts"]["requests"][0]["body"]["text"]
    assert '"insert":{"after":"a[-1]","items":[2],"before":"b[0]"}' in js_body
    patch = make_client(token=TOKEN).patch("doc-1").append("a", [1])
    with pytest.raises(ValueError, match="one insert"):
        patch.prepend("b", [2])
