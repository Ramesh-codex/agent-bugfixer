import pytest

from agent.aggregate import aggregate_bug_repeats, aggregate_overall


def _attempt(fixed: bool, tool_calls: int, cost: float, wall_time: float) -> dict:
    return {
        "fixed": fixed,
        "tool_calls": tool_calls,
        "estimated_cost_usd": cost,
        "wall_time_s": wall_time,
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
