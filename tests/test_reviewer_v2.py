"""Reviewer v2 (FINDINGS.md F7-F9) and the --reviewer v1|v2 switch. Offline:
the same scripted fake model and stubbed sandbox as tests/test_graph.py."""

import sys

import pytest
from pydantic import ValidationError

from agent import run as run_cli
from agent.handoffs import ReviewDecision, ReviewDecisionV1, handoff_tool_definition, references_tests_path
from agent.roles import REVIEWER_PROMPT_V1, REVIEWER_PROMPT_V2, is_under_tests
from tests.test_graph import (  # noqa: F401  (env, no_docker are fixtures)
    APPROVE,
    CHANGES,
    REPORT,
    FakeModel,
    env,
    events,
    no_docker,
    run,
    tool,
)

TEST_EDIT = {"path": "tests/test_tax.py", "old_string": "== 3", "new_string": "== -1"}
ESCALATE = {"decision": "escalate", "reasons": ["test expects 15, issue implies 16"], "required_changes": []}
TESTS_REQUEST = {
    "decision": "request_changes",
    "reasons": ["test looks stale"],
    "required_changes": ["Update tests/test_tax.py to expect -1"],
}
LIBRARY_REQUEST = {
    "decision": "request_changes",
    "reasons": ["test_add fails"],
    "required_changes": ["Fix library/tax.py so test_add passes"],
}


def _test_file(env) -> str:
    return (env.workspace / "tests" / "test_tax.py").read_text(encoding="utf-8")


# --- (3a) Coder cannot edit tests/ under v2 --------------------------------

@pytest.mark.parametrize(
    "path", ["tests/test_tax.py", "./tests/test_tax.py", "tests\\test_tax.py", "Tests/test_tax.py"]
)
def test_v2_coder_edit_under_tests_is_rejected_and_logged(env, path):
    before = _test_file(env)
    script = {
        "researcher": [[tool("submit_research_report", **REPORT)]],
        "coder": [[tool("edit_file", **dict(TEST_EDIT, path=path))], [tool("submit_changes", **CHANGES)]],
        "reviewer": [[tool("submit_review", **APPROVE)]],
    }

    result = run(env, FakeModel(script))

    assert _test_file(env) == before
    assert result.usage["coder"]["rejected_calls"] == 1
    rejected = events(env, "tool_call_rejected")
    assert [(r["agent"], r["tool"]) for r in rejected] == [("coder", "edit_file")]
    assert "tests are the specification" in rejected[0]["reason"]


def test_v2_coder_can_still_edit_library(env):
    script = {
        "researcher": [[tool("submit_research_report", **REPORT)]],
        "coder": [[tool("edit_file", path="library/tax.py", old_string="a - b", new_string="a + b")],
                  [tool("submit_changes", **CHANGES)]],
        "reviewer": [[tool("submit_review", **APPROVE)]],
    }
    result = run(env, FakeModel(script))
    assert result.usage["coder"]["rejected_calls"] == 0
    assert "a + b" in (env.workspace / "library" / "tax.py").read_text(encoding="utf-8")


def test_v1_coder_edit_under_tests_still_allowed(env):
    # v1 must reproduce the M3 baseline, where this tampering happened (F8).
    script = {
        "researcher": [[tool("submit_research_report", **REPORT)]],
        "coder": [[tool("edit_file", **TEST_EDIT)], [tool("submit_changes", **CHANGES)]],
        "reviewer": [[tool("submit_review", **APPROVE)]],
    }
    run(env, FakeModel(script), reviewer="v1")
    assert "== -1" in _test_file(env)


def test_is_under_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "library").mkdir()
    assert is_under_tests(tmp_path, "tests/test_x.py")
    assert is_under_tests(tmp_path, "tests")
    assert not is_under_tests(tmp_path, "library/tests_helper.py")
    assert not is_under_tests(tmp_path, "library/x.py")
    assert not is_under_tests(tmp_path, "../tests/x.py")  # escape: dispatch_tool rejects it


# --- (3b) ReviewDecision: required_changes may not reference tests/ --------

@pytest.mark.parametrize(
    "change",
    [
        "Update tests/test_dates.py to expect 16",
        "edit `tests\\test_dates.py`",
        "change ./tests/test_discounts.py",
        "Fix the assertion in test_discounts.py",
        "Update TESTS/test_dates.py",
    ],
)
def test_v2_decision_rejects_required_changes_referencing_tests(change):
    with pytest.raises(ValidationError, match="must not reference tests/"):
        ReviewDecision.model_validate({"decision": "request_changes", "reasons": ["x"], "required_changes": [change]})


@pytest.mark.parametrize(
    "change",
    [
        "Fix library/dates.py so test_to_local_converts_utc_to_state_offset passes",
        "Make the failing tests pass without touching unrelated code",
        "Remove the unrelated edit to library/pytests_util.py",
    ],
)
def test_v2_decision_allows_library_changes_naming_test_functions(change):
    ReviewDecision.model_validate({"decision": "request_changes", "reasons": ["x"], "required_changes": [change]})
    assert not references_tests_path(change)


def test_v1_decision_unchanged():
    ReviewDecisionV1.model_validate(TESTS_REQUEST)  # v1 never checked this
    with pytest.raises(ValidationError):
        ReviewDecisionV1.model_validate(ESCALATE)


def test_tests_reference_retries_once_then_stops(env):
    script = {
        "researcher": [[tool("submit_research_report", **REPORT)]],
        "coder": [[tool("submit_changes", **CHANGES)]],
        "reviewer": [[tool("submit_review", **TESTS_REQUEST)], [tool("submit_review", **TESTS_REQUEST)]],
    }
    model = FakeModel(script)

    result = run(env, model)

    assert result.stop_reason == "invalid_handoff"
    assert result.stop_agent == "reviewer"
    assert result.review_decisions == []
    assert model.calls == {"researcher": 1, "coder": 1, "reviewer": 2}
    invalid = events(env, "invalid_handoff")
    assert [e["attempt"] for e in invalid] == [1, 2]
    assert all("must not reference tests/" in e["reason"] for e in invalid)


def test_tests_reference_retry_can_succeed(env):
    script = {
        "researcher": [[tool("submit_research_report", **REPORT)]],
        "coder": [[tool("submit_changes", **CHANGES)]] * 2,
        "reviewer": [[tool("submit_review", **TESTS_REQUEST)], [tool("submit_review", **LIBRARY_REQUEST)],
                     [tool("submit_review", **APPROVE)]],
    }
    result = run(env, FakeModel(script))
    assert result.stop_reason == "approved"
    assert [d["decision"] for d in result.review_decisions] == ["request_changes", "approve"]


# --- escalate ----------------------------------------------------------------

def test_escalate_stops_graph(env):
    script = {
        "researcher": [[tool("submit_research_report", **REPORT)]],
        "coder": [[tool("submit_changes", **CHANGES)]],
        "reviewer": [[tool("submit_review", **ESCALATE)]],
    }
    model = FakeModel(script)

    result = run(env, model)

    assert result.stop_reason == "escalated"
    assert result.stop_agent == "reviewer"
    assert result.reviewer_decision == "escalate"
    assert result.revisions == 0
    assert model.calls["coder"] == 1  # no revision round after an escalation


def test_v1_cannot_escalate(env):
    script = {
        "researcher": [[tool("submit_research_report", **REPORT)]],
        "coder": [[tool("submit_changes", **CHANGES)]],
        "reviewer": [[tool("submit_review", **ESCALATE)]] * 2,
    }
    result = run(env, FakeModel(script), reviewer="v1")
    assert result.stop_reason == "invalid_handoff"


# --- (2) prompt v2 and the tool schema the model sees ------------------------

def test_v2_prompt_removes_test_is_wrong_exception():
    assert "unless you can show from the code that the failing test itself is wrong" in REVIEWER_PROMPT_V1
    assert "unless" not in REVIEWER_PROMPT_V2
    for phrase in ("specification", "non-blocking", "escalate", "Never request changes to tests/"):
        assert phrase in REVIEWER_PROMPT_V2


def test_review_tool_schema_per_version():
    def enum(model):
        return handoff_tool_definition("submit_review", "d", model)["input_schema"]["properties"]["decision"]["enum"]

    assert enum(ReviewDecisionV1) == ["approve", "request_changes", "reject"]
    assert enum(ReviewDecision) == ["approve", "request_changes", "reject", "escalate"]


def test_each_version_gets_its_own_prompt(env):
    script = {
        "researcher": [[tool("submit_research_report", **REPORT)]],
        "coder": [[tool("submit_changes", **CHANGES)]],
        "reviewer": [[tool("submit_review", **APPROVE)]],
    }
    model = FakeModel(script)
    result = run(env, model, reviewer="v1")
    assert model.systems["reviewer"] == [REVIEWER_PROMPT_V1]
    assert result.reviewer_version == "v1"


def test_resume_keeps_the_threads_reviewer_version(env):
    script = {
        "researcher": [[tool("submit_research_report", **REPORT)]],
        "coder": [[tool("submit_changes", **CHANGES)]],
    }
    with pytest.raises(RuntimeError):
        run(env, FakeModel(script, raise_for={"reviewer"}), reviewer="v1")

    resumed = FakeModel({"reviewer": [[tool("submit_review", **APPROVE)]]})
    result = run(env, resumed, resume=True, reviewer="v2")  # caller's v2 is ignored on resume

    assert resumed.systems["reviewer"] == [REVIEWER_PROMPT_V1]
    assert result.reviewer_version == "v1"


# --- (4) CLI: --reviewer and report filenames --------------------------------

def test_report_filenames_include_mode_and_reviewer():
    assert run_cli.report_filename("baseline", "single", None, "m", "id") == "baseline_single_m_id.json"
    assert run_cli.report_filename("baseline", "multi", "v2", "m", "id") == "baseline_multi_v2_m_id.json"
    assert run_cli.report_filename("repeats", "multi", "v1", "m", "id", 3) == "repeats_multi_v1_m_3x_id.json"


def test_reviewer_flag_rejected_in_single_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agent.run", "--bug", "BUG-001", "--reviewer", "v1"])
    assert run_cli.main() == 1


def test_reviewer_flag_choices(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agent.run", "--mode", "multi", "--bug", "BUG-001", "--reviewer", "v3"])
    with pytest.raises(SystemExit):
        run_cli.main()
