from __future__ import annotations

from typing import AsyncIterator, Iterator, List, Tuple

import httpx
import pytest

from sanity_client import (
    AsyncSanityClient,
    ChannelError,
    ConnectionFailedError,
    DisconnectError,
    ListenEvent,
    MessageError,
    MessageParseError,
    SanityClient,
)
from sanity_client._listen import SSEDecoder, is_transient_status

from .conftest import BASE, SSE_BODY


def decode(text: str) -> List[Tuple[str, str, object]]:
    decoder = SSEDecoder()
    out: List[Tuple[str, str, object]] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        message = decoder.feed(line)
        if message is not None:
            out.append((message.event, message.data, message.id))
    return out


def test_decoder_basic_event() -> None:
    assert decode("event: welcome\ndata: {}\nid: 1\n\n") == [("welcome", "{}", "1")]


def test_decoder_multiline_data_and_default_type() -> None:
    assert decode("data: a\ndata: b\n\n") == [("message", "a\nb", None)]


def test_decoder_ignores_comments_and_empty_data() -> None:
    assert decode(": ping\n\nevent: x\n\n") == []


def test_decoder_keeps_last_event_id_across_events() -> None:
    events = decode("id: 7\ndata: 1\n\ndata: 2\n\n")
    assert [e[2] for e in events] == ["7", "7"]


def test_decoder_value_without_space_and_retry() -> None:
    decoder = SSEDecoder()
    decoder.feed("retry: 2500")
    decoder.feed("data:x")
    message = decoder.feed("")
    assert decoder.retry == 2500
    assert message is not None and message.data == "x"


def test_decoder_ignores_id_with_null() -> None:
    decoder = SSEDecoder("keep")
    decoder.feed("id: a\x00b")
    assert decoder.last_event_id == "keep"


@pytest.mark.parametrize(("status", "transient"), [(500, True), (503, True), (408, True), (429, True), (401, False), (404, False)])
def test_transient_statuses(status: int, transient: bool) -> None:
    assert is_transient_status(status) is transient


def stream_client(responses: Iterator[httpx.Response], seen: List[httpx.Request], **extra: object) -> SanityClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(responses)

    client = SanityClient(**{**BASE, **extra}, transport=httpx.MockTransport(handler))
    client._sleep = lambda _s: None
    return client


def sse(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "text/event-stream"}, text=text)


def test_listen_yields_mutation_events() -> None:
    seen: List[httpx.Request] = []
    client = stream_client(iter([sse(SSE_BODY)]), seen)
    with client.listen('*[_type == "post"]') as events:
        event = next(events)
    assert isinstance(event, ListenEvent)
    assert event.type == "mutation"
    assert event.id == "evt-1"
    assert event.document_id == "doc-1"
    assert event.transition == "update"
    assert event.transaction_id == "trx-1"
    assert event["eventId"] == "trx-1#doc-1"
    assert event.to_dict()["type"] == "mutation"
    assert event.result is None and event.previous is None
    assert event.get("missing", 5) == 5


def test_listen_welcome_filtering() -> None:
    client = stream_client(iter([sse(SSE_BODY)]), [])
    with client.listen("*", events=["welcome"]) as events:
        event = next(events)
    assert event.type == "welcome"
    assert event.data == {"listenerName": "L1"}


def test_listen_reconnects_with_last_event_id() -> None:
    seen: List[httpx.Request] = []
    responses = iter(
        [
            sse("event: mutation\nid: e1\ndata: {\"documentId\":\"a\"}\n\n"),
            sse("event: mutation\nid: e2\ndata: {\"documentId\":\"b\"}\n\n"),
        ]
    )
    client = stream_client(responses, seen)
    with client.listen("*", events=["mutation", "reconnect"]) as events:
        first = next(events)
        second = next(events)
        third = next(events)
    assert (first.document_id, second.type, third.document_id) == ("a", "reconnect", "b")
    assert "last-event-id" not in seen[0].headers
    assert seen[1].headers["last-event-id"] == "e1"


def test_listen_retries_5xx_and_stops_on_4xx() -> None:
    seen: List[httpx.Request] = []
    responses = iter([httpx.Response(503, text="busy"), httpx.Response(401, json={"error": "Unauthorized"})])
    client = stream_client(responses, seen)
    with pytest.raises(ConnectionFailedError) as info:
        next(iter(client.listen("*")))
    assert info.value.status == 401
    assert info.value.body == {"error": "Unauthorized"}
    assert len(seen) == 2


def test_listen_max_reconnects() -> None:
    def failing() -> Iterator[httpx.Response]:
        while True:
            yield httpx.Response(502, text="bad gateway")

    client = stream_client(failing(), [])
    with pytest.raises(ConnectionFailedError, match="after 2 reconnects"):
        next(client.listen("*", max_reconnects=2))


def test_listen_network_error_reconnects() -> None:
    seen: List[httpx.Request] = []
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("down", request=request)
        return sse(SSE_BODY)

    client = SanityClient(**BASE, transport=httpx.MockTransport(handler))
    client._sleep = lambda _s: None
    assert next(client.listen("*")).type == "mutation"
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ('event: channelError\ndata: {"message":"bad filter"}\n\n', ChannelError),
        ('event: disconnect\ndata: {"reason":"dataset deleted"}\n\n', DisconnectError),
        ('event: error\ndata: {"message":"server says no"}\n\n', MessageError),
        ("event: mutation\ndata: {not json\n\n", MessageParseError),
    ],
)
def test_listen_server_errors(body: str, error: type) -> None:
    client = stream_client(iter([sse(body)]), [])
    with pytest.raises(error):
        next(client.listen("*"))


def test_disconnect_reason_in_message() -> None:
    client = stream_client(iter([sse('event: disconnect\ndata: {"reason":"gone"}\n\n')]), [])
    with pytest.raises(DisconnectError, match="Server disconnected client: gone") as info:
        next(client.listen("*"))
    assert info.value.reason == "gone"


def test_channel_error_carries_data() -> None:
    client = stream_client(iter([sse('event: channelError\ndata: {"error":{"description":"nope"}}\n\n')]), [])
    with pytest.raises(ChannelError, match="nope") as info:
        next(client.listen("*"))
    assert info.value.data == {"error": {"description": "nope"}}


def test_open_event_when_requested() -> None:
    client = stream_client(iter([sse(SSE_BODY)]), [])
    events = client.listen("*", events=["open", "welcome"])
    assert [next(events).type, next(events).type] == ["open", "welcome"]
    events.close()


def test_initial_last_event_id_is_sent() -> None:
    seen: List[httpx.Request] = []
    client = stream_client(iter([sse(SSE_BODY)]), seen)
    next(client.listen("*", last_event_id="resume-here", enable_resume=True))
    assert seen[0].headers["last-event-id"] == "resume-here"
    assert "enableResume=true" in str(seen[0].url)


def test_query_too_large_for_listener() -> None:
    client = SanityClient(**BASE)
    with pytest.raises(ValueError, match="too large"):
        client.listen("*[" + "a" * 20000 + "]")
    client.close()


def test_listener_never_uses_cdn() -> None:
    seen: List[httpx.Request] = []
    client = stream_client(iter([sse(SSE_BODY)]), seen, use_cdn=True)
    next(client.listen("*"))
    assert seen[0].url.host == "abc123.api.sanity.io"


async def test_async_listen() -> None:
    seen: List[httpx.Request] = []
    responses = iter([httpx.Response(500, text="x"), sse(SSE_BODY)])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(responses)

    async def no_sleep(_s: float) -> None:
        return None

    client = AsyncSanityClient(**BASE, transport=httpx.MockTransport(handler))
    client._sleep = no_sleep
    async with client.listen("*", events=["welcome", "mutation", "reconnect"]) as events:
        got = [event.type async for event in _take(events, 3)]
    assert got == ["reconnect", "welcome", "mutation"]
    await client.aclose()


async def _take(events: AsyncIterator[ListenEvent], count: int) -> AsyncIterator[ListenEvent]:
    n = 0
    async for event in events:
        yield event
        n += 1
        if n >= count:
            return
