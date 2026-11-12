"""Server-sent events for ``client.listen()``.

The listener behaves like the JS client's EventSource wrapper:

* ``channelError`` ends the stream with :class:`~sanity_client.ChannelError`,
  ``disconnect`` with :class:`~sanity_client.DisconnectError`, and an
  ``error`` event with a payload with :class:`~sanity_client.MessageError`.
* Network errors, a closed stream, 5xx, 408 and 429 reconnect after a short
  wait and send ``Last-Event-ID``, so a server started with
  ``enable_resume=True`` can replay what was missed. A ``reconnect`` event is
  yielded first when it was asked for.
* Any other 4xx (an expired token, say) raises
  :class:`~sanity_client.ConnectionFailedError` with the status.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import (
    Any,
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    Generator,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
)

import httpx

from .errors import (
    ChannelError,
    ConnectionFailedError,
    DisconnectError,
    MessageError,
    MessageParseError,
    extract_listener_error_message,
)
from .types import ListenEvent

__all__ = ["AsyncListener", "Listener", "SSEDecoder", "SSEMessage"]

REQUIRED_EVENTS: FrozenSet[str] = frozenset({"channelError", "disconnect"})
_LOCAL_EVENTS: FrozenSet[str] = frozenset({"error", "open", "reconnect"})


@dataclass(frozen=True)
class SSEMessage:
    """One dispatched server-sent event, before JSON parsing."""

    event: str
    data: str
    id: Optional[str]


class SSEDecoder:
    """Line-by-line decoder for ``text/event-stream`` (WHATWG rules)."""

    def __init__(self, last_event_id: Optional[str] = None) -> None:
        self._event = ""
        self._data: List[str] = []
        self.last_event_id = last_event_id
        self.retry: Optional[int] = None

    def feed(self, line: str) -> Optional[SSEMessage]:
        """Feed one line without its line ending. Returns a message on a blank line."""
        if line == "":
            if not self._data:
                self._event = ""
                return None
            message = SSEMessage(
                event=self._event or "message",
                data="\n".join(self._data),
                id=self.last_event_id,
            )
            self._event = ""
            self._data = []
            return message
        if line.startswith(":"):
            return None
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id":
            if "\x00" not in value:
                self.last_event_id = value
        elif name == "retry":
            if value.isdigit():
                self.retry = int(value)
        return None


def is_transient_status(status: int) -> bool:
    """Statuses the listener reconnects on: 408, 429 and 5xx."""
    return status >= 500 or status in (408, 429)


class _ListenState:
    """Turns SSE messages into :class:`ListenEvent` values or errors."""

    def __init__(self, events: Sequence[str], tag: Optional[str]) -> None:
        self.requested = frozenset(events)
        self.tag = tag
        self.subscribed = (REQUIRED_EVENTS | self.requested) - _LOCAL_EVENTS

    def handle(self, message: SSEMessage) -> Optional[ListenEvent]:
        if message.event == "error":
            try:
                data = json.loads(message.data)
            except ValueError as exc:
                raise MessageParseError("Unable to parse EventSource error message") from exc
            text = data.get("message") if isinstance(data, dict) else None
            raise MessageError(text if isinstance(text, str) else "", data)
        if message.event not in self.subscribed:
            return None
        try:
            data = json.loads(message.data)
        except ValueError as exc:
            raise MessageParseError("Unable to parse EventSource message") from exc
        if message.event == "channelError":
            raise ChannelError(extract_listener_error_message(data, self.tag), data)
        if message.event == "disconnect":
            reason = data.get("reason") if isinstance(data, dict) else None
            reason_text = reason if isinstance(reason, str) and reason else None
            raise DisconnectError(f"Server disconnected client: {reason_text or 'unknown error'}", reason_text)
        payload: Mapping[str, Any] = data if isinstance(data, dict) else ({"value": data} if data else {})
        return ListenEvent(type=message.event, data=payload, id=message.id)


class _Retry(Exception):
    def __init__(self, status: Optional[int] = None) -> None:
        super().__init__(status)
        self.status = status


def _stream_timeout(read_timeout: Optional[float], connect_timeout: float) -> httpx.Timeout:
    return httpx.Timeout(connect_timeout, read=read_timeout)


def _failure(status: int, body: bytes) -> ConnectionFailedError:
    text = body.decode("utf-8", errors="replace")
    try:
        parsed: Any = json.loads(text) if text else None
    except ValueError:
        parsed = text
    return ConnectionFailedError(f"EventSource connection failed with HTTP {status}", status=status, body=parsed)


class _ListenerBase:
    def __init__(
        self,
        url: str,
        headers: Mapping[str, str],
        events: Sequence[str],
        *,
        tag: Optional[str],
        reconnect_delay: float,
        max_reconnects: Optional[int],
        read_timeout: Optional[float],
        connect_timeout: float,
        last_event_id: Optional[str],
    ) -> None:
        self.url = url
        self._headers = dict(headers)
        self._state = _ListenState(events, tag)
        self._reconnect_delay = reconnect_delay
        self._max_reconnects = max_reconnects
        self._timeout = _stream_timeout(read_timeout, connect_timeout)
        self.last_event_id = last_event_id
        self.reconnects = 0
        self._server_retry: Optional[float] = None
        self._closed = False

    def _request_headers(self) -> Dict[str, str]:
        headers = dict(self._headers)
        if self.last_event_id and not any(k.lower() == "last-event-id" for k in headers):
            headers["Last-Event-ID"] = self.last_event_id
        return headers

    def _after_failure(self, status: Optional[int]) -> Optional[ListenEvent]:
        """Count a failed connection. Returns a ``reconnect`` event if one is wanted."""
        self.reconnects += 1
        if self._max_reconnects is not None and self.reconnects > self._max_reconnects:
            message = "EventSource connection failed"
            if status is not None:
                message += f" with HTTP {status}"
            raise ConnectionFailedError(message + f" after {self._max_reconnects} reconnects", status=status)
        if "reconnect" in self._state.requested:
            return ListenEvent(type="reconnect")
        return None

    def _delay(self) -> float:
        return self._server_retry if self._server_retry is not None else self._reconnect_delay

    def _track(self, decoder: SSEDecoder) -> None:
        self.last_event_id = decoder.last_event_id
        if decoder.retry is not None:
            self._server_retry = decoder.retry / 1000.0


class Listener(_ListenerBase):
    """Blocking iterator over listener events. Use it in a ``with`` block or call :meth:`close`."""

    def __init__(
        self,
        http: httpx.Client,
        url: str,
        headers: Mapping[str, str],
        events: Sequence[str],
        *,
        tag: Optional[str] = None,
        reconnect_delay: float = 1.0,
        max_reconnects: Optional[int] = None,
        read_timeout: Optional[float] = None,
        connect_timeout: float = 30.0,
        last_event_id: Optional[str] = None,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        super().__init__(
            url,
            headers,
            events,
            tag=tag,
            reconnect_delay=reconnect_delay,
            max_reconnects=max_reconnects,
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
            last_event_id=last_event_id,
        )
        self._http = http
        self._sleep = sleep
        self._gen: Generator[ListenEvent, None, None] = self._run()

    def __iter__(self) -> Iterator[ListenEvent]:
        return self

    def __next__(self) -> ListenEvent:
        return next(self._gen)

    def close(self) -> None:
        """Close the connection. Iteration stops."""
        self._closed = True
        self._gen.close()

    def __enter__(self) -> "Listener":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _run(self) -> Generator[ListenEvent, None, None]:
        while not self._closed:
            status: Optional[int] = None
            try:
                with self._http.stream(
                    "GET", self.url, headers=self._request_headers(), timeout=self._timeout
                ) as response:
                    status = response.status_code
                    if status >= 400 or status == 204:
                        body = response.read()
                        if status == 204 or not is_transient_status(status):
                            raise _failure(status, body)
                        raise _Retry(status)
                    if "open" in self._state.requested:
                        yield ListenEvent(type="open")
                    decoder = SSEDecoder(self.last_event_id)
                    for line in response.iter_lines():
                        message = decoder.feed(line)
                        self._track(decoder)
                        if message is None:
                            continue
                        event = self._state.handle(message)
                        if event is not None:
                            self.reconnects = 0
                            yield event
            except _Retry as exc:
                status = exc.status
            except httpx.TransportError:
                status = None
            if self._closed:
                return
            reconnect_event = self._after_failure(status)
            if reconnect_event is not None:
                yield reconnect_event
            self._sleep(self._delay())


class AsyncListener(_ListenerBase):
    """Async iterator over listener events. Use ``async with`` or call :meth:`aclose`."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        headers: Mapping[str, str],
        events: Sequence[str],
        *,
        tag: Optional[str] = None,
        reconnect_delay: float = 1.0,
        max_reconnects: Optional[int] = None,
        read_timeout: Optional[float] = None,
        connect_timeout: float = 30.0,
        last_event_id: Optional[str] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        super().__init__(
            url,
            headers,
            events,
            tag=tag,
            reconnect_delay=reconnect_delay,
            max_reconnects=max_reconnects,
            read_timeout=read_timeout,
            connect_timeout=connect_timeout,
            last_event_id=last_event_id,
        )
        self._http = http
        self._sleep = sleep
        self._gen: AsyncGenerator[ListenEvent, None] = self._run()

    def __aiter__(self) -> AsyncIterator[ListenEvent]:
        return self

    async def __anext__(self) -> ListenEvent:
        return await self._gen.__anext__()

    async def aclose(self) -> None:
        """Close the connection. Iteration stops."""
        self._closed = True
        await self._gen.aclose()

    async def __aenter__(self) -> "AsyncListener":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _run(self) -> AsyncGenerator[ListenEvent, None]:
        while not self._closed:
            status: Optional[int] = None
            try:
                async with self._http.stream(
                    "GET", self.url, headers=self._request_headers(), timeout=self._timeout
                ) as response:
                    status = response.status_code
                    if status >= 400 or status == 204:
                        body = await response.aread()
                        if status == 204 or not is_transient_status(status):
                            raise _failure(status, body)
                        raise _Retry(status)
                    if "open" in self._state.requested:
                        yield ListenEvent(type="open")
                    decoder = SSEDecoder(self.last_event_id)
                    async for line in response.aiter_lines():
                        message = decoder.feed(line)
                        self._track(decoder)
                        if message is None:
                            continue
                        event = self._state.handle(message)
                        if event is not None:
                            self.reconnects = 0
                            yield event
            except _Retry as exc:
                status = exc.status
            except httpx.TransportError:
                status = None
            if self._closed:
                return
            reconnect_event = self._after_failure(status)
            if reconnect_event is not None:
                yield reconnect_event
            await self._sleep(self._delay())
