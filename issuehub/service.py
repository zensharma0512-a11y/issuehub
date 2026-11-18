"""IssueHub core service: validation and authorisation on top of a ``Store``.

Design notes
------------
* Every mutation is validated in full *before* the store is called, so a failed
  call leaves no partial writes (no issue, no stored idempotency key, no used
  number).
* The atomic parts (number allocation, idempotent create, version-checked status
  change) live in the store: ``MemoryStore`` uses per-project locks,
  ``MongoStore`` uses single-document atomic updates and unique indexes.
* Callers only ever receive freshly built dicts, so mutating a result cannot
  corrupt stored state.
* Non-members get ``NotFound`` for a project, so the API does not reveal which
  project IDs exist.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from . import validation as v
from .errors import (
    InvalidTransition,
    NotFound,
    PermissionDenied,
    ValidationError,
    VersionConflict,
)
from .store import MemoryStore, Store


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


def _utcnow_ms() -> datetime:
    # MongoDB keeps millisecond precision. Truncating here means both stores
    # return exactly the timestamps they were given.
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


class IssueService:
    def __init__(self, store: Store | None = None, clock: Callable[[], datetime] = _utcnow_ms):
        self._store: Store = store if store is not None else MemoryStore()
        self._clock = clock

    # ------------------------------------------------------------------ projects

    def create_project(self, actor, project_id, name) -> dict:
        actor = v.require_actor(actor)
        project_id = v.identifier(project_id, "project_id")
        name = v.text(name, "name", min_len=1, max_len=100)
        self._store.create_project(project_id, name, actor)
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
        project = self._authorize(actor, project_id, Role.ADMIN)
        self._store.set_member(project["id"], user_id, role.value)
        return {"project_id": project["id"], "user_id": user_id, "role": role.value}

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

        project = self._authorize(actor, project_id, Role.MEMBER)
        issue, created = self._store.create_issue(
            project["id"],
            actor,
            {"title": title, "description": description, "priority": priority},
            key,
            fingerprint,
            self._clock(),
        )
        return _issue_snapshot(issue), created

    def get_issue(self, actor, project_id, issue_id) -> dict:
        actor = v.require_actor(actor)
        issue_id = v.strict_int(issue_id, "issue_id", minimum=1)
        project = self._authorize(actor, project_id, Role.VIEWER)
        return _issue_snapshot(self._issue_in(project["id"], issue_id))

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

        project = self._authorize(actor, project_id, Role.MEMBER)
        issue = self._issue_in(project["id"], issue_id)
        if issue["version"] != expected_version:
            raise _conflict(expected_version, issue["version"])
        current = Status(issue["status"])
        if target not in TRANSITIONS[current]:
            raise InvalidTransition(
                f"cannot move issue from {current.value} to {target.value}",
                current=current.value,
                allowed=sorted(s.value for s in TRANSITIONS[current]),
            )
        if current is Status.CLOSED:
            self._require_role(project, actor, Role.ADMIN)

        # The store re-checks version *and* status atomically while applying the
        # change, so a write that sneaks in after our read makes this fail.
        updated = self._store.update_status(
            project["id"], issue_id, expected_version, current.value, target.value, self._clock()
        )
        if updated is None:
            latest = self._issue_in(project["id"], issue_id)
            raise _conflict(expected_version, latest["version"])
        return _issue_snapshot(updated)

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

        project = self._authorize(actor, project_id, Role.VIEWER)
        after = 0
        if cursor is not None:
            after = _decode_cursor(cursor, project["id"], status_filter)

        # Ask for one extra row to learn whether another page exists.
        rows = self._store.list_issues(
            project["id"], after, status_filter.value if status_filter else None, limit + 1
        )
        has_more = len(rows) > limit
        page = [_issue_snapshot(r) for r in rows[:limit]]
        next_cursor = (
            _encode_cursor(project["id"], status_filter, page[-1]["id"]) if has_more else None
        )
        return {"items": page, "next_cursor": next_cursor}

    # ------------------------------------------------------------------ comments

    def add_comment(self, actor, project_id, issue_id, body) -> dict:
        actor = v.require_actor(actor)
        issue_id = v.strict_int(issue_id, "issue_id", minimum=1)
        body = v.text(body, "body", min_len=1, max_len=5_000)
        project = self._authorize(actor, project_id, Role.MEMBER)
        self._issue_in(project["id"], issue_id)
        comment = self._store.add_comment(project["id"], issue_id, actor, body, self._clock())
        return _comment_snapshot(comment)

    def add_reply(self, actor, project_id, issue_id, comment_id, body) -> dict:
        """Reply to a top-level comment. Threads are exactly two levels deep."""
        actor = v.require_actor(actor)
        issue_id = v.strict_int(issue_id, "issue_id", minimum=1)
        comment_id = v.strict_int(comment_id, "comment_id", minimum=1)
        body = v.text(body, "body", min_len=1, max_len=5_000)
        project = self._authorize(actor, project_id, Role.MEMBER)
        self._issue_in(project["id"], issue_id)
        reply = self._store.add_reply(
            project["id"], issue_id, comment_id, actor, body, self._clock()
        )
        # A comment ID from a different issue is treated as missing.
        if reply is None:
            raise NotFound("comment not found", comment_id=comment_id)
        return _reply_snapshot(reply)

    def get_thread(self, actor, project_id, issue_id) -> list[dict]:
        actor = v.require_actor(actor)
        issue_id = v.strict_int(issue_id, "issue_id", minimum=1)
        project = self._authorize(actor, project_id, Role.VIEWER)
        self._issue_in(project["id"], issue_id)
        return [_comment_snapshot(c) for c in self._store.get_thread(project["id"], issue_id)]

    # ------------------------------------------------------------------- helpers

    def _authorize(self, actor: str, project_id, minimum: Role) -> dict:
        project_id = v.identifier(project_id, "project_id")
        project = self._store.get_project(project_id)
        # Same error for "missing" and "not a member": don't leak existence.
        if project is None or actor not in project["members"]:
            raise NotFound("project not found", project_id=project_id)
        self._require_role(project, actor, minimum)
        return project

    @staticmethod
    def _require_role(project: dict, actor: str, minimum: Role) -> None:
        role = Role(project["members"][actor])
        if _ROLE_RANK[role] < _ROLE_RANK[minimum]:
            raise PermissionDenied(f"requires {minimum.value} role", role=role.value)

    def _issue_in(self, project_id: str, issue_id: int) -> dict:
        issue = self._store.get_issue(project_id, issue_id)
        if issue is None:
            raise NotFound("issue not found", issue_id=issue_id)
        return issue


# ----------------------------------------------------------------- module helpers


def _conflict(expected: int, current: int) -> VersionConflict:
    return VersionConflict(
        "issue was modified by someone else", expected_version=expected, current_version=current
    )


def _iso(value: datetime) -> str:
    if value.tzinfo is None:  # defensive: treat naive values as UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _issue_snapshot(issue: dict) -> dict:
    return {
        "id": issue["id"],
        "title": issue["title"],
        "description": issue["description"],
        "priority": issue["priority"],
        "status": issue["status"],
        "reporter": issue["reporter"],
        "version": issue["version"],
        "created_at": _iso(issue["created_at"]),
        "updated_at": _iso(issue["updated_at"]),
    }


def _reply_snapshot(reply: dict) -> dict:
    return {
        "id": reply["id"],
        "author": reply["author"],
        "body": reply["body"],
        "created_at": _iso(reply["created_at"]),
    }


def _comment_snapshot(comment: dict) -> dict:
    return {
        "id": comment["id"],
        "author": comment["author"],
        "body": comment["body"],
        "created_at": _iso(comment["created_at"]),
        "replies": [_reply_snapshot(r) for r in comment["replies"]],
    }


def _fingerprint(**fields) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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
