"""Typed errors built from the Content Lake error body.

Messages follow @sanity/client's ``extractErrorProps``: mutation errors list
the first five item descriptions, GROQ parse errors get a code frame pointing
at the failing token, Boom-style bodies read ``error - message``, and every
message ends with the trace ID taken from the ``traceparent`` header.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Type, Union

__all__ = [
    "AuthenticationError",
    "ChannelError",
    "ClientError",
    "ConflictError",
    "ConnectionFailedError",
    "DisconnectError",
    "HttpError",
    "HttpResponseInfo",
    "ListenError",
    "MessageError",
    "MessageParseError",
    "MutationError",
    "NetworkError",
    "NotFoundError",
    "PermissionDeniedError",
    "QueryParseError",
    "RateLimitError",
    "RequestTimeoutError",
    "SanityError",
    "ServerError",
    "code_frame",
    "error_from_response",
    "format_query_parse_error",
]

MAX_ITEMS_IN_ERROR_MESSAGE = 5


class SanityError(Exception):
    """Base class for every error this package raises on purpose."""


class NetworkError(SanityError):
    """The request never got an HTTP response (DNS, TLS, connection reset)."""


class RequestTimeoutError(NetworkError):
    """The request ran past its timeout."""


@dataclass(frozen=True)
class HttpResponseInfo:
    """The parts of an error response worth keeping, like the JS ``err.response``."""

    status_code: int
    status_message: Optional[str]
    headers: Mapping[str, str]
    body: Any
    url: str
    method: str


class HttpError(SanityError):
    """An HTTP response with status 400 or above.

    Attributes:
        status_code: HTTP status.
        response: Status, headers, parsed body, URL and method.
        response_body: The body as text. JSON bodies are pretty printed.
        trace_id: Trace ID from the ``traceparent`` header, if any.
        details: The structured ``error`` object for mutation, action, query
            and database errors, else ``None``.
        tag: Request tag that was sent, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        response: HttpResponseInfo,
        response_body: Any = None,
        trace_id: Optional[str] = None,
        details: Any = None,
        tag: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = response.status_code
        self.response = response
        self.response_body = response_body
        self.trace_id = trace_id
        self.details = details
        self.tag = tag

    @property
    def error_type(self) -> Optional[str]:
        """``error.type`` from the body, for example ``mutationError``."""
        if isinstance(self.details, Mapping):
            value = self.details.get("type")
            return value if isinstance(value, str) else None
        body = self.response.body
        if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
            value = body["error"].get("type")
            return value if isinstance(value, str) else None
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status_code={self.status_code}, message={self.message!r})"


class ClientError(HttpError):
    """4xx response."""


class ServerError(HttpError):
    """5xx response."""


class AuthenticationError(ClientError):
    """401, missing or invalid token."""


class PermissionDeniedError(ClientError):
    """403, the token lacks a permission."""


class NotFoundError(ClientError):
    """404, for example an unknown dataset. Missing documents are not 404s."""


class ConflictError(ClientError):
    """409, a document conflict such as a revision mismatch."""


class RateLimitError(ClientError):
    """429, too many requests. Retried automatically before this is raised."""


class QueryParseError(ClientError):
    """The GROQ query did not parse. ``details`` holds ``start``, ``end`` and ``query``."""

    @property
    def query(self) -> Optional[str]:
        return _detail_str(self.details, "query")

    @property
    def start(self) -> Optional[int]:
        return _detail_int(self.details, "start")

    @property
    def end(self) -> Optional[int]:
        return _detail_int(self.details, "end")

    @property
    def description(self) -> Optional[str]:
        return _detail_str(self.details, "description")


class MutationError(ClientError):
    """A mutation or action was rejected. ``items`` lists the per-mutation errors."""

    @property
    def description(self) -> Optional[str]:
        return _detail_str(self.details, "description")

    @property
    def items(self) -> List[Dict[str, Any]]:
        if isinstance(self.details, Mapping):
            items = self.details.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []


class ListenError(SanityError):
    """Base class for errors from a ``listen()`` stream."""


class ConnectionFailedError(ListenError):
    """The listener could not connect, or the server rejected it.

    Transient failures (network errors, 5xx, 408, 429) are retried by the
    listener. Other 4xx responses, such as an expired token, end the stream
    with this error. ``status`` holds the HTTP status when there was one.
    """

    def __init__(self, message: str, status: Optional[int] = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class DisconnectError(ListenError):
    """The server told the listener to disconnect and not come back."""

    def __init__(self, message: str, reason: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason = reason


class ChannelError(ListenError):
    """The server sent ``channelError``, usually a bad filter or unknown dataset."""

    def __init__(self, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.data = data


class MessageError(ListenError):
    """The server sent an ``error`` event with a payload."""

    def __init__(self, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.data = data


class MessageParseError(ListenError):
    """An event's ``data`` was not valid JSON."""


def _detail_str(details: Any, key: str) -> Optional[str]:
    if isinstance(details, Mapping):
        value = details.get(key)
        return value if isinstance(value, str) else None
    return None


def _detail_int(details: Any, key: str) -> Optional[int]:
    if isinstance(details, Mapping):
        value = details.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


# ---------------------------------------------------------------------------
# Code frames for GROQ parse errors
# ---------------------------------------------------------------------------

_NEWLINE = re.compile("\r\n|[\n\r  ]")


def _column_to_line(column: int, lines: List[str]) -> Dict[str, int]:
    offset = 0
    for index, line in enumerate(lines):
        length = len(line) + 1
        if offset + length > column:
            return {"line": index + 1, "column": column - offset}
        offset += length
    return {"line": len(lines), "column": len(lines[-1]) if lines else 0}


def code_frame(query: str, start: int, end: Optional[int] = None, message: Optional[str] = None) -> str:
    """Draw the lines around ``query[start:end]`` with ``^`` markers under it.

    Same layout as the JS client's ``codeFrame``. One difference: the API
    reports 0-based offsets and the JS port feeds them into a 1-based column
    routine, so its caret lands one character to the left of the token. Here
    the caret sits under the token itself.
    """
    lines = _NEWLINE.split(query)
    start_loc = _column_to_line(start, lines)
    end_loc = _column_to_line(end, lines) if end else dict(start_loc)
    lines_above, lines_below = 2, 3
    start_line, start_col = start_loc["line"], start_loc["column"]
    end_line, end_col = end_loc["line"], end_loc["column"]
    first = max(start_line - (lines_above + 1), 0)
    last = min(len(lines), end_line + lines_below)

    # Markers per line: (column, width). Columns are 0-based here.
    markers: Dict[int, Optional[tuple[int, int]]] = {}
    line_diff = end_line - start_line
    if line_diff:
        for i in range(line_diff + 1):
            number = start_line + i
            if i == 0:
                markers[number] = (start_col, max(len(lines[number - 1]) - start_col, 1))
            elif i == line_diff:
                markers[number] = (0, end_col)
            else:
                markers[number] = (0, len(lines[number - 1]))
    else:
        markers[start_line] = (start_col, max(end_col - start_col, 1))

    width = len(str(last))
    out: List[str] = []
    for index, line in enumerate(lines[first:last]):
        number = first + 1 + index
        padded = (" " + str(number))[-width:]
        gutter = f" {padded} |"
        content = f" {line}" if line else ""
        marker = markers.get(number)
        if number not in markers:
            out.append(f" {gutter}{content}")
            continue
        marker_line = ""
        if marker is not None:
            column, count = marker
            spacing = re.sub(r"[^\t]", " ", line[:column])
            marker_line = "\n " + re.sub(r"\d", " ", gutter) + " " + spacing + "^" * max(count, 1)
            if (number + 1) not in markers and message:
                marker_line += " " + message
        out.append(">" + gutter + content + marker_line)
    return "\n".join(out)


def format_query_parse_error(
    error: Mapping[str, Any], tag: Optional[str] = None, trace_id: Optional[str] = None
) -> str:
    """Human readable text for a ``queryParseError`` body."""
    query = error.get("query")
    start = error.get("start")
    end = error.get("end")
    description = error.get("description")
    with_trace = f"\n(traceId: {trace_id})" if trace_id else ""
    if not query or not isinstance(start, int):
        return f"GROQ query parse error: {description}{with_trace}"
    with_tag = f"\n\nTag: {tag}" if tag else ""
    framed = code_frame(
        str(query), start, end if isinstance(end, int) else None, str(description) if description else None
    )
    return f"GROQ query parse error:\n{framed}{with_tag}{with_trace}"


def is_query_parse_error(error: Any) -> bool:
    return (
        isinstance(error, Mapping)
        and error.get("type") == "queryParseError"
        and isinstance(error.get("query"), str)
        and isinstance(error.get("start"), int)
        and isinstance(error.get("end"), int)
    )


# ---------------------------------------------------------------------------
# Building errors from responses
# ---------------------------------------------------------------------------


def _trace_id(headers: Mapping[str, str]) -> Optional[str]:
    traceparent = headers.get("traceparent")
    if not traceparent:
        return None
    parts = traceparent.split("-")
    return parts[1] if len(parts) > 1 else None


def _with_trace(trace_id: Optional[str]) -> str:
    return f" (traceId: {trace_id})" if trace_id else ""


def _slice_with_ellipsis(text: str, limit: int) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def _http_error_message(response: HttpResponseInfo) -> str:
    body = response.body
    details = f" ({_slice_with_ellipsis(body, 100)})" if isinstance(body, str) else ""
    status_message = f" {response.status_message}" if response.status_message else ""
    return (
        f"{response.method}-request to {response.url} resulted in HTTP "
        f"{response.status_code}{status_message}{details}"
    )


def _stringify_body(response: HttpResponseInfo) -> Any:
    content_type = response.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        return json.dumps(response.body, indent=2, ensure_ascii=False)
    return response.body


def _is_item_error(error: Mapping[str, Any]) -> bool:
    return error.get("type") in ("mutationError", "actionError") and isinstance(
        error.get("description"), str
    )


_STATUS_CLASSES: Dict[int, Type[ClientError]] = {
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    429: RateLimitError,
}


def error_from_response(response: HttpResponseInfo, tag: Optional[str] = None) -> HttpError:
    """Build the matching :class:`HttpError` subclass for an error response."""
    trace_id = _trace_id(response.headers)
    body = response.body
    message: str
    details: Any = None
    body_class: Optional[Type[ClientError]] = None

    error = body.get("error") if isinstance(body, Mapping) else None
    if not isinstance(body, Mapping):
        message = _http_error_message(response) + _with_trace(trace_id)
    elif isinstance(error, str) and isinstance(body.get("message"), str):
        message = f"{error} - {body['message']}{_with_trace(trace_id)}"
    elif not isinstance(error, Mapping):
        if isinstance(error, str):
            message = error + _with_trace(trace_id)
        elif isinstance(body.get("message"), str):
            message = str(body["message"]) + _with_trace(trace_id)
        else:
            message = _http_error_message(response) + _with_trace(trace_id)
    elif _is_item_error(error):
        all_items = error.get("items") or []
        descriptions = [
            item["error"]["description"]
            for item in all_items[:MAX_ITEMS_IN_ERROR_MESSAGE]
            if isinstance(item, Mapping)
            and isinstance(item.get("error"), Mapping)
            and item["error"].get("description")
        ]
        items_text = (":\n- " + "\n- ".join(str(d) for d in descriptions)) if descriptions else ""
        if len(all_items) > MAX_ITEMS_IN_ERROR_MESSAGE:
            items_text += f"\n...and {len(all_items) - MAX_ITEMS_IN_ERROR_MESSAGE} more"
        message = f"{error['description']}{_with_trace(trace_id)}{items_text}"
        details = error
        body_class = MutationError
    elif is_query_parse_error(error):
        message = format_query_parse_error(error, tag, trace_id)
        details = error
        body_class = QueryParseError
    elif isinstance(error.get("description"), str):
        message = f"{error['description']}{_with_trace(trace_id)}"
        details = error
    else:
        message = _http_error_message(response) + _with_trace(trace_id)

    cls: Type[HttpError]
    if response.status_code >= 500:
        cls = ServerError
    elif body_class is not None:
        cls = body_class
    else:
        cls = _STATUS_CLASSES.get(response.status_code, ClientError)
    return cls(
        message,
        response=response,
        response_body=_stringify_body(response),
        trace_id=trace_id,
        details=details,
        tag=tag,
    )


def extract_listener_error_message(data: Any, tag: Optional[str] = None) -> str:
    """Message for a ``channelError`` payload, like the JS ``extractErrorMessage``."""
    error = data.get("error") if isinstance(data, Mapping) else None
    if not error:
        message = data.get("message") if isinstance(data, Mapping) else None
        return message if isinstance(message, str) and message else "Unknown listener error"
    if isinstance(error, Mapping):
        if is_query_parse_error(error):
            return format_query_parse_error(error, tag)
        if isinstance(error.get("description"), str):
            return str(error["description"])
    return error if isinstance(error, str) else json.dumps(error, indent=2)


ErrorLike = Union[HttpError, ListenError]
