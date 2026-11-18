"""Framework-agnostic HTTP adapter.

``HttpAdapter.handle`` takes a method, path, query, headers and raw body and
returns ``(status, headers, json_body)``. It knows nothing about any web
framework, so it is unit-testable without a server; ``app.py`` wires it into
Flask for a runnable demo.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Mapping

from .errors import AuthenticationError, IssueHubError, NotFound, ValidationError
from .service import IssueService

log = logging.getLogger(__name__)

Response = tuple[int, dict[str, str], dict | list | None]
Authenticator = Callable[[str], str | None]

_MAX_BODY_BYTES = 64 * 1024


class StaticTokenAuth:
    """Maps bearer tokens to user IDs. Swap for JWT/session auth in production."""

    def __init__(self, tokens: Mapping[str, str]):
        self._tokens = dict(tokens)

    def __call__(self, token: str) -> str | None:
        return self._tokens.get(token)


_ROUTES: list[tuple[str, re.Pattern, str]] = [
    ("POST", re.compile(r"^/projects$"), "create_project"),
    ("POST", re.compile(r"^/projects/(?P<pid>[^/]+)/members$"), "add_member"),
    ("POST", re.compile(r"^/projects/(?P<pid>[^/]+)/issues$"), "create_issue"),
    ("GET", re.compile(r"^/projects/(?P<pid>[^/]+)/issues$"), "list_issues"),
    ("GET", re.compile(r"^/projects/(?P<pid>[^/]+)/issues/(?P<iid>\d+)$"), "get_issue"),
    (
        "POST",
        re.compile(r"^/projects/(?P<pid>[^/]+)/issues/(?P<iid>\d+)/transitions$"),
        "transition",
    ),
    (
        "POST",
        re.compile(r"^/projects/(?P<pid>[^/]+)/issues/(?P<iid>\d+)/comments$"),
        "add_comment",
    ),
    (
        "GET",
        re.compile(r"^/projects/(?P<pid>[^/]+)/issues/(?P<iid>\d+)/comments$"),
        "get_thread",
    ),
    (
        "POST",
        re.compile(
            r"^/projects/(?P<pid>[^/]+)/issues/(?P<iid>\d+)/comments/(?P<cid>\d+)/replies$"
        ),
        "add_reply",
    ),
]


class HttpAdapter:
    def __init__(self, service: IssueService, authenticate: Authenticator):
        self._service = service
        self._authenticate = authenticate

    def handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        query = dict(query or {})
        try:
            handler_name, params = self._route(method.upper(), path)
            actor = self._actor(headers)
            payload = self._json_body(body) if method.upper() == "POST" else {}
            return getattr(self, "_" + handler_name)(actor, params, query, headers, payload)
        except _MethodNotAllowed as exc:
            return 405, {"Allow": ", ".join(exc.allowed)}, {
                "error": "method_not_allowed",
                "message": "method not allowed",
            }
        except IssueHubError as exc:
            extra = {}
            if exc.http_status == 401:
                extra["WWW-Authenticate"] = "Bearer"
            if exc.http_status == 503:
                extra["Retry-After"] = "2"
            return exc.http_status, extra, exc.to_dict()
        except Exception:  # never leak internals to clients
            log.exception("unhandled error")
            return 500, {}, {"error": "internal_error", "message": "internal server error"}

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _route(method: str, path: str) -> tuple[str, dict]:
        allowed = []
        for route_method, pattern, name in _ROUTES:
            match = pattern.match(path)
            if match:
                if route_method == method:
                    return name, match.groupdict()
                allowed.append(route_method)
        if allowed:
            raise _MethodNotAllowed(allowed)
        raise NotFound("route not found")

    def _actor(self, headers: dict) -> str:
        auth = headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError("missing bearer token")
        actor = self._authenticate(token.strip())
        if actor is None:
            raise AuthenticationError("invalid token")
        return actor

    @staticmethod
    def _json_body(body: bytes | None) -> dict:
        if not body:
            return {}
        if len(body) > _MAX_BODY_BYTES:
            raise ValidationError("request body too large")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("request body must be valid JSON") from None
        if not isinstance(payload, dict):
            raise ValidationError("request body must be a JSON object")
        return payload

    @staticmethod
    def _query_int(query: dict, name: str, default: int) -> int:
        raw = query.get(name)
        if raw is None:
            return default
        if not re.fullmatch(r"\d{1,6}", raw):
            raise ValidationError(f"{name} must be a positive integer", field=name)
        return int(raw)

    # ------------------------------------------------------------------ handlers

    def _create_project(self, actor, params, query, headers, payload) -> Response:
        project = self._service.create_project(actor, payload.get("id"), payload.get("name"))
        return 201, {}, project

    def _add_member(self, actor, params, query, headers, payload) -> Response:
        member = self._service.add_member(
            actor, params["pid"], payload.get("user_id"), payload.get("role")
        )
        return 200, {}, member

    def _create_issue(self, actor, params, query, headers, payload) -> Response:
        issue, created = self._service.create_issue(
            actor,
            params["pid"],
            payload.get("title"),
            payload.get("description", ""),
            payload.get("priority", "medium"),
            idempotency_key=headers.get("idempotency-key"),
        )
        out_headers = {"ETag": f'"{issue["version"]}"'}
        if not created:
            out_headers["Idempotent-Replayed"] = "true"
        return (201 if created else 200), out_headers, issue

    def _list_issues(self, actor, params, query, headers, payload) -> Response:
        page = self._service.list_issues(
            actor,
            params["pid"],
            limit=self._query_int(query, "limit", 20),
            cursor=query.get("cursor"),
            status=query.get("status"),
        )
        return 200, {}, page

    def _get_issue(self, actor, params, query, headers, payload) -> Response:
        issue = self._service.get_issue(actor, params["pid"], int(params["iid"]))
        return 200, {"ETag": f'"{issue["version"]}"'}, issue

    def _transition(self, actor, params, query, headers, payload) -> Response:
        issue = self._service.transition(
            actor,
            params["pid"],
            int(params["iid"]),
            payload.get("to"),
            payload.get("expected_version"),
        )
        return 200, {"ETag": f'"{issue["version"]}"'}, issue

    def _add_comment(self, actor, params, query, headers, payload) -> Response:
        comment = self._service.add_comment(
            actor, params["pid"], int(params["iid"]), payload.get("body")
        )
        return 201, {}, comment

    def _get_thread(self, actor, params, query, headers, payload) -> Response:
        thread = self._service.get_thread(actor, params["pid"], int(params["iid"]))
        return 200, {}, {"comments": thread}

    def _add_reply(self, actor, params, query, headers, payload) -> Response:
        reply = self._service.add_reply(
            actor, params["pid"], int(params["iid"]), int(params["cid"]), payload.get("body")
        )
        return 201, {}, reply


class _MethodNotAllowed(Exception):
    def __init__(self, allowed: list[str]):
        self.allowed = sorted(set(allowed))
