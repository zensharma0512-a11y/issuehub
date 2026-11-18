# IssueHub

[![CI](https://github.com/zensharma0512-a11y/issuehub/actions/workflows/ci.yml/badge.svg)](https://github.com/zensharma0512-a11y/issuehub/actions/workflows/ci.yml)

A multi-project issue tracker service that stays correct when clients retry requests and when teammates edit the same issue at the same time.

It is a Python service with a small HTTP API, backed by MongoDB, with an in-memory store for fast local runs. The point of the project is the correctness work, not the CRUD: idempotent creates, optimistic locking, keyset pagination, per-project isolation and tests that hammer each of those with concurrent threads. In CI, every test runs against both stores, including a real MongoDB 7.

## Features

- **Projects with roles.** `viewer` can read, `member` can create, comment and move issues, `admin` manages members and can reopen closed issues.
- **Idempotent issue creation.** Send an `Idempotency-Key` header and retries return the original issue instead of creating duplicates.
- **Optimistic locking.** Every issue has a `version`. Status changes must send the version they read. Stale writes get `409 Conflict`.
- **Status workflow.** `open → in_progress → resolved → closed`, with only valid moves allowed.
- **Cursor pagination.** Stable pages that never skip or repeat items, even while new issues are being created.
- **Comment threads.** Comments on issues with one level of replies. Comments and replies have their own ID sequences and keep creation order.
- **Two stores, one contract.** `MongoStore` for real deployments and `MemoryStore` for tests and demos. The service doesn't know which one it's using.
- **Framework-agnostic HTTP adapter.** Easy to unit-test, plus a Flask wrapper you can run.

## Design decisions

### Idempotency keys are scoped to (project, user)
A key only means something for the user who sent it. If keys were global, two users who happened to pick the same key would collide, and one could get back the other's issue. The service stores a SHA-256 fingerprint of the request body next to each key:

| Retry | Result |
| --- | --- |
| Same key, same body | `200`, original issue, `Idempotent-Replayed: true` |
| Same key, different body | `422 idempotency_key_reused` |
| Request fails validation | Nothing is stored, so the key can be used again |

Thirty-two concurrent requests with the same key produce exactly one issue (`test_concurrent_retries_create_exactly_one`). In MongoDB, the key and fingerprint are stored on the issue itself, with a unique partial index on `(project_id, reporter, idempotency_key)`. When two retries race, the database lets one insert win and rejects the other with a duplicate-key error, and the loser replays the winner's issue. No "check, then insert" race window, and no transaction needed.

### Optimistic locking instead of pessimistic locks
Clients read an issue, decide, then send `expected_version`. The version check and the increment happen in one atomic step: in MongoDB, a `find_one_and_update` whose filter holds the issue number, the expected version *and* the status that was read, with `$inc: {version: 1}` in the same update. So of N concurrent writers holding version `v`, exactly one wins and the rest get `VersionConflict` with the current version (`test_concurrent_updates_on_same_version_succeed_once`, 50 threads). This keeps no lock open between a client's read and its write, which matters over HTTP where a client can disappear mid-edit.

### Atomicity without multi-document transactions
Every guarantee rests on a single-document atomic operation or a unique index, so the MongoDB store works on a standalone server as well as a replica set:

| Need | MongoDB mechanism |
| --- | --- |
| Unique, increasing issue/comment/reply numbers | `$inc` on a per-project counter document |
| One issue per idempotency key | Unique partial index on `(project_id, reporter, idempotency_key)` |
| Exactly one winner per version | Filter on `number + version + status`, `$inc` version in the same update |
| Two-level threads | Replies embedded in their comment, appended with `$push` |
| No duplicate members | `$push` guarded by `members.user: {$ne: user}` in the filter |

The in-memory store gives the same guarantees with one lock per project, so traffic on one project never waits on another.

**Trade-off:** in MongoDB, a number taken by a request that then loses an idempotency race isn't reused, so issue numbers can have gaps. A related edge: two issues can get numbers 4 and 5 but commit in the order 5, 4, so a client paging at exactly that moment could see 5 before 4 exists. Fixing that needs a transaction or a "committed up to" watermark. Both are noted under next steps.

### Keyset pagination with opaque, bound cursors
Issues are never deleted and IDs only increase, so "issues with `id > last_seen`" is a stable query. Each page starts with a binary search, so it costs O(log n + page size), not O(offset). The cursor is base64-encoded JSON holding the project and status filter it was issued for. Reusing it on a different project or filter returns `400`, not a silently wrong page.

### Atomic failures and isolated results
Every call validates all of its input before touching any state. That's why a failed create never uses up a number or stores a key. Callers only receive freshly built dicts, so changing a returned object can't corrupt the store (see `TestSnapshots`).

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

Errors use one shape: `{"error": "<code>", "message": "...", "details": {...}}`. The codes are `400` validation, `401` auth, `403` role, `404` not found, `405` wrong method, `409` version or transition conflict, `422` idempotency key reuse, `503` database unavailable (with `Retry-After`), `500` anything else, with no internals leaked.

## Running it

```bash
pip install -e ".[dev]"
pytest                                        # 68 tests, in-memory store

docker compose up -d                          # local MongoDB 7
MONGO_URL=mongodb://localhost:27017 pytest    # every test against both stores

export MONGO_URL=mongodb://localhost:27017
ISSUEHUB_TOKENS="alice-token:alice,bob-token:bob" python -m issuehub.app
curl -X POST localhost:8000/projects -H "Authorization: Bearer alice-token" \
     -H "Content-Type: application/json" -d '{"id": "web", "name": "Website"}'
curl -X POST localhost:8000/projects/web/issues -H "Authorization: Bearer alice-token" \
     -H "Idempotency-Key: 7f3c" -H "Content-Type: application/json" -d '{"title": "Login button broken"}'
```

## What would change in production

- **Gap-free, commit-ordered numbers.** Use a MongoDB transaction (on a replica set) around "allocate number + insert", or page by a "committed up to" watermark.
- **Write and read concern.** Use `majority` so an acknowledged write survives a primary failover.
- **Key expiry.** Keep idempotency keys for a window (for example 24 hours) instead of forever.
- **Auth.** Replace the static token map with signed JWTs or sessions.

## Layout

```
issuehub/
  service.py     validation, roles, transitions, cursors (storage-agnostic)
  store.py       Store contract + MemoryStore
  mongo.py       MongoStore: indexes, atomic counters, version-checked updates
  validation.py  strict input validation
  errors.py      error types with codes and HTTP statuses
  http.py        framework-agnostic HTTP adapter
  app.py         Flask server
tests/
  conftest.py      runs every test against each available store
  test_service.py  behaviour, concurrency and snapshot tests
  test_http.py     HTTP status codes, headers, Flask end-to-end
```
