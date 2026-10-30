import importlib.util
import json
from pathlib import Path

import pytest

from agent.aggregate import aggregate_bug_repeats, aggregate_overall, count_stop_reasons, format_counts


def _attempt(
    fixed: bool,
    tool_calls: int,
    cost: float,
    wall_time: float,
    tampered: bool = False,
    injection: bool = False,
    stop_reason: str = "finish",
) -> dict:
    return {
        "stop_reason": stop_reason,
        "fixed": fixed,
        "tool_calls": tool_calls,
        "estimated_cost_usd": cost,
        "wall_time_s": wall_time,
        "tests_tampered": tampered,
        "injection_flag": injection,
    }


def test_aggregate_bug_repeats_computes_counts_and_rate():
    attempts = [
        _attempt(True, 5, 0.0010, 5.0),
        _attempt(True, 7, 0.0020, 6.0),
        _attempt(False, 6, 0.0015, 5.5),
    ]
    result = aggregate_bug_repeats("BUG-010", "multi-file", attempts)

    assert result["id"] == "BUG-010"
    assert result["category"] == "multi-file"
    assert result["repeats"] == 3
    assert result["fixed_count"] == 2
    assert result["fix_rate"] == pytest.approx(2 / 3)


def test_aggregate_bug_repeats_computes_mean_min_max():
    attempts = [
        _attempt(True, 5, 0.0010, 5.0),
        _attempt(True, 7, 0.0020, 6.0),
        _attempt(False, 6, 0.0015, 5.5),
    ]
    result = aggregate_bug_repeats("BUG-010", "multi-file", attempts)

    assert result["tool_calls_mean"] == pytest.approx(6.0)
    assert result["tool_calls_min"] == 5
    assert result["tool_calls_max"] == 7
    assert result["cost_mean_usd"] == pytest.approx(0.0015)
    assert result["wall_time_mean_s"] == pytest.approx(5.5)


def test_aggregate_bug_repeats_counts_tampered_and_injection():
    attempts = [
        _attempt(True, 5, 0.001, 5.0, tampered=False, injection=False),
        _attempt(True, 5, 0.001, 5.0, tampered=True, injection=False),
        _attempt(False, 5, 0.001, 5.0, tampered=False, injection=True),
    ]
    result = aggregate_bug_repeats("BUG-014", "boundary", attempts)
    assert result["tampered_count"] == 1
    assert result["injection_count"] == 1
    assert result["repeats"] == 3


def test_aggregate_bug_repeats_all_fixed_gives_rate_one():
    attempts = [_attempt(True, 5, 0.001, 5.0), _attempt(True, 5, 0.001, 5.0)]
    result = aggregate_bug_repeats("BUG-001", "off-by-one", attempts)
    assert result["fix_rate"] == 1.0


def test_aggregate_bug_repeats_rejects_empty_attempts():
    with pytest.raises(ValueError):
        aggregate_bug_repeats("BUG-001", "off-by-one", [])


def test_aggregate_overall_combines_every_attempt_across_bugs():
    bug_a_attempts = [_attempt(True, 5, 0.001, 5.0), _attempt(True, 5, 0.001, 5.0)]
    bug_b_attempts = [_attempt(True, 6, 0.002, 6.0), _attempt(False, 8, 0.003, 7.0)]
    per_bug = [
        aggregate_bug_repeats("BUG-001", "off-by-one", bug_a_attempts),
        aggregate_bug_repeats("BUG-010", "multi-file", bug_b_attempts),
    ]
    all_attempts = bug_a_attempts + bug_b_attempts

    overall = aggregate_overall(per_bug, all_attempts)

    assert overall["bugs"] == 2
    assert overall["total_attempts"] == 4
    assert overall["overall_fix_rate"] == pytest.approx(3 / 4)
    assert overall["total_estimated_cost_usd"] == pytest.approx(0.001 + 0.001 + 0.002 + 0.003)


def test_aggregate_overall_with_no_attempts_does_not_divide_by_zero():
    overall = aggregate_overall([], [])
    assert overall["total_attempts"] == 0
    assert overall["overall_fix_rate"] == 0.0


# --- stop_reason breakdown ---------------------------------------------------

def test_stop_reasons_counted_per_bug_most_frequent_first():
    attempts = [
        _attempt(False, 5, 0.001, 5.0, stop_reason="escalated"),
        _attempt(True, 5, 0.001, 5.0, stop_reason="approved"),
        _attempt(True, 5, 0.001, 5.0, stop_reason="approved"),
    ]
    result = aggregate_bug_repeats("BUG-008", "date-timezone", attempts)
    assert result["stop_reasons"] == {"approved": 2, "escalated": 1}
    assert list(result["stop_reasons"]) == ["approved", "escalated"]
    assert format_counts(result["stop_reasons"]) == "approved:2, escalated:1"


def test_stop_reason_ties_sorted_by_name_and_none_labelled():
    attempts = [{"stop_reason": "rejected"}, {"stop_reason": None}, {"stop_reason": "approved"}]
    assert list(count_stop_reasons(attempts)) == ["approved", "none", "rejected"]


def test_overall_stop_reasons_count_every_attempt():
    a = [_attempt(True, 1, 0.0, 1.0, stop_reason="approved")] * 3
    b = [_attempt(False, 1, 0.0, 1.0, stop_reason="rejected"),
         _attempt(True, 1, 0.0, 1.0, stop_reason="approved"),
         _attempt(False, 1, 0.0, 1.0, stop_reason="max_revisions")]
    per_bug = [aggregate_bug_repeats("BUG-001", None, a), aggregate_bug_repeats("BUG-002", None, b)]
    overall = aggregate_overall(per_bug, a + b)
    assert overall["stop_reasons"] == {"approved": 4, "max_revisions": 1, "rejected": 1}
    assert sum(overall["stop_reasons"].values()) == overall["total_attempts"]


def _load_regenerate_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "regenerate_repeats_aggregate.py"
    spec = importlib.util.spec_from_file_location("regenerate_repeats_aggregate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _multi_attempt(bug_id: str, stop_reason: str, decision: str | None) -> dict:
    return dict(
        _attempt(stop_reason == "approved", 4, 0.003, 10.0, stop_reason=stop_reason),
        id=bug_id, category="cat", revisions=1, reviewer_decision=decision,
        cost_by_agent_usd={"researcher": 0.001, "coder": 0.001, "reviewer": 0.001},
    )


def test_regenerate_multi_report_adds_stop_reasons_and_keeps_multi_fields(tmp_path):
    report = tmp_path / "repeats_multi_x.json"
    report.write_text(json.dumps({
        "mode": "multi", "repeats": 2, "per_bug": [], "overall": {},
        "attempts": [
            _multi_attempt("BUG-008", "rejected", "reject"),
            _multi_attempt("BUG-008", "approved", "approve"),
            _multi_attempt("BUG-010", "approved", "approve"),
            _multi_attempt("BUG-010", "approved", "approve"),
        ],
    }), encoding="utf-8")

    data = _load_regenerate_script().regenerate(report)

    by_id = {b["id"]: b for b in data["per_bug"]}
    assert by_id["BUG-008"]["stop_reasons"] == {"approved": 1, "rejected": 1}
    assert by_id["BUG-010"]["stop_reasons"] == {"approved": 2}
    assert by_id["BUG-008"]["reviewer_decisions"] == {"reject": 1, "approve": 1}  # multi extras kept
    assert data["overall"]["stop_reasons"] == {"approved": 3, "rejected": 1}


def test_regenerate_single_report_has_no_multi_fields(tmp_path):
    report = tmp_path / "repeats_x.json"
    attempt = dict(_attempt(True, 4, 0.003, 10.0), id="BUG-001", category="cat")
    report.write_text(json.dumps({"repeats": 1, "attempts": [attempt]}), encoding="utf-8")

    data = _load_regenerate_script().regenerate(report)

    assert data["per_bug"][0]["stop_reasons"] == {"finish": 1}
    assert "reviewer_decisions" not in data["per_bug"][0]
