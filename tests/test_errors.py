from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from sanity_client import (
    AuthenticationError,
    ClientError,
    ConflictError,
    HttpError,
    MutationError,
    NotFoundError,
    PermissionDeniedError,
    QueryParseError,
    RateLimitError,
    ServerError,
)
from sanity_client.errors import (
    HttpResponseInfo,
    code_frame,
    error_from_response,
    extract_listener_error_message,
    format_query_parse_error,
)

URL = "https://p.api.sanity.io/v1/data/query/d?query=*"
JSON = {"content-type": "application/json; charset=utf-8"}


def info(status: int, body: Any, headers: Optional[Dict[str, str]] = None, reason: str = "") -> HttpResponseInfo:
    return HttpResponseInfo(
        status_code=status,
        status_message=reason or None,
        headers={**JSON, **(headers or {})} if not isinstance(body, str) else (headers or {}),
        body=body,
        url=URL,
        method="GET",
    )


def test_boom_body_message() -> None:
    err = error_from_response(info(404, {"error": "Not Found", "message": "Dataset not found", "statusCode": 404}))
    assert isinstance(err, NotFoundError)
    assert str(err) == "Not Found - Dataset not found"
    assert err.status_code == 404


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (400, ClientError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (429, RateLimitError),
        (500, ServerError),
        (503, ServerError),
    ],
)
def test_status_classes(status: int, cls: type) -> None:
    err = error_from_response(info(status, {"error": "E", "message": "m"}))
    assert type(err) is cls
    assert isinstance(err, HttpError)


def test_mutation_error_lists_items() -> None:
    body = {
        "error": {
            "type": "mutationError",
            "description": "The mutation(s) failed: Document by ID doc-1 already exists",
            "items": [
                {"error": {"description": "Document by ID doc-1 already exists", "type": "documentAlreadyExistsError"}},
            ],
        }
    }
    err = error_from_response(info(409, body))
    assert isinstance(err, MutationError)
    assert str(err) == (
        "The mutation(s) failed: Document by ID doc-1 already exists:\n- Document by ID doc-1 already exists"
    )
    assert err.items[0]["error"]["type"] == "documentAlreadyExistsError"
    assert err.error_type == "mutationError"
    assert err.description is not None


def test_mutation_error_caps_items_at_five() -> None:
    items = [{"error": {"description": f"bad {i}"}} for i in range(8)]
    err = error_from_response(info(400, {"error": {"type": "mutationError", "description": "failed", "items": items}}))
    assert str(err).endswith("- bad 4\n...and 3 more")
    assert "bad 5" not in str(err)


def test_action_errors_are_mutation_errors() -> None:
    err = error_from_response(info(400, {"error": {"type": "actionError", "description": "nope", "items": []}}))
    assert isinstance(err, MutationError)
    assert str(err) == "nope"


def test_query_parse_error_has_code_frame() -> None:
    body = {
        "error": {
            "type": "queryParseError",
            "description": "unexpected token",
            "query": "*[_type == movie && ]",
            "start": 20,
            "end": 21,
        }
    }
    err = error_from_response(info(400, body), tag="home")
    assert isinstance(err, QueryParseError)
    assert err.start == 20 and err.end == 21 and err.query == "*[_type == movie && ]"
    assert err.description == "unexpected token"
    lines = str(err).split("\n")
    assert lines[0] == "GROQ query parse error:"
    assert lines[1] == "> 1 | *[_type == movie && ]"
    caret = lines[2].index("^")
    assert lines[1][caret] == "]"
    assert lines[2].endswith("^ unexpected token")
    assert lines[-1] == "Tag: home"


def test_trace_id_from_traceparent() -> None:
    headers = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
    err = error_from_response(info(500, {"error": "Internal", "message": "boom"}, headers))
    assert err.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert str(err) == "Internal - boom (traceId: 4bf92f3577b34da6a3ce929d0e0e4736)"


def test_text_body_message() -> None:
    err = error_from_response(info(502, "<html>bad gateway</html>", reason="Bad Gateway"))
    assert isinstance(err, ServerError)
    assert str(err) == f"GET-request to {URL} resulted in HTTP 502 Bad Gateway (<html>bad gateway</html>)"


def test_long_text_body_is_cut() -> None:
    err = error_from_response(info(500, "x" * 300))
    assert str(err).endswith("x" * 100 + "…)")


def test_description_only_error_keeps_details() -> None:
    err = error_from_response(info(400, {"error": {"type": "validationError", "description": "bad field"}}))
    assert type(err) is ClientError
    assert str(err) == "bad field"
    assert err.details == {"type": "validationError", "description": "bad field"}
    assert err.error_type == "validationError"


def test_message_only_body() -> None:
    assert str(error_from_response(info(400, {"message": "just a message"}))) == "just a message"


def test_error_string_only_body() -> None:
    assert str(error_from_response(info(400, {"error": "only error"}))) == "only error"


def test_unknown_json_shape_falls_back_to_status_line() -> None:
    err = error_from_response(info(418, {"something": "else"}))
    assert str(err) == f"GET-request to {URL} resulted in HTTP 418"


def test_response_body_is_pretty_json() -> None:
    err = error_from_response(info(400, {"error": "E", "message": "m"}))
    assert err.response_body == '{\n  "error": "E",\n  "message": "m"\n}'


def test_repr_is_short() -> None:
    err = error_from_response(info(404, {"error": "Not Found", "message": "x"}))
    assert repr(err) == "NotFoundError(status_code=404, message='Not Found - x')"


def test_code_frame_multiline_range() -> None:
    query = "*[\n  _type == 'a'\n  && broken(\n]"
    frame = code_frame(query, 20, 30, "oops")
    assert frame.split("\n") == [
        "  1 | *[",
        "  2 |   _type == 'a'",
        "> 3 |   && broken(",
        "    |   ^^^^^^^^^^ oops",
        "  4 | ]",
    ]


def test_code_frame_points_at_first_character() -> None:
    frame = code_frame("abc", 0, 1)
    assert frame == "> 1 | abc\n    | ^"


def test_format_query_parse_error_without_position() -> None:
    text = format_query_parse_error({"description": "bad"}, trace_id="t1")
    assert text == "GROQ query parse error: bad\n(traceId: t1)"


def test_listener_error_messages() -> None:
    assert extract_listener_error_message({"message": "m"}) == "m"
    assert extract_listener_error_message({}) == "Unknown listener error"
    assert extract_listener_error_message({"error": "plain"}) == "plain"
    assert extract_listener_error_message({"error": {"description": "desc"}}) == "desc"
    parse = {"error": {"type": "queryParseError", "description": "d", "query": "*[", "start": 1, "end": 2}}
    assert extract_listener_error_message(parse, "t").startswith("GROQ query parse error:")
