"""The sync and async clients."""

from __future__ import annotations

import asyncio
import datetime
import mimetypes
import os
import time
from typing import (
    IO,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    overload,
)

import httpx

from . import _core
from ._core import RequestSpec
from ._listen import AsyncListener, Listener
from ._types import Document, ListenVisibility, Visibility
from .config import DEFAULT_API_HOST, DEFAULT_TIMEOUT, ClientConfig, Perspective
from .errors import NetworkError, RequestTimeoutError
from .patch import AsyncPatch, Patch, PatchSelection
from .retry import RetryPolicy
from .transaction import AsyncTransaction, Transaction
from .types import MutationResult

__all__ = ["AsyncSanityClient", "SanityClient"]

_DEFAULT: Any = _core._DEFAULT

UploadBody = Union[bytes, bytearray, memoryview, IO[bytes], "os.PathLike[str]"]
Mutations = _core.MutationsInput
Actions = Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]


def _read_upload(
    body: UploadBody, filename: Optional[str], content_type: Optional[str]
) -> Tuple[bytes, Optional[str], Optional[str]]:
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body), filename, content_type
    if isinstance(body, os.PathLike):
        path = os.fspath(body)
        with open(path, "rb") as handle:
            data = handle.read()
        name = filename or os.path.basename(path)
        guessed = content_type or mimetypes.guess_type(name)[0]
        return data, name, guessed
    if isinstance(body, str):
        raise TypeError("upload() takes bytes, a binary file object or a pathlib.Path, not str")
    if hasattr(body, "read"):
        data = body.read()
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("upload() needs a file opened in binary mode")
        return bytes(data), filename, content_type
    raise TypeError("upload() takes bytes, a binary file object or a pathlib.Path")


def _first_history_document(body: Any) -> Any:
    if isinstance(body, dict) and isinstance(body.get("documents"), list):
        documents = body["documents"]
        return documents[0] if documents else None
    return body


class _BaseClient:
    _config: ClientConfig
    _retry: RetryPolicy

    @staticmethod
    def _build_config(
        config: Optional[ClientConfig],
        *,
        project_id: Optional[str],
        dataset: Optional[str],
        api_version: Union[str, datetime.date, None],
        token: Optional[str],
        use_cdn: bool,
        perspective: Optional[Perspective],
        api_host: str,
        use_project_hostname: bool,
        request_tag_prefix: Optional[str],
        headers: Optional[Mapping[str, str]],
        timeout: Optional[float],
        result_source_map: Union[bool, str, None],
    ) -> ClientConfig:
        if config is not None:
            return config
        return ClientConfig.create(
            project_id=project_id,
            dataset=dataset,
            api_version=api_version,
            token=token,
            use_cdn=use_cdn,
            perspective=perspective,
            api_host=api_host,
            use_project_hostname=use_project_hostname,
            request_tag_prefix=request_tag_prefix,
            headers=headers,
            timeout=timeout,
            result_source_map=result_source_map,
        )

    @property
    def config(self) -> ClientConfig:
        """The resolved, immutable configuration."""
        return self._config

    @property
    def retry(self) -> RetryPolicy:
        """The retry policy in use."""
        return self._retry

    def get_url(self, uri: str, use_cdn: bool = False) -> str:
        """Absolute URL for ``uri`` on the API (or the API CDN)."""
        return _core.get_url(self._config, uri, use_cdn)

    def get_data_url(self, operation: str, path: Optional[str] = None) -> str:
        """``/data/<operation>/<dataset>[/<path>]``, relative to the API version."""
        return _core.get_data_url(self._config, operation, path)

    def _error_for(self, exc: httpx.TransportError, spec: RequestSpec) -> NetworkError:
        if isinstance(exc, httpx.TimeoutException):
            return RequestTimeoutError(f"{spec.method} {spec.url} timed out: {exc}")
        return NetworkError(f"{spec.method} {spec.url} failed: {exc}")

    def __repr__(self) -> str:
        cfg = self._config
        return (
            f"{type(self).__name__}(project_id={cfg.project_id!r}, dataset={cfg.dataset!r}, "
            f"api_version={cfg.api_version!r}, use_cdn={cfg.use_cdn!r})"
        )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class SanityClient(_BaseClient):
    """Client for the Sanity Content Lake HTTP API.

    Example::

        from sanity_client import SanityClient

        client = SanityClient(
            project_id="zp7mbokg",
            dataset="production",
            api_version="2025-02-19",
        )
        movies = client.fetch('*[_type == "movie"][0...3]{title}')

    Arguments match the JS client's config in snake case. Extra arguments:

    Args:
        retry: How failed requests are retried. Defaults to the JS rules.
        http_client: Your own ``httpx.Client``. It is not closed by
            :meth:`close`.
        transport: An httpx transport for a client created here, handy for
            tests (``httpx.MockTransport``).
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        dataset: Optional[str] = None,
        *,
        api_version: Union[str, datetime.date, None] = None,
        token: Optional[str] = None,
        use_cdn: bool = True,
        perspective: Optional[Perspective] = None,
        api_host: str = DEFAULT_API_HOST,
        use_project_hostname: bool = True,
        request_tag_prefix: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = DEFAULT_TIMEOUT,
        result_source_map: Union[bool, str, None] = None,
        retry: Optional[RetryPolicy] = None,
        http_client: Optional[httpx.Client] = None,
        transport: Optional[httpx.BaseTransport] = None,
        config: Optional[ClientConfig] = None,
    ) -> None:
        self._config = self._build_config(
            config,
            project_id=project_id,
            dataset=dataset,
            api_version=api_version,
            token=token,
            use_cdn=use_cdn,
            perspective=perspective,
            api_host=api_host,
            use_project_hostname=use_project_hostname,
            request_tag_prefix=request_tag_prefix,
            headers=headers,
            timeout=timeout,
            result_source_map=result_source_map,
        )
        self._retry = retry or RetryPolicy()
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(transport=transport)
        self._sleep: Callable[[float], Any] = time.sleep
        self.assets = Assets(self)
        self.projects = Projects(self)
        self.datasets = Datasets(self)
        self.users = Users(self)
        self.history = History(self)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Close the HTTP connection pool, if this client created it."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "SanityClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def with_config(self, **changes: Any) -> "SanityClient":
        """A new client with some settings changed. It shares this client's connections.

        Example: ``client.with_config(perspective="drafts", use_cdn=False)``.
        """
        retry = changes.pop("retry", self._retry)
        clone = SanityClient(config=self._config.merge(**changes), retry=retry, http_client=self._http)
        clone._sleep = self._sleep
        return clone

    def clone(self) -> "SanityClient":
        """Same as ``with_config()`` with no changes."""
        return self.with_config()

    # -- transport ----------------------------------------------------------

    def _send(self, spec: RequestSpec) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    spec.method,
                    spec.url,
                    headers=spec.headers,
                    content=spec.content,
                    timeout=spec.timeout,
                )
            except httpx.TransportError as exc:
                if self._retry.should_retry_error(spec.method, spec.uri, attempt):
                    self._sleep(self._retry.backoff(attempt))
                    attempt += 1
                    continue
                raise self._error_for(exc, spec) from exc
            status = response.status_code
            if status >= 400 and self._retry.should_retry_status(spec.method, spec.uri, status, attempt):
                delay = self._retry.delay_for(attempt, response.headers, status)
                if delay is not None:
                    response.close()
                    self._sleep(delay)
                    attempt += 1
                    continue
            return response

    def _execute(self, spec: RequestSpec) -> Any:
        response = self._send(spec)
        return _core.check_response(
            spec, response.status_code, response.reason_phrase, response.headers, response.content
        )

    # -- queries ------------------------------------------------------------

    def fetch(
        self,
        query: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        filter_response: bool = True,
        return_query: Optional[bool] = None,
        tag: Optional[str] = None,
        perspective: Optional[Perspective] = None,
        result_source_map: Union[bool, str, None] = None,
        cache_mode: Optional[Literal["noStale"]] = None,
        last_live_event_id: Optional[str] = None,
        use_cdn: Optional[bool] = None,
        timeout: Optional[float] = _DEFAULT,
        headers: Optional[Mapping[str, str]] = None,
        token: Optional[str] = None,
    ) -> Any:
        """Run a GROQ query.

        ``params`` are sent JSON encoded as ``$name`` parameters. Returns the
        ``result`` field, or the whole response (``result``, ``ms``,
        ``query``, ``syncTags``...) when ``filter_response=False``.
        """
        spec = _core.fetch_request(
            self._config,
            query,
            params,
            filter_response=filter_response,
            return_query=return_query,
            tag=tag,
            perspective=perspective,
            result_source_map=result_source_map,
            cache_mode=cache_mode,
            last_live_event_id=last_live_event_id,
            use_cdn=use_cdn,
            timeout=timeout,
            headers=headers,
            token=token,
        )
        body = self._execute(spec)
        if not filter_response:
            return body
        return body.get("result") if isinstance(body, dict) else body

    # -- documents ----------------------------------------------------------

    @overload
    def get_document(
        self,
        document_id: str,
        *,
        tag: Optional[str] = ...,
        release_id: Optional[str] = ...,
        include_all_versions: Literal[True],
        timeout: Optional[float] = ...,
    ) -> List[Document]: ...

    @overload
    def get_document(
        self,
        document_id: str,
        *,
        tag: Optional[str] = ...,
        release_id: Optional[str] = ...,
        include_all_versions: Optional[Literal[False]] = ...,
        timeout: Optional[float] = ...,
    ) -> Optional[Document]: ...

    def get_document(
        self,
        document_id: str,
        *,
        tag: Optional[str] = None,
        release_id: Optional[str] = None,
        include_all_versions: Optional[bool] = None,
        timeout: Optional[float] = _DEFAULT,
    ) -> Union[Optional[Document], List[Document]]:
        """One document by ID, or ``None``.

        ``release_id`` reads the document's version in that release.
        ``include_all_versions=True`` returns the published, draft and
        release versions as a list.
        """
        spec = _core.get_document_request(
            self._config,
            document_id,
            tag=tag,
            release_id=release_id,
            include_all_versions=include_all_versions,
            timeout=timeout,
        )
        result: Union[Optional[Document], List[Document]] = _core.map_get_document(
            self._execute(spec), include_all_versions
        )
        return result

    def get_documents(
        self, ids: Sequence[str], *, tag: Optional[str] = None, timeout: Optional[float] = _DEFAULT
    ) -> List[Optional[Document]]:
        """Several documents by ID, in the order asked, ``None`` for missing ones."""
        spec = _core.get_documents_request(self._config, ids, tag=tag, timeout=timeout)
        return _core.map_get_documents(self._execute(spec), ids)

    def documents_exists(
        self, ids: Sequence[str], *, tag: Optional[str] = None, timeout: Optional[float] = _DEFAULT
    ) -> Set[str]:
        """The subset of ``ids`` that exist, checked 100 at a time without fetching content."""
        existing: Set[str] = set()
        for spec, batch in _core.documents_exists_requests(self._config, ids, tag=tag, timeout=timeout):
            existing |= _core.map_documents_exists(self._execute(spec), batch)
        return existing

    # -- mutations ----------------------------------------------------------

    def mutate(
        self,
        mutations: Mutations,
        *,
        return_documents: Optional[bool] = None,
        visibility: Optional[Visibility] = None,
        dry_run: Optional[bool] = None,
        auto_generate_array_keys: Optional[bool] = None,
        skip_cross_dataset_reference_validation: Optional[bool] = None,
        transaction_id: Optional[str] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = _DEFAULT,
    ) -> MutationResult:
        """Send raw mutations, a patch or a transaction in one request.

        ``return_documents`` defaults to on, so the result carries the
        documents as stored. ``visibility`` defaults to ``"sync"``.
        """
        spec = _core.mutate_request(
            self._config,
            mutations,
            return_documents=return_documents,
            visibility=visibility,
            dry_run=dry_run,
            auto_generate_array_keys=auto_generate_array_keys,
            skip_cross_dataset_reference_validation=skip_cross_dataset_reference_validation,
            transaction_id=transaction_id,
            tag=tag,
            timeout=timeout,
        )
        return MutationResult.from_response(self._execute(spec))

    def create(self, document: Mapping[str, Any], **options: Any) -> MutationResult:
        """Create a document. Fails if ``_id`` is given and already taken.

        Takes the keyword options of :meth:`mutate`. ``result.document``
        is the stored document.
        """
        return self.mutate([_core.single_mutation("create", document)], **options)

    def create_if_not_exists(self, document: Mapping[str, Any], **options: Any) -> MutationResult:
        """Create the document unless one with its ``_id`` exists."""
        return self.mutate([_core.single_mutation("createIfNotExists", document)], **options)

    def create_or_replace(self, document: Mapping[str, Any], **options: Any) -> MutationResult:
        """Create the document or replace the one with its ``_id``."""
        return self.mutate([_core.single_mutation("createOrReplace", document)], **options)

    def delete(self, selection: PatchSelection, **options: Any) -> MutationResult:
        """Delete by ID, by list of IDs, or by ``{"query": ..., "params": ...}``."""
        return self.mutate([_core.delete_mutation(selection)], **options)

    def patch(self, selection: PatchSelection, operations: Optional[Mapping[str, Any]] = None) -> Patch:
        """Start a patch. Chain operations and call ``.commit()``."""
        return Patch(selection, operations, self)

    def transaction(
        self,
        operations: Optional[Sequence[Mapping[str, Any]]] = None,
        transaction_id: Optional[str] = None,
    ) -> Transaction:
        """Start a transaction. Chain mutations and call ``.commit()``."""
        return Transaction(operations, self, transaction_id)

    def action(
        self,
        actions: Actions,
        *,
        transaction_id: Optional[str] = None,
        skip_cross_dataset_reference_validation: Optional[bool] = None,
        dry_run: Optional[bool] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = _DEFAULT,
    ) -> Dict[str, Any]:
        """Run one or more document actions (publish, discard, unpublish...)."""
        spec = _core.action_request(
            self._config,
            actions,
            transaction_id=transaction_id,
            skip_cross_dataset_reference_validation=skip_cross_dataset_reference_validation,
            dry_run=dry_run,
            tag=tag,
            timeout=timeout,
        )
        body = self._execute(spec)
        return body if isinstance(body, dict) else {}

    # -- listen -------------------------------------------------------------

    def listen(
        self,
        query: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        events: Sequence[str] = ("mutation",),
        include_result: bool = True,
        include_previous_revision: Optional[bool] = None,
        include_mutations: Optional[bool] = None,
        include_all_versions: Optional[bool] = None,
        visibility: Optional[ListenVisibility] = None,
        effect_format: Optional[Literal["mendoza"]] = None,
        enable_resume: Optional[bool] = None,
        tag: Optional[str] = None,
        last_event_id: Optional[str] = None,
        reconnect_delay: float = 1.0,
        max_reconnects: Optional[int] = None,
        read_timeout: Optional[float] = None,
    ) -> Listener:
        """Stream changes to documents matching a GROQ filter.

        Returns an iterator of :class:`~sanity_client.ListenEvent`. Only the
        event types in ``events`` are yielded (``"mutation"`` by default,
        also ``"welcome"``, ``"reconnect"``, ``"welcomeback"``, ``"reset"``
        and ``"open"``). Close it, or use it in a ``with`` block::

            with client.listen('*[_type == "movie"]', events=["welcome", "mutation"]) as events:
                for event in events:
                    print(event.type, event.document_id)
        """
        url = _core.listen_url(
            self._config,
            query,
            params,
            include_result=include_result,
            include_previous_revision=include_previous_revision,
            include_mutations=include_mutations,
            include_all_versions=include_all_versions,
            visibility=visibility,
            effect_format=effect_format,
            enable_resume=enable_resume,
            tag=tag,
        )
        return Listener(
            self._http,
            url,
            _core.listen_headers(self._config),
            list(events),
            tag=_core.resolve_tag(self._config, tag) if tag else None,
            reconnect_delay=reconnect_delay,
            max_reconnects=max_reconnects,
            read_timeout=read_timeout,
            last_event_id=last_event_id,
            sleep=self._sleep,
        )

    # -- raw ----------------------------------------------------------------

    def request(
        self,
        uri: str,
        *,
        method: str = "GET",
        query: Optional[Mapping[str, Any]] = None,
        body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        tag: Optional[str] = None,
        use_cdn: Optional[bool] = None,
        timeout: Optional[float] = _DEFAULT,
        token: Optional[str] = None,
    ) -> Any:
        """Call any endpoint below the API version, for example ``/users/me``.

        ``body`` is sent as JSON, or as raw bytes when it is ``bytes``.
        """
        spec = _core.raw_request(
            self._config,
            uri,
            method=method,
            query=query,
            body=body,
            headers=headers,
            tag=tag,
            use_cdn=use_cdn,
            timeout=timeout,
            token=token,
        )
        return self._execute(spec)


class _Resource:
    def __init__(self, client: SanityClient) -> None:
        self._client = client


class Assets(_Resource):
    """``client.assets``: image and file uploads."""

    def upload(
        self,
        asset_type: Literal["image", "file"],
        body: UploadBody,
        *,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        extract: Optional[Sequence[str]] = None,
        label: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        credit_line: Optional[str] = None,
        source: Optional[Mapping[str, str]] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Document:
        """Upload an image or file and return its asset document.

        ``body`` is bytes, a binary file object or a ``pathlib.Path``. For a
        path, ``filename`` and ``content_type`` default to the file's name
        and guessed type. ``extract`` picks image metadata
        (``["palette", "exif", "location", "lqip", "blurhash"...]``), an
        empty list asks for none. No timeout unless you set one.
        """
        data, name, mime = _read_upload(body, filename, content_type)
        spec = _core.upload_request(
            self._client._config,
            asset_type,
            data,
            filename=name,
            content_type=mime,
            extract=extract,
            label=label,
            title=title,
            description=description,
            credit_line=credit_line,
            source=source,
            tag=tag,
            timeout=timeout,
        )
        result = self._client._execute(spec)
        document = result.get("document") if isinstance(result, dict) else None
        return document if isinstance(document, dict) else {}


class Projects(_Resource):
    """``client.projects``: projects the token can see."""

    def list(
        self,
        *,
        include_members: Optional[bool] = None,
        include_features: Optional[bool] = None,
        organization_id: Optional[str] = None,
        only_explicit_membership: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        spec = _core.projects_list_request(
            self._client._config,
            include_members=include_members,
            include_features=include_features,
            organization_id=organization_id,
            only_explicit_membership=only_explicit_membership,
        )
        result: List[Dict[str, Any]] = self._client._execute(spec)
        return result

    def get_by_id(self, project_id: str) -> Dict[str, Any]:
        result: Dict[str, Any] = self._client._execute(
            _core.build_request(self._client._config, f"/projects/{project_id}")
        )
        return result


class Datasets(_Resource):
    """``client.datasets``: list, create, edit and delete datasets."""

    def list(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = self._client._execute(
            _core.dataset_request(self._client._config, "GET")
        )
        return result

    def create(
        self, name: str, *, acl_mode: Optional[str] = None, embeddings: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        body = {k: v for k, v in (("aclMode", acl_mode), ("embeddings", embeddings)) if v is not None}
        result: Dict[str, Any] = self._client._execute(
            _core.dataset_request(self._client._config, "PUT", name, body)
        )
        return result

    def edit(self, name: str, *, acl_mode: Optional[str] = None) -> Dict[str, Any]:
        body = {"aclMode": acl_mode} if acl_mode is not None else None
        result: Dict[str, Any] = self._client._execute(
            _core.dataset_request(self._client._config, "PATCH", name, body)
        )
        return result

    def delete(self, name: str) -> Dict[str, Any]:
        result: Dict[str, Any] = self._client._execute(
            _core.dataset_request(self._client._config, "DELETE", name)
        )
        return result


class Users(_Resource):
    """``client.users``: user lookups. ``get_by_id("me")`` is the token's user."""

    def get_by_id(self, user_id: str) -> Dict[str, Any]:
        result: Dict[str, Any] = self._client._execute(
            _core.build_request(self._client._config, f"/users/{user_id}")
        )
        return result


class History(_Resource):
    """``client.history``: the History API (needs a token with read access)."""

    def get_document(
        self,
        document_id: str,
        *,
        revision: Optional[str] = None,
        time: Optional[str] = None,
        last_revision: Optional[bool] = None,
        tag: Optional[str] = None,
    ) -> Optional[Document]:
        """A document as it was at ``revision`` or at ``time`` (ISO 8601)."""
        spec = _core.history_document_request(
            self._client._config,
            document_id,
            revision=revision,
            time=time,
            last_revision=last_revision,
            tag=tag,
        )
        result: Optional[Document] = _first_history_document(self._client._execute(spec))
        return result

    def transactions(
        self,
        document_ids: Optional[Sequence[str]] = None,
        *,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        from_transaction: Optional[str] = None,
        to_transaction: Optional[str] = None,
        authors: Optional[Sequence[str]] = None,
        reverse: Optional[bool] = None,
        limit: Optional[int] = None,
        effect_format: Optional[str] = None,
        include_identified_documents_only: Optional[bool] = None,
        tag: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Transactions that touched the dataset (or the given documents), parsed from NDJSON."""
        spec = _core.history_transactions_request(
            self._client._config,
            document_ids,
            from_time=from_time,
            to_time=to_time,
            from_transaction=from_transaction,
            to_transaction=to_transaction,
            authors=authors,
            reverse=reverse,
            limit=limit,
            effect_format=effect_format,
            include_identified_documents_only=include_identified_documents_only,
            tag=tag,
        )
        result: List[Dict[str, Any]] = self._client._execute(spec)
        return result


# ---------------------------------------------------------------------------
# Async
# ---------------------------------------------------------------------------


class AsyncSanityClient(_BaseClient):
    """Async twin of :class:`SanityClient`. Every request method is a coroutine.

    Example::

        async with AsyncSanityClient(project_id="zp7mbokg", dataset="production",
                                     api_version="2025-02-19") as client:
            movies = await client.fetch('*[_type == "movie"][0...3]{title}')
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        dataset: Optional[str] = None,
        *,
        api_version: Union[str, datetime.date, None] = None,
        token: Optional[str] = None,
        use_cdn: bool = True,
        perspective: Optional[Perspective] = None,
        api_host: str = DEFAULT_API_HOST,
        use_project_hostname: bool = True,
        request_tag_prefix: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = DEFAULT_TIMEOUT,
        result_source_map: Union[bool, str, None] = None,
        retry: Optional[RetryPolicy] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        config: Optional[ClientConfig] = None,
    ) -> None:
        self._config = self._build_config(
            config,
            project_id=project_id,
            dataset=dataset,
            api_version=api_version,
            token=token,
            use_cdn=use_cdn,
            perspective=perspective,
            api_host=api_host,
            use_project_hostname=use_project_hostname,
            request_tag_prefix=request_tag_prefix,
            headers=headers,
            timeout=timeout,
            result_source_map=result_source_map,
        )
        self._retry = retry or RetryPolicy()
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(transport=transport)
        self._sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
        self.assets = AsyncAssets(self)
        self.projects = AsyncProjects(self)
        self.datasets = AsyncDatasets(self)
        self.users = AsyncUsers(self)
        self.history = AsyncHistory(self)

    async def aclose(self) -> None:
        """Close the HTTP connection pool, if this client created it."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncSanityClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def with_config(self, **changes: Any) -> "AsyncSanityClient":
        """A new client with some settings changed. It shares this client's connections."""
        retry = changes.pop("retry", self._retry)
        clone = AsyncSanityClient(config=self._config.merge(**changes), retry=retry, http_client=self._http)
        clone._sleep = self._sleep
        return clone

    def clone(self) -> "AsyncSanityClient":
        """Same as ``with_config()`` with no changes."""
        return self.with_config()

    async def _send(self, spec: RequestSpec) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    spec.method,
                    spec.url,
                    headers=spec.headers,
                    content=spec.content,
                    timeout=spec.timeout,
                )
            except httpx.TransportError as exc:
                if self._retry.should_retry_error(spec.method, spec.uri, attempt):
                    await self._sleep(self._retry.backoff(attempt))
                    attempt += 1
                    continue
                raise self._error_for(exc, spec) from exc
            status = response.status_code
            if status >= 400 and self._retry.should_retry_status(spec.method, spec.uri, status, attempt):
                delay = self._retry.delay_for(attempt, response.headers, status)
                if delay is not None:
                    await response.aclose()
                    await self._sleep(delay)
                    attempt += 1
                    continue
            return response

    async def _execute(self, spec: RequestSpec) -> Any:
        response = await self._send(spec)
        return _core.check_response(
            spec, response.status_code, response.reason_phrase, response.headers, response.content
        )

    async def fetch(
        self,
        query: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        filter_response: bool = True,
        return_query: Optional[bool] = None,
        tag: Optional[str] = None,
        perspective: Optional[Perspective] = None,
        result_source_map: Union[bool, str, None] = None,
        cache_mode: Optional[Literal["noStale"]] = None,
        last_live_event_id: Optional[str] = None,
        use_cdn: Optional[bool] = None,
        timeout: Optional[float] = _DEFAULT,
        headers: Optional[Mapping[str, str]] = None,
        token: Optional[str] = None,
    ) -> Any:
        """Run a GROQ query. See :meth:`SanityClient.fetch`."""
        spec = _core.fetch_request(
            self._config,
            query,
            params,
            filter_response=filter_response,
            return_query=return_query,
            tag=tag,
            perspective=perspective,
            result_source_map=result_source_map,
            cache_mode=cache_mode,
            last_live_event_id=last_live_event_id,
            use_cdn=use_cdn,
            timeout=timeout,
            headers=headers,
            token=token,
        )
        body = await self._execute(spec)
        if not filter_response:
            return body
        return body.get("result") if isinstance(body, dict) else body

    @overload
    async def get_document(
        self,
        document_id: str,
        *,
        tag: Optional[str] = ...,
        release_id: Optional[str] = ...,
        include_all_versions: Literal[True],
        timeout: Optional[float] = ...,
    ) -> List[Document]: ...

    @overload
    async def get_document(
        self,
        document_id: str,
        *,
        tag: Optional[str] = ...,
        release_id: Optional[str] = ...,
        include_all_versions: Optional[Literal[False]] = ...,
        timeout: Optional[float] = ...,
    ) -> Optional[Document]: ...

    async def get_document(
        self,
        document_id: str,
        *,
        tag: Optional[str] = None,
        release_id: Optional[str] = None,
        include_all_versions: Optional[bool] = None,
        timeout: Optional[float] = _DEFAULT,
    ) -> Union[Optional[Document], List[Document]]:
        """One document by ID, or ``None``. See :meth:`SanityClient.get_document`."""
        spec = _core.get_document_request(
            self._config,
            document_id,
            tag=tag,
            release_id=release_id,
            include_all_versions=include_all_versions,
            timeout=timeout,
        )
        result: Union[Optional[Document], List[Document]] = _core.map_get_document(
            await self._execute(spec), include_all_versions
        )
        return result

    async def get_documents(
        self, ids: Sequence[str], *, tag: Optional[str] = None, timeout: Optional[float] = _DEFAULT
    ) -> List[Optional[Document]]:
        """Several documents by ID, in the order asked, ``None`` for missing ones."""
        spec = _core.get_documents_request(self._config, ids, tag=tag, timeout=timeout)
        return _core.map_get_documents(await self._execute(spec), ids)

    async def documents_exists(
        self, ids: Sequence[str], *, tag: Optional[str] = None, timeout: Optional[float] = _DEFAULT
    ) -> Set[str]:
        """The subset of ``ids`` that exist."""
        existing: Set[str] = set()
        for spec, batch in _core.documents_exists_requests(self._config, ids, tag=tag, timeout=timeout):
            existing |= _core.map_documents_exists(await self._execute(spec), batch)
        return existing

    async def mutate(
        self,
        mutations: Mutations,
        *,
        return_documents: Optional[bool] = None,
        visibility: Optional[Visibility] = None,
        dry_run: Optional[bool] = None,
        auto_generate_array_keys: Optional[bool] = None,
        skip_cross_dataset_reference_validation: Optional[bool] = None,
        transaction_id: Optional[str] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = _DEFAULT,
    ) -> MutationResult:
        """Send raw mutations, a patch or a transaction. See :meth:`SanityClient.mutate`."""
        spec = _core.mutate_request(
            self._config,
            mutations,
            return_documents=return_documents,
            visibility=visibility,
            dry_run=dry_run,
            auto_generate_array_keys=auto_generate_array_keys,
            skip_cross_dataset_reference_validation=skip_cross_dataset_reference_validation,
            transaction_id=transaction_id,
            tag=tag,
            timeout=timeout,
        )
        return MutationResult.from_response(await self._execute(spec))

    async def create(self, document: Mapping[str, Any], **options: Any) -> MutationResult:
        """Create a document."""
        return await self.mutate([_core.single_mutation("create", document)], **options)

    async def create_if_not_exists(self, document: Mapping[str, Any], **options: Any) -> MutationResult:
        """Create the document unless one with its ``_id`` exists."""
        return await self.mutate([_core.single_mutation("createIfNotExists", document)], **options)

    async def create_or_replace(self, document: Mapping[str, Any], **options: Any) -> MutationResult:
        """Create the document or replace the one with its ``_id``."""
        return await self.mutate([_core.single_mutation("createOrReplace", document)], **options)

    async def delete(self, selection: PatchSelection, **options: Any) -> MutationResult:
        """Delete by ID, by list of IDs, or by ``{"query": ..., "params": ...}``."""
        return await self.mutate([_core.delete_mutation(selection)], **options)

    def patch(self, selection: PatchSelection, operations: Optional[Mapping[str, Any]] = None) -> AsyncPatch:
        """Start a patch. Chain operations and ``await patch.commit()``."""
        return AsyncPatch(selection, operations, self)

    def transaction(
        self,
        operations: Optional[Sequence[Mapping[str, Any]]] = None,
        transaction_id: Optional[str] = None,
    ) -> AsyncTransaction:
        """Start a transaction. Chain mutations and ``await trx.commit()``."""
        return AsyncTransaction(operations, self, transaction_id)

    async def action(
        self,
        actions: Actions,
        *,
        transaction_id: Optional[str] = None,
        skip_cross_dataset_reference_validation: Optional[bool] = None,
        dry_run: Optional[bool] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = _DEFAULT,
    ) -> Dict[str, Any]:
        """Run one or more document actions."""
        spec = _core.action_request(
            self._config,
            actions,
            transaction_id=transaction_id,
            skip_cross_dataset_reference_validation=skip_cross_dataset_reference_validation,
            dry_run=dry_run,
            tag=tag,
            timeout=timeout,
        )
        body = await self._execute(spec)
        return body if isinstance(body, dict) else {}

    def listen(
        self,
        query: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        events: Sequence[str] = ("mutation",),
        include_result: bool = True,
        include_previous_revision: Optional[bool] = None,
        include_mutations: Optional[bool] = None,
        include_all_versions: Optional[bool] = None,
        visibility: Optional[ListenVisibility] = None,
        effect_format: Optional[Literal["mendoza"]] = None,
        enable_resume: Optional[bool] = None,
        tag: Optional[str] = None,
        last_event_id: Optional[str] = None,
        reconnect_delay: float = 1.0,
        max_reconnects: Optional[int] = None,
        read_timeout: Optional[float] = None,
    ) -> AsyncListener:
        """Stream changes as an async iterator. See :meth:`SanityClient.listen`."""
        url = _core.listen_url(
            self._config,
            query,
            params,
            include_result=include_result,
            include_previous_revision=include_previous_revision,
            include_mutations=include_mutations,
            include_all_versions=include_all_versions,
            visibility=visibility,
            effect_format=effect_format,
            enable_resume=enable_resume,
            tag=tag,
        )
        return AsyncListener(
            self._http,
            url,
            _core.listen_headers(self._config),
            list(events),
            tag=_core.resolve_tag(self._config, tag) if tag else None,
            reconnect_delay=reconnect_delay,
            max_reconnects=max_reconnects,
            read_timeout=read_timeout,
            last_event_id=last_event_id,
            sleep=self._sleep,
        )

    async def request(
        self,
        uri: str,
        *,
        method: str = "GET",
        query: Optional[Mapping[str, Any]] = None,
        body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        tag: Optional[str] = None,
        use_cdn: Optional[bool] = None,
        timeout: Optional[float] = _DEFAULT,
        token: Optional[str] = None,
    ) -> Any:
        """Call any endpoint below the API version. See :meth:`SanityClient.request`."""
        spec = _core.raw_request(
            self._config,
            uri,
            method=method,
            query=query,
            body=body,
            headers=headers,
            tag=tag,
            use_cdn=use_cdn,
            timeout=timeout,
            token=token,
        )
        return await self._execute(spec)


class _AsyncResource:
    def __init__(self, client: AsyncSanityClient) -> None:
        self._client = client


class AsyncAssets(_AsyncResource):
    """``client.assets`` on the async client."""

    async def upload(
        self,
        asset_type: Literal["image", "file"],
        body: UploadBody,
        *,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        extract: Optional[Sequence[str]] = None,
        label: Optional[str] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        credit_line: Optional[str] = None,
        source: Optional[Mapping[str, str]] = None,
        tag: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Document:
        """Upload an image or file. See :meth:`Assets.upload`."""
        data, name, mime = _read_upload(body, filename, content_type)
        spec = _core.upload_request(
            self._client._config,
            asset_type,
            data,
            filename=name,
            content_type=mime,
            extract=extract,
            label=label,
            title=title,
            description=description,
            credit_line=credit_line,
            source=source,
            tag=tag,
            timeout=timeout,
        )
        result = await self._client._execute(spec)
        document = result.get("document") if isinstance(result, dict) else None
        return document if isinstance(document, dict) else {}


class AsyncProjects(_AsyncResource):
    async def list(
        self,
        *,
        include_members: Optional[bool] = None,
        include_features: Optional[bool] = None,
        organization_id: Optional[str] = None,
        only_explicit_membership: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        spec = _core.projects_list_request(
            self._client._config,
            include_members=include_members,
            include_features=include_features,
            organization_id=organization_id,
            only_explicit_membership=only_explicit_membership,
        )
        result: List[Dict[str, Any]] = await self._client._execute(spec)
        return result

    async def get_by_id(self, project_id: str) -> Dict[str, Any]:
        result: Dict[str, Any] = await self._client._execute(
            _core.build_request(self._client._config, f"/projects/{project_id}")
        )
        return result


class AsyncDatasets(_AsyncResource):
    async def list(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = await self._client._execute(
            _core.dataset_request(self._client._config, "GET")
        )
        return result

    async def create(
        self, name: str, *, acl_mode: Optional[str] = None, embeddings: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        body = {k: v for k, v in (("aclMode", acl_mode), ("embeddings", embeddings)) if v is not None}
        result: Dict[str, Any] = await self._client._execute(
            _core.dataset_request(self._client._config, "PUT", name, body)
        )
        return result

    async def edit(self, name: str, *, acl_mode: Optional[str] = None) -> Dict[str, Any]:
        body = {"aclMode": acl_mode} if acl_mode is not None else None
        result: Dict[str, Any] = await self._client._execute(
            _core.dataset_request(self._client._config, "PATCH", name, body)
        )
        return result

    async def delete(self, name: str) -> Dict[str, Any]:
        result: Dict[str, Any] = await self._client._execute(
            _core.dataset_request(self._client._config, "DELETE", name)
        )
        return result


class AsyncUsers(_AsyncResource):
    async def get_by_id(self, user_id: str) -> Dict[str, Any]:
        result: Dict[str, Any] = await self._client._execute(
            _core.build_request(self._client._config, f"/users/{user_id}")
        )
        return result


class AsyncHistory(_AsyncResource):
    async def get_document(
        self,
        document_id: str,
        *,
        revision: Optional[str] = None,
        time: Optional[str] = None,
        last_revision: Optional[bool] = None,
        tag: Optional[str] = None,
    ) -> Optional[Document]:
        spec = _core.history_document_request(
            self._client._config,
            document_id,
            revision=revision,
            time=time,
            last_revision=last_revision,
            tag=tag,
        )
        result: Optional[Document] = _first_history_document(await self._client._execute(spec))
        return result

    async def transactions(
        self,
        document_ids: Optional[Sequence[str]] = None,
        *,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        from_transaction: Optional[str] = None,
        to_transaction: Optional[str] = None,
        authors: Optional[Sequence[str]] = None,
        reverse: Optional[bool] = None,
        limit: Optional[int] = None,
        effect_format: Optional[str] = None,
        include_identified_documents_only: Optional[bool] = None,
        tag: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        spec = _core.history_transactions_request(
            self._client._config,
            document_ids,
            from_time=from_time,
            to_time=to_time,
            from_transaction=from_transaction,
            to_transaction=to_transaction,
            authors=authors,
            reverse=reverse,
            limit=limit,
            effect_format=effect_format,
            include_identified_documents_only=include_identified_documents_only,
            tag=tag,
        )
        result: List[Dict[str, Any]] = await self._client._execute(spec)
        return result
