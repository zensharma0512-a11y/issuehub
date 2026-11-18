import json

import pytest

from issuehub.app import create_app
from issuehub.http import HttpAdapter, StaticTokenAuth
from issuehub.service import IssueService

ALICE = {"Authorization": "Bearer t-alice"}
BOB = {"Authorization": "Bearer t-bob"}


@pytest.fixture
def api():
    adapter = HttpAdapter(IssueService(), StaticTokenAuth({"t-alice": "alice", "t-bob": "bob"}))

    def call(method, path, body=None, headers=None, query=None):
        raw = json.dumps(body).encode() if body is not None else None
        return adapter.handle(method, path, query=query, headers=headers or {}, body=raw)

    status, _, _ = call("POST", "/projects", {"id": "web", "name": "Website"}, ALICE)
    assert status == 201
    call("POST", "/projects/web/members", {"user_id": "bob", "role": "member"}, ALICE)
    return call


def test_requires_bearer_token(api):
    status, headers, body = api("GET", "/projects/web/issues")
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    status, _, _ = api("GET", "/projects/web/issues", headers={"Authorization": "Bearer nope"})
    assert status == 401


def test_create_then_replay_with_idempotency_key(api):
    h = {**BOB, "Idempotency-Key": "abc"}
    s1, h1, issue = api("POST", "/projects/web/issues", {"title": "Bug"}, h)
    s2, h2, replay = api("POST", "/projects/web/issues", {"title": "Bug"}, h)
    assert (s1, s2) == (201, 200)
    assert h2["Idempotent-Replayed"] == "true" and replay == issue
    s3, _, body = api("POST", "/projects/web/issues", {"title": "Other"}, h)
    assert s3 == 422 and body["error"] == "idempotency_key_reused"


def test_transition_conflict_is_409(api):
    _, _, issue = api("POST", "/projects/web/issues", {"title": "Bug"}, BOB)
    path = f"/projects/web/issues/{issue['id']}/transitions"
    ok, headers, moved = api("POST", path, {"to": "in_progress", "expected_version": 1}, BOB)
    assert ok == 200 and headers["ETag"] == '"2"'
    stale, _, body = api("POST", path, {"to": "resolved", "expected_version": 1}, BOB)
    assert stale == 409 and body["details"]["current_version"] == 2


def test_pagination_over_http(api):
    for i in range(5):
        api("POST", "/projects/web/issues", {"title": f"I{i}"}, BOB)
    _, _, page = api("GET", "/projects/web/issues", headers=BOB, query={"limit": "2"})
    _, _, page2 = api(
        "GET", "/projects/web/issues", headers=BOB, query={"limit": "2", "cursor": page["next_cursor"]}
    )
    assert [i["title"] for i in page["items"] + page2["items"]] == ["I0", "I1", "I2", "I3"]
    bad, _, _ = api("GET", "/projects/web/issues", headers=BOB, query={"limit": "-1"})
    assert bad == 400


def test_comment_thread_over_http(api):
    _, _, issue = api("POST", "/projects/web/issues", {"title": "Bug"}, BOB)
    base = f"/projects/web/issues/{issue['id']}/comments"
    _, _, c = api("POST", base, {"body": "hi"}, BOB)
    s, _, _ = api("POST", f"{base}/{c['id']}/replies", {"body": "hello"}, ALICE)
    assert s == 201
    _, _, thread = api("GET", base, headers=BOB)
    assert thread["comments"][0]["replies"][0]["body"] == "hello"


@pytest.mark.parametrize("raw", [b"not json", b"[1,2]", b'{"title": 5}', b"\xff\xfe"])
def test_bad_bodies_are_400(raw):
    adapter = HttpAdapter(IssueService(), StaticTokenAuth({"t": "u"}))
    adapter.handle("POST", "/projects", headers={"Authorization": "Bearer t"},
                   body=json.dumps({"id": "p", "name": "P"}).encode())
    status, _, body = adapter.handle(
        "POST", "/projects/p/issues", headers={"Authorization": "Bearer t"}, body=raw
    )
    assert status == 400 and body["error"] == "validation_error"


def test_unknown_route_and_wrong_method(api):
    assert api("GET", "/nope", headers=BOB)[0] == 404
    status, headers, _ = api("DELETE", "/projects/web/issues", headers=BOB)
    assert status == 405 and headers["Allow"] == "GET, POST"


def test_non_member_gets_404_not_403(api):
    adapter = HttpAdapter(IssueService(), StaticTokenAuth({"a": "alice", "m": "mallory"}))
    adapter.handle("POST", "/projects", headers={"Authorization": "Bearer a"},
                   body=json.dumps({"id": "secret", "name": "S"}).encode())
    status, _, _ = adapter.handle("GET", "/projects/secret/issues", headers={"Authorization": "Bearer m"})
    assert status == 404


def test_flask_app_end_to_end():
    adapter = HttpAdapter(IssueService(), StaticTokenAuth({"t": "alice"}))
    client = create_app(adapter).test_client()
    auth = {"Authorization": "Bearer t"}
    assert client.post("/projects", json={"id": "web", "name": "W"}, headers=auth).status_code == 201
    r = client.post("/projects/web/issues", json={"title": "Bug"}, headers={**auth, "Idempotency-Key": "k"})
    assert r.status_code == 201 and r.get_json()["id"] == 1
    r = client.get("/projects/web/issues/1", headers=auth)
    assert r.status_code == 200 and r.headers["ETag"] == '"1"'
