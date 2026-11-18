import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from issuehub import (
    AlreadyExists,
    AuthenticationError,
    IdempotencyConflict,
    InvalidTransition,
    IssueService,
    NotFound,
    PermissionDenied,
    ValidationError,
    VersionConflict,
)


@pytest.fixture
def svc(make_service):
    s = make_service()
    s.create_project("alice", "web", "Website")
    s.add_member("alice", "web", "bob", "member")
    s.add_member("alice", "web", "vera", "viewer")
    return s


def run_concurrently(n, fn):
    barrier = threading.Barrier(n)

    def task(i):
        barrier.wait()
        try:
            return ("ok", fn(i))
        except Exception as exc:  # noqa: BLE001 - we want to inspect every outcome
            return ("err", exc)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(task, range(n)))


# ------------------------------------------------------------------ authorization


class TestAuthorization:
    def test_unauthenticated_actor_rejected(self, svc):
        for bad in (None, "", "   ", 42, True, "has space"):
            with pytest.raises(AuthenticationError):
                svc.create_issue(bad, "web", "t")

    def test_non_member_cannot_tell_project_exists(self, svc):
        with pytest.raises(NotFound) as missing:
            svc.get_issue("mallory", "does-not-exist", 1)
        with pytest.raises(NotFound) as hidden:
            svc.create_issue("mallory", "web", "sneaky")
        assert missing.value.code == hidden.value.code == "not_found"

    def test_viewer_can_read_but_not_write(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        assert svc.get_issue("vera", "web", issue["id"])["title"] == "Bug"
        with pytest.raises(PermissionDenied):
            svc.create_issue("vera", "web", "nope")
        with pytest.raises(PermissionDenied):
            svc.transition("vera", "web", issue["id"], "in_progress", 1)
        with pytest.raises(PermissionDenied):
            svc.add_comment("vera", "web", issue["id"], "hi")

    def test_only_admin_manages_members(self, svc):
        with pytest.raises(PermissionDenied):
            svc.add_member("bob", "web", "carol", "admin")

    def test_only_admin_reopens_closed_issue(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        closed = svc.transition("bob", "web", issue["id"], "closed", 1)
        with pytest.raises(PermissionDenied):
            svc.transition("bob", "web", issue["id"], "open", closed["version"])
        reopened = svc.transition("alice", "web", issue["id"], "open", closed["version"])
        assert reopened["status"] == "open"

    def test_projects_are_isolated(self, svc):
        svc.create_project("carol", "ops", "Ops")
        svc.create_issue("carol", "ops", "Ops issue")
        a, _ = svc.create_issue("bob", "web", "Web issue")
        # Same numeric id exists in both projects, but bob cannot read ops.
        assert a["id"] == 1
        with pytest.raises(NotFound):
            svc.get_issue("bob", "ops", 1)

    def test_duplicate_project_rejected(self, svc):
        with pytest.raises(AlreadyExists):
            svc.create_project("bob", "web", "Again")


# ---------------------------------------------------------------------- validation


class TestValidation:
    @pytest.mark.parametrize("title", ["", "   ", None, 123, "x" * 201])
    def test_bad_titles(self, svc, title):
        with pytest.raises(ValidationError):
            svc.create_issue("bob", "web", title)

    def test_title_is_trimmed(self, svc):
        issue, _ = svc.create_issue("bob", "web", "  Bug  ")
        assert issue["title"] == "Bug"

    def test_bad_priority(self, svc):
        with pytest.raises(ValidationError):
            svc.create_issue("bob", "web", "t", priority="urgent")

    @pytest.mark.parametrize("version", [True, False, 0, -1, "1", 1.0, None])
    def test_expected_version_must_be_strict_positive_int(self, svc, version):
        issue, _ = svc.create_issue("bob", "web", "t")
        with pytest.raises(ValidationError):
            svc.transition("bob", "web", issue["id"], "in_progress", version)

    @pytest.mark.parametrize("limit", [0, 101, True, "5", None])
    def test_bad_page_limits(self, svc, limit):
        with pytest.raises(ValidationError):
            svc.list_issues("bob", "web", limit=limit)

    def test_bad_idempotency_key(self, svc):
        for key in ("", "has space", "x" * 129, 5):
            with pytest.raises(ValidationError):
                svc.create_issue("bob", "web", "t", idempotency_key=key)


# --------------------------------------------------------------------- idempotency


class TestIdempotency:
    def test_retry_returns_original(self, svc):
        first, created1 = svc.create_issue("bob", "web", "Bug", idempotency_key="k1")
        again, created2 = svc.create_issue("bob", "web", "Bug", idempotency_key="k1")
        assert created1 is True and created2 is False
        assert again == first
        assert len(svc.list_issues("bob", "web")["items"]) == 1

    def test_same_key_different_body_rejected(self, svc):
        svc.create_issue("bob", "web", "Bug", idempotency_key="k1")
        with pytest.raises(IdempotencyConflict):
            svc.create_issue("bob", "web", "Different", idempotency_key="k1")

    def test_keys_are_scoped_per_user(self, svc):
        a, _ = svc.create_issue("bob", "web", "Bug", idempotency_key="k1")
        b, created = svc.create_issue("alice", "web", "Bug", idempotency_key="k1")
        assert created is True and a["id"] != b["id"]

    def test_failed_create_does_not_burn_key(self, svc):
        with pytest.raises(ValidationError):
            svc.create_issue("bob", "web", "", idempotency_key="k1")
        issue, created = svc.create_issue("bob", "web", "Real", idempotency_key="k1")
        assert created is True and issue["id"] == 1

    def test_concurrent_retries_create_exactly_one(self, svc):
        results = run_concurrently(
            32, lambda i: svc.create_issue("bob", "web", "Bug", idempotency_key="same")
        )
        issues = [r[1][0] for r in results if r[0] == "ok"]
        assert len(issues) == 32
        assert len({i["id"] for i in issues}) == 1
        assert sum(1 for r in results if r[1][1]) == 1  # one "created", rest replays
        assert len(svc.list_issues("bob", "web")["items"]) == 1


# -------------------------------------------------------------- versions / status


class TestTransitions:
    def test_happy_path_bumps_version(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        moved = svc.transition("bob", "web", issue["id"], "in_progress", 1)
        assert moved["status"] == "in_progress" and moved["version"] == 2

    def test_stale_version_rejected_and_state_unchanged(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        svc.transition("bob", "web", issue["id"], "in_progress", 1)
        with pytest.raises(VersionConflict) as err:
            svc.transition("bob", "web", issue["id"], "resolved", 1)
        assert err.value.details == {"expected_version": 1, "current_version": 2}
        assert svc.get_issue("bob", "web", issue["id"])["status"] == "in_progress"

    def test_illegal_transition_rejected_without_version_bump(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        with pytest.raises(InvalidTransition):
            svc.transition("bob", "web", issue["id"], "resolved", 1)
        assert svc.get_issue("bob", "web", issue["id"])["version"] == 1

    def test_unknown_status(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        with pytest.raises(ValidationError):
            svc.transition("bob", "web", issue["id"], "done", 1)

    def test_concurrent_updates_on_same_version_succeed_once(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        results = run_concurrently(
            50, lambda i: svc.transition("bob", "web", issue["id"], "in_progress", 1)
        )
        oks = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if isinstance(r[1], VersionConflict)]
        assert len(oks) == 1 and len(conflicts) == 49
        assert svc.get_issue("bob", "web", issue["id"])["version"] == 2


# ---------------------------------------------------------------------- pagination


class TestPagination:
    def _seed(self, svc, n):
        return [svc.create_issue("bob", "web", f"Issue {i}")[0]["id"] for i in range(n)]

    def test_walks_all_pages_in_order(self, svc):
        ids = self._seed(svc, 25)
        seen, cursor = [], None
        while True:
            page = svc.list_issues("bob", "web", limit=10, cursor=cursor)
            seen += [i["id"] for i in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == ids

    def test_exact_multiple_has_no_dangling_empty_page(self, svc):
        self._seed(svc, 10)
        page = svc.list_issues("bob", "web", limit=10)
        assert len(page["items"]) == 10 and page["next_cursor"] is None

    def test_inserts_during_paging_do_not_duplicate_or_skip(self, svc):
        self._seed(svc, 5)
        page1 = svc.list_issues("bob", "web", limit=3)
        svc.create_issue("bob", "web", "Late arrival")
        page2 = svc.list_issues("bob", "web", limit=10, cursor=page1["next_cursor"])
        all_ids = [i["id"] for i in page1["items"] + page2["items"]]
        assert all_ids == [1, 2, 3, 4, 5, 6]

    def test_status_filter(self, svc):
        ids = self._seed(svc, 6)
        for i in ids[::2]:
            svc.transition("bob", "web", i, "in_progress", 1)
        page = svc.list_issues("bob", "web", limit=2, status="in_progress")
        page2 = svc.list_issues("bob", "web", limit=2, status="in_progress", cursor=page["next_cursor"])
        assert [i["id"] for i in page["items"] + page2["items"]] == ids[::2]
        assert page2["next_cursor"] is None

    def test_cursor_bound_to_project_and_filter(self, svc):
        self._seed(svc, 5)
        cursor = svc.list_issues("bob", "web", limit=2)["next_cursor"]
        with pytest.raises(ValidationError):
            svc.list_issues("bob", "web", limit=2, cursor=cursor, status="open")
        svc.create_project("bob", "other", "Other")
        with pytest.raises(ValidationError):
            svc.list_issues("bob", "other", cursor=cursor)

    @pytest.mark.parametrize("cursor", ["", "!!!", "bm90IGpzb24", 7])
    def test_garbage_cursor(self, svc, cursor):
        with pytest.raises(ValidationError):
            svc.list_issues("bob", "web", cursor=cursor)


# ------------------------------------------------------------------------ comments


class TestComments:
    def test_two_level_thread_in_creation_order(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        c1 = svc.add_comment("bob", "web", issue["id"], "first")
        c2 = svc.add_comment("alice", "web", issue["id"], "second")
        svc.add_reply("alice", "web", issue["id"], c1["id"], "re: first")
        svc.add_reply("bob", "web", issue["id"], c1["id"], "re: first again")
        thread = svc.get_thread("vera", "web", issue["id"])
        assert [c["body"] for c in thread] == ["first", "second"]
        assert [r["body"] for r in thread[0]["replies"]] == ["re: first", "re: first again"]
        assert thread[1]["id"] == c2["id"] and thread[1]["replies"] == []

    def test_comment_and_reply_ids_are_independent(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        c1 = svc.add_comment("bob", "web", issue["id"], "a")
        r1 = svc.add_reply("bob", "web", issue["id"], c1["id"], "b")
        c2 = svc.add_comment("bob", "web", issue["id"], "c")
        assert (c1["id"], r1["id"], c2["id"]) == (1, 1, 2)

    def test_reply_to_comment_on_other_issue_is_not_found(self, svc):
        a, _ = svc.create_issue("bob", "web", "A")
        b, _ = svc.create_issue("bob", "web", "B")
        c = svc.add_comment("bob", "web", a["id"], "on A")
        with pytest.raises(NotFound):
            svc.add_reply("bob", "web", b["id"], c["id"], "wrong issue")

    def test_missing_issue_or_comment(self, svc):
        with pytest.raises(NotFound):
            svc.add_comment("bob", "web", 99, "x")
        issue, _ = svc.create_issue("bob", "web", "Bug")
        with pytest.raises(NotFound):
            svc.add_reply("bob", "web", issue["id"], 99, "x")

    def test_concurrent_comments_get_unique_sequential_ids(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        results = run_concurrently(40, lambda i: svc.add_comment("bob", "web", issue["id"], f"c{i}"))
        ids = sorted(r[1]["id"] for r in results)
        assert ids == list(range(1, 41))
        assert [c["id"] for c in svc.get_thread("bob", "web", issue["id"])] == list(range(1, 41))


# ------------------------------------------------------------------ snapshot safety


class TestSnapshots:
    def test_mutating_returned_issue_does_not_change_store(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        issue["title"] = "hacked"
        issue["version"] = 99
        fresh = svc.get_issue("bob", "web", issue["id"])
        assert fresh["title"] == "Bug" and fresh["version"] == 1

    def test_mutating_returned_thread_does_not_change_store(self, svc):
        issue, _ = svc.create_issue("bob", "web", "Bug")
        c = svc.add_comment("bob", "web", issue["id"], "hello")
        thread = svc.get_thread("bob", "web", issue["id"])
        thread[0]["replies"].append({"body": "injected"})
        thread.clear()
        again = svc.get_thread("bob", "web", issue["id"])
        assert len(again) == 1 and again[0]["id"] == c["id"] and again[0]["replies"] == []

    def test_replayed_idempotent_result_is_a_fresh_copy(self, svc):
        first, _ = svc.create_issue("bob", "web", "Bug", idempotency_key="k")
        first["title"] = "changed"
        again, _ = svc.create_issue("bob", "web", "Bug", idempotency_key="k")
        assert again["title"] == "Bug"

    def test_services_do_not_share_state(self, make_service):
        a, b = make_service(), make_service()
        a.create_project("alice", "web", "A")
        b.create_project("alice", "web", "B")  # no AlreadyExists: separate instances
        a.create_issue("alice", "web", "only in a")
        assert b.list_issues("alice", "web")["items"] == []
