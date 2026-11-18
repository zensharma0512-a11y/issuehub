"""IssueHub core service.

Design notes
------------
* Every project owns its own lock, so traffic on one project never blocks another.
  A small registry lock only guards creating/looking up projects.
* All mutations validate fully *before* touching state, so a failed call leaves
  no partial writes (no half-created issue, no stored idempotency key).
* Callers only ever receive freshly built dicts; internal records are never
  handed out, so mutating a result cannot corrupt the store.
* Non-members get ``NotFound`` for a project, so the API does not reveal which
  project IDs exist.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from . import validation as v
from .errors import (
    AlreadyExists,
    IdempotencyConflict,
    InvalidTransition,
    NotFound,
    PermissionDenied,
    ValidationError,
    VersionConflict,
)


class Role(str, Enum):
    VIEWER = "viewer"
    MEMBER = "member"
    ADMIN = "admin"


_ROLE_RANK = {Role.VIEWER: 0, Role.MEMBER: 1, Role.ADMIN: 2}


class Status(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.OPEN: frozenset({Status.IN_PROGRESS, Status.CLOSED}),
    Status.IN_PROGRESS: frozenset({Status.OPEN, Status.RESOLVED, Status.CLOSED}),
    Status.RESOLVED: frozenset({Status.IN_PROGRESS, Status.CLOSED}),
    Status.CLOSED: frozenset({Status.OPEN}),  # reopening requires admin, see below
}

MAX_PAGE_SIZE = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Issue:
    id: int
    title: str
    description: str
    priority: str
    status: Status
    reporter: str
    version: int
    created_at: datetime
    updated_at: datetime

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "status": self.status.value,
            "reporter": self.reporter,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class _Reply:
    id: int
    author: str
    body: str
    created_at: datetime

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class _Comment:
    id: int
    author: str
    body: str
    created_at: datetime
    replies: list[_Reply] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at.isoformat(),
            "replies": [r.snapshot() for r in self.replies],
        }


@dataclass
class _Project:
    id: str
    name: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    members: dict[str, Role] = field(default_factory=dict)
    issues: dict[int, _Issue] = field(default_factory=dict)
    # Issues are never deleted and IDs only grow, so insertion order == ID order.
    # That makes "id > cursor" a stable keyset for pagination.
    issue_ids: list[int] = field(default_factory=list)
    next_issue_id: int = 1
    # (actor, key) -> (request fingerprint, issue id)
    idempotency: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)
    # issue id -> comments in creation order
    comments: dict[int, list[_Comment]] = field(default_factory=dict)
    comment_index: dict[int, tuple[int, _Comment]] = field(default_factory=dict)
    next_comment_id: int = 1
    next_reply_id: int = 1


class IssueService:
    def __init__(self, clock: Callable[[], datetime] = _utcnow):
        self._clock = clock
        self._registry_lock = threading.Lock()
        self._projects: dict[str, _Project] = {}

    # ------------------------------------------------------------------ projects

    def create_project(self, actor, project_id, name) -> dict:
        actor = v.require_actor(actor)
        project_id = v.identifier(project_id, "project_id")
        name = v.text(name, "name", min_len=1, max_len=100)
        with self._registry_lock:
            if project_id in self._projects:
                raise AlreadyExists("project already exists", project_id=project_id)
            project = _Project(id=project_id, name=name)
            project.members[actor] = Role.ADMIN
            self._projects[project_id] = project
        return {"id": project_id, "name": name, "role": Role.ADMIN.value}

    def add_member(self, actor, project_id, user_id, role) -> dict:
        actor = v.require_actor(actor)
        user_id = v.identifier(user_id, "user_id")
        try:
            role = Role(role)
        except ValueError:
            raise ValidationError(
                "role must be one of viewer, member, admin", field="role"
            ) from None
        project = self._project_for(actor, project_id)
        with project.lock:
            self._require_role(project, actor, Role.ADMIN)
            project.members[user_id] = role
        return {"project_id": project.id, "user_id": user_id, "role": role.value}

    # -------------------------------------------------------------------- issues

    def create_issue(
        self,
        actor,
        project_id,
        title,
        description="",
        priority="medium",
        idempotency_key=None,
    ) -> tuple[dict, bool]:
        """Create an issue. Returns ``(issue, created)``.

        With an idempotency key, retries of the same request return the original
        issue with ``created=False``. Reusing the key for a different request is
        rejected. The key is scoped to (project, user), so two users can never
        collide or see each other's issue through a shared key.
        """
        actor = v.require_actor(actor)
        title = v.text(title, "title", min_len=1, max_len=200)
        description = v.text(description, "description", min_len=0, max_len=10_000)
        priority = v.priority(priority)
        key = v.idempotency_key(idempotency_key)
        fingerprint = _fingerprint(title=title, description=description, priority=priority)

        project = self._project_for(actor, project_id)
        with project.lock:
            self._require_role(project, actor, Role.MEMBER)
            if key is not None:
                stored = project.idempotency.get((actor, key))
                if stored is not None:
                    stored_fingerprint, issue_id = stored
                    if stored_fingerprint != fingerprint:
                        raise IdempotencyConflict(
                            "idempotency key was already used with a different request"
                        )
                    return project.issues[issue_id].snapshot(), False

            now = self._clock()
            issue = _Issue(
                id=project.next_issue_id,
                title=title,
                description=description,
                priority=priority,
                status=Status.OPEN,
                reporter=actor,
                version=1,
                created_at=now,
                updated_at=now,
            )
            # All checks passed: commit every piece of state together.
            project.next_issue_id += 1
            project.issues[issue.id] = issue
            project.issue_ids.append(issue.id)
            project.comments[issue.id] = []
            if key is not None:
                project.idempotency[(actor, key)] = (fingerprint, issue.id)
            return issue.snapshot(), True

    def get_issue(self, actor, project_id, issue_id) -> dict:
        actor = v.require_actor(actor)
        issue_id = v.strict_int(issue_id, "issue_id", minimum=1)
        project = self._project_for(actor, project_id)
        with project.lock:
            self._require_role(project, actor, Role.VIEWER)
            return self._issue_in(project, issue_id).snapshot()

    def transition(self, actor, project_id, issue_id, to_status, expected_version) -> dict:
        """Move an issue to a new status if ``expected_version`` is still current.

        Of N concurrent callers holding the same version, exactly one succeeds;
        the rest get ``VersionConflict`` and must re-read before retrying.
        """
        actor = v.require_actor(actor)
        issue_id = v.strict_int(issue_id, "issue_id", minimum=1)
        expected_version = v.strict_int(expected_version, "expected_version", minimum=1)
        try:
            target = Status(to_status)
        except ValueError:
            raise ValidationError(
                "to must be one of " + ", ".join(s.value for s in Status), field="to"
            ) from None

        project = self._project_for(actor, project_id)
        with project.lock:
            self._require_role(project, actor, Role.MEMBER)
            issue = self._issue_in(project, issue_id)
            if issue.version != expected_version:
                raise VersionConflict(
                    "issue was modified by someone else",
                    expected_version=expected_version,
                    current_version=issue.version,
                )
            if target not in TRANSITIONS[issue.status]:
                raise InvalidTransition(
                    f"cannot move issue from {issue.status.value} to {target.value}",
                    current=issue.status.value,
                    allowed=sorted(s.value for s in TRANSITIONS[issue.status]),
                )
            if issue.status is Status.CLOSED:
                self._require_role(project, actor, Role.ADMIN)
            issue.status = target
            issue.version += 1
            issue.updated_at = self._clock()
            return issue.snapshot()

    def list_issues(self, actor, project_id, limit=20, cursor=None, status=None) -> dict:
        """Keyset pagination ordered by issue id (= creation order).

        The cursor is opaque to clients and bound to the project and filter it was
        issued for, so it cannot be replayed against a different query.
        """
        actor = v.require_actor(actor)
        limit = v.strict_int(limit, "limit", minimum=1, maximum=MAX_PAGE_SIZE)
        status_filter = None
        if status is not None:
            try:
                status_filter = Status(status)
            except ValueError:
                raise ValidationError("unknown status filter", field="status") from None

        project = self._project_for(actor, project_id)
        after = 0
        if cursor is not None:
            after = _decode_cursor(cursor, project.id, status_filter)

        with project.lock:
            self._require_role(project, actor, Role.VIEWER)
            ids = project.issue_ids
            start = _first_index_after(ids, after)
            page: list[dict] = []
            last_seen = None
            has_more = False
            for idx in range(start, len(ids)):
                issue = project.issues[ids[idx]]
                if status_filter is not None and issue.status is not status_filter:
                    continue
                if len(page) == limit:
                    has_more = True
                    break
                page.append(issue.snapshot())
                last_seen = issue.id

        next_cursor = _encode_cursor(project.id, status_filter, last_seen) if has_more else None
        return {"items": page, "next_cursor": next_cursor}

    # ------------------------------------------------------------------ comments

    def add_comment(self, actor, project_id, issue_id, body) -> dict:
        actor = v.require_actor(actor)
        issue_id = v.strict_int(issue_id, "issue_id", minimum=1)
        body = v.text(body, "body", min_len=1, max_len=5_000)
        project = self._project_for(actor, project_id)
        with project.lock:
            self._require_role(project, actor, Role.MEMBER)
            self._issue_in(project, issue_id)
            comment = _Comment(
                id=project.next_comment_id, author=actor, body=body, created_at=self._clock()
            )
            project.next_comment_id += 1
            project.comments[issue_id].append(comment)
            project.comment_index[comment.id] = (issue_id, comment)
            return comment.snapshot()

    def add_reply(self, actor, project_id, issue_id, comment_id, body) -> dict:
        """Reply to a top-level comment. Threads are exactly two levels deep."""
        actor = v.require_actor(actor)
        issue_id = v.strict_int(issue_id, "issue_id", minimum=1)
        comment_id = v.strict_int(comment_id, "comment_id", minimum=1)
        body = v.text(body, "body", min_len=1, max_len=5_000)
        project = self._project_for(actor, project_id)
        with project.lock:
            self._require_role(project, actor, Role.MEMBER)
            self._issue_in(project, issue_id)
            found = project.comment_index.get(comment_id)
            # A comment ID from a different issue is treated as missing.
            if found is None or found[0] != issue_id:
                raise NotFound("comment not found", comment_id=comment_id)
            reply = _Reply(
                id=project.next_reply_id, author=actor, body=body, created_at=self._clock()
            )
            project.next_reply_id += 1
            found[1].replies.append(reply)
            return reply.snapshot()

    def get_thread(self, actor, project_id, issue_id) -> list[dict]:
        actor = v.require_actor(actor)
        issue_id = v.strict_int(issue_id, "issue_id", minimum=1)
        project = self._project_for(actor, project_id)
        with project.lock:
            self._require_role(project, actor, Role.VIEWER)
            self._issue_in(project, issue_id)
            return [c.snapshot() for c in project.comments[issue_id]]

    # ------------------------------------------------------------------- helpers

    def _project_for(self, actor: str, project_id) -> _Project:
        project_id = v.identifier(project_id, "project_id")
        with self._registry_lock:
            project = self._projects.get(project_id)
        # Same error for "missing" and "not a member": don't leak existence.
        if project is None or actor not in project.members:
            raise NotFound("project not found", project_id=project_id)
        return project

    @staticmethod
    def _require_role(project: _Project, actor: str, minimum: Role) -> None:
        role = project.members.get(actor)
        if role is None:
            raise NotFound("project not found", project_id=project.id)
        if _ROLE_RANK[role] < _ROLE_RANK[minimum]:
            raise PermissionDenied(f"requires {minimum.value} role", role=role.value)

    @staticmethod
    def _issue_in(project: _Project, issue_id: int) -> _Issue:
        issue = project.issues.get(issue_id)
        if issue is None:
            raise NotFound("issue not found", issue_id=issue_id)
        return issue


# ----------------------------------------------------------------- module helpers


def _fingerprint(**fields) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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


def _encode_cursor(project_id: str, status: Status | None, after: int) -> str:
    payload = {"p": project_id, "s": status.value if status else None, "a": after}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor, project_id: str, status: Status | None) -> int:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise ValidationError("invalid cursor", field="cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ValidationError("invalid cursor", field="cursor") from None
    if (
        not isinstance(payload, dict)
        or payload.get("p") != project_id
        or payload.get("s") != (status.value if status else None)
        or type(payload.get("a")) is not int
        or payload["a"] < 1
    ):
        raise ValidationError("cursor does not match this query", field="cursor")
    return payload["a"]
