"""Functions resources for the sync and async clients."""

from __future__ import annotations

import asyncio
from threading import Event
from typing import TYPE_CHECKING, Any, Optional, Union

from . import _core
from .types import DurableCallbackFailure, DurableExecution, InvokeFunctionRequest

if TYPE_CHECKING:
    from ._client import AsyncSanityClient, SanityClient

_DEFAULT: Any = _core._DEFAULT


class Functions:
    """Invoke deployed functions and manage durable executions."""

    def __init__(self, client: SanityClient) -> None:
        self._client = client
        self.durables = Durables(client)

    def invoke(
        self,
        function_name: str,
        request: Optional[InvokeFunctionRequest[Event]] = None,
        *,
        sync: bool = False,
    ) -> Any:
        """Start a function, or wait for its return value with `sync=True`."""
        options: InvokeFunctionRequest[Event] = request if request is not None else {}
        spec = _core.functions_invoke_request(self._client.config, function_name, options, sync=sync)
        return self._client._execute(spec, signal=options.get("signal"))


class Durables:
    """Complete callbacks and stop durable executions."""

    def __init__(self, client: SanityClient) -> None:
        self._client = client

    def callback_success(
        self,
        callback_id: str,
        result: object = None,
        *,
        timeout: Optional[float] = _DEFAULT,
        signal: Optional[Event] = None,
    ) -> None:
        """Resume a waiting function with a JSON-serializable result."""
        spec = _core.functions_callback_request(self._client.config, callback_id, "success", result, timeout)
        self._client._execute(spec, signal=signal)

    def callback_failure(
        self,
        callback_id: str,
        failure: Union[Exception, DurableCallbackFailure, None] = None,
        *,
        timeout: Optional[float] = _DEFAULT,
        signal: Optional[Event] = None,
    ) -> None:
        """Fail a waiting callback with an exception or the supplied error fields."""
        body = _core.durable_callback_failure_body(failure)
        spec = _core.functions_callback_request(self._client.config, callback_id, "failure", body, timeout)
        self._client._execute(spec, signal=signal)

    def stop_execution(
        self,
        function_id: str,
        execution_id: str,
        *,
        organization_id: Optional[str] = None,
        timeout: Optional[float] = _DEFAULT,
        signal: Optional[Event] = None,
    ) -> DurableExecution:
        """Stop an execution and return its ID and stop timestamp."""
        spec = _core.functions_stop_execution_request(
            self._client.config, function_id, execution_id, organization_id=organization_id, timeout=timeout
        )
        return _core.stopped_execution(self._client._execute(spec, signal=signal))


class AsyncFunctions:
    """Functions resource for `AsyncSanityClient`."""

    def __init__(self, client: AsyncSanityClient) -> None:
        self._client = client
        self.durables = AsyncDurables(client)

    async def invoke(
        self,
        function_name: str,
        request: Optional[InvokeFunctionRequest[asyncio.Event]] = None,
        *,
        sync: bool = False,
    ) -> Any:
        """Start a function, or await its return value with `sync=True`."""
        options: InvokeFunctionRequest[asyncio.Event] = request if request is not None else {}
        spec = _core.functions_invoke_request(self._client.config, function_name, options, sync=sync)
        return await _execute_async(self._client, spec, options.get("signal"))


class AsyncDurables:
    """Durables resource for `AsyncSanityClient`."""

    def __init__(self, client: AsyncSanityClient) -> None:
        self._client = client

    async def callback_success(
        self,
        callback_id: str,
        result: object = None,
        *,
        timeout: Optional[float] = _DEFAULT,
        signal: Optional[asyncio.Event] = None,
    ) -> None:
        """Resume a waiting function with a JSON-serializable result."""
        spec = _core.functions_callback_request(self._client.config, callback_id, "success", result, timeout)
        await _execute_async(self._client, spec, signal)

    async def callback_failure(
        self,
        callback_id: str,
        failure: Union[Exception, DurableCallbackFailure, None] = None,
        *,
        timeout: Optional[float] = _DEFAULT,
        signal: Optional[asyncio.Event] = None,
    ) -> None:
        """Fail a waiting callback with an exception or the supplied error fields."""
        body = _core.durable_callback_failure_body(failure)
        spec = _core.functions_callback_request(self._client.config, callback_id, "failure", body, timeout)
        await _execute_async(self._client, spec, signal)

    async def stop_execution(
        self,
        function_id: str,
        execution_id: str,
        *,
        organization_id: Optional[str] = None,
        timeout: Optional[float] = _DEFAULT,
        signal: Optional[asyncio.Event] = None,
    ) -> DurableExecution:
        """Stop an execution and return its ID and stop timestamp."""
        spec = _core.functions_stop_execution_request(
            self._client.config, function_id, execution_id, organization_id=organization_id, timeout=timeout
        )
        return _core.stopped_execution(await _execute_async(self._client, spec, signal))


async def _execute_async(
    client: AsyncSanityClient, spec: _core.RequestSpec, signal: Optional[asyncio.Event]
) -> Any:
    if signal is None:
        return await client._execute(spec)
    if signal.is_set():
        raise asyncio.CancelledError("Sanity Functions request cancelled")
    request = asyncio.create_task(client._execute(spec))
    abort = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait((request, abort), return_when=asyncio.FIRST_COMPLETED)
        if abort in done:
            raise asyncio.CancelledError("Sanity Functions request cancelled")
        return request.result()
    finally:
        request.cancel()
        abort.cancel()
        await asyncio.gather(request, abort, return_exceptions=True)
