"""Request building shared by the sync and async clients.

Every public method turns into a :class:`RequestSpec` here, with no I/O, so
both clients send the same bytes and the tests can check the specs directly.
The rules follow ``_prepareRequest``, ``requestOptions`` and the data method
helpers in @sanity/client.
"""

from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ._encoding import encode_query_string, encode_uri_component, form_urlencode, js_json_dumps, query_pairs
from ._types import Visibility
from ._version import __version__
from .config import ClientConfig, validate_dataset, validate_perspective, validate_request_tag
from .errors import HttpResponseInfo, error_from_response
from .ids import get_version_from_id, get_version_id, is_draft_id
from .patch import BasePatch, get_selection
from .transaction import BaseTransaction, _require_document_id
from .types import MutationResult

__all__ = ["RequestSpec", "SanityApiWarning", "build_request"]

QUERY_SIZE_LIMIT = 11264
"""Encoded query strings this long or longer go in a ``POST`` body."""

LISTEN_MAX_URL_LENGTH = 16000 - 1200
DOCUMENTS_EXISTS_BATCH_SIZE = 100
USER_AGENT = f"sanity-client-py {__version__}"

_DEFAULT: Any = object()


class SanityApiWarning(UserWarning):
    """A warning the API sent in the ``X-Sanity-Warning`` header."""


class SanityDeprecationWarning(DeprecationWarning):
    """Use of an option the API or client is phasing out."""


_printed_warnings: Set[str] = set()


def emit_api_warnings(headers: Mapping[str, str]) -> None:
    """Show each ``X-Sanity-Warning`` once per process, like the JS client."""
    raw = headers.get("x-sanity-warning")
    if not raw:
        return
    for message in [part.strip() for part in raw.split("\n")]:
        if message and message not in _printed_warnings:
            _printed_warnings.add(message)
            warnings.warn(message, SanityApiWarning, stacklevel=4)


@dataclass
class RequestSpec:
    """A fully built request.

    Attributes:
        method: HTTP method.
        url: Absolute URL, query string included and already encoded.
        uri: The path below the API version (``/data/query/production?...``).
            Retry rules look at it.
        headers: Request headers, without transport ones such as ``Host``.
        content: Body bytes, or ``None``.
        timeout: Seconds, or ``None`` for no timeout.
        tag: Request tag that was sent, if any.
    """

    method: str
    url: str
    uri: str
    headers: Dict[str, str]
    content: Optional[bytes] = None
    timeout: Optional[float] = None
    tag: Optional[str] = None
    expect: str = field(default="json")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def get_data_url(config: ClientConfig, operation: str, path: Optional[str] = None) -> str:
    """``/data/<operation>/<dataset>[/<path>]`` with the JS trailing slash rule."""
    dataset = config.require_dataset()
    base = f"/{operation}/{dataset}"
    uri = f"{base}/{path}" if path is not None else base
    return re.sub(r"/($|\?)", r"\1", f"/data{uri}", count=1)


def get_url(config: ClientConfig, uri: str, use_cdn: bool = False) -> str:
    """Absolute URL for ``uri`` on the API or the API CDN."""
    base = config.cdn_url if use_cdn else config.url
    return f"{base}/{uri[1:] if uri.startswith('/') else uri}"


def _is_query(config: ClientConfig, uri: str) -> bool:
    return bool(config.dataset) and uri.startswith(get_data_url(config, "query"))


def _is_data(config: ClientConfig, uri: str) -> bool:
    if uri.startswith("/data/"):
        return True
    if not config.dataset:
        return False
    return any(
        uri.startswith(get_data_url(config, op, path))
        for op, path in (("query", None), ("mutate", None), ("doc", ""), ("listen", None), ("history", ""))
    )


def _add_query(url: str, query: Mapping[str, Any]) -> str:
    pairs = query_pairs(query)
    if not pairs:
        return url
    return url + ("&" if "?" in url else "?") + form_urlencode(pairs)


def resolve_tag(config: ClientConfig, tag: Optional[str]) -> Optional[str]:
    if tag and config.request_tag_prefix:
        return f"{config.request_tag_prefix}.{tag}"
    return tag or config.request_tag_prefix


# ---------------------------------------------------------------------------
# The generic request builder
# ---------------------------------------------------------------------------


def build_request(
    config: ClientConfig,
    uri: str,
    *,
    method: str = "GET",
    query: Optional[Mapping[str, Any]] = None,
    body: Any = _DEFAULT,
    content: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    token: Optional[str] = None,
    tag: Optional[str] = None,
    no_tag: bool = False,
    can_use_cdn: Optional[bool] = None,
    use_cdn: Optional[bool] = None,
    perspective: Any = None,
    result_source_map: Union[bool, str, None] = None,
    return_query: Optional[bool] = None,
    last_live_event_id: Optional[str] = None,
    cache_mode: Optional[str] = None,
    timeout: Any = _DEFAULT,
    expect: str = "json",
) -> RequestSpec:
    """Port of ``_prepareRequest`` plus ``requestOptions``.

    ``body`` is serialized as JSON (``JSON.stringify`` rules), ``content`` is
    sent as raw bytes. ``no_tag`` drops the tag and the configured prefix,
    the same as passing ``tag: null`` in JS.
    """
    method = method.upper()
    q: Dict[str, Any] = dict(query or {})

    if can_use_cdn is None:
        can_use_cdn = method in ("GET", "HEAD") and _is_data(config, uri)
    cdn = bool((use_cdn if use_cdn is not None else config.use_cdn) and can_use_cdn)

    full_tag = resolve_tag(config, tag)
    sent_tag: Optional[str] = None
    if full_tag and not no_tag:
        sent_tag = validate_request_tag(full_tag)
        q = {"tag": sent_tag, **q}

    if method in ("GET", "HEAD", "POST") and _is_query(config, uri):
        rsm = result_source_map if result_source_map is not None else config.result_source_map
        if rsm is not None and rsm is not False:
            q = {"resultSourceMap": rsm, **q}
        persp = perspective or config.perspective
        if persp is not None:
            normalized = validate_perspective(persp)
            if normalized == "previewDrafts":
                warnings.warn(
                    "The `previewDrafts` perspective has been renamed to `drafts` and will be removed "
                    "in a future API version",
                    SanityDeprecationWarning,
                    stacklevel=4,
                )
            value = ",".join(normalized) if isinstance(normalized, tuple) else normalized
            q = {"perspective": value, **q}
            if cdn and (
                (isinstance(normalized, tuple) and len(normalized) > 0)
                or normalized in ("previewDrafts", "drafts")
            ):
                cdn = False
                warnings.warn(
                    "The Sanity client is configured with `use_cdn` set to `True`, but the `drafts` "
                    "perspective or a perspective stack is set. The API CDN does not support these, "
                    "so the request goes to the live API.",
                    UserWarning,
                    stacklevel=4,
                )
        if last_live_event_id:
            q = {**q, "lastLiveEventId": last_live_event_id}
        if return_query is False:
            q = {"returnQuery": "false", **q}
        if cdn and cache_mode == "noStale":
            q = {"cacheMode": "noStale", **q}

    url = _add_query(get_url(config, uri, cdn), q)

    out_headers: Dict[str, str] = dict(config.headers)
    bearer = token or config.token
    if bearer:
        out_headers["Authorization"] = f"Bearer {bearer}"
    if not config.use_project_hostname and config.project_id:
        out_headers["X-Sanity-Project-ID"] = config.project_id
    if headers:
        out_headers.update(headers)

    data: Optional[bytes] = content
    if body is not _DEFAULT and body is not None:
        data = js_json_dumps(body).encode("utf-8")
        if not any(key.lower() == "content-type" for key in out_headers):
            out_headers["Content-Type"] = "application/json"
    if not any(key.lower() == "user-agent" for key in out_headers):
        out_headers["User-Agent"] = USER_AGENT

    resolved_timeout = config.timeout if timeout is _DEFAULT else timeout
    return RequestSpec(
        method=method,
        url=url,
        uri=uri,
        headers=out_headers,
        content=data,
        timeout=resolved_timeout,
        tag=sent_tag,
        expect=expect,
    )


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def parse_body(headers: Mapping[str, str], content: bytes) -> Any:
    """JSON when the server says so (or the text parses as JSON), else text."""
    if not content:
        return None
    text = content.decode("utf-8", errors="replace")
    content_type = headers.get("content-type", "").lower()
    if "json" in content_type and "ndjson" not in content_type:
        try:
            return json.loads(text)
        except ValueError:
            return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_ndjson(content: bytes) -> List[Any]:
    """One JSON value per non-empty line."""
    out: List[Any] = []
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def check_response(
    spec: RequestSpec,
    status_code: int,
    reason: Optional[str],
    headers: Mapping[str, str],
    content: bytes,
) -> Any:
    """Raise the typed error for a failed response, else return the parsed body."""
    lowered = {k.lower(): v for k, v in headers.items()}
    emit_api_warnings(lowered)
    if status_code >= 400:
        body = parse_body(lowered, content)
        info = HttpResponseInfo(
            status_code=status_code,
            status_message=reason or None,
            headers=lowered,
            body=body,
            url=spec.url,
            method=spec.method,
        )
        raise error_from_response(info, spec.tag)
    if spec.expect == "ndjson":
        return parse_ndjson(content)
    if spec.expect == "raw":
        return content
    return parse_body(lowered, content)


# ---------------------------------------------------------------------------
# Per-operation builders
# ---------------------------------------------------------------------------


def fetch_request(
    config: ClientConfig,
    query: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    filter_response: bool = True,
    return_query: Optional[bool] = None,
    tag: Optional[str] = None,
    perspective: Any = None,
    result_source_map: Union[bool, str, None] = None,
    cache_mode: Optional[str] = None,
    last_live_event_id: Optional[str] = None,
    use_cdn: Optional[bool] = None,
    timeout: Any = _DEFAULT,
    headers: Optional[Mapping[str, str]] = None,
    token: Optional[str] = None,
) -> RequestSpec:
    """``client.fetch()``: GET under 11,264 encoded characters, POST above."""
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    clean_params: Dict[str, Any] = dict(params or {})
    effective_return_query = filter_response is False and return_query is not False
    qs = encode_query_string(query, clean_params)
    use_get = len(qs) < QUERY_SIZE_LIMIT
    uri = get_data_url(config, "query", qs if use_get else "")
    return build_request(
        config,
        uri,
        method="GET" if use_get else "POST",
        body=None if use_get else {"query": query, "params": clean_params},
        headers=headers,
        token=token,
        tag=tag,
        can_use_cdn=True,
        use_cdn=use_cdn,
        perspective=perspective,
        result_source_map=result_source_map,
        return_query=effective_return_query,
        last_live_event_id=last_live_event_id,
        cache_mode=cache_mode,
        timeout=timeout,
    )


def get_document_request(
    config: ClientConfig,
    document_id: str,
    *,
    tag: Optional[str] = None,
    release_id: Optional[str] = None,
    include_all_versions: Optional[bool] = None,
    timeout: Any = _DEFAULT,
) -> RequestSpec:
    doc_id = document_id
    if release_id:
        version = get_version_from_id(document_id)
        if not version:
            if is_draft_id(document_id):
                raise ValueError(
                    f"The document ID (`{document_id}`) is a draft, but `release_id` is set as `{release_id}`"
                )
            doc_id = get_version_id(document_id, release_id)
        elif version != release_id:
            raise ValueError(
                f"The document ID (`{document_id}`) is already a version of `{version}` release, but this "
                f"does not match the provided `release_id` (`{release_id}`)"
            )
    query = {"includeAllVersions": include_all_versions} if include_all_versions is not None else None
    return build_request(
        config, get_data_url(config, "doc", doc_id), query=query, tag=tag, timeout=timeout
    )


def map_get_document(body: Any, include_all_versions: Optional[bool]) -> Any:
    documents = body.get("documents") if isinstance(body, dict) else None
    if not documents:
        return [] if include_all_versions else None
    return documents if include_all_versions else documents[0]


def get_documents_request(
    config: ClientConfig, ids: Sequence[str], *, tag: Optional[str] = None, timeout: Any = _DEFAULT
) -> RequestSpec:
    if isinstance(ids, str):
        raise TypeError("get_documents() takes a list of IDs")
    return build_request(config, get_data_url(config, "doc", ",".join(ids)), tag=tag, timeout=timeout)


def map_get_documents(body: Any, ids: Sequence[str]) -> List[Optional[Dict[str, Any]]]:
    documents = body.get("documents") if isinstance(body, dict) else None
    indexed = {doc.get("_id"): doc for doc in documents or [] if isinstance(doc, dict)}
    return [indexed.get(doc_id) for doc_id in ids]


def documents_exists_requests(
    config: ClientConfig, ids: Sequence[str], *, tag: Optional[str] = None, timeout: Any = _DEFAULT
) -> List[Tuple[RequestSpec, List[str]]]:
    if isinstance(ids, str):
        raise TypeError("documents_exists() takes a list of IDs")
    batches: List[Tuple[RequestSpec, List[str]]] = []
    for start in range(0, len(ids), DOCUMENTS_EXISTS_BATCH_SIZE):
        batch = list(ids[start : start + DOCUMENTS_EXISTS_BATCH_SIZE])
        path = ",".join(encode_uri_component(doc_id) for doc_id in batch)
        spec = build_request(
            config,
            get_data_url(config, "doc", path),
            query={"excludeContent": True},
            tag=tag,
            timeout=timeout,
        )
        batches.append((spec, batch))
    return batches


def map_documents_exists(body: Any, batch: Sequence[str]) -> Set[str]:
    omitted = body.get("omitted") if isinstance(body, dict) else None
    missing = {
        entry.get("id")
        for entry in omitted or []
        if isinstance(entry, dict) and entry.get("reason") == "existence"
    }
    return {doc_id for doc_id in batch if doc_id not in missing}


def mutation_query(
    *,
    dry_run: Optional[bool] = None,
    return_documents: Optional[bool] = None,
    visibility: Optional[Visibility] = None,
    auto_generate_array_keys: Optional[bool] = None,
    skip_cross_dataset_reference_validation: Optional[bool] = None,
) -> Dict[str, Any]:
    """Port of ``getMutationQuery``. ``return_documents=False`` drops the key."""
    return {
        "dryRun": dry_run,
        "returnIds": True,
        "returnDocuments": None if return_documents is False else (True if return_documents is None else return_documents),
        "visibility": visibility or "sync",
        "autoGenerateArrayKeys": auto_generate_array_keys,
        "skipCrossDatasetReferenceValidation": skip_cross_dataset_reference_validation,
    }


MutationsInput = Union[BasePatch, BaseTransaction[Any], Mapping[str, Any], Sequence[Mapping[str, Any]]]


def serialize_mutations(mutations: MutationsInput) -> List[Any]:
    if isinstance(mutations, BasePatch):
        return [{"patch": mutations.serialize()}]
    if isinstance(mutations, BaseTransaction):
        return mutations.serialize()
    if isinstance(mutations, Mapping):
        return [dict(mutations)]
    if isinstance(mutations, (list, tuple)):
        return list(mutations)
    raise TypeError("mutate() takes a mutation, a list of mutations, a patch or a transaction")


def mutate_request(
    config: ClientConfig,
    mutations: MutationsInput,
    *,
    return_documents: Optional[bool] = None,
    visibility: Optional[Visibility] = None,
    dry_run: Optional[bool] = None,
    auto_generate_array_keys: Optional[bool] = None,
    skip_cross_dataset_reference_validation: Optional[bool] = None,
    transaction_id: Optional[str] = None,
    tag: Optional[str] = None,
    timeout: Any = _DEFAULT,
) -> RequestSpec:
    body: Dict[str, Any] = {"mutations": serialize_mutations(mutations)}
    if isinstance(mutations, BaseTransaction) and not transaction_id:
        transaction_id = mutations.transaction_id
    if transaction_id:
        body["transactionId"] = transaction_id
    return build_request(
        config,
        get_data_url(config, "mutate"),
        method="POST",
        query=mutation_query(
            dry_run=dry_run,
            return_documents=return_documents,
            visibility=visibility,
            auto_generate_array_keys=auto_generate_array_keys,
            skip_cross_dataset_reference_validation=skip_cross_dataset_reference_validation,
        ),
        body=body,
        tag=tag,
        can_use_cdn=False,
        timeout=timeout,
    )


def single_mutation(op: str, document: Any) -> Dict[str, Any]:
    if not isinstance(document, Mapping):
        raise TypeError(f"{op}() takes an object of properties")
    if op in ("createIfNotExists", "createOrReplace"):
        _require_document_id(op, document)
    return {op: dict(document)}


def delete_mutation(selection: Any) -> Dict[str, Any]:
    return {"delete": get_selection(selection)}


def action_request(
    config: ClientConfig,
    actions: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
    *,
    transaction_id: Optional[str] = None,
    skip_cross_dataset_reference_validation: Optional[bool] = None,
    dry_run: Optional[bool] = None,
    tag: Optional[str] = None,
    timeout: Any = _DEFAULT,
) -> RequestSpec:
    acts = [dict(actions)] if isinstance(actions, Mapping) else [dict(a) for a in actions]
    body: Dict[str, Any] = {"actions": acts}
    if transaction_id:
        body["transactionId"] = transaction_id
    if skip_cross_dataset_reference_validation:
        body["skipCrossDatasetReferenceValidation"] = True
    if dry_run:
        body["dryRun"] = True
    return build_request(
        config,
        get_data_url(config, "actions"),
        method="POST",
        body=body,
        tag=tag,
        can_use_cdn=False,
        timeout=timeout,
    )


def listen_url(
    config: ClientConfig,
    query: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    include_result: bool = True,
    include_previous_revision: Optional[bool] = None,
    include_mutations: Optional[bool] = None,
    include_all_versions: Optional[bool] = None,
    visibility: Optional[str] = None,
    effect_format: Optional[str] = None,
    enable_resume: Optional[bool] = None,
    tag: Optional[str] = None,
) -> str:
    """URL for ``/data/listen``. Always the live API, never the CDN."""
    full_tag = f"{config.request_tag_prefix}.{tag}" if tag and config.request_tag_prefix else tag
    options: List[Tuple[str, Any]] = [
        ("tag", full_tag),
        ("includePreviousRevision", include_previous_revision),
        ("includeResult", include_result),
        ("includeAllVersions", include_all_versions),
        ("visibility", visibility),
        ("effectFormat", effect_format),
        ("enableResume", enable_resume),
        ("includeMutations", include_mutations),
    ]
    qs = encode_query_string(query, dict(params or {}), options)
    url = f"{config.url}{get_data_url(config, 'listen', qs)}"
    if len(url) > LISTEN_MAX_URL_LENGTH:
        raise ValueError("Query too large for listener")
    return url


def listen_headers(config: ClientConfig, token: Optional[str] = None) -> Dict[str, str]:
    headers: Dict[str, str] = dict(config.headers)
    bearer = token or config.token
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    headers["Accept"] = "text/event-stream"
    if not any(key.lower() == "user-agent" for key in headers):
        headers["User-Agent"] = USER_AGENT
    return headers


AssetBody = Union[bytes, bytearray, memoryview]


def upload_request(
    config: ClientConfig,
    asset_type: str,
    content: bytes,
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
) -> RequestSpec:
    if asset_type not in ("image", "file"):
        raise ValueError(f"Invalid asset type: {asset_type}. Must be one of image, file")
    meta: Optional[List[str]] = None
    if extract is not None:
        meta = list(extract) or ["none"]
    query: Dict[str, Any] = {
        "label": label,
        "title": title,
        "description": description,
        "filename": filename,
        "meta": meta,
        "creditLine": credit_line,
    }
    if source:
        query["sourceId"] = source.get("id")
        query["sourceName"] = source.get("name")
        query["sourceUrl"] = source.get("url")
    dataset = config.require_dataset()
    folder = "images" if asset_type == "image" else "files"
    return build_request(
        config,
        f"assets/{folder}/{dataset}",
        method="POST",
        query=query,
        content=bytes(content),
        headers={"Content-Type": content_type} if content_type else None,
        tag=tag,
        timeout=timeout,
    )


def projects_list_request(
    config: ClientConfig,
    *,
    include_members: Optional[bool] = None,
    include_features: Optional[bool] = None,
    organization_id: Optional[str] = None,
    only_explicit_membership: Optional[bool] = None,
) -> RequestSpec:
    query: Dict[str, str] = {}
    if include_members is False:
        query["includeMembers"] = "false"
    if include_features is False:
        query["includeFeatures"] = "false"
    if organization_id:
        query["organizationId"] = organization_id
    if only_explicit_membership:
        query["onlyExplicitMembership"] = "true"
    return build_request(config, "/projects", query=query)


def dataset_request(
    config: ClientConfig,
    method: str,
    name: Optional[str] = None,
    body: Optional[Mapping[str, Any]] = None,
) -> RequestSpec:
    if name is None:
        uri = f"/projects/{config.project_id}/datasets" if not config.use_project_hostname else "/datasets"
    else:
        validate_dataset(name)
        uri = f"/datasets/{name}"
    return build_request(
        config, uri, method=method, body=dict(body) if body else None, no_tag=True
    )


def history_document_request(
    config: ClientConfig,
    document_id: str,
    *,
    revision: Optional[str] = None,
    time: Optional[str] = None,
    last_revision: Optional[bool] = None,
    tag: Optional[str] = None,
) -> RequestSpec:
    if revision and time:
        raise ValueError("pass either `revision` or `time`, not both")
    dataset = config.require_dataset()
    query = {"revision": revision, "time": time, "lastRevision": last_revision}
    return build_request(
        config,
        f"/data/history/{dataset}/documents/{encode_uri_component(document_id)}",
        query=query,
        tag=tag,
        can_use_cdn=False,
    )


def history_transactions_request(
    config: ClientConfig,
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
) -> RequestSpec:
    dataset = config.require_dataset()
    uri = f"/data/history/{dataset}/transactions"
    if document_ids:
        if isinstance(document_ids, str):
            document_ids = [document_ids]
        uri += "/" + ",".join(encode_uri_component(i) for i in document_ids)
    query: Dict[str, Any] = {
        # The endpoint answers 403 unless excludeContent=true is sent.
        "excludeContent": True,
        "fromTime": from_time,
        "toTime": to_time,
        "fromTransaction": from_transaction,
        "toTransaction": to_transaction,
        "authors": ",".join(authors) if authors else None,
        "reverse": reverse,
        "limit": limit,
        "effectFormat": effect_format,
        "includeIdentifiedDocumentsOnly": include_identified_documents_only,
    }
    return build_request(config, uri, query=query, tag=tag, can_use_cdn=False, expect="ndjson")


def raw_request(
    config: ClientConfig,
    uri: str,
    *,
    method: str = "GET",
    query: Optional[Mapping[str, Any]] = None,
    body: Any = None,
    headers: Optional[Mapping[str, str]] = None,
    tag: Optional[str] = None,
    use_cdn: Optional[bool] = None,
    timeout: Any = _DEFAULT,
    token: Optional[str] = None,
) -> RequestSpec:
    if isinstance(body, (bytes, bytearray, memoryview)):
        return build_request(
            config,
            uri,
            method=method,
            query=query,
            content=bytes(body),
            headers=headers,
            tag=tag,
            use_cdn=use_cdn,
            timeout=timeout,
            token=token,
        )
    return build_request(
        config,
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


def mutation_result(body: Any) -> MutationResult:
    return MutationResult.from_response(body)


Sleep = Callable[[float], Any]
