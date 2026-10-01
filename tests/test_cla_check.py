"""Unit tests for .github/scripts/cla_check.py (the CLA pull-request check)."""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "cla_check.py"
_spec = importlib.util.spec_from_file_location("cla_check", _SCRIPT)
cla = importlib.util.module_from_spec(_spec)
sys.modules["cla_check"] = cla  # dataclasses resolve their module by name
_spec.loader.exec_module(cla)

OPENER = cla.Person("alice", 1001)
MAINTAINER = cla.Person("debpalash", 4178343)


def actor(login=None, id=None, name="Someone", email="someone@example.com"):
    return {"name": name, "email": email, "login": login, "id": id}


# ── Signing comments ────────────────────────────────────────────────────


@pytest.mark.parametrize("body", [
    cla.SIGN_PHRASE,
    cla.SIGN_PHRASE + "\n",
    "  " + cla.SIGN_PHRASE.upper() + "  ",
    "Thanks!\n\n" + cla.SIGN_PHRASE + "\n\nCheers",
    cla.SIGN_PHRASE.replace(" ", "  "),
])
def test_sign_phrase_is_recognised_on_its_own_line(body):
    assert cla.is_sign_comment(body)


@pytest.mark.parametrize("body", [
    "> " + cla.SIGN_PHRASE,  # quoting the bot is not signing
    "I will sign: " + cla.SIGN_PHRASE,
    cla.SIGN_PHRASE.replace("1.0", "0.9"),
    "",
    None,
])
def test_quotes_and_other_text_are_not_signatures(body):
    assert not cla.is_sign_comment(body)


# ── Who must sign ───────────────────────────────────────────────────────


def test_signed_opener_and_authors_pass():
    result = cla.evaluate(OPENER, False, [actor("alice", 1001), actor("bob", 1002)], {1001, 1002})
    assert result.passed


def test_unsigned_linked_author_is_listed_once():
    result = cla.evaluate(OPENER, False, [actor("bob", 1002), actor("bob", 1002)], {1001})
    assert result.unsigned == [cla.Person("bob", 1002)]


def test_opener_must_sign_even_without_commits():
    result = cla.evaluate(OPENER, False, [], set())
    assert result.unsigned == [OPENER]


def test_git_name_cannot_impersonate_the_maintainer():
    # Unlinked email with the maintainer's name: matched by account, not name.
    spoof = actor(name="debpalash", email="someone@gmail.com")
    result = cla.evaluate(OPENER, False, [spoof], {1001})
    assert result.unknown == ["debpalash <someone@gmail.com>"]


def test_maintainer_and_bots_are_exempt_by_id():
    actors = [actor("debpalash", 4178343), actor("dependabot[bot]", 49699333)]
    assert cla.evaluate(MAINTAINER, False, actors, set()).passed


def test_noreply_email_maps_to_account_id():
    result = cla.evaluate(OPENER, False, [actor(email="2002+carol@users.noreply.github.com")], {1001})
    assert result.unsigned == [cla.Person("carol", 2002)]


def test_ai_tool_co_authors_are_skipped_but_people_are_not():
    actors = cla.co_authors(
        "fix: thing\n\nCo-authored-by: Cursor Agent <cursoragent@cursor.com>\n"
        "Co-authored-by: Dave <3003+dave@users.noreply.github.com>\n"
    )
    result = cla.evaluate(OPENER, False, actors, {1001})
    assert result.unsigned == [cla.Person("dave", 3003)] and not result.unknown


def test_bot_opened_pr_skips_bot_commits_but_not_people():
    bot = cla.Person("dependabot[bot]", 49699333)
    actors = [actor(email="49699333+dependabot[bot]@users.noreply.github.com"), actor("erin", 4004)]
    result = cla.evaluate(bot, True, actors, set())
    assert result.unsigned == [cla.Person("erin", 4004)]


def test_unknown_identity_fails_the_check():
    result = cla.evaluate(OPENER, False, [actor(name="mergetest", email="test@local")], {1001})
    assert not result.passed and result.unknown == ["mergetest <test@local>"]


# ── Store and comment ───────────────────────────────────────────────────


def test_signatures_are_added_once_per_account():
    store = cla.empty_store()
    assert cla.add_signatures(store, [{"id": 1, "login": "a"}])
    assert not cla.add_signatures(store, [{"id": 1, "login": "a-renamed"}])
    assert [s["id"] for s in store["signatures"]] == [1]


def test_comment_renders_untrusted_identities_as_inert_code():
    evaluation = cla.Evaluation(unknown=["[click](https://evil.example) @everyone <a`b@x.test>"])
    body = cla.render_comment(evaluation, "https://example.test/cla")
    line = next(l for l in body.splitlines() if "evil" in l)
    assert line.startswith("- `") and line.count("`") == 2
    assert cla.SIGN_PHRASE not in body  # nobody listed as unsigned → no sign instructions


# ── End-to-end with a fake GitHub ───────────────────────────────────────


class FakeGitHub:
    repo = "debpalash/VoiceStudio"

    def __init__(self, comments, actors, store=None, pr=None, others=None):
        self.comments, self.actors = comments, actors
        self.pr, self.others = pr or {}, others or {}
        self.files = {} if store is None else {cla.SIGNATURE_PATH: store}
        self.branch = store is not None
        self.statuses, self.posted, self.patched = [], [], []

    def get(self, path):
        assert path.endswith("/pulls/7")
        return {"state": "open", "user": {"login": "alice", "id": 1001, "type": "User"},
                "head": {"sha": "abc"}, "base": {"repo": {"id": 99, "default_branch": "main"}}, **self.pr}

    def paginate(self, path, limit=None):
        return self.comments

    def graphql(self, query, variables):
        nodes = [{"commit": {"authors": {"nodes": [
            {"name": a["name"], "email": a["email"],
             "user": {"login": a["login"], "databaseId": a["id"]} if a["id"] else None}]}}} for a in self.actors]
        return {"repository": {"pullRequest": {"commits": {
            "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}

    def request(self, method, path, body=None):
        if method == "GET" and "/pulls/" in path:
            number = int(path.rsplit("/", 1)[1])
            return (200, {"user": self.others[number]}, {}) if number in self.others else (404, None, {})
        if "/contents/" in path and method == "GET":
            if cla.SIGNATURE_PATH not in self.files:
                return 404, None, {}
            raw = base64.b64encode(json.dumps(self.files[cla.SIGNATURE_PATH]).encode()).decode()
            return 200, {"content": raw, "sha": "s1"}, {}
        if "/contents/" in path and method == "PUT":
            self.files[cla.SIGNATURE_PATH] = json.loads(base64.b64decode(body["content"]))
            return 201, {}, {}
        if "/git/ref/heads/" in path:
            return (200 if self.branch else 404), {}, {}
        if path.endswith("/git/trees"):
            return 201, {"sha": "t"}, {}
        if path.endswith("/git/commits"):
            assert body["parents"] == []
            return 201, {"sha": "c"}, {}
        if path.endswith("/git/refs"):
            self.branch = True
            return 201, {}, {}
        if "/statuses/" in path:
            self.statuses.append(body)
            return 201, {}, {}
        if method == "POST" and path.endswith("/comments"):
            self.posted.append(body["body"])
            return 201, {}, {}
        if method == "PATCH":
            self.patched.append(body["body"])
            return 200, {}, {}
        raise AssertionError(f"unexpected {method} {path}")


def test_first_signature_creates_the_store_and_turns_the_status_green():
    comments = [{"id": 5, "user": {"id": 1001, "login": "alice", "type": "User"},
                 "body": cla.SIGN_PHRASE, "created_at": "2026-10-02T00:00:00Z"}]
    gh = FakeGitHub(comments, [actor("alice", 1001)])
    result = cla.run(gh, "issue_comment", {"issue": {"number": 7}})
    assert result.passed and gh.branch
    saved = gh.files[cla.SIGNATURE_PATH]["signatures"]
    assert len(saved) == 1 and len(saved[0].pop("body_sha256")) == 64
    assert saved == [{"login": "alice", "id": 1001, "pull_request": 7, "comment_id": 5,
                      "signed_at": "2026-10-02T00:00:00Z", "version": cla.CLA_VERSION, "repo_id": 99}]
    assert gh.statuses[-1]["state"] == "success" and gh.statuses[-1]["context"] == "CLA"
    assert not gh.posted  # nothing to ask for, so no new comment


def test_unsigned_pr_fails_and_asks_once():
    gh = FakeGitHub([], [actor("alice", 1001)], store=cla.empty_store())
    result = cla.run(gh, "pull_request_target", {"pull_request": {"number": 7}})
    assert not result.passed and gh.statuses[-1]["state"] == "failure"
    assert len(gh.posted) == 1 and "@alice" in gh.posted[0]


def test_someone_elses_sign_comment_does_not_count():
    comments = [{"id": 5, "user": {"id": 2002, "login": "mallory", "type": "User"},
                 "body": cla.SIGN_PHRASE, "created_at": "2026-10-02T00:00:00Z"}]
    gh = FakeGitHub(comments, [actor("alice", 1001)], store=cla.empty_store())
    assert not cla.run(gh, "issue_comment", {"issue": {"number": 7}}).passed
    assert gh.files[cla.SIGNATURE_PATH]["signatures"] == []


def _comment(user_id=1001, login="alice", body=None, edited=False, user_type="User", cid=5):
    created = "2026-10-02T00:00:00Z"
    return {"id": cid, "user": {"id": user_id, "login": login, "type": user_type},
            "body": cla.SIGN_PHRASE if body is None else body,
            "created_at": created, "updated_at": "2026-10-03T00:00:00Z" if edited else created}


def test_edited_sign_comments_do_not_count():
    gh = FakeGitHub([_comment(edited=True)], [actor("alice", 1001)], store=cla.empty_store())
    assert not cla.run(gh, "issue_comment", {"issue": {"number": 7}}).passed
    assert gh.files[cla.SIGNATURE_PATH]["signatures"] == []


def test_stored_signature_records_a_hash_of_the_comment():
    gh = FakeGitHub([_comment()], [actor("alice", 1001)], store=cla.empty_store())
    cla.run(gh, "issue_comment", {"issue": {"number": 7}})
    stored = gh.files[cla.SIGNATURE_PATH]["signatures"][0]
    assert len(stored["body_sha256"]) == 64 and stored["pull_request"] == 7


@pytest.mark.parametrize("email", [
    "cline@openai.com", "codex@openai.com", "175728472+Copilot@users.noreply.github.com",
    "cursoragent@cursor.com", "41898282+github-actions[bot]@users.noreply.github.com",
])
def test_agent_identities_never_need_to_sign(email):
    linked = actor("Copilot", 175728472, email=email) if "Copilot" in email else actor(email=email)
    assert cla.evaluate(OPENER, False, [linked], {1001}).passed


def test_superseded_pull_request_authors_must_sign():
    assert cla.superseded_numbers("Consolidates fixes.\nSupersedes #12, #15 and #12.\nCloses #99") == [12, 15]


def _issue_event(labels):
    return {"issue": {"number": 42, "labels": [{"name": l} for l in labels]}, "repository": {"id": 99}}


def _with_reactions(gh):
    gh.reactions = []
    original = gh.request

    def request(method, path, body=None):
        if path.endswith("/reactions"):
            gh.reactions.append(path)
            return 201, {}, {}
        return original(method, path, body)

    gh.request = request
    return gh


def test_past_contributors_sign_on_a_labelled_issue_and_none_are_lost():
    comments = [_comment(270455167, "velixio", cid=1), _comment(2002, "carol", cid=2),
                _comment(3003, "dave", body="> " + cla.SIGN_PHRASE, cid=3),
                _comment(4004, "erin", edited=True, cid=4), _comment(5005, "bot", user_type="Bot", cid=5)]
    gh = _with_reactions(FakeGitHub(comments, [], store=cla.empty_store()))
    assert cla.sign_on_issue(gh, _issue_event(["cla"])) == 2
    stored = gh.files[cla.SIGNATURE_PATH]["signatures"]
    assert [s["id"] for s in stored] == [270455167, 2002] and stored[0]["issue"] == 42
    assert len(gh.reactions) == 2
    assert cla.sign_on_issue(gh, _issue_event(["cla"])) == 0  # idempotent


def test_unlabelled_issues_record_nothing():
    gh = FakeGitHub([_comment()], [], store=cla.empty_store())
    assert cla.sign_on_issue(gh, _issue_event([])) == 0
    assert gh.files[cla.SIGNATURE_PATH]["signatures"] == []


def test_superseding_pr_waits_for_the_original_authors():
    others = {12: {"login": "frank", "id": 6006, "type": "User"},
              13: {"login": "dependabot[bot]", "id": 49699333, "type": "Bot"}}
    gh = FakeGitHub([], [actor("alice", 1001)], store=cla.empty_store(),
                    pr={"body": "Supersedes #12 and #13"}, others=others)
    result = cla.run(gh, "pull_request_target", {"pull_request": {"number": 7}})
    assert result.unsigned == [cla.Person("alice", 1001), cla.Person("frank", 6006)]


def test_maintainer_override_label_passes_without_checking():
    gh = FakeGitHub([], [actor(email="you@example.com")], store=cla.empty_store(),
                    pr={"labels": [{"name": cla.OVERRIDE_LABEL}]})
    assert cla.run(gh, "pull_request_target", {"pull_request": {"number": 7}}).passed
    assert gh.statuses[-1]["state"] == "success" and "maintainer" in gh.statuses[-1]["description"]
