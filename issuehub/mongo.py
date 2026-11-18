"""MongoDB store.

Collections
-----------
``projects``  ``{_id: project_id, name, members: [{user, role}]}``
``issues``    one document per issue, keyed by ``(project_id, number)``
``comments``  one document per comment, with its replies embedded
``counters``  ``{_id: "<project>:<kind>", seq}``, one per project and kind

How each guarantee is kept without multi-document transactions
---------------------------------------------------------------
* **Unique, increasing numbers.** ``find_one_and_update`` with ``$inc`` on a
  counter document is atomic, so concurrent callers always get different numbers.
* **Idempotent creates.** The key and request fingerprint are stored *on the
  issue*, with a unique partial index on ``(project_id, reporter,
  idempotency_key)``. If two retries race, MongoDB lets one insert win and
  rejects the other with a duplicate-key error. The loser then reads the
  winner's issue and replays it.
* **Optimistic locking.** The status change filters on ``number``, ``version``
  *and* ``status`` and increments ``version`` in the same update. Of N
  concurrent writers holding the same version, exactly one matches.
* **Two-level threads.** Replies are embedded in their comment and appended
  with ``$push``, which is atomic per document.

One trade-off: a number taken by a request that then loses an idempotency race
is not reused, so issue numbers can have gaps. The in-memory store has none.
"""

from __future__ import annotations

import logging

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import (
    AutoReconnect,
    ConnectionFailure,
    DuplicateKeyError,
    ExecutionTimeout,
    NetworkTimeout,
    PyMongoError,
    ServerSelectionTimeoutError,
    WriteConcernError,
)

from .errors import AlreadyExists, IdempotencyConflict, StorageError, StorageUnavailable

log = logging.getLogger(__name__)

_UNAVAILABLE = (
    ServerSelectionTimeoutError,
    ConnectionFailure,
    AutoReconnect,
    NetworkTimeout,
    ExecutionTimeout,
    WriteConcernError,
)
_COUNTERS = ("issue", "comment", "reply")
_ISSUE_FIELDS = ("title", "description", "priority", "status", "reporter", "version",
                 "created_at", "updated_at")


def connect(url: str, db_name: str = "issuehub", timeout_ms: int = 3000) -> "MongoStore":
    # tz_aware=True: datetimes come back as aware UTC values, not naive ones.
    client = MongoClient(url, tz_aware=True, serverSelectionTimeoutMS=timeout_ms)
    store = MongoStore(client[db_name])
    store.ensure_indexes()
    return store


class MongoStore:
    def __init__(self, db):
        self._projects = db["projects"]
        self._issues = db["issues"]
        self._comments = db["comments"]
        self._counters = db["counters"]

    def ensure_indexes(self) -> None:
        with _mapped_errors():
            self._issues.create_index(
                [("project_id", ASCENDING), ("number", ASCENDING)], unique=True, name="project_number"
            )
            self._issues.create_index(
                [("project_id", ASCENDING), ("status", ASCENDING), ("number", ASCENDING)],
                name="project_status_number",
            )
            self._issues.create_index(
                [("project_id", ASCENDING), ("reporter", ASCENDING), ("idempotency_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"idempotency_key": {"$type": "string"}},
                name="idempotency_key_unique",
            )
            self._comments.create_index(
                [("project_id", ASCENDING), ("number", ASCENDING)], unique=True, name="project_comment"
            )
            self._comments.create_index(
                [("project_id", ASCENDING), ("issue_number", ASCENDING), ("number", ASCENDING)],
                name="issue_thread",
            )

    # ------------------------------------------------------------------ projects

    def create_project(self, project_id, name, owner_id):
        with _mapped_errors():
            try:
                self._projects.insert_one(
                    {"_id": project_id, "name": name, "members": [{"user": owner_id, "role": "admin"}]}
                )
            except DuplicateKeyError:
                raise AlreadyExists("project already exists", project_id=project_id) from None
            # Counters exist before anything uses them, so increments never upsert.
            for kind in _COUNTERS:
                self._counters.update_one(
                    {"_id": f"{project_id}:{kind}"}, {"$setOnInsert": {"seq": 0}}, upsert=True
                )

    def get_project(self, project_id):
        with _mapped_errors():
            doc = self._projects.find_one({"_id": project_id})
        if doc is None:
            return None
        return {
            "id": doc["_id"],
            "name": doc["name"],
            "members": {m["user"]: m["role"] for m in doc["members"]},
        }

    def set_member(self, project_id, user_id, role):
        with _mapped_errors():
            result = self._projects.update_one(
                {"_id": project_id, "members.user": user_id}, {"$set": {"members.$.role": role}}
            )
            if result.matched_count == 0:
                # The $ne filter makes the push a no-op if a concurrent call
                # already added this user, so a member is never listed twice.
                self._projects.update_one(
                    {"_id": project_id, "members.user": {"$ne": user_id}},
                    {"$push": {"members": {"user": user_id, "role": role}}},
                )

    # -------------------------------------------------------------------- issues

    def create_issue(self, project_id, reporter, fields, idempotency_key, fingerprint, now):
        with _mapped_errors():
            if idempotency_key is not None:
                existing = self._find_by_key(project_id, reporter, idempotency_key)
                if existing is not None:
                    return self._replay(existing, fingerprint), False
            doc = {
                "project_id": project_id,
                "number": self._next(project_id, "issue"),
                **fields,
                "status": "open",
                "reporter": reporter,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
            if idempotency_key is not None:
                doc["idempotency_key"] = idempotency_key
                doc["fingerprint"] = fingerprint
            try:
                self._issues.insert_one(doc)
            except DuplicateKeyError:
                if idempotency_key is None:
                    raise
                # Lost a race with a concurrent retry: replay the winner.
                winner = self._find_by_key(project_id, reporter, idempotency_key)
                if winner is None:
                    raise
                return self._replay(winner, fingerprint), False
            return _issue_out(doc), True

    def get_issue(self, project_id, issue_id):
        with _mapped_errors():
            doc = self._issues.find_one({"project_id": project_id, "number": issue_id})
        return _issue_out(doc) if doc is not None else None

    def update_status(self, project_id, issue_id, expected_version, from_status, to_status, now):
        with _mapped_errors():
            doc = self._issues.find_one_and_update(
                {
                    "project_id": project_id,
                    "number": issue_id,
                    "version": expected_version,
                    "status": from_status,
                },
                {"$set": {"status": to_status, "updated_at": now}, "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER,
            )
        return _issue_out(doc) if doc is not None else None

    def list_issues(self, project_id, after_id, status, limit):
        query: dict = {"project_id": project_id, "number": {"$gt": after_id}}
        if status is not None:
            query["status"] = status
        with _mapped_errors():
            docs = list(self._issues.find(query).sort("number", ASCENDING).limit(limit))
        return [_issue_out(d) for d in docs]

    # ------------------------------------------------------------------ comments

    def add_comment(self, project_id, issue_id, author, body, now):
        with _mapped_errors():
            doc = {
                "project_id": project_id,
                "issue_number": issue_id,
                "number": self._next(project_id, "comment"),
                "author": author,
                "body": body,
                "created_at": now,
                "replies": [],
            }
            self._comments.insert_one(doc)
        return _comment_out(doc)

    def add_reply(self, project_id, issue_id, comment_id, author, body, now):
        where = {"project_id": project_id, "issue_number": issue_id, "number": comment_id}
        with _mapped_errors():
            if self._comments.count_documents(where, limit=1) == 0:
                return None
            reply = {
                "number": self._next(project_id, "reply"),
                "author": author,
                "body": body,
                "created_at": now,
            }
            self._comments.update_one(where, {"$push": {"replies": reply}})
        return _reply_out(reply)

    def get_thread(self, project_id, issue_id):
        with _mapped_errors():
            docs = list(
                self._comments.find({"project_id": project_id, "issue_number": issue_id}).sort(
                    "number", ASCENDING
                )
            )
        return [_comment_out(d) for d in docs]

    # ------------------------------------------------------------------- helpers

    def _next(self, project_id: str, kind: str) -> int:
        doc = self._counters.find_one_and_update(
            {"_id": f"{project_id}:{kind}"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return doc["seq"]

    def _find_by_key(self, project_id, reporter, key):
        return self._issues.find_one(
            {"project_id": project_id, "reporter": reporter, "idempotency_key": key}
        )

    @staticmethod
    def _replay(doc: dict, fingerprint: str) -> dict:
        if doc["fingerprint"] != fingerprint:
            raise IdempotencyConflict("idempotency key was already used with a different request")
        return _issue_out(doc)


def _issue_out(doc: dict) -> dict:
    return {"id": doc["number"], **{k: doc[k] for k in _ISSUE_FIELDS}}


def _reply_out(doc: dict) -> dict:
    return {"id": doc["number"], "author": doc["author"], "body": doc["body"], "created_at": doc["created_at"]}


def _comment_out(doc: dict) -> dict:
    replies = sorted(doc.get("replies", []), key=lambda r: r["number"])
    return {
        "id": doc["number"],
        "author": doc["author"],
        "body": doc["body"],
        "created_at": doc["created_at"],
        "replies": [_reply_out(r) for r in replies],
    }


class _mapped_errors:
    """Translate driver exceptions into domain errors without leaking details."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None or not isinstance(exc, PyMongoError):
            return False
        if isinstance(exc, _UNAVAILABLE):
            log.warning("mongodb unavailable: %s", exc)
            raise StorageUnavailable("storage is temporarily unavailable") from None
        log.exception("mongodb error")
        raise StorageError("storage error") from None
