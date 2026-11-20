# Changelog

## 0.2.0 (2026-11-21)

- Added `client.functions.invoke()` to both Python clients, with background and synchronous invocation, and project or organization scope.
- Added durable callback completion and `stop_execution()`, which returns the execution ID and stop timestamp.
- Re-recorded the request fixtures with `@sanity/client` 8.17.0, including 11 Functions cases.

## 0.1.0

First release.

- Sync `SanityClient` and async `AsyncSanityClient` on httpx, configured like `@sanity/client`.
- GROQ `fetch()` with JSON encoded `$params`, GET or POST by query size, perspectives, API CDN, result source maps, `cacheMode` and `lastLiveEventId`.
- `get_document()`, `get_documents()` and `documents_exists()`, with release versions.
- `mutate()`, `create()`, `create_if_not_exists()`, `create_or_replace()`, `delete()`, patch and transaction builders, and document actions.
- `listen()` over server-sent events with reconnects, `Last-Event-ID` resume and typed listener errors.
- Image and file uploads, projects, datasets, users and the History API.
- Typed errors per status, GROQ parse errors with a code frame, retries with backoff and `Retry-After`.
- Request tests against 88 calls recorded from `@sanity/client` 8.16.0, live read-only tests, and `mypy --strict`.
