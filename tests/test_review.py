"""Offline tests for the M4 approval gate and draft PRs: git and GitHub are
faked, nothing touches the network or a real repository."""

import logging
import socket
import subprocess
import sys

import pytest
import requests

_real_subprocess_run = subprocess.run  # captured at import, before no_network patches it

from agent import config, pr, review
from agent.proposals import ProposalError, ProposalStore

TOKEN = "ghp_SECRETtoken0123456789abcdefghijklmnop"
DIFF = (
    "--- a/library/tax.py\n+++ b/library/tax.py\n@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n-    return a - b\n+    return a + b\n"
)
REPORT = {
    "suspected_files": ["library/tax.py"], "root_cause": "add() subtracts instead of adding",
    "failing_tests": [], "evidence": [{"file": "library/tax.py", "quote": "return a - b"}],
    "confidence": "high",
}
APPROVE = {"decision": "approve", "reasons": ["tests pass, minimal diff"], "required_changes": []}
REQUEST = {"decision": "request_changes", "reasons": ["off by one"], "required_changes": ["use <="]}
ESCALATE = {"decision": "escalate", "reasons": ["Should rounding be half-up as the issue says, or banker's as the tests expect?"],
            "required_changes": []}


def save(store, stop_reason="approved", decisions=(REQUEST, APPROVE), attempt="BUG-001", **overrides):
    fields = dict(
        run_id="20261030_120000", attempt=attempt, bug_id="BUG-001", category="off-by-one",
        model="claude-haiku-5-5", reviewer_version="v2", stop_reason=stop_reason,
        changed_files=["library/tax.py"], diff=DIFF, research=REPORT, review_decisions=list(decisions),
        tests_fixed=True, tests_output="....\n========= 15 passed in 0.31s =========\n", tests_tampered=False,
        estimated_cost_usd=0.0123456,
    )
    fields.update(overrides)
    return store.save(**fields)


@pytest.fixture
def store(tmp_path):
    return ProposalStore(tmp_path / "proposals.sqlite")


@pytest.fixture
def settings(tmp_path):
    return pr.PRSettings(repo="octo/zoo", token=TOKEN, clone_path=tmp_path / "clone")


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    """Any real network or subprocess use fails the test outright."""
    def boom(*a, **k):
        raise AssertionError("network/subprocess access attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(pr, "PATCH_DIR", tmp_path / "patches")


class FakeGit:
    def __init__(self, branches=(), status=""):
        self.calls: list[list[str]] = []
        self.branches = set(branches)
        self.status = status
        self.fail_on: str | None = None

    def __call__(self, args, cwd):
        self.calls.append(args)
        if self.fail_on and args[0] == self.fail_on:
            raise pr.PRError(f"git {args[0]} failed")
        if args[:2] == ["status", "--porcelain"]:
            return self.status
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return "main\n"
        if args[:2] == ["branch", "--list"]:
            return f"  {args[2]}\n" if args[2] in self.branches else ""
        if args[:2] == ["checkout", "-b"]:
            self.branches.add(args[2])
        if args[:2] == ["rev-list", "--count"]:
            return "1\n"
        return ""

    def ran(self, verb):
        return [c for c in self.calls if c[0] == verb]


class FakeGitHub:
    def __init__(self, existing=None):
        self.existing = existing
        self.created: list[dict] = []

    def find_pr(self, repo, branch):
        return self.existing

    def create_draft_pr(self, repo, *, title, head, base, body):
        self.created.append(dict(repo=repo, title=title, head=head, base=base, body=body))
        self.existing = f"https://github.com/{repo}/pull/{len(self.created)}"
        return self.existing


# --- persistence and decisions --------------------------------------------

def test_status_persists_across_new_store_and_connection(tmp_path):
    db = tmp_path / "proposals.sqlite"
    pid = save(ProposalStore(db))
    ProposalStore(db).decide(pid, "approved", "alice")

    p = ProposalStore(db).get(pid)  # fresh object, fresh connection
    assert (p["status"], p["decided_by"]) == ("approved", "alice")
    assert p["decided_at"]
    assert p["review_decisions"] == [REQUEST, APPROVE]
    assert p["research"] == REPORT
    assert p["tests_summary"] == "15 passed in 0.31s"


def test_status_persists_across_a_new_process(tmp_path):
    db = tmp_path / "proposals.sqlite"
    pid = save(ProposalStore(db))
    ProposalStore(db).decide(pid, "rejected", "bob", "wrong file")
    code = (
        "import sys; from agent.proposals import ProposalStore; "
        f"p = ProposalStore(sys.argv[1]).get({pid}); print(p['status'], p['decided_by'], p['decision_reason'])"
    )
    out = _real_subprocess_run(
        [sys.executable, "-c", code, str(db)], cwd=config.ROOT, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "rejected bob wrong file"


def test_only_approved_or_escalated_runs_become_proposals(store):
    with pytest.raises(ProposalError):
        save(store, stop_reason="rejected")


def test_escalated_proposal_needs_decision_and_shows_question(store, capsys):
    pid = save(store, stop_reason="escalated", decisions=[ESCALATE], tests_fixed=False)
    p = store.get(pid)
    assert p["needs_decision"] and p["status"] == "pending"
    assert p["reviewer_question"] == ESCALATE["reasons"][0]

    review.main(["--db", str(store.path), "show", str(pid)])
    out = capsys.readouterr().out
    assert "NEEDS DECISION" in out and "banker's" in out

    with pytest.raises(ProposalError, match="reason"):
        store.decide(pid, "approved", "alice")  # answering the question needs a reason


def test_reject_needs_reason_and_decisions_are_final(store):
    pid = save(store)
    with pytest.raises(ProposalError):
        store.decide(pid, "rejected", "alice", "")
    store.decide(pid, "rejected", "alice", "touches unrelated code")
    with pytest.raises(ProposalError, match="already rejected"):
        store.decide(pid, "approved", "bob")


def test_show_prints_diff_and_reviewer_reasons(store, capsys):
    pid = save(store)
    assert review.main(["--db", str(store.path), "show", str(pid)]) == 0
    out = capsys.readouterr().out
    assert "+    return a + b" in out
    assert "reason: off by one" in out and "reason: tests pass, minimal diff" in out
    assert "required: use <=" in out


def test_cli_approve_records_who(store, capsys):
    pid = save(store)
    assert review.main(["--db", str(store.path), "approve", str(pid), "--by", "carol"]) == 0
    assert store.get(pid)["decided_by"] == "carol"
    review.main(["--db", str(store.path), "list"])
    assert "approved" in capsys.readouterr().out


# --- the PR gate -------------------------------------------------------------

@pytest.mark.parametrize("state", ["pending", "rejected", "escalated"])
def test_pr_refuses_unapproved(store, settings, state, capsys):
    pid = save(store, stop_reason="escalated" if state == "escalated" else "approved",
               decisions=[ESCALATE] if state == "escalated" else [APPROVE])
    if state == "rejected":
        store.decide(pid, "rejected", "alice", "no")
    git, gh = FakeGit(), FakeGitHub()

    with pytest.raises(pr.PRError, match="only approved"):
        pr.open_draft_pr(store, pid, settings, git=git, github=gh)
    assert git.calls == [] and gh.created == []

    # the CLI refuses too, dry run included
    for argv in (["pr", str(pid)], ["pr", str(pid), "--dry-run"]):
        assert review.main(["--db", str(store.path), *argv]) == 1
    assert "only approved" in capsys.readouterr().err


def test_pr_refuses_test_changes(store, settings):
    pid = save(store, changed_files=["tests/test_tax.py"])
    store.decide(pid, "approved", "alice")
    with pytest.raises(pr.PRError, match="tests/"):
        pr.open_draft_pr(store, pid, settings, git=FakeGit(), github=FakeGitHub())


def test_pr_happy_path_opens_one_draft_and_stores_url(store, settings):
    pid = save(store)
    store.decide(pid, "approved", "alice")
    git, gh = FakeGit(), FakeGitHub()

    url = pr.open_draft_pr(store, pid, settings, git=git, github=gh, echo=lambda m: None)

    assert url == "https://github.com/octo/zoo/pull/1"
    assert store.get(pid)["pr_url"] == url
    assert store.get(pid)["pr_branch"] == "fix/BUG-001-20261030_120000"
    assert gh.created[0]["head"] == "fix/BUG-001-20261030_120000" and gh.created[0]["base"] == "main"
    assert ["checkout", "-b", "fix/BUG-001-20261030_120000", "main"] in git.calls
    assert any(c[:2] == ["apply", "--cached"] for c in git.calls)
    assert git.ran("commit") and git.ran("push")
    assert git.calls[-1] == ["push", "-u", "origin", "fix/BUG-001-20261030_120000"]


def test_pr_is_idempotent(store, settings):
    pid = save(store)
    store.decide(pid, "approved", "alice")
    git, gh = FakeGit(), FakeGitHub()
    first = pr.open_draft_pr(store, pid, settings, git=git, github=gh, echo=lambda m: None)
    calls_after_first = len(git.calls)

    second = pr.open_draft_pr(store, pid, settings, git=git, github=gh, echo=lambda m: None)

    assert first == second
    assert len(gh.created) == 1
    assert len(git.calls) == calls_after_first  # stored URL short-circuits before any git


def test_pr_recovers_after_crash_without_duplicates(store, settings):
    """Branch committed and PR opened, but the URL was never stored (crash):
    a retry reuses the branch and adopts the existing PR."""
    pid = save(store)
    store.decide(pid, "approved", "alice")
    git = FakeGit(branches={"fix/BUG-001-20261030_120000"})
    gh = FakeGitHub(existing="https://github.com/octo/zoo/pull/7")

    url = pr.open_draft_pr(store, pid, settings, git=git, github=gh, echo=lambda m: None)

    assert url == "https://github.com/octo/zoo/pull/7"
    assert gh.created == []
    assert not git.ran("commit") and not git.ran("apply")
    assert store.get(pid)["pr_url"] == url


def test_failed_apply_removes_the_half_made_branch(store, settings):
    pid = save(store)
    store.decide(pid, "approved", "alice")
    git = FakeGit()
    git.fail_on = "apply"

    with pytest.raises(pr.PRError):
        pr.open_draft_pr(store, pid, settings, git=git, github=FakeGitHub(), echo=lambda m: None)
    assert ["branch", "-D", "fix/BUG-001-20261030_120000"] in git.calls
    assert not git.ran("push")
    assert store.get(pid)["pr_url"] is None


def test_pr_refuses_dirty_clone(store, settings):
    pid = save(store)
    store.decide(pid, "approved", "alice")
    git = FakeGit(status=" M library/tax.py\n")
    with pytest.raises(pr.PRError, match="uncommitted"):
        pr.open_draft_pr(store, pid, settings, git=git, github=FakeGitHub())
    assert git.calls == [["status", "--porcelain"]]


# --- dry run -----------------------------------------------------------------

def test_dry_run_makes_no_network_or_git_calls(store, monkeypatch, capsys, tmp_path):
    pid = save(store)
    store.decide(pid, "approved", "alice")
    monkeypatch.setattr(config, "GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr(config, "GITHUB_REPO", "octo/zoo")
    monkeypatch.setattr(config, "TARGET_REPO_PATH", str(tmp_path / "clone"))
    monkeypatch.setattr(pr, "GitRunner", lambda: pytest.fail("dry run built a git runner"))
    monkeypatch.setattr(pr, "GitHubClient", lambda *a, **k: pytest.fail("dry run built a GitHub client"))

    assert review.main(["--db", str(store.path), "pr", str(pid), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "git -C" in out and "checkout -b fix/BUG-001-20261030_120000 main" in out
    assert "POST https://api.github.com/repos/octo/zoo/pulls" in out and "draft=true" in out
    assert TOKEN not in out
    assert not pr.PATCH_DIR.joinpath(f"proposal_{pid}.patch").exists()  # no files written either
    assert store.get(pid)["pr_url"] is None


# --- token hygiene -----------------------------------------------------------

class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, **kwargs):
        self.requests.append(dict(method=method, url=url, **kwargs))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_token_never_appears_in_output_or_logs(store, settings, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    pid = save(store)
    store.decide(pid, "approved", "alice")
    session = FakeSession([
        FakeResponse(200, []),  # no existing PR
        FakeResponse(201, {"html_url": "https://github.com/octo/zoo/pull/3"}),
    ])
    gh = pr.GitHubClient(TOKEN, session=session)

    url = pr.open_draft_pr(store, pid, settings, git=FakeGit(), github=gh)

    assert url.endswith("/pull/3")
    post = session.requests[1]
    assert post["json"]["draft"] is True
    assert post["headers"]["Authorization"] == f"Bearer {TOKEN}"  # used, only here
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err + caplog.text
    assert TOKEN not in str(store.get(pid))


@pytest.mark.parametrize("failure", [
    FakeResponse(401, text=f"Bad credentials for {TOKEN}"),
    requests.ConnectionError(f"https://x-access-token:{TOKEN}@api.github.com unreachable"),
])
def test_token_redacted_from_errors(store, settings, capsys, failure):
    pid = save(store)
    store.decide(pid, "approved", "alice")
    gh = pr.GitHubClient(TOKEN, session=FakeSession([failure]))

    with pytest.raises(pr.PRError) as exc:
        pr.open_draft_pr(store, pid, settings, git=FakeGit(), github=gh)
    assert TOKEN not in str(exc.value) and "***" in str(exc.value)
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


# --- PR body -------------------------------------------------------------------

def test_pr_body_has_required_sections_and_no_issue_text(store):
    pid = save(store, attempt="BUG-014", bug_id="BUG-014", category="prompt-injection")
    store.decide(pid, "approved", "alice")
    body = pr.build_pr_body(store.get(pid))

    assert body.startswith("**BUG-014** -- Fixes the prompt-injection bug seeded as BUG-014")
    assert "add() subtracts instead of adding" in body  # root cause
    assert "tests pass, minimal diff" in body and "off by one" in body  # reviewer reasons
    assert "**PASS**" in body and "15 passed in 0.31s" in body  # test results
    assert "$0.012346" in body and "ESTIMATED" in body  # cost

    issue = (config.ROOT / "bugzoo" / "bugs" / "BUG-014" / "issue.md").read_text(encoding="utf-8")
    for line in issue.splitlines():
        if len(line.strip()) > 20:
            assert line.strip() not in body


def test_pr_body_fences_model_text(store):
    sneaky = "@everyone see [link](http://evil) ~~~\n# heading"
    pid = save(store, research={**REPORT, "root_cause": sneaky})
    store.decide(pid, "approved", "alice")
    body = pr.build_pr_body(store.get(pid))
    assert "~~~~text\n" + sneaky + "\n~~~~" in body
