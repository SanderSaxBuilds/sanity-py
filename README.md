# sanity-py

A typed Python client for the [Sanity Content Lake HTTP API](https://www.sanity.io/docs/http-reference). It has the same surface as [`@sanity/client`](https://github.com/sanity-io/client) in snake case, with a sync client and an async client built on httpx.

- GROQ queries with `$params`, perspectives, the API CDN and result source maps.
- Documents by ID, existence checks, mutations, patch and transaction builders, and document actions.
- `listen()` as a plain iterator over server-sent events, with reconnects and `Last-Event-ID` resume.
- Image and file uploads, datasets, projects, users and the History API.
- Typed errors for every status, GROQ parse errors with a code frame, and retries with backoff.
- Passes `mypy --strict`.

Every request this client builds is checked byte for byte against the requests `@sanity/client` 8.16.0 sends for the same call: method, URL, query string order, headers and body. See [Tests](#tests).

Python 3.10 or newer. The only dependencies are `httpx` and `typing_extensions`. The distribution is called `sanity-client-py` and the import is `sanity_client`.

## Install

From a checkout of this repository:

```bash
pip install .
```

For development, with the test and type-check tools:

```bash
pip install -e ".[dev]"
```

## Quick start

```python
from sanity_client import SanityClient

client = SanityClient(
    project_id="zp7mbokg",
    dataset="production",
    api_version="2025-02-19",
    use_cdn=True,
)

movies = client.fetch(
    '*[_type == "movie" && releaseDate >= $from] | order(releaseDate asc)[0...$n]{_id, title}',
    {"from": "1980-01-01", "n": 5},
)
for movie in movies:
    print(movie["_id"], movie["title"])
```

`zp7mbokg/production` is Sanity's public movies demo, so this runs as is without a token.

The options follow the JS client config:

```python
client = SanityClient(
    project_id="your-project",
    dataset="production",
    api_version="2025-02-19",      # a date string, "1", "X" or a datetime.date
    token="sk...",                 # needed for writes and private datasets
    use_cdn=False,                 # True sends queries to apicdn.sanity.io
    perspective="published",       # "drafts", "raw" or a release stack like ["rSale", "drafts"]
    request_tag_prefix="website",  # tags become website.<tag>
    timeout=300,                   # seconds, None for no timeout
    headers={"X-Team": "web"},
    retry=None,                    # a RetryPolicy, see Retries
    http_client=None,              # bring your own httpx.Client, it is not closed for you
)
```

`create_client(**config)` does the same as `SanityClient(**config)`. `client.with_config(dataset="staging")` returns a new client with some settings changed, sharing the same connection pool. Clients work as context managers and close their pool on exit.

## Queries

```python
client.fetch("count(*[_type == 'movie'])")                      # the result only
client.fetch("*[_id == $id][0]", {"id": "movie_10681"})          # $params are JSON encoded
client.fetch("*[0]", filter_response=False)                      # result, ms, query, syncTags
client.fetch("*[_type == 'post']", perspective="drafts", tag="blog-index")
```

Queries are sent as GET while the encoded query string stays under 11,264 characters, and as POST above that, the same cut-off the JS client uses. Parameters are encoded the way `URLSearchParams` and `JSON.stringify` encode them in the browser, so `1.0` is sent as `1` and a space as `+`.

The `drafts` perspective and perspective stacks are not served by the API CDN. When `use_cdn=True` and one of them is set, the request goes to the live API and a `UserWarning` says so. `previewDrafts` still works and raises a `SanityDeprecationWarning`.

## Documents

```python
client.get_document("movie_10681")                       # dict, or None
client.get_document("movie_10681", release_id="rSale")   # the version in a release
client.get_document("movie_10681", include_all_versions=True)  # published, draft and versions
client.get_documents(["a", "b", "missing"])              # [doc, doc, None] in the order asked
client.documents_exists(["a", "b", "missing"])           # {"a", "b"}, no content fetched
```

`documents_exists()` asks for 100 IDs per request.

## Mutations

```python
result = client.create({"_type": "movie", "title": "Alien"})
print(result.transaction_id, result.document_id, result.document)

client.create_if_not_exists({"_id": "settings", "_type": "siteSettings"})
client.create_or_replace({"_id": "settings", "_type": "siteSettings", "title": "Home"})
client.delete("movie_10681")
client.delete({"query": "*[_type == 'movie' && year < $y]", "params": {"y": 1950}})
```

Every write returns a `MutationResult` with `transaction_id`, `results`, `document_ids`, `documents` and `raw`. They take the options of `mutate()`: `return_documents`, `visibility` (`"sync"`, `"async"` or `"deferred"`), `dry_run`, `auto_generate_array_keys`, `skip_cross_dataset_reference_validation`, `transaction_id` and `tag`.

### Patches

```python
(
    client.patch("movie_10681")
    .set({"title": "WALL·E", "rating.imdb": 8.4})
    .set_if_missing({"tags": []})
    .unset(["draftNote"])
    .inc({"views": 1})
    .append("tags", ["sci-fi"])
    .if_revision_id("rev-abc")
    .commit()
)

client.patch(["a", "b"]).set({"archived": True}).commit()
client.patch({"query": "*[_type == 'movie' && !defined(slug)]"}).set({"needsSlug": True}).commit()
```

The other operations are `dec`, `insert(position, selector, items)`, `prepend`, `splice(selector, start, delete_count, items)` and `diff_match_patch`. A patch can be built without a client and passed to `mutate()` or a transaction.

### Transactions

```python
(
    client.transaction()
    .create({"_type": "person", "name": "Ripley"})
    .create_or_replace({"_id": "settings", "_type": "siteSettings"})
    .patch("movie_10681", lambda p: p.set({"featured": True}))
    .delete("old-draft")
    .commit(visibility="async")
)
```

A transaction's `commit()` defaults to `return_documents=False`, like the JS client. `transaction.serialize()` gives the mutation list, and `client.mutate(transaction)` sends it in one request with the transaction's ID.

### Actions

```python
client.action({
    "actionType": "sanity.action.document.publish",
    "draftId": "drafts.movie_10681",
    "publishedId": "movie_10681",
})
```

## Listening for changes

```python
with client.listen("*[_type == 'movie']", events=["welcome", "mutation"]) as events:
    for event in events:
        if event.type == "welcome":
            print("listening")
        else:
            print(event.transition, event.document_id, event.result)
```

`listen()` returns an iterator of `ListenEvent`. Each one has `type`, `data` and `id`, with shortcuts such as `document_id`, `transition`, `result`, `previous` and `transaction_id`. By default only `mutation` events are yielded. Add `welcome`, `reconnect`, `welcomeback`, `reset` or `open` to `events` to see them too.

When the stream breaks, or the server answers 5xx, 408 or 429, the listener waits, reconnects and sends the last event ID it saw in `Last-Event-ID`. Pass `enable_resume=True` to ask the server to resume from it. Other 4xx answers raise `ConnectionFailedError` with the status and body. A `channelError` event raises `ChannelError`, a `disconnect` event raises `DisconnectError`, and `max_reconnects=` caps the attempts. The listener always talks to the live API, never the CDN.

## Assets

```python
from pathlib import Path

image = client.assets.upload("image", Path("cover.jpg"), extract=["palette", "lqip"])
print(image["_id"], image["url"])

with open("report.pdf", "rb") as handle:
    client.assets.upload("file", handle, filename="report.pdf", title="Q3 report")
```

`upload()` takes bytes, a binary file object or a `pathlib.Path`. For a path the file name and content type are taken from the file. Uploads have no timeout unless you pass one.

## Projects, datasets, users and history

```python
client.projects.list(include_members=False)
client.datasets.list()
client.datasets.create("staging", acl_mode="private")
client.users.get_by_id("me")

client.history.get_document("movie_10681", time="2026-01-01T00:00:00Z")
for trx in client.history.transactions(["movie_10681"], limit=20, reverse=True):
    print(trx["id"])
```

`client.request("/any/endpoint", method="POST", body={...})` calls anything else with the same auth, tags, retries and errors.

## Async

`AsyncSanityClient` has the same methods. The ones that make requests are coroutines, `patch().commit()` and `transaction().commit()` are awaited, and `listen()` returns an async iterator.

```python
import asyncio
from sanity_client import AsyncSanityClient

async def main() -> None:
    async with AsyncSanityClient("zp7mbokg", "production", api_version="2025-02-19") as client:
        count = await client.fetch("count(*[_type == 'movie'])")
        async with client.listen("*[_type == 'movie']", events=["welcome"]) as events:
            async for event in events:
                print(count, event.type)
                break

asyncio.run(main())
```

## Errors

Every exception derives from `SanityError`.

| Exception | When |
| --- | --- |
| `QueryParseError` | the GROQ query does not parse, see `.start`, `.end` and `.description` |
| `MutationError` | a mutation or action failed, see `.items` |
| `AuthenticationError` | 401 |
| `PermissionDeniedError` | 403 |
| `NotFoundError` | 404 |
| `ConflictError` | 409 |
| `RateLimitError` | 429, after retries ran out |
| `ClientError` | any other 4xx |
| `ServerError` | 5xx |
| `HttpError` | the base class of all of the above |
| `RequestTimeoutError` | the request timed out |
| `NetworkError` | the server could not be reached |
| `ListenError` | the base of the listener errors |

HTTP errors carry `status_code`, `response`, `response_body`, `details` and `trace_id`. A query parse error prints the query with a pointer to the problem:

```
GROQ query parse error:
> 1 | *[_type == "movie" && ]
    |                       ^ unexpected token "]", expected expression
(traceId: 92057dc93cebb205e715a91f99657225)
```

## Retries

The defaults follow the JS client: up to 5 retries for 429, 502 and 503 on GET, HEAD and queries, and for network errors on GET and HEAD. Mutations and actions are not retried. The wait doubles from 100 ms with jitter, up to 10 seconds. One addition: a `Retry-After` header is honored, and a wait longer than `max_retry_after` (60 seconds) raises at once.

```python
from sanity_client import RetryPolicy, SanityClient

SanityClient(..., retry=RetryPolicy(max_retries=0))           # no retries
SanityClient(..., retry=RetryPolicy(retry_writes=True))       # retry mutations too
```

## Differences from @sanity/client

The goal is the same requests, so behavior only differs where the JS client loses data or the result is plainly off:

- Two inserts in one patch (`append` then `prepend`, for example) raise `ValueError`. The JS client merges them into one `insert` object and the first list of items is lost.
- The query parse code frame puts the caret under the right character. The JS one points one column to the left.
- `client.mutate(transaction)` sends the transaction's own ID.
- Writes always return a `MutationResult` instead of a shape that depends on the options.
- `history` is a resource here, and `history.transactions()` always sends `excludeContent=true`, which the endpoint needs.

## Notes on the HTTP API and the JS client

Things found while matching this client to the API docs and to `@sanity/client`:

- The mutate docs name the flag `skipCrossDatasetReferencesValidation`, while the client sends `skipCrossDatasetReferenceValidation`. This package sends what the client sends.
- The docs list `transactionId` as a query parameter for mutations, the client sends it in the body.
- History transactions answer 403 without `excludeContent=true`. `documentIds` is marked required in the docs and described as optional.
- The docs give the GET limit as 11 KB. The client cuts over at 11,264 characters of query string.
- `getDocument` puts the ID in the path as is, `documentsExists` URL-encodes each ID.
- The listener sends no User-Agent header.

## Tests

```bash
pip install -e ".[dev]"
pytest                  # unit tests, no network
pytest -m live          # read-only checks against the public zp7mbokg/production dataset
mypy
```

`tests/fixtures/js_requests.json` holds the requests `@sanity/client` sends for 88 calls, recorded with its `fetch` replaced by a recorder. `tests/test_js_parity.py` makes the same calls with the sync and the async client and compares method, URL, headers and body. To record them again, for example after a JS client release:

```bash
cd tools/js-capture
npm install
node capture.mjs ../../tests/fixtures/js_requests.json
```

## License

MIT, see [LICENSE](LICENSE).
