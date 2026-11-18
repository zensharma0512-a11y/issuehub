"""Storage boundary for IssueHub.

``IssueService`` does validation and authorisation, then calls a ``Store`` for
every read and write. A store owns the *atomic* part of each operation:

* allocating issue, comment and reply numbers without duplicates,
* creating an issue and recording its idempotency key as one step,
* changing status only if the version (and status) are still what the caller
  read, and bumping the version in the same step.

``MemoryStore`` does this with one lock per project. ``MongoStore`` (mongo.py)
does it with MongoDB's single-document atomic updates and unique indexes, so it
also stays correct with several server processes sharing one database.

Stores return plain dicts and never hand out references to what they keep.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .errors import AlreadyExists, IdempotencyConflict


class Store(Protocol):
    def create_project(self, project_id: str, name: str, owner_id: str) -> None:
        """Raises AlreadyExists."""

    def get_project(self, project_id: str) -> dict | None:
        """``{"id", "name", "members": {user_id: role}}``"""

    def set_member(self, project_id: str, user_id: str, role: str) -> None: ...

    def create_issue(
        self,
        project_id: str,
        reporter: str,
        fields: dict,
        idempotency_key: str | None,
        fingerprint: str,
        now: datetime,
    ) -> tuple[dict, bool]:
        """Returns ``(issue, created)``. A retry with the same key and fingerprint
        returns the original issue with ``created=False``. Raises
        IdempotencyConflict if the key was used with a different fingerprint."""

    def get_issue(self, project_id: str, issue_id: int) -> dict | None: ...

    def update_status(
        self,
        project_id: str,
        issue_id: int,
        expected_version: int,
        from_status: str,
        to_status: str,
        now: datetime,
    ) -> dict | None:
        """Apply only if version and status still match. ``None`` = no match."""

    def list_issues(
        self, project_id: str, after_id: int, status: str | None, limit: int
    ) -> list[dict]:
        """Issues with ``id > after_id`` in id order."""

    def add_comment(
        self, project_id: str, issue_id: int, author: str, body: str, now: datetime
    ) -> dict: ...

    def add_reply(
        self,
        project_id: str,
        issue_id: int,
        comment_id: int,
        author: str,
        body: str,
        now: datetime,
    ) -> dict | None:
        """``None`` if the comment doesn't exist on that issue."""

    def get_thread(self, project_id: str, issue_id: int) -> list[dict]:
        """Comments in creation order, each with its replies in creation order."""


# ------------------------------------------------------------------ in memory


@dataclass
class _Project:
    id: str
    name: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    members: dict[str, str] = field(default_factory=dict)
    issues: dict[int, dict] = field(default_factory=dict)
    # Issue ids only grow and issues are never deleted, so this list is sorted.
    issue_ids: list[int] = field(default_factory=list)
    next_issue_id: int = 1
    idempotency: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)
    comments: dict[int, list[dict]] = field(default_factory=dict)
    next_comment_id: int = 1
    next_reply_id: int = 1


class MemoryStore:
    def __init__(self):
        self._registry_lock = threading.Lock()
        self._projects: dict[str, _Project] = {}

    def create_project(self, project_id, name, owner_id):
        with self._registry_lock:
            if project_id in self._projects:
                raise AlreadyExists("project already exists", project_id=project_id)
            project = _Project(id=project_id, name=name)
            project.members[owner_id] = "admin"
            self._projects[project_id] = project

    def get_project(self, project_id):
        project = self._get(project_id)
        if project is None:
            return None
        with project.lock:
            return {"id": project.id, "name": project.name, "members": dict(project.members)}

    def set_member(self, project_id, user_id, role):
        project = self._get(project_id)
        with project.lock:
            project.members[user_id] = role

    def create_issue(self, project_id, reporter, fields, idempotency_key, fingerprint, now):
        project = self._get(project_id)
        with project.lock:
            if idempotency_key is not None:
                stored = project.idempotency.get((reporter, idempotency_key))
                if stored is not None:
                    stored_fingerprint, issue_id = stored
                    if stored_fingerprint != fingerprint:
                        raise IdempotencyConflict(
                            "idempotency key was already used with a different request"
                        )
                    return copy.deepcopy(project.issues[issue_id]), False
            issue = {
                "id": project.next_issue_id,
                **fields,
                "status": "open",
                "reporter": reporter,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
            project.next_issue_id += 1
            project.issues[issue["id"]] = issue
            project.issue_ids.append(issue["id"])
            project.comments[issue["id"]] = []
            if idempotency_key is not None:
                project.idempotency[(reporter, idempotency_key)] = (fingerprint, issue["id"])
            return copy.deepcopy(issue), True

    def get_issue(self, project_id, issue_id):
        project = self._get(project_id)
        with project.lock:
            issue = project.issues.get(issue_id)
            return copy.deepcopy(issue) if issue is not None else None

    def update_status(self, project_id, issue_id, expected_version, from_status, to_status, now):
        project = self._get(project_id)
        with project.lock:
            issue = project.issues.get(issue_id)
            if issue is None or issue["version"] != expected_version or issue["status"] != from_status:
                return None
            issue["status"] = to_status
            issue["version"] += 1
            issue["updated_at"] = now
            return copy.deepcopy(issue)

    def list_issues(self, project_id, after_id, status, limit):
        project = self._get(project_id)
        with project.lock:
            ids = project.issue_ids
            out = []
            for idx in range(_first_index_after(ids, after_id), len(ids)):
                issue = project.issues[ids[idx]]
                if status is not None and issue["status"] != status:
                    continue
                out.append(copy.deepcopy(issue))
                if len(out) == limit:
                    break
            return out

    def add_comment(self, project_id, issue_id, author, body, now):
        project = self._get(project_id)
        with project.lock:
            comment = {
                "id": project.next_comment_id,
                "author": author,
                "body": body,
                "created_at": now,
                "replies": [],
            }
            project.next_comment_id += 1
            project.comments[issue_id].append(comment)
            return copy.deepcopy(comment)

    def add_reply(self, project_id, issue_id, comment_id, author, body, now):
        project = self._get(project_id)
        with project.lock:
            comment = next((c for c in project.comments.get(issue_id, []) if c["id"] == comment_id), None)
            if comment is None:
                return None
            reply = {"id": project.next_reply_id, "author": author, "body": body, "created_at": now}
            project.next_reply_id += 1
            comment["replies"].append(reply)
            return copy.deepcopy(reply)

    def get_thread(self, project_id, issue_id):
        project = self._get(project_id)
        with project.lock:
            return copy.deepcopy(project.comments.get(issue_id, []))

    def _get(self, project_id) -> _Project | None:
        with self._registry_lock:
            return self._projects.get(project_id)


def _first_index_after(ids: list[int], after: int) -> int:
    """Binary search: index of the first id strictly greater than ``after``."""
    lo, hi = 0, len(ids)
    while lo < hi:
        mid = (lo + hi) // 2
        if ids[mid] <= after:
            lo = mid + 1
        else:
            hi = mid
    return lo
