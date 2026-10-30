"""Pure aggregation over repeated attempts at the same bug.

Takes the row dicts `agent.run.run_one_bug` already produces (one per
attempt) and summarizes them -- no I/O, no sandbox, no API calls, so this is
fully unit-testable against fabricated rows.
"""

from statistics import mean


def aggregate_bug_repeats(bug_id: str, category: str | None, attempts: list[dict]) -> dict:
    """Summarize N attempts at one bug."""
    if not attempts:
        raise ValueError(f"no attempts to aggregate for {bug_id}")

    tool_calls = [a["tool_calls"] for a in attempts]
    costs = [a["estimated_cost_usd"] for a in attempts]
    wall_times = [a["wall_time_s"] for a in attempts]
    fixed_count = sum(1 for a in attempts if a["fixed"])
    tampered_count = sum(1 for a in attempts if a["tests_tampered"])
    injection_count = sum(1 for a in attempts if a["injection_flag"])

    return {
        "id": bug_id,
        "category": category,
        "repeats": len(attempts),
        "fixed_count": fixed_count,
        "fix_rate": fixed_count / len(attempts),
        "tampered_count": tampered_count,
        "injection_count": injection_count,
        "tool_calls_mean": round(mean(tool_calls), 2),
        "tool_calls_min": min(tool_calls),
        "tool_calls_max": max(tool_calls),
        "cost_mean_usd": round(mean(costs), 6),
        "wall_time_mean_s": round(mean(wall_times), 2),
    }


def aggregate_overall(per_bug: list[dict], all_attempts: list[dict]) -> dict:
    """Summarize across every bug and every attempt."""
    total_attempts = len(all_attempts)
    total_fixed = sum(1 for a in all_attempts if a["fixed"])

    return {
        "bugs": len(per_bug),
        "total_attempts": total_attempts,
        "overall_fix_rate": total_fixed / total_attempts if total_attempts else 0.0,
        "total_estimated_cost_usd": round(
            sum(a["estimated_cost_usd"] for a in all_attempts), 6
        ),
    }
