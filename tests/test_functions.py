from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError
from threading import Event
from typing import Callable

import httpx
import pytest

from sanity_client import AsyncSanityClient, RetryPolicy, SanityClient

from .conftest import Recorder, json_body

MISSING_STACK = (
    "`functions.invoke()` requires a `stackId`, either in the client config or on the request. "
    "Function names are only unique within a stack."
)
MISSING_PROJECT = (
    "A functions call requires a `projectId` to be set in the client config, or an "
    "`organizationId` for a stack deployed at organization scope"
)
BAD_EXECUTION = "Expected the stopped execution in the response"


@pytest.mark.parametrize("missing_scope", [False, True])
def test_sync_invoke_validates_before_sending(
    make_client: Callable[..., SanityClient], recorder: Recorder, missing_scope: bool
) -> None:
    options = {"project_id": None, "use_project_hostname": False} if missing_scope else {}
    with make_client(**options) as client:
        with pytest.raises(ValueError) as error:
            client.functions.invoke("process")
    assert str(error.value) == (MISSING_PROJECT if missing_scope else MISSING_STACK)
    assert recorder.requests == []


@pytest.mark.parametrize("missing_scope", [False, True])
async def test_async_invoke_validates_before_sending(
    make_async_client: Callable[..., AsyncSanityClient], recorder: Recorder, missing_scope: bool
) -> None:
    options = {"project_id": None, "use_project_hostname": False} if missing_scope else {}
    async with make_async_client(**options) as client:
        with pytest.raises(ValueError) as error:
            await client.functions.invoke("process")
    assert str(error.value) == (MISSING_PROJECT if missing_scope else MISSING_STACK)
    assert recorder.requests == []


def test_stop_execution_requires_scope(make_client: Callable[..., SanityClient], recorder: Recorder) -> None:
    with make_client(project_id=None, use_project_hostname=False) as client:
        with pytest.raises(ValueError) as error:
            client.functions.durables.stop_execution("fn-1", "run/42")
    assert str(error.value) == MISSING_PROJECT
    assert recorder.requests == []


@pytest.mark.parametrize(
    "body",
    [
        None,
        "unexpected text",
        {},
        {"execution": []},
        {"execution": {"stopTimestamp": 1}},
        {"execution": {"executionId": "run/42", "stopTimestamp": "1"}},
        {"execution": {"executionId": "run/42", "stopTimestamp": True}},
    ],
)
def test_sync_stop_execution_rejects_bad_response(body: object) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    with SanityClient("abc123", api_version="2025-02-19", transport=transport) as client:
        with pytest.raises(ValueError, match=BAD_EXECUTION):
            client.functions.durables.stop_execution("fn-1", "run/42")


async def test_async_stop_execution_rejects_bad_response() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="malformed response"))
    async with AsyncSanityClient("abc123", api_version="2025-02-19", transport=transport) as client:
        with pytest.raises(ValueError, match=BAD_EXECUTION):
            await client.functions.durables.stop_execution("fn-1", "run/42")


def test_sync_functions_return_values_and_timeouts(
    make_client: Callable[..., SanityClient], recorder: Recorder
) -> None:
    with make_client(timeout=12) as client:
        configured = client.with_config(stack_id="stack")
        assert configured.functions.invoke("process") is None
        assert recorder.last.extensions["timeout"]["read"] == 12
        assert configured.functions.invoke("process", {"timeout": 2.5}, sync=True) == {"processed": True}
        assert recorder.last.extensions["timeout"]["read"] == 2.5
        configured.functions.durables.callback_success("callback", timeout=0)
        assert recorder.last.extensions["timeout"]["read"] is None


async def test_async_functions_return_values(make_async_client: Callable[..., AsyncSanityClient]) -> None:
    async with make_async_client(stack_id="stack") as client:
        assert await client.functions.invoke("process") is None
        assert await client.functions.invoke("process", sync=True) == {"processed": True}
        await client.functions.durables.callback_failure("callback")
        stopped = await client.functions.durables.stop_execution("fn-1", "run/42")
        assert stopped == {"executionId": "run/42", "stopTimestamp": 1795219200000}


def test_stop_execution_returns_the_execution_with_extra_fields() -> None:
    execution = {"executionId": "run/42", "stopTimestamp": 2.5, "status": "stopped"}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"execution": execution}))
    with SanityClient("abc123", api_version="2025-02-19", transport=transport) as client:
        stopped = client.functions.durables.stop_execution("fn-1", "run/42")
    assert stopped == execution


def test_callback_failure_includes_exception_traceback(
    make_client: Callable[..., SanityClient], recorder: Recorder
) -> None:
    with make_client() as client:
        try:
            raise RuntimeError("declined")
        except RuntimeError as exc:
            client.functions.durables.callback_failure("callback", exc)
    body = json_body(recorder.last)
    assert body["errorMessage"] == "declined"
    assert "test_callback_failure_includes_exception_traceback" in body["stackTrace"][0]


def test_sync_invoke_pre_cancelled(make_client: Callable[..., SanityClient], recorder: Recorder) -> None:
    signal = Event()
    signal.set()
    with make_client(stack_id="stack") as client:
        with pytest.raises(CancelledError):
            client.functions.invoke("process", {"signal": signal})
    assert recorder.requests == []


def test_sync_cancelled_attempt_does_not_retry() -> None:
    signal = Event()

    def handle(request: httpx.Request) -> httpx.Response:
        signal.set()
        return httpx.Response(503)

    recorder = Recorder(handle)
    with SanityClient(
        "abc123", api_version="2025-02-19", retry=RetryPolicy(retry_writes=True),
        transport=httpx.MockTransport(recorder),
    ) as client:
        with pytest.raises(CancelledError):
            client.functions.durables.callback_success("callback", signal=signal)
    assert len(recorder.requests) == 1


async def test_async_invoke_pre_cancelled(
    make_async_client: Callable[..., AsyncSanityClient], recorder: Recorder
) -> None:
    signal = asyncio.Event()
    signal.set()
    async with make_async_client(stack_id="stack") as client:
        with pytest.raises(asyncio.CancelledError):
            await client.functions.invoke("process", {"signal": signal})
    assert recorder.requests == []


async def test_async_signal_cancels_in_flight_transport() -> None:
    signal = asyncio.Event()
    cancelled = False

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        signal.set()
        try:
            await asyncio.Future[None]()
        except asyncio.CancelledError:
            cancelled = True
            raise
        raise AssertionError("transport should have been cancelled")

    transport = httpx.MockTransport(handle)
    async with AsyncSanityClient("abc123", api_version="2025-02-19", transport=transport) as client:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(client.functions.durables.callback_success("callback", signal=signal), timeout=2)
    assert cancelled
