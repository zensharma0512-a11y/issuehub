# IssueHub

[![CI](https://github.com/zensharma0512-a11y/issuehub/actions/workflows/ci.yml/badge.svg)](https://github.com/zensharma0512-a11y/issuehub/actions/workflows/ci.yml)

A multi-project issue tracker service that stays correct when clients retry requests and when teammates edit the same issue at the same time.

It is an in-memory Python service with a small HTTP API. The point of the project is the correctness work, not the CRUD: idempotent creates, optimistic locking, keyset pagination, per-project isolation and tests that hammer each of those with concurrent threads.

## Features

- **Projects with roles.** `viewer` can read, `member` can create, comment and move issues, `admin` manages members and can reopen closed issues.
- **Idempotent issue creation.** Send an `Idempotency-Key` header and retries return the original issue instead of creating duplicates.
- **Optimistic locking.** Every issue has a `version`. Status changes must send the version they read. Stale writes get `409 Conflict`.
- **Status workflow.** `open → in_progress → resolved → closed`, with only valid moves allowed.
- **Cursor pagination.** Stable pages that never skip or repeat items, even while new issues are being created.
- **Comment threads.** Comments on issues with one level of replies. Comments and replies have their own ID sequences and keep creation order.
- **Framework-agnostic HTTP adapter.** Easy to unit-test, plus a Flask wrapper you can run.

## Design decisions

### Idempotency keys are scoped to (project, user)
A key only means something for the user who sent it. If keys were global, two users who happened to pick the same key would collide, and one could get back the other's issue. The service stores a SHA-256 fingerprint of the request body next to each key:

| Retry | Result |
| --- | --- |
| Same key, same body | `200`, original issue, `Idempotent-Replayed: true` |
| Same key, different body | `422 idempotency_key_reused` |
| Request fails validation | Nothing is stored, so the key can be used again |

Thirty-two concurrent requests with the same key produce exactly one issue (`test_concurrent_retries_create_exactly_one`).

### Optimistic locking instead of pessimistic locks
Clients read an issue, decide, then send `expected_version`. The version check and the increment happen together under the project lock, so of N concurrent writers holding version `v`, exactly one wins and the rest get `VersionConflict` with the current version (`test_concurrent_updates_on_same_version_succeed_once`, 50 threads). This keeps no lock open between a client's read and its write, which matters over HTTP where a client can disappear mid-edit.

### One lock per project
Each project has its own lock. Traffic on one project never waits on another. A small registry lock only guards creating and looking up projects.

### Keyset pagination with opaque, bound cursors
Issues are never deleted and IDs only increase, so "issues with `id > last_seen`" is a stable query. Each page starts with a binary search, so it costs O(log n + page size), not O(offset). The cursor is base64-encoded JSON holding the project and status filter it was issued for. Reusing it on a different project or filter returns `400`, not a silently wrong page.

### Atomic failures and isolated results
Every call validates all of its input before touching any state. That's why a failed create never leaves behind an ID gap or a stored key. Callers only receive freshly built dicts, so changing a returned object can't corrupt the store (see `TestSnapshots`).

### No leaking project existence
For a non-member, a project that exists and one that doesn't both return `404`. A `403` would confirm the project exists.

### Strict types
Python treats `True` as an `int`. So `expected_version=True` or `limit=True` are rejected explicitly rather than read as `1`.

## API

All requests need `Authorization: Bearer <token>`.

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/projects` | `{"id", "name"}`. The creator becomes admin |
| `POST` | `/projects/{pid}/members` | `{"user_id", "role"}`. Admin only |
| `POST` | `/projects/{pid}/issues` | `{"title", "description?", "priority?"}`, optional `Idempotency-Key` |
| `GET` | `/projects/{pid}/issues?limit=&cursor=&status=` | Returns `{"items", "next_cursor"}` |
| `GET` | `/projects/{pid}/issues/{id}` | Returns an `ETag` holding the version |
| `POST` | `/projects/{pid}/issues/{id}/transitions` | `{"to", "expected_version"}` |
| `POST` | `/projects/{pid}/issues/{id}/comments` | `{"body"}` |
| `POST` | `/projects/{pid}/issues/{id}/comments/{cid}/replies` | `{"body"}` |
| `GET` | `/projects/{pid}/issues/{id}/comments` | Full two-level thread |

Errors use one shape: `{"error": "<code>", "message": "...", "details": {...}}`. The codes are `400` validation, `401` auth, `403` role, `404` not found, `405` wrong method, `409` version or transition conflict, `422` idempotency key reuse.

## Running it

```bash
pip install -e ".[dev]"
pytest                      # 67 tests

ISSUEHUB_TOKENS="alice-token:alice,bob-token:bob" python -m issuehub.app
curl -X POST localhost:8000/projects -H "Authorization: Bearer alice-token" \
     -H "Content-Type: application/json" -d '{"id": "web", "name": "Website"}'
curl -X POST localhost:8000/projects/web/issues -H "Authorization: Bearer alice-token" \
     -H "Idempotency-Key: 7f3c" -H "Content-Type: application/json" -d '{"title": "Login button broken"}'
```

## What would change in production

- **Storage.** Move to Postgres with `UPDATE ... WHERE id = $1 AND version = $2` for locking, a unique index on `(project_id, user_id, idempotency_key)` for idempotency, and keyset queries on `(project_id, id)`.
- **Key expiry.** Keep idempotency keys for a window (for example 24 hours) instead of forever.
- **Auth.** Replace the static token map with signed JWTs or sessions.
- **Concurrent first requests.** Two requests with the same key arriving at the same moment would need a "request in progress" record so the second one waits or gets a `409`. Here the project lock handles that.

## Layout

```
issuehub/
  service.py     core logic: projects, issues, versions, pagination, comments
  validation.py  strict input validation
  errors.py      error types with codes and HTTP statuses
  http.py        framework-agnostic HTTP adapter
  app.py         Flask server
tests/
  test_service.py  behaviour, concurrency and snapshot tests
  test_http.py     HTTP status codes, headers, Flask end-to-end
```
