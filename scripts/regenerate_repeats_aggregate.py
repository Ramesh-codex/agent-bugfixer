"""Offline regeneration of a --repeats combined report's `per_bug`/`overall`
sections from its already-recorded `attempts` -- no live run, no sandbox, no
API calls. Useful after `agent/aggregate.py`'s schema changes (e.g. new
fields) so an old report can be brought up to date without re-running the
agent. Multi-agent reports (`"mode": "multi"`) also get the multi-only
per-bug fields, exactly as a live `--mode multi --repeats` run computes them.

    python -m scripts.regenerate_repeats_aggregate reports/repeats_<mode>_..._<N>x_<run_id>.json
"""

import json
import sys
from pathlib import Path

from agent import aggregate
from agent.run import print_overall, print_repeats_table


def regenerate(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    attempts = data["attempts"]

    by_bug: dict[str, list[dict]] = {}
    order: list[str] = []
    for attempt in attempts:
        bug_id = attempt["id"]
        if bug_id not in by_bug:
            by_bug[bug_id] = []
            order.append(bug_id)
        by_bug[bug_id].append(attempt)

    per_bug = []
    for bug_id in order:
        summary = aggregate.aggregate_bug_repeats(bug_id, by_bug[bug_id][0].get("category"), by_bug[bug_id])
        if data.get("mode") == "multi":
            summary.update(aggregate.aggregate_multi_extras(by_bug[bug_id]))
        per_bug.append(summary)
    overall = aggregate.aggregate_overall(per_bug, attempts)

    data["per_bug"] = per_bug
    data["overall"] = overall
    return data


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.regenerate_repeats_aggregate <repeats_report.json>", file=sys.stderr)
        return 1

    path = Path(sys.argv[1])
    data = regenerate(path)

    print_repeats_table(data["per_bug"])
    print_overall(data["overall"], data["repeats"])

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"\nRegenerated {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
